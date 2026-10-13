"""Explicit Gaia submission, separate from the cancel-only preflight.

UI acceptance is not proof of per-row success or Doudian publication. Missing
or unclear feedback requires reconciliation and is never retried here.
"""

import re
import time
from datetime import datetime
from pathlib import Path

from .probe import _screenshot, _emit
from .upload_form import with_gaia_upload_form
from .file_attachment import wait_for_attachment
from .channel_reconcile import snapshot_channel_products, reconcile_channel_products


def submit_gaia_batch(*, work_dir, batch_id, shop_id, profile_dir, artifact_dir,
                      verify_handoff, before_submit, check_cancelled=None,
                      auto_price=False, expected_products=None):
    expected_products = expected_products or []

    def before_create(page, batch):
        if not auto_price:
            return None
        if batch.get("scene") != 2 or not expected_products:
            raise RuntimeError("自动分期缺少直播货架商品型号清单")
        return snapshot_channel_products(page, str(shop_id), expected_products)

    def confirm_once(page, dialog, confirm, result):
        verify_handoff()
        result["final_attachment_readback"] = wait_for_attachment(
            page, dialog, expected_name=result["output_name"], expected_size=result["output_size"],
            batch_id=batch_id, phase="before_confirm",
            require_list=result["attachment_readback"]["source"] == "upload_list",
            check_cancelled=check_cancelled,
        )
        selected = dialog.get_by_text(result["shop_name"], exact=True).filter(visible=True)
        quantity = dialog.get_by_placeholder("请输入1-10的整数", exact=True)
        if selected.count() != 1 or quantity.input_value() != "1":
            raise RuntimeError("最终提交前店铺或数量发生变化，已停止")
        if not confirm.is_enabled():
            raise RuntimeError("最终提交前确定按钮不可用，已停止")
        messages = page.locator(
            '.ant-message-notice, .ant-notification-notice, .el-message, [role="alert"]'
        ).filter(visible=True)
        baseline = {text.strip() for text in messages.all_inner_texts()}
        # Persist intent BEFORE clicking. A timeout can mean Gaia received it.
        before_submit()
        submitted_at = datetime.now()
        result["submitted"] = True
        confirm.click(timeout=8000)
        deadline = time.monotonic() + 20
        result["outcome"] = "unknown"
        result["observed_feedback"] = []
        while time.monotonic() < deadline:
            texts = [text.strip() for text in messages.all_inner_texts()
                     if text.strip() and text.strip() not in baseline]
            for text in texts:
                if text[:1200] not in result["observed_feedback"] and len(result["observed_feedback"]) < 20:
                    result["observed_feedback"].append(text[:1200])
                    _emit("gaia_submit_feedback", batch_id=batch_id, message=text[:1200])
            errors = [text for text in texts if re.search(
                r"失败|错误|异常|无权限|(?:没有|未找到|找不到|不存在|无对应).{0,80}goods\s*id"
                r"|goods\s*id.{0,80}(?:不存在|未找到|为空|缺失|不能为空)", text, re.I)]
            success = [text for text in texts if re.search(r"(?:提交|上传|导入|操作|创建|受理)成功", text)]
            if errors:
                result.update(outcome="rejected", feedback="；".join(errors)[:1200])
                break
            if success and not dialog.is_visible():
                result.update(outcome="accepted", feedback="；".join(success)[:1200])
                break
            page.wait_for_timeout(200)
        result["screenshot_result"] = _screenshot(page, Path(artifact_dir), "04_submit_feedback")
        if result["outcome"] == "unknown":
            result["feedback"] = "已点击确定，未取得明确受理反馈；请核对盖亚任务，禁止重复提交"
        result["success"] = result["outcome"] == "accepted"
        if result["success"] and auto_price:
            # Gaia acknowledges the upload before its channel-product index is
            # necessarily updated. Poll read-only; never resubmit the workbook.
            reconcile_deadline = time.monotonic() + 45
            while True:
                try:
                    after = snapshot_channel_products(page, str(shop_id), expected_products)
                    result["verified_product_ids"] = reconcile_channel_products(
                        result["before_channel_snapshot"], after,
                        shop_id=str(shop_id), expected_products=expected_products,
                        submitted_at=submitted_at, observed_at=datetime.now(),
                    )
                    result["price_reconcile_status"] = "verified"
                    break
                except Exception as exc:
                    # Only an empty post-upload result can plausibly be index
                    # lag. Ambiguity or a changed page contract fails closed.
                    retryable = "本次新增商品不能唯一确认（0 个）" in str(exc)
                    if not retryable or time.monotonic() >= reconcile_deadline:
                        result["price_reconcile_status"] = "blocked"
                        result["price_reconcile_error"] = str(exc).splitlines()[0][:600]
                        break
                    page.wait_for_timeout(3000)
        result.pop("before_channel_snapshot", None)
        return result

    return with_gaia_upload_form(
        work_dir=work_dir, batch_id=batch_id, shop_id=shop_id,
        profile_dir=profile_dir, artifact_dir=artifact_dir, login_wait=120,
        verify_handoff=verify_handoff, on_ready=confirm_once,
        check_cancelled=check_cancelled, before_create=before_create,
    )
