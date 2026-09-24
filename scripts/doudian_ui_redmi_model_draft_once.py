"""One locked DouDian draft: correct Redmi brand/model with Playwright.

The legacy specification switch is closed before Save Draft. No Publish action
is reachable from this script. Existing SKU structure is checked by API readback
before any subsequent media step may run.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any, Mapping

from playwright.sync_api import sync_playwright

from doudian_api import MODEL_PROPERTY_ID
from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from scripts.doudian_sku_color_draft_once import _product_id, _source_fingerprint, _write_json


class UIModelDraftError(RuntimeError):
    pass


def _props(detail: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_product_format_for_edit(parse_product_format(detail.get("product_format_new")))


def _verify(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if _product_id(after) != _product_id(before):
        issues.append("商品 ID 改变")
    for key in ("status", "check_status", "draft_status", "name_suffix", "brand_id", "spec_id", "specs",
                "spec_prices", "spec_pics", "pic", "description", "freight_id",
                "product_type", "white_back_ground_pic_url", "main_pic_3_4"):
        if before.get(key) != after.get(key):
            issues.append(key + " 改变")
    if (after.get("status"), after.get("check_status"), after.get("draft_status")) != (0, 1, 1):
        issues.append("商品不再是未发布草稿")
    if "Redmi" not in str(after.get("name_prefix") or ""):
        issues.append("标题品牌前缀未变为 Redmi")
    if str(after.get("name") or "") != (
            str(after.get("name_prefix") or "") + str(before.get("name") or "")):
        issues.append("平台重组后的商品标题与原商品名不一致")
    if str(after.get("standard_brand_id")) != "663234468":
        issues.append("标准品牌 ID 未变为平台 Redmi 选项")
    before_props, after_props = _props(before), _props(after)
    model = after_props.get(str(MODEL_PROPERTY_ID)) or []
    if (len(model) != 1 or not isinstance(model[0], Mapping)
            or str(model[0].get("value")) != "488936"
            or model[0].get("name") != "REDMI Turbo 4"
            or model[0].get("diy_type") != 0):
        issues.append("官方型号 488936 未保存")
    before_props.pop(str(MODEL_PROPERTY_ID), None)
    after_props.pop(str(MODEL_PROPERTY_ID), None)
    if before_props != after_props:
        issues.append("型号之外的属性改变")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="One Redmi Turbo 4 browser draft save")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--report-root", default="panel_data/doudian_ui_redmi_model_canary")
    args = parser.parse_args()
    if (str(args.shop_id), str(args.product_id)) != ("156450040", "3842662503586857408"):
        raise UIModelDraftError("本脚本仅锁定已预检的 156450040 / 3842662503586857408")

    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, args.shop_id))
    before = dict(reader.get_product_detail(args.product_id, show_draft=True))
    if (_product_id(before) != args.product_id or before.get("status") != 0
            or before.get("check_status") != 1 or before.get("draft_status") != 1
            or "REDMI Turbo 4" not in str(before.get("name") or "")):
        raise UIModelDraftError("目标商品状态或型号不符合已预检草稿")
    if before.get("spec_pics"):
        raise UIModelDraftError("草稿已存在颜色规格图，不属于本次首次全流程验收")

    stamp = int(time.time())
    stem = Path(args.report_root) / args.shop_id / f"{stamp}_{args.product_id}"
    _write_json(stem.with_suffix(".before.json"), {"detail": before, "captured_at": stamp})
    report: dict[str, Any] = {"shop_id": args.shop_id, "product_id": args.product_id,
                              "save_draft_only": True, "publish": False,
                              "result": "started", "save_clicked": False}
    _write_json(stem.with_suffix(".report.json"), report)
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(Path(args.profile).resolve()), headless=False,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                viewport={"width": 1440, "height": 900},
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(f"https://fxg.jinritemai.com/ffa/g/create?product_id={args.product_id}&entrance=draft",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4500)
                if args.product_id not in page.url or "login" in page.url.lower():
                    raise UIModelDraftError("未进入目标草稿编辑页")
                label = page.get_by_text("修改规格信息", exact=True)
                if label.count() != 1:
                    raise UIModelDraftError("规格信息标签不唯一")
                switch = label.locator("xpath=..").get_by_role("switch")
                if switch.count() != 1:
                    raise UIModelDraftError("规格信息开关不唯一")
                switch.scroll_into_view_if_needed()
                prompt = page.get_by_text("我知道了", exact=True)
                if prompt.count() == 1 and prompt.is_visible():
                    prompt.evaluate("el => el.closest('button')?.click()")
                    page.wait_for_timeout(300)
                report["switch_before"] = switch.get_attribute("aria-checked")
                if report["switch_before"] == "true":
                    switch.click(timeout=8000)
                if switch.get_attribute("aria-checked") != "false":
                    raise UIModelDraftError("修改规格信息开关未关闭")
                report["switch_after"] = "false"

                # The editor lazily initializes its model search only after
                # the first focus. Warm it before changing the selected brand.
                initial_model_selector = page.locator(
                    '[attr-field-id="型号"] .ecom-g-select-selector')
                initial_model_input = page.locator(
                    '[attr-field-id="型号"] input[role="combobox"]')
                if initial_model_selector.count() != 1 or initial_model_input.count() != 1:
                    raise UIModelDraftError("初始型号选择框不唯一")
                initial_model_selector.click(timeout=8000)
                initial_model_input.fill("REDMI Turbo 4")
                page.wait_for_timeout(1100)
                page.keyboard.press("Escape")

                brand_selector = page.locator('[attr-field-id="品牌"] .ecom-g-select-selector')
                brand_input = page.locator('[attr-field-id="品牌"] input[role="combobox"]')
                if brand_selector.count() != 1 or brand_input.count() != 1:
                    raise UIModelDraftError("品牌选择框不唯一")
                brand_selector.click(timeout=8000)
                brand_input.fill("Redmi")
                page.wait_for_timeout(1000)
                brand = page.locator('.ecom-g-select-dropdown:visible').get_by_text(
                    "Redmi/红米", exact=True)
                if brand.count() != 1:
                    raise UIModelDraftError("平台 Redmi/红米 品牌选项不唯一")
                brand.click(timeout=8000)
                page.wait_for_timeout(1200)
                model_selector = page.locator('[attr-field-id="型号"] .ecom-g-select-selector')
                model_input = page.locator('[attr-field-id="型号"] input[role="combobox"]')
                if model_selector.count() != 1 or model_input.count() != 1:
                    raise UIModelDraftError("型号选择框不唯一")
                model_selector.click(timeout=8000)
                model_input.fill("REDMI Turbo 4")
                page.wait_for_timeout(2200)
                dropdown = page.locator('.ecom-g-select-dropdown:visible')
                report["model_dropdown_text"] = [text.strip() for text in
                                                 dropdown.all_inner_texts() if text.strip()][:5]
                model = dropdown.get_by_text(
                    "REDMI Turbo 4", exact=True)
                report["model_option_count"] = model.count()
                if model.count() != 1:
                    page.screenshot(path=str(stem.with_suffix(".model_blocked.png")),
                                    full_page=False)
                    report["model_blocked_screenshot"] = str(stem.with_suffix(".model_blocked.png"))
                    raise UIModelDraftError(f"平台型号 REDMI Turbo 4 选项数量异常：{model.count()}")
                model.click(timeout=8000)
                page.wait_for_timeout(700)
                brand_text = page.locator('[attr-field-id="品牌"]').inner_text()
                model_text = page.locator('[attr-field-id="型号"]').inner_text()
                report["brand_field"] = brand_text[:200]
                report["model_field"] = model_text[:200]
                if ("Redmi/红米" not in brand_text or "REDMI Turbo 4" not in model_text
                        or switch.get_attribute("aria-checked") != "false"):
                    raise UIModelDraftError("保存前页面字段未满足品牌、型号和关闭规格的三重校验")
                page.screenshot(path=str(stem.with_suffix(".before_save.png")), full_page=False)
                latest = dict(reader.get_product_detail(args.product_id, show_draft=True))
                if _source_fingerprint(before) != _source_fingerprint(latest):
                    raise UIModelDraftError("页面操作期间草稿被其他进程修改；未保存")
                save = page.get_by_role("button", name="保存草稿", exact=True)
                if save.count() != 1 or not save.is_enabled():
                    raise UIModelDraftError("保存草稿按钮不可用或不唯一")
                report["result"] = "save_started"
                _write_json(stem.with_suffix(".report.json"), report)
                save.click(timeout=8000)
                report["save_clicked"] = True
                page.wait_for_timeout(5000)
                report["url_after_save"] = page.url
                report["visible_text_after_save"] = page.locator("body").inner_text()[:1200]
                page.screenshot(path=str(stem.with_suffix(".after_save.png")), full_page=False)
            finally:
                context.close()
        after = dict(reader.get_product_detail(args.product_id, show_draft=True))
        _write_json(stem.with_suffix(".after.json"), {"detail": after,
                                                     "captured_at": int(time.time())})
        report["brand_id_before"] = before.get("brand_id")
        report["brand_id_after"] = after.get("brand_id")
        report["issues"] = _verify(before, after)
        report["result"] = "verified_draft" if not report["issues"] else "needs_manual_review"
    except Exception as exc:
        report["result"] = "needs_manual_review" if report["save_clicked"] else "blocked_before_save"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    _write_json(stem.with_suffix(".report.json"), report)
    print({"result": report["result"], "report": str(stem.with_suffix(".report.json")),
           "save_clicked": report["save_clicked"], "publish": False}, flush=True)
    return 0 if report["result"] == "verified_draft" else 2


if __name__ == "__main__":
    raise SystemExit(main())
