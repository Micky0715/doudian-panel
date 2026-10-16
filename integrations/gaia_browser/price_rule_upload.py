"""Import verified Douyin product IDs into one exact Gaia price-uplift rule."""

from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright

from app.gaia_listing.application.price_uplift import PRICE_RULE_NAMES, PriceUpliftError
from .file_attachment import wait_for_attachment
from .price_rule_probe import RULES_URL, locate_price_rule
from .probe import _screenshot


def _price_file_button(dialog):
    # Ant Upload exposes its wrapper and nested native button as role=button.
    label = dialog.get_by_text("选择文件", exact=True).filter(visible=True)
    if label.count() != 1:
        raise PriceUpliftError("价格上浮文件选择文字不唯一")
    button = label.locator("xpath=ancestor::button[1]")
    if button.count() != 1:
        raise PriceUpliftError("价格上浮文件选择按钮不唯一")
    return button


def _import_product_button(detail, timeout_seconds: float = 15):
    """Wait for the async edit form to render one visible native import button."""
    buttons = detail.get_by_role("button", name="导入商品ID", exact=True).filter(visible=True)
    deadline = time.monotonic() + timeout_seconds
    count = 0
    while time.monotonic() < deadline:
        count = buttons.count()
        if count == 1 and buttons.first.is_enabled():
            return buttons.first
        detail.wait_for_timeout(100)
    if count == 0:
        raise PriceUpliftError("价格规则编辑页未出现可用的导入商品 ID 按钮")
    raise PriceUpliftError(f"价格规则编辑页有 {count} 个可见导入商品 ID 按钮，无法唯一定位")


def _response_summary(response) -> dict:
    """Keep submission diagnostics without persisting tokens or request bodies."""
    parts = urlsplit(response.url)
    summary = {"path": parts.path, "status": response.status}
    api_name = parse_qs(parts.query).get("apiName", [""])[0]
    if api_name:
        summary["api_name"] = api_name[:200]
    try:
        payload = response.json()
    except Exception:
        return summary
    if isinstance(payload, dict):
        for key in ("code", "status_code", "msg", "message", "sub_code", "sub_msg"):
            value = payload.get(key)
            if isinstance(value, (str, int, float)):
                summary[key] = str(value)[:300]
        data = payload.get("data")
        if isinstance(data, dict):
            for key in ("code", "status_code", "msg", "message", "success"):
                value = data.get(key)
                if isinstance(value, (str, int, float, bool)):
                    summary[f"data_{key}"] = str(value)[:300]
    return summary


def _response_rejection(responses: list[dict]) -> str:
    for item in responses:
        if not re.search(r"import|batch|rule|product|goods|strategy",
                         item.get("path", "") + item.get("api_name", ""), re.I):
            continue
        code = item.get("code") or item.get("status_code")
        message = "；".join(str(item[key]) for key in
                           ("msg", "message", "sub_msg", "data_msg", "data_message")
                           if item.get(key))
        if (item.get("status", 200) >= 400 or (code and code not in {"0", "10000", "200"})
                or item.get("data_success") == "False"
                or re.search(r"失败|错误|异常|无权限", message)):
            return (message or f"提交接口返回 HTTP {item['status']} / code {code}")[:500]
    return ""


