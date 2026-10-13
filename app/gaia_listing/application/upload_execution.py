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
from .price_uplift import PRICE_RULE_NAMES, PRICE_RULE_RATES, prepare_price_uplift, rule_name_matches_shop_term
from ..infrastructure.price_uplift_template import write_price_uplift_template


class GaiaUploadExecution:
    def __init__(self, work_dir, repository, inspect, enabled, runner=None,
                 price_template=None, price_runner=None, price_probe=None,
                 activity_runner=None, activity_probe=None, activity_session_probe=None):
        self.root = Path(work_dir)
        self.repository = repository
        self.inspect = inspect
        self.enabled = enabled
        self.runner = runner
        self.price_template = price_template
        self.price_runner = price_runner
        self.price_probe = price_probe
        self.activity_runner = activity_runner
        self.activity_probe = activity_probe
        self.activity_session_probe = activity_session_probe

    def _verify_price_rule(self, shop_id, installments):
        probe = self.price_probe
        if probe is None:
            from integrations.gaia_browser.price_rule_probe import probe_exact_rule
            probe = probe_exact_rule
        checked = probe(str(self.root / "gaia_browser_data"), shop_id, installments)
        rate = PRICE_RULE_RATES[str(shop_id)][int(installments)]
        if (not rule_name_matches_shop_term(checked.get("rule_name", ""), shop_id, installments)
                or checked.get("submitted") is not False
                or "浮动比例：" + rate not in checked.get("rate_text", "")):
            raise RuntimeError("当前盖亚分期规则的店铺 ID、期数或比例不一致，拒绝继续提交")

    @staticmethod
    def _confirmed_rejected_price_import(receipt, ids):
        result = receipt.get("price_result") or {}
        membership = result.get("membership") or {}
        rejected_api = any(
            item.get("api_name") == "pop.digital.dy.product.floating.adjust.temp.import"
            and str(item.get("code")) not in {"", "200"}
            for item in result.get("submit_responses") or []
        )
        return (receipt.get("price_phase") == "rejected"
                and result.get("outcome") == "rejected" and rejected_api
                and not membership.get("found_ids")
                and set(membership.get("missing_ids") or []) == set(ids))

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

    def start(self, batch_id, *, shop_id, expected_hash, confirmed, installments=None, activity_name=None):
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
        if installments is not None:
            if str(installments) not in {"12", "24"}:
                raise ValueError("分期数只能选择 12 期或 24 期")
            if current.get("scene") != 2:
                raise ValueError("自动分期仅支持直播货架场域")
            if int(installments) not in PRICE_RULE_NAMES.get(str(shop_id), {}):
                raise ValueError("该店铺缺少已核实的分期价格上浮规则")
            expected = current.get("summary", {}).get("expected_products")
            if not isinstance(expected, list) or not expected:
                raise ValueError("本批缺少商品型号清单，请重新生成后启动")
            if self.price_template is None or self.price_template() is None:
                raise ValueError("未找到已校验的价格上浮官方导入模板")
            if not isinstance(activity_name, str) or not activity_name.strip():
                raise ValueError("请选择分期时须填写抖店活动卡片的完整名称")
            activity_name = activity_name.strip()
            if len(activity_name) > 120 or any(char in activity_name for char in "\r\n\t"):
                raise ValueError("分期免息活动名称格式无效")
            if "全店报名" in activity_name:
                raise ValueError("仅支持单品/部分商品报名，不可选择全店报名活动")
        elif activity_name:
            raise ValueError("全款不需要分期免息活动名称")
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
            if installments is not None:
                from integrations.doudian.cpay_browser import probe_cpay_session
                session_probe = self.activity_session_probe or probe_cpay_session
                session_probe(str(self.root / "doudian_cpay_browser_data"), shop_id, activity_name)
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
                    "price_installments": int(installments) if installments is not None else None,
                    "price_phase": "waiting_gaia" if installments is not None else "not_requested",
                    "activity_name": activity_name if installments is not None else None,
                    "activity_phase": "waiting_price" if installments is not None else "not_requested",
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
            batch = self.repository.get(batch_id) or {}
            installments = (batch.get("gaia_receipt") or {}).get("price_installments")
            if installments is not None:
                self._verify_price_rule(shop_id, installments)
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
                auto_price=bool((self.repository.get(batch_id) or {}).get("gaia_receipt", {}).get("price_installments")),
                expected_products=(self.repository.get(batch_id) or {}).get("summary", {}).get("expected_products"),
            )
            outcome = result.get("outcome", "unknown")
            state = {"accepted": "gaia_accepted", "rejected": "gaia_rejected"}.get(outcome, "manual_required")
            message = result.get("feedback") or "没有明确回执，请核对盖亚任务，禁止重复提交"
            if state == "gaia_accepted":
                message += "；仅表示盖亚已受理，逐行创建结果须在盖亚核对，未启动抖店发布"
            self._update(batch_id, state, message, phase=outcome, result=result)
            if state == "gaia_accepted":
                self._run_price_uplift(batch_id, shop_id, result)
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

    def _run_price_uplift(self, batch_id, shop_id, result):
        batch = self.repository.get(batch_id) or {}
        installments = batch.get("gaia_receipt", {}).get("price_installments")
        if installments is None:
            return
        ids = result.get("verified_product_ids")
        expected = batch.get("summary", {}).get("expected_product_count")
        if not isinstance(ids, list) or len(ids) != expected:
            reason = result.get("price_reconcile_error") or "未能唯一核实本批新建商品 ID"
            self._update(batch_id, "manual_required", "盖亚已受理；自动分期已停止：" + reason,
                         price_phase="id_blocked", price_error=reason)
            return

        def linked(data):
            linked_batch = GaiaBatch(**data)
            if linked_batch.state != "gaia_accepted" or linked_batch.doudian_draft_ids:
                raise RuntimeError("商品 ID 关联状态已变化，禁止自动导入")
            linked_batch.doudian_draft_ids = ids
            linked_batch.state = "doudian_drafts_matched"
            linked_batch.gaia_receipt.update(price_phase="ids_verified",
                                             message=f"已核实 {len(ids)} 个本批新建商品 ID，准备导入 {installments} 期规则")
            linked_batch.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
            return linked_batch

        try:
            if batch.get("state") != "doudian_drafts_matched" or batch.get("doudian_draft_ids") != ids:
                self.repository.mutate(batch_id, linked)
            batch = self.repository.get(batch_id)
            prepared = prepare_price_uplift(batch, shop_id=shop_id, installments=installments)
            template = self.price_template() if self.price_template else None
            if template is None:
                raise RuntimeError("价格上浮官方模板在生成前已失效")
            output = self.root / "panel_data" / "gaia_listing" / "price_uplift" / f"{batch_id}_{installments}.xlsx"
            write_price_uplift_template(str(template), str(output), prepared["product_ids"])
            from integrations.gaia_browser.price_rule_upload import upload_price_rule
            runner = self.price_runner or upload_price_rule

            def before_price_submit():
                current = self.repository.get(batch_id) or {}
                if current.get("state") != "doudian_drafts_matched" or current.get("doudian_draft_ids") != ids:
                    raise RuntimeError("价格上浮导入前商品 ID 已变化")
                self._update(batch_id, "doudian_drafts_matched", "已记录价格上浮提交意图；正在等待盖亚反馈",
                             price_phase="submitting", price_submit_intent=True)

            receipt = runner(
                profile_dir=str(self.root / "gaia_browser_data"), shop_id=shop_id,
                installments=installments, workbook_path=str(output),
                artifact_dir=str(self.root / "panel_data" / "gaia_listing" / "browser_submit" / batch_id),
                before_submit=before_price_submit, expected_product_ids=ids,
            )
            if receipt.get("outcome") == "accepted":
                self._update(batch_id, "handed_off", f"盖亚已受理；{installments} 期价格上浮导入已受理",
                             price_phase="accepted", price_result=receipt)
                self._run_activity_enrollment(batch_id, shop_id, installments, ids)
            else:
                self._update(batch_id, "manual_required", "盖亚已受理；价格上浮结果待核对，禁止自动重试："
                             + str(receipt.get("feedback") or "无明确回执")[:500],
                             price_phase=receipt.get("outcome", "unknown"), price_result=receipt)
        except Exception as exc:
            current = self.repository.get(batch_id) or {}
            intent = current.get("gaia_receipt", {}).get("price_submit_intent")
            prefix = "价格上浮提交结果不确定，禁止自动重试：" if intent else "价格上浮提交前停止："
            self._update(batch_id, "manual_required", "盖亚已受理；" + prefix + str(exc).splitlines()[0][:500],
                         price_phase="unknown" if intent else "failed")

    def _run_activity_enrollment(self, batch_id, shop_id, installments, ids):
        """Enroll once after price import; never infer success from a button click."""
        batch = self.repository.get(batch_id) or {}
        receipt = batch.get("gaia_receipt") or {}
        if receipt.get("activity_phase") not in {"waiting_price", "ready"}:
            return
        activity_name = receipt.get("activity_name")
        if (batch.get("state") != "handed_off" or receipt.get("price_phase") != "accepted"
                or batch.get("doudian_draft_ids") != ids or not activity_name):
            self._update(batch_id, "manual_required", "分期活动报名条件已变化，未提交抖店",
                         activity_phase="blocked")
            return
        try:
            from integrations.doudian.cpay_browser import enroll_cpay_batch
            from integrations.doudian.cpay_readback import verify_cpay_membership
            from integrations.doudian.cpay_terms import required_cpay_terms
            runner = self.activity_runner or enroll_cpay_batch
            probe = self.activity_probe or verify_cpay_membership
            precheck = probe(str(self.root / "env.local"), shop_id, installments, ids, attempts=1)
            required_terms = set(required_cpay_terms(installments))
            if any(required_terms.intersection(item.get("periods", []))
                   for item in (precheck.get("products") or {}).values()):
                raise RuntimeError("本批已有商品报名目标期数，禁止重复创建活动")

            def before_activity_submit():
                current = self.repository.get(batch_id) or {}
                current_receipt = current.get("gaia_receipt") or {}
                if (current.get("state") != "handed_off" or current.get("shop_id") != shop_id
                        or current.get("doudian_draft_ids") != ids
                        or current_receipt.get("activity_name") != activity_name
                        or current_receipt.get("activity_submit_intent")):
                    raise RuntimeError("活动提交前批次身份或商品 ID 已变化")
                self._update(batch_id, "handed_off", "已记录抖店活动提交意图，正在等待回执；不可重复提交",
                             activity_phase="submitting", activity_submit_intent=True)

            self._update(batch_id, "handed_off", "正在核对抖店店铺、活动名称、期数和本批商品 ID",
                         activity_phase="preparing")
            result = runner(
                profile_dir=str(self.root / "doudian_cpay_browser_data"),
                batch_id=batch_id, shop_id=shop_id, installments=installments,
                activity_name=activity_name,
                product_ids=ids, before_submit=before_activity_submit,
            )
            if result.get("outcome") != "submitted_unverified":
                raise RuntimeError("抖店报名提交未得到可核对回执")
            verified = probe(str(self.root / "env.local"), shop_id, installments, ids)
            if verified.get("confirmed"):
                self._update(batch_id, "handed_off",
                             f"盖亚价格上浮和抖店 {'、'.join(map(str, required_cpay_terms(installments)))} 期活动已完成；{len(ids)} 件商品已回读报名期数",
                             activity_phase="verified", activity_result=result,
                             activity_readback=verified)
            else:
                self._update(batch_id, "manual_required",
                             "抖店分期活动已点击提交，但商品报名尚未全部回读；请在抖店核对，禁止重复报名",
                             activity_phase="submitted_unverified", activity_result=result,
                             activity_readback=verified)
        except Exception as exc:
            current = self.repository.get(batch_id) or {}
            intent = (current.get("gaia_receipt") or {}).get("activity_submit_intent")
            self._update(batch_id, "manual_required",
                         ("抖店活动提交结果不确定，禁止重试：" if intent
                          else "抖店活动提交前停止：") + str(exc).splitlines()[0][:500],
                         activity_phase="unknown" if intent else "failed")

    def resume_price_only(self, batch_id, *, shop_id, expected_hash, confirmed):
        """Retry only an unsubmitted price import; never create Gaia goods again."""
        if not self.enabled() or confirmed is not True:
            raise RuntimeError("请明确确认仅恢复本批分期导入")
        batch = self.repository.get(batch_id)
        if not batch or batch.get("shop_id") != shop_id or batch.get("output_hash") != expected_hash:
            raise ValueError("批次店铺或文件版本不一致")
        receipt = batch.get("gaia_receipt") or {}
        installments = receipt.get("price_installments")
        ids = batch.get("doudian_draft_ids")
        verified = (receipt.get("result") or {}).get("verified_product_ids")
        attempts = int(receipt.get("price_resume_attempts") or (1 if receipt.get("price_resume_started") else 0))
        rejected_retry = self._confirmed_rejected_price_import(receipt, ids or [])
        before_submit_retry = receipt.get("price_phase") == "failed" and not receipt.get("price_submit_intent")
        if (batch.get("state") != "manual_required" or receipt.get("phase") != "accepted"
                or not (before_submit_retry or rejected_retry)
                or attempts >= 2 or receipt.get("submit_intent") is not True
                or not isinstance(ids, list) or not ids or ids != verified
                or len(ids) != batch.get("summary", {}).get("expected_product_count")
                or installments not in PRICE_RULE_NAMES.get(str(shop_id), {})):
            raise RuntimeError("仅允许价格尚未提交或有明确失败回执且商品未入规则的批次最多恢复两次")
        if self.price_template is None or self.price_template() is None:
            raise RuntimeError("未找到已校验的价格上浮官方导入模板")
        lock_path = self.root / "panel_data" / "gaia_listing" / "upload.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError("盖亚浏览器正在处理另一批，请等待其结束")
        try:
            self._verify_price_rule(shop_id, installments)
            if rejected_retry:
                from integrations.gaia_browser.price_rule_probe import probe_rule_membership
                current_membership = probe_rule_membership(
                    str(self.root / "gaia_browser_data"), shop_id, installments, ids,
                )
                if current_membership.get("found_ids") or set(current_membership.get("missing_ids") or []) != set(ids):
                    raise RuntimeError("规则中已有目标商品或核对不完整，禁止重复导入")
            def claim(data):
                current = data.get("gaia_receipt") or {}
                current_attempts = int(current.get("price_resume_attempts") or (
                    1 if current.get("price_resume_started") else 0))
                current_safe = ((current.get("price_phase") == "failed" and not current.get("price_submit_intent"))
                                or self._confirmed_rejected_price_import(current, ids))
                if (data.get("state") != "manual_required" or not current_safe or current_attempts >= 2
                        or data.get("doudian_draft_ids") != ids):
                    raise RuntimeError("分期恢复状态已变化，禁止重复启动")
                linked = GaiaBatch(**data)
                linked.state = "doudian_drafts_matched"
                linked.updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
                linked.gaia_receipt.update(price_phase="ids_verified", price_submit_intent=False,
                                           price_resume_started=True,
                                           price_resume_attempts=current_attempts + 1,
                                           worker_pid=os.getpid(),
                                           message="仅恢复已核实商品的分期导入，不会重新创建盖亚商品")
                linked.events.append({"at": linked.updated_at, "action": "resume_price_only",
                                      "to": linked.state, "message": linked.gaia_receipt["message"]})
                return linked
            claimed = self.repository.mutate(batch_id, claim)
            def run():
                try:
                    self._run_price_uplift(batch_id, shop_id, receipt["result"])
                finally:
                    handle.close()
            threading.Thread(target=run, daemon=True).start()
            return claimed
        except Exception:
            handle.close()
            raise

    def recover_interrupted(self, batch):
        """A dead worker is not an eternally running job and must not be replayed."""
        receipt = batch.get("gaia_receipt", {})
        price_running = (batch.get("state") in {"gaia_accepted", "doudian_drafts_matched"}
                         and receipt.get("price_phase") in {"waiting_gaia", "ids_verified", "submitting"})
        if price_running:
            try:
                os.kill(int(receipt.get("worker_pid", 0)), 0)
            except (ProcessLookupError, ValueError):
                return self._update(batch["id"], "manual_required",
                                    "分期执行进程已中断；请核对价格规则，禁止自动重试",
                                    price_phase="unknown")
            except PermissionError:
                pass
            return batch
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
