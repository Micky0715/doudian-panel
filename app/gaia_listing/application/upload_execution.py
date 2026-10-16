"""Background Gaia execution with persisted progress and no automatic retry.

This component only consumes a validated batch. It cannot select models,
generate a workbook, call Doudian, or publish Doudian products.
"""

import fcntl
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from ..domain.models import GaiaBatch
from .submission_guard import can_submit, submission_idempotency_key
from .rejected_skus import missing_goods_pair
from .retry_batch import create_remaining_batch
from .batch_lifecycle import cancel_batch_record
from .price_uplift import PRICE_RULE_NAMES, PRICE_RULE_RATES, prepare_price_uplift, rule_name_matches_shop_term
from ..infrastructure.price_uplift_template import write_price_uplift_template
from integrations.doudian.cpay_terms import required_cpay_terms
from integrations.doudian.browser_profiles import browser_profile_for_shop


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

    def _verify_price_rule(self, shop_id, installments, scene=2):
        probe = self.price_probe
        if probe is None:
            from integrations.gaia_browser.price_rule_probe import probe_exact_rule
            probe = probe_exact_rule
        args = (str(self.root / "gaia_browser_data"), shop_id, installments)
        checked = probe(*args, scene=scene) if scene == 1 else probe(*args)
        rate = PRICE_RULE_RATES.get(str(shop_id), {}).get(int(installments)) if scene == 2 else None
        rate_text = str(checked.get("rate_text", ""))
        if (not rule_name_matches_shop_term(checked.get("rule_name", ""), shop_id, installments, scene)
                or checked.get("submitted") is not False
                or (rate and "浮动比例：" + rate not in rate_text)
                or (not rate and not re.search(r"浮动比例\s*[:：]\s*\d+(?:\.\d+)?%", rate_text))):
            raise RuntimeError("当前盖亚分期规则的店铺 ID、期数或比例不一致，拒绝继续提交")
        return checked

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

    def start(self, batch_id, *, shop_id, expected_hash, confirmed, installments=None,
              installment_terms=None, activity_name=None):
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
        split_ids = current.get("summary", {}).get("split_batch_ids") or [batch_id]
        if (not isinstance(split_ids, list) or not split_ids or split_ids[0] != batch_id
                or len(split_ids) != len(set(split_ids))):
            raise ValueError("自动分表批次清单无效")
        for index, part_id in enumerate(split_ids):
            part = self.repository.get(part_id)
            if (not part or part.get("state") != "validation_passed"
                    or part.get("shop_id") != shop_id
                    or any(part.get(field) != current.get(field) for field in (
                        "source_hash", "mapping_version_id", "mapping_hash",
                        "scene", "quality_ids", "create_count"))
                    or part.get("summary", {}).get("split_part_index", index + 1) != index + 1
                    or part.get("summary", {}).get("split_batch_ids", split_ids) != split_ids):
                raise ValueError("自动分表第 %s 份状态或店铺不一致，请重新生成" % (index + 1))
            products = part.get("summary", {}).get("expected_products")
            if isinstance(products, list) and len(products) != 1:
                raise ValueError("自动分表第 %s 份含 %s 个型号；旧多型号文件不可上传，请重新生成单型号文件" % (
                    index + 1, len(products)))
            inspection = self.inspect(part, shop_id)
            if not inspection["ready"]:
                raise ValueError("自动分表第 %s 份校验失败：%s" % (
                    index + 1, "；".join(inspection["errors"])))
        if installment_terms is not None and installments is not None:
            raise ValueError("不能同时传入新旧分期参数")
        if installment_terms is not None and not isinstance(installment_terms, list):
            raise ValueError("分期期数必须为数组")
        terms = required_cpay_terms(installment_terms if installment_terms is not None else installments) if (
            installment_terms or installments is not None) else ()
        if installment_terms == [] and activity_name:
            raise ValueError("全款不需要分期免息活动名称")
        price_term = max(terms) if terms else None
        if price_term is not None:
            if current.get("scene") not in {1, 2}:
                raise ValueError("自动分期场域无效")
            if str(shop_id) not in PRICE_RULE_NAMES:
                raise ValueError("店铺 ID 不在已授权店铺清单中")
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
            if price_term is not None:
                from integrations.doudian.cpay_browser import probe_cpay_session
                session_probe = self.activity_session_probe or probe_cpay_session
                cpay_profile = browser_profile_for_shop(self.root, "cpay", shop_id)
                session_probe(str(cpay_profile), shop_id, activity_name)
                # For new requests, prove the exact rule exists before creating Gaia goods.
                if installment_terms is not None:
                    self._verify_price_rule(shop_id, price_term, current["scene"])
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
                    "price_installments": price_term,
                    "activity_terms": list(terms) if installment_terms is not None else None,
                    "price_phase": "waiting_gaia" if price_term is not None else "not_requested",
                    "activity_name": activity_name if price_term is not None else None,
                    "activity_phase": "waiting_price" if price_term is not None else "not_requested",
                    "split_phase": "running" if len(split_ids) > 1 else "not_requested",
                    "split_completed_parts": 0,
                    "split_total_parts": len(split_ids),
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
            target = self._run_split_group if len(split_ids) > 1 else self._run
            thread = threading.Thread(target=target, args=(batch_id, shop_id, handle), daemon=True)
            thread.start()
            return batch
        except Exception:
            handle.close()
            if claimed:
                self._update(batch_id, "manual_required", "执行器未启动；未提交盖亚", phase="failed")
            raise

    def _run(self, batch_id, shop_id, handle, *, defer_price=False, close_handle=True):
        try:
            batch = self.repository.get(batch_id) or {}
            installments = (batch.get("gaia_receipt") or {}).get("price_installments")
            if installments is not None:
                self._verify_price_rule(shop_id, installments, batch.get("scene", 2))
            runner = self.runner
            if runner is None:
                from integrations.gaia_browser.submit import submit_gaia_batch
                runner = submit_gaia_batch
            retry_history = []
            run_id = batch_id
            for attempt in range(13):
                run_batch = self.repository.get(run_id) or {}

                def before_submit(target_id=run_id):
                    self.verified(target_id, shop_id)
                    self._update(target_id, "gaia_submitted", "已记录提交意图，正在等待盖亚反馈；禁止重复提交",
                                 phase="submitting", submit_intent=True)

                result = runner(
                    work_dir=str(self.root), batch_id=run_id, shop_id=shop_id,
                    profile_dir=str(self.root / "gaia_browser_data"),
                    artifact_dir=str(self.root / "panel_data" / "gaia_listing" / "browser_submit" / run_id),
                    verify_handoff=lambda target_id=run_id: self.verified(target_id, shop_id),
                    before_submit=before_submit,
                    check_cancelled=lambda: self.check_cancelled(run_id),
                    auto_price=bool((self.repository.get(batch_id) or {}).get("gaia_receipt", {}).get("price_installments")),
                    expected_products=run_batch.get("summary", {}).get("expected_products"),
                )
                pair = missing_goods_pair(result.get("feedback")) if result.get("outcome") == "rejected" else None
                if not (pair and result.get("rejection_no_new_products_verified") is True):
                    break
                if attempt >= 12:
                    result["recovery_error"] = "安全隔离次数超过 12 次，停止自动继续"
                    break
                if run_id != batch_id:
                    self._update(run_id, "gaia_rejected", result.get("feedback", "盖亚拒绝"),
                                 phase="rejected", result=result)
                retry_history.append({"batch_id": run_id, "sku_id": pair[0],
                                      "quality_id": pair[1], "zero_new_products_verified": True,
                                      "feedback": result.get("feedback", "")})
                try:
                    from ..infrastructure.model_mapping_repository import ModelMappingRepository
                    from ..infrastructure.excel_inspector import load_profiles
                    remaining = create_remaining_batch(
                        previous=dict(run_batch, state="gaia_rejected"), rejected_pair=pair,
                        repository=self.repository,
                        outputs_dir=self.root / "panel_data" / "gaia_listing" / "outputs",
                        mapping_repository=ModelMappingRepository(
                            self.root / "panel_data" / "gaia_listing" / "imports" / "model_mappings"),
                        profiles=load_profiles(str(self.root / "config" / "gaia_excel_profiles.json")),
                    )
                except Exception as exc:
                    result["recovery_error"] = str(exc).splitlines()[0][:600]
                    break
                self._update(batch_id, "gaia_submitted",
                             "已隔离盖亚拒绝组合 %s / %s，正在继续上传其余商品" % pair,
                             phase="submitting", retry_history=retry_history,
                             retry_child_batch_id=remaining["id"] if remaining else None)
                if remaining is None:
                    result["recovery_error"] = "所有组合均被盖亚明确拒绝，没有可继续上传的行"
                    break
                run_id = remaining["id"]
                self._update(run_id, "confirmed", "剩余安全组合准备上传", phase="uploading",
                             execution_key=submission_idempotency_key(run_id, remaining["output_hash"]),
                             submit_intent=False, doudian_published=False)
            if run_id != batch_id:
                terminal = "gaia_accepted" if result.get("outcome") == "accepted" else (
                    "gaia_rejected" if result.get("outcome") == "rejected" else "manual_required")
                self._update(run_id, terminal, result.get("feedback") or "子批次结果待核对",
                             phase=result.get("outcome", "unknown"), result=result)
                if terminal == "gaia_accepted":
                    child_summary = (self.repository.get(run_id) or {}).get("summary", {})
                    def adjust_parent(data):
                        parent = GaiaBatch(**data)
                        parent.summary.update({key: child_summary[key] for key in (
                            "generated_rows", "expected_product_count", "expected_products")})
                        parent.summary["skipped_pairs"] = [
                            {"sku_id": item["sku_id"], "quality_id": item["quality_id"]}
                            for item in retry_history
                        ]
                        return parent
                    self.repository.mutate(batch_id, adjust_parent)
            if retry_history:
                result["retry_history"] = retry_history
            outcome = result.get("outcome", "unknown")
            state = {"accepted": "gaia_accepted", "rejected": "gaia_rejected"}.get(outcome, "manual_required")
            message = result.get("feedback") or "没有明确回执，请核对盖亚任务，禁止重复提交"
            if result.get("recovery_error"):
                state = "manual_required"
                message = "安全续传已停止：" + result["recovery_error"]
            if state == "gaia_accepted":
                current_receipt = (self.repository.get(batch_id) or {}).get("gaia_receipt") or {}
                expected = (self.repository.get(batch_id) or {}).get("summary", {}).get("expected_product_count")
                ids = result.get("verified_product_ids")
                if (not isinstance(expected, int) or expected < 1 or not isinstance(ids, list)
                        or len(ids) != expected or len(set(ids)) != expected):
                    reason = (result.get("product_reconcile_error") or result.get("price_reconcile_error")
                              or "未能核实本批新建商品 ID")
                    state = "manual_required"
                    message = "盖亚已受理，但新建商品未核实；抖店草稿未确认，禁止重复导入：" + reason
                    if current_receipt.get("price_installments") is not None:
                        self._update(batch_id, state, message, phase=outcome, result=result,
                                     retry_history=retry_history,
                                     price_phase="id_blocked", price_error=reason)
                        return
                else:
                    message = f"已核实盖亚渠道新建 {len(ids)} 个商品；抖店草稿箱状态尚未核实，未启动抖店发布"
                    if retry_history:
                        skipped = "、".join("%s / 成色 %s" % (item["sku_id"], item["quality_id"])
                                           for item in retry_history)
                        message += "；已跳过 %s 个盖亚明确拒绝的组合：%s" % (len(retry_history), skipped)
            self._update(batch_id, state, message, phase=outcome, result=result,
                         retry_history=retry_history)
            if state == "gaia_accepted" and not defer_price:
                self._run_price_uplift(batch_id, shop_id, result)
        except Exception as exc:
            current = self.repository.get(batch_id) or {}
            if current.get("state") != "cancelled":
                intent = current.get("gaia_receipt", {}).get("submit_intent", False)
                message = ("提交结果不确定，禁止重试，请核对盖亚任务：" if intent else "上传前停止，未提交盖亚：")
                self._update(batch_id, "manual_required", message + str(exc).splitlines()[0][:600],
                             phase="unknown" if intent else "failed")
        finally:
            if close_handle:
                handle.close()
                self._finish_stop(batch_id)

    def _run_split_group(self, batch_id, shop_id, handle):
        """Upload pre-generated model-sized files once, in order, under one lock.

        A missing receipt or incomplete product reconciliation halts the whole
        group. Already submitted files are never regenerated or replayed.
        """
        try:
            root = self.repository.get(batch_id) or {}
            part_ids = root.get("summary", {}).get("split_batch_ids") or []
            if not part_ids or part_ids[0] != batch_id:
                raise RuntimeError("自动分表批次清单已变化")
            all_ids: list[str] = []
            for index, part_id in enumerate(part_ids, start=1):
                if index > 1:
                    current = self.repository.get(batch_id) or {}
                    if (current.get("gaia_receipt") or {}).get("split_phase") != "running":
                        raise RuntimeError("自动分表执行状态已变化，后续文件未上传")
                    part = self.repository.get(part_id) or {}
                    if (part.get("state") != "validation_passed" or part.get("shop_id") != shop_id
                            or part.get("summary", {}).get("split_batch_ids") != part_ids):
                        raise RuntimeError("自动分表第 %s 份批次状态已变化" % index)
                    check = self.inspect(part, shop_id)
                    if not check["ready"]:
                        raise RuntimeError("自动分表第 %s 份上传前校验失败：%s" % (
                            index, "；".join(check["errors"])))
                    self._update(part_id, "confirmed", "自动分表第 %s/%s 份准备上传" % (
                        index, len(part_ids)), phase="uploading",
                        execution_key=submission_idempotency_key(part_id, part["output_hash"]),
                        worker_pid=os.getpid(), submit_intent=False, doudian_published=False)
                    self._update(batch_id, "gaia_accepted", "第 %s/%s 份正在上传；已受理的文件不会重传" % (
                        index, len(part_ids)), split_phase="running")
                self._run(part_id, shop_id, handle, defer_price=True, close_handle=False)
                finished = self.repository.get(part_id) or {}
                receipt = finished.get("gaia_receipt") or {}
                ids = (receipt.get("result") or {}).get("verified_product_ids")
                expected = finished.get("summary", {}).get("expected_product_count")
                if (finished.get("state") != "gaia_accepted" or not isinstance(ids, list)
                        or not isinstance(expected, int) or len(ids) != expected
                        or len(set(ids)) != expected or set(ids).intersection(all_ids)):
                    reason = receipt.get("message") or "未核实本份新建商品，后续文件未上传"
                    self._update(batch_id, "manual_required",
                                 "自动分表在第 %s/%s 份停止；已受理文件不可重传：%s" % (
                                     index, len(part_ids), reason),
                                 split_phase="manual_required", split_completed_parts=index - 1)
                    return
                all_ids.extend(ids)
                self._update(batch_id, "gaia_accepted", "第 %s/%s 份已受理并核实商品，准备下一份" % (
                    index, len(part_ids)), split_phase="running", split_completed_parts=index)

            def complete(data):
                parent = GaiaBatch(**data)
                if (parent.gaia_receipt.get("split_phase") != "running"
                        or parent.gaia_receipt.get("split_completed_parts") != len(part_ids)):
                    raise RuntimeError("自动分表汇总状态已变化")
                products = parent.summary["split_total_products"]
                if len(all_ids) != len(products):
                    raise RuntimeError("自动分表商品 ID 总数与型号数不一致")
                parent.summary["expected_product_count"] = len(products)
                parent.summary["expected_products"] = products
                result = dict(parent.gaia_receipt.get("result") or {})
                result["verified_product_ids"] = list(all_ids)
                parent.gaia_receipt.update(
                    result=result, split_phase="done",
                    message="%s 份盖亚文件已顺序受理，核实 %s 件商品" % (
                        len(part_ids), len(all_ids)),
                )
                return parent
            parent = self.repository.mutate(batch_id, complete)
            installments = (parent.get("gaia_receipt") or {}).get("price_installments")
            if installments is not None:
                self._run_price_uplift(batch_id, shop_id, parent["gaia_receipt"]["result"])
        except Exception as exc:
            current = self.repository.get(batch_id) or {}
            if current.get("state") != "cancelled":
                self._update(batch_id, "manual_required",
                             "自动分表执行中断，请核对已上传文件，禁止整批重试："
                             + str(exc).splitlines()[0][:400], split_phase="manual_required")
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
            reason = result.get("product_reconcile_error") or result.get("price_reconcile_error") or "未能唯一核实本批新建商品 ID"
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
            rule_evidence = self._verify_price_rule(shop_id, installments, batch.get("scene", 2))
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

            extra = ({"scene": batch["scene"], "expected_rule_name": rule_evidence["rule_name"],
                      "expected_rate_text": rule_evidence["rate_text"]}
                     if batch.get("scene") == 1 or (batch.get("gaia_receipt") or {}).get("activity_terms") is not None
                     else {})
            receipt = runner(
                profile_dir=str(self.root / "gaia_browser_data"), shop_id=shop_id,
                installments=installments, workbook_path=str(output),
                artifact_dir=str(self.root / "panel_data" / "gaia_listing" / "browser_submit" / batch_id),
                before_submit=before_price_submit, expected_product_ids=ids,
                **extra,
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
        selected_terms = receipt.get("activity_terms") if receipt.get("activity_terms") is not None else installments
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
            precheck = probe(str(self.root / "env.local"), shop_id, selected_terms, ids, attempts=1)
            required_terms = set(required_cpay_terms(selected_terms))
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
                profile_dir=str(browser_profile_for_shop(self.root, "cpay", shop_id)),
                batch_id=batch_id, shop_id=shop_id, installments=selected_terms,
                activity_name=activity_name,
                product_ids=ids, before_submit=before_activity_submit,
            )
            if result.get("outcome") != "submitted_unverified":
                raise RuntimeError("抖店报名提交未得到可核对回执")
            verified = probe(str(self.root / "env.local"), shop_id, selected_terms, ids)
            if verified.get("confirmed"):
                self._update(batch_id, "handed_off",
                             f"盖亚价格上浮和抖店 {'、'.join(map(str, required_cpay_terms(selected_terms)))} 期活动已完成；{len(ids)} 件商品已回读报名期数",
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
                or str(shop_id) not in PRICE_RULE_NAMES or installments not in {6, 12, 24}):
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
            self._verify_price_rule(shop_id, installments, batch.get("scene", 2))
            if rejected_retry:
                from integrations.gaia_browser.price_rule_probe import probe_rule_membership
                current_membership = probe_rule_membership(
                    str(self.root / "gaia_browser_data"), shop_id, installments, ids,
                    **({"scene": batch["scene"]} if batch.get("scene") == 1 else {}),
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
        if receipt.get("split_phase") == "running":
            try:
                os.kill(int(receipt.get("worker_pid", 0)), 0)
            except (ProcessLookupError, ValueError):
                return self._update(batch["id"], "manual_required",
                                    "自动分表进程已中断；请逐份核对盖亚结果，禁止整批重试",
                                    split_phase="manual_required")
            except PermissionError:
                pass
            return batch
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