def _read_rule_product_ids(detail, rule_name: str, expected_ids: set[str]) -> dict:
    """Reload and read the complete rule membership; never change the rule."""
    detail.reload(wait_until="domcontentloaded", timeout=30000)
    detail.get_by_role("button", name="导入商品ID", exact=True).wait_for(state="visible", timeout=15000)
    detail.wait_for_function(
        "name => [...document.querySelectorAll('input')].some(input => input.value === name)",
        arg=rule_name, timeout=10000,
    )
    total = None
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        total_text = detail.locator(".ant-pagination-total-text").filter(visible=True)
        match = (re.search(r"共\s*(\d+)\s*条", total_text.inner_text())
                 if total_text.count() == 1 else None)
        rows = detail.locator("table tbody tr").filter(visible=True).all_inner_texts()
        first_ids = {item.group(0) for row in rows for item in
                     re.finditer(r"(?<!\d)\d{10,20}(?!\d)", row)}
        if match and first_ids and int(match.group(1)) >= len(first_ids):
            total = int(match.group(1))
            break
        detail.wait_for_timeout(100)
    if total is None or total > 1000:
        raise PriceUpliftError("规则商品总数或首屏商品 ID 无法完整回读")
    seen: set[str] = set()
    page_size = 0
    while len(seen) < total:
        rows = detail.locator("table tbody tr").filter(visible=True).all_inner_texts()
        page_ids = {match.group(0) for row in rows for match in
                    re.finditer(r"(?<!\d)\d{10,20}(?!\d)", row)}
        if not page_ids or seen.intersection(page_ids):
            raise PriceUpliftError("规则商品分页内容重复或为空，无法完整核对")
        if not page_size:
            page_size = len(page_ids)
        seen.update(page_ids)
        if len(seen) >= total:
            break
        next_button = detail.locator(".ant-pagination-next button").filter(visible=True)
        if next_button.count() != 1 or next_button.is_disabled():
            raise PriceUpliftError("规则商品分页未完整读取")
        next_button.click(timeout=8000)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail.wait_for_timeout(100)
            fresh = {match.group(0) for row in detail.locator(
                "table tbody tr").filter(visible=True).all_inner_texts() for match in
                re.finditer(r"(?<!\d)\d{10,20}(?!\d)", row)}
            if len(fresh) == min(page_size, total - len(seen)) and not seen.intersection(fresh):
                break
        else:
            raise PriceUpliftError("规则商品翻页后新结果未完整加载")
    if len(seen) != total:
        raise PriceUpliftError("规则商品实际条数与分页总数不一致")
    return {"total": total, "found_ids": sorted(seen.intersection(expected_ids)),
            "missing_ids": sorted(expected_ids - seen)}


