"""Background Gaia execution with persisted progress and no automatic retry.

This component only consumes a validated batch. It cannot select models,
generate a workbook, call Doudian, or publish Doudian products.
"""

import fcntl
import os
import threading
from datetime import datetime
from pathlib import Path

from ..domain.models import GaiaBatch
from .submission_guard import can_submit, submission_idempotency_key
from .batch_lifecycle import cancel_batch_record


class GaiaUploadExecution:
    def __init__(self, work_dir, repository, inspect, enabled, runner=None):
        self.root = Path(work_dir)
        self.repository = repository
        self.inspect = inspect
        self.enabled = enabled
        self.runner = runner

    def _update(self, batch_id, state, message, **details):
        def change(data):
            batch = GaiaBatch(**data)
            if batch.state == "cancelled":
                raise RuntimeError("批次已经取消，禁止继续")
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            batch.state, batch.updated_at = state, now
            batch.gaia_receipt.update(message=message, **details)
            batch.events.append({"at": now, "action": "gaia_upload", "to": state, "message": message})
            return batch
        return self.repository.mutate(batch_id, change)

    def verified(self, batch_id, shop_id):
        batch = self.repository.get(batch_id)
        if not batch or batch["state"] != "confirmed":
            raise RuntimeError("批次已取消或不在上传准备状态")
        ok, reason = can_submit(batch, feature_enabled=self.enabled(), confirmed=True)
        if not ok:
            raise RuntimeError(reason)
        # Integrity inspection is unchanged; only the claimed state differs.
        check = self.inspect(dict(batch, state="validation_passed"), shop_id)
        if not check["ready"]:
            raise RuntimeError("；".join(check["errors"]))
        return batch, check

    def check_cancelled(self, batch_id):
        current = self.repository.get(batch_id)
        if not current or current.get("state") == "cancelled":
            raise RuntimeError("运营已停止本批次，禁止继续提交")

    def stop(self, batch_id, *, shop_id, expected_hash):
        """Serialize stop against before_submit; never claim to revoke Gaia work."""
        def change(data):
            if data.get("shop_id") != shop_id or not expected_hash or data.get("output_hash") != expected_hash:
                raise ValueError("批次店铺或文件版本不一致，不能停止其他任务")
            receipt = data.get("gaia_receipt", {})
            if receipt.get("submit_intent"):
                raise RuntimeError("本批次已进入盖亚提交阶段，无法通过本地停止撤回；请核对盖亚任务，勿重复提交")
            if data.get("state") == "cancelled":
                return GaiaBatch(**data)
            running = data.get("state") == "confirmed" and receipt.get("phase") == "uploading"
            batch = cancel_batch_record(data, shop_id=shop_id, reason="运营点击停止")
            batch.gaia_receipt.update(
                stop_requested=True, phase="stopping" if running else "stopped",
                message="已阻止提交，正在关闭本批次浏览器…" if running else "已停止，未提交盖亚",
            )
            return batch
        result = self.repository.mutate(batch_id, change)
        if not result:
            raise ValueError("批次不存在")
        return result

    def _finish_stop(self, batch_id):
        def change(data):
            batch = GaiaBatch(**data)
            if batch.state == "cancelled" and batch.gaia_receipt.get("phase") == "stopping":
                batch.gaia_receipt.update(phase="stopped", message="已停止，未提交盖亚")
                batch.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            return batch
        return self.repository.mutate(batch_id, change)

    def start(self, batch_id, *, shop_id, expected_hash, confirmed):
        if not self.enabled():
            raise RuntimeError("盖亚上传执行未启用")
        if confirmed is not True:
            raise ValueError("请通过“上传并启动盖亚”按钮明确启动")
        current = self.repository.get(batch_id)
        if not current or current["shop_id"] != shop_id:
            raise ValueError("批次不存在或店铺不一致")
        if not expected_hash or expected_hash != current["output_hash"]:
            raise ValueError("生成文件版本不一致，请重新检查")
        if current.get("gaia_receipt", {}).get("execution_key"):
            return current  # Repeated HTTP requests must not trigger a second click.
        checked = self.inspect(current, shop_id)
        if not checked["ready"]:
            raise ValueError("；".join(checked["errors"]))
        # The dedicated browser profile is shared: serialize Gaia separately
        # from the existing four Doudian worker slots. flock releases on death.
        lock_path = self.root / "panel_data" / "gaia_listing" / "upload.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("盖亚浏览器正在处理另一批，请等待其结束")
        claimed = False
        try:
            def claim(data):
                nonlocal claimed
                if data.get("gaia_receipt", {}).get("execution_key"):
                    return GaiaBatch(**data)
                if data["state"] != "validation_passed" or data["output_hash"] != expected_hash:
                    raise ValueError("批次状态已变化，禁止启动")
                batch = GaiaBatch(**data)
                batch.state = "confirmed"
                batch.gaia_receipt = {
                    "execution_key": submission_idempotency_key(batch_id, expected_hash),
                    "phase": "uploading", "worker_pid": os.getpid(),
                    "message": "正在打开盖亚并上传生成文件；如提示登录，请在专用窗口登录",
                    "submit_intent": False, "doudian_published": False,
                }
                batch.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
                batch.events.append({"at": batch.updated_at, "action": "start_gaia_upload",
                                     "to": "confirmed", "message": batch.gaia_receipt["message"]})
                claimed = True
                return batch
            batch = self.repository.mutate(batch_id, claim)
            if not claimed:
                handle.close()
                return batch
            thread = threading.Thread(target=self._run, args=(batch_id, shop_id, handle), daemon=True)
            thread.start()
            return batch
        except Exception:
            handle.close()
            if claimed:
                self._update(batch_id, "manual_required", "执行器未启动；未提交盖亚", phase="failed")
            raise

    def _run(self, batch_id, shop_id, handle):
        try:
            runner = self.runner
            if runner is None:
                from integrations.gaia_browser.submit import submit_gaia_batch
                runner = submit_gaia_batch

            def before_submit():
                self.verified(batch_id, shop_id)
                self._update(batch_id, "gaia_submitted", "已记录提交意图，正在等待盖亚反馈；禁止重复提交",
                             phase="submitting", submit_intent=True)

            result = runner(
                work_dir=str(self.root), batch_id=batch_id, shop_id=shop_id,
                profile_dir=str(self.root / "gaia_browser_data"),
                artifact_dir=str(self.root / "panel_data" / "gaia_listing" / "browser_submit" / batch_id),
                verify_handoff=lambda: self.verified(batch_id, shop_id), before_submit=before_submit,
                check_cancelled=lambda: self.check_cancelled(batch_id),
            )
            outcome = result.get("outcome", "unknown")
            state = {"accepted": "gaia_accepted", "rejected": "gaia_rejected"}.get(outcome, "manual_required")
            message = result.get("feedback") or "没有明确回执，请核对盖亚任务，禁止重复提交"
            if state == "gaia_accepted":
                message += "；仅表示盖亚已受理，逐行创建结果须在盖亚核对，未启动抖店发布"
            self._update(batch_id, state, message, phase=outcome, result=result)
        except Exception as exc:
            current = self.repository.get(batch_id) or {}
            if current.get("state") != "cancelled":
                intent = current.get("gaia_receipt", {}).get("submit_intent", False)
                message = ("提交结果不确定，禁止重试，请核对盖亚任务：" if intent else "上传前停止，未提交盖亚：")
                self._update(batch_id, "manual_required", message + str(exc).splitlines()[0][:600],
                             phase="unknown" if intent else "failed")
        finally:
            handle.close()
            self._finish_stop(batch_id)

    def recover_interrupted(self, batch):
        """A dead worker is not an eternally running job and must not be replayed."""
        receipt = batch.get("gaia_receipt", {})
        if batch.get("state") == "cancelled" and receipt.get("phase") == "stopping":
            try:
                os.kill(int(receipt.get("worker_pid", 0)), 0)
            except (ProcessLookupError, ValueError):
                return self._finish_stop(batch["id"])
            except PermissionError:
                pass
            return batch
        if batch.get("state") not in {"confirmed", "gaia_submitted"} or receipt.get("phase") not in {"uploading", "submitting"}:
            return batch
        try:
            os.kill(int(receipt.get("worker_pid", 0)), 0)
        except (ProcessLookupError, ValueError):
            return self._update(batch["id"], "manual_required", "执行进程已中断，请核对盖亚结果；不会自动重试",
                                phase="unknown")
        except PermissionError:
            pass
        return batch