def upload_price_rule(*, profile_dir: str, shop_id: str, installments: int,
                      workbook_path: str, artifact_dir: str, before_submit,
                      expected_product_ids: list[str] | None = None, scene: int = 2,
                      expected_rule_name: str | None = None,
                      expected_rate_text: str | None = None) -> dict:
    """One-click submission. The caller persists intent before the final click."""
    if str(shop_id) not in PRICE_RULE_NAMES or int(installments) not in {6, 12, 24} or scene not in {1, 2}:
        raise PriceUpliftError("店铺、期数或场域不在允许范围")
    workbook = Path(workbook_path).resolve()
    if workbook.suffix.lower() != ".xlsx" or not workbook.is_file() or workbook.stat().st_size <= 0:
        raise PriceUpliftError("价格上浮导入文件不存在")
    artifacts = Path(artifact_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(RULES_URL, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("价格上浮", exact=True).first.wait_for(state="visible", timeout=15000)
            page.locator("table tbody tr").filter(visible=True).first.wait_for(state="visible", timeout=15000)
            matched, cells = locate_price_rule(page, shop_id, installments, scene)
            rule_name = cells[0]
            if ((expected_rule_name and rule_name != expected_rule_name)
                    or (expected_rate_text and cells[2] != expected_rate_text)):
                raise PriceUpliftError("价格规则名称或上浮比例在预检后发生变化")
            edit = matched.get_by_role("button", name="编辑", exact=True)
            if edit.count() != 1:
                raise PriceUpliftError("规则编辑按钮不唯一")
            with page.expect_popup(timeout=10000) as popup:
                edit.click(timeout=8000)
            detail = popup.value
            detail.wait_for_load_state("domcontentloaded", timeout=15000)
            if "ruleType=priceIncrease" not in detail.url or "type=edit" not in detail.url:
                raise PriceUpliftError("打开的不是价格上浮规则编辑页")
            detail.wait_for_function(
                "name => [...document.querySelectorAll('input')].some(input => input.value === name)",
                arg=rule_name, timeout=10000,
            )
            if sum(value == rule_name for value in detail.locator("input").evaluate_all(
                    "inputs => inputs.map(input => input.value)")) != 1:
                raise PriceUpliftError("规则编辑页名称回读不一致")
            import_button = _import_product_button(detail)
            import_button.click(timeout=8000)
            dialog = detail.get_by_role("dialog").filter(visible=True)
            if dialog.count() != 1 or "批量操作" not in dialog.inner_text():
                raise PriceUpliftError("价格上浮文件上传弹窗不唯一")
            choose = _price_file_button(dialog)
            with detail.expect_file_chooser(timeout=8000) as chooser:
                choose.click(timeout=8000)
            chooser.value.set_files(str(workbook))
            readback = wait_for_attachment(
                detail, dialog, expected_name=workbook.name, expected_size=workbook.stat().st_size,
                batch_id=artifacts.name, phase="price_uplift_before_submit",
            )
            confirm = dialog.get_by_role("button", name=re.compile(r"确\s*定")).filter(visible=True)
            if confirm.count() != 1 or not confirm.is_enabled():
                raise PriceUpliftError("价格上浮导入确定按钮不可用")
            screenshot_ready = _screenshot(detail, artifacts, "price_01_ready")
            baseline = {item.strip() for item in detail.locator(
                '.ant-message-notice, .ant-notification-notice, [role="alert"]'
            ).filter(visible=True).all_inner_texts()}
            responses: list[dict] = []
            submit_requests: list[str] = []
            request_failures: list[dict] = []

            def record_request(request):
                parts = urlsplit(request.url)
                if request.method in {"POST", "PUT", "PATCH"} or parts.path == "/api":
                    api_name = parse_qs(parts.query).get("apiName", [""])[0]
                    submit_requests.append(api_name[:200] or parts.path)

            def record_response(response):
                if (response.request.method in {"POST", "PUT", "PATCH"}
                        or urlsplit(response.url).path == "/api"):
                    responses.append(_response_summary(response))

            def record_request_failure(request):
                parts = urlsplit(request.url)
                if request.method in {"POST", "PUT", "PATCH"} or parts.path == "/api":
                    request_failures.append({"path": parts.path,
                                             "api_name": parse_qs(parts.query).get("apiName", [""])[0][:200],
                                             "failure": str(request.failure)[:300]})

            detail.on("request", record_request)
            detail.on("response", record_response)
            detail.on("requestfailed", record_request_failure)
            before_submit()
            confirm.click(timeout=8000)
            deadline = time.monotonic() + 25
            feedback = ""
            outcome = "unknown"
            while time.monotonic() < deadline:
                messages = [item.strip() for item in detail.locator(
                    '.ant-message-notice, .ant-notification-notice, [role="alert"]'
                ).filter(visible=True).all_inner_texts()]
                fresh = [item for item in messages if item and item not in baseline]
                if any(re.search(r"失败|错误|异常|无权限", item) for item in fresh):
                    outcome, feedback = "rejected", "；".join(fresh)[:800]
                    break
                response_error = _response_rejection(responses)
                if response_error:
                    outcome, feedback = "rejected", response_error
                    break
                if any(re.search(r"(?:导入|上传|操作)成功", item) for item in fresh):
                    outcome, feedback = "accepted", "；".join(fresh)[:800]
                    break
                detail.wait_for_timeout(200)
            screenshot_result = _screenshot(detail, artifacts, "price_02_feedback")
            membership = None
            membership_error = None
            if expected_product_ids:
                expected = {str(item) for item in expected_product_ids}
                try:
                    membership = _read_rule_product_ids(detail, rule_name, expected)
                    if not membership["missing_ids"]:
                        outcome, feedback = "accepted", "规则商品列表已完整核对，全部目标商品 ID 均存在"
                    elif outcome == "accepted":
                        outcome, feedback = "unknown", "页面提示成功，但规则列表未包含全部目标商品 ID"
                except Exception as exc:
                    membership_error = str(exc).splitlines()[0][:500]
                    if outcome == "accepted":
                        outcome, feedback = "unknown", "页面提示成功，但规则商品列表核对失败"
            if outcome == "unknown" and not feedback:
                if not submit_requests:
                    feedback = "点击确定后未观察到提交请求；规则商品未验证新增"
                elif request_failures:
                    feedback = "提交请求失败；规则商品未验证新增"
                elif responses:
                    feedback = "提交接口已响应，但规则商品未验证新增"
            return {"outcome": outcome, "feedback": feedback or "未取得明确导入回执",
                    "rule_name": rule_name, "installments": int(installments),
                    "workbook_name": workbook.name, "attachment_readback": readback,
                    "screenshot_ready": screenshot_ready, "screenshot_result": screenshot_result,
                    "submit_requests": submit_requests[:20],
                    "submit_responses": responses[:20], "request_failures": request_failures[:20],
                    "membership": membership, "membership_error": membership_error,
                    "submitted": True}
        finally:
            context.close()
