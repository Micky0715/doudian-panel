"""Inspect and temporarily close a draft editor's specification switch.

The switch changes only the open browser form. This probe deliberately never
clicks Save Draft or Publish, so it does not claim persistence.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe DouDian draft specification switch")
    parser.add_argument("product_id")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--output-root", default="panel_data/doudian_ui_spec_switch_probes")
    parser.add_argument("--try-redmi-selection", action="store_true",
                        help="select Redmi in the unsaved form to inspect model options")
    parser.add_argument("--model-search", default="Turbo 4")
    parser.add_argument("--try-model-selection", action="store_true",
                        help="select one exact live model in the unsaved form")
    args = parser.parse_args()
    if not args.product_id.isdigit():
        parser.error("product_id must be numeric")
    output = Path(args.output_root) / f"{int(time.time())}_{args.product_id}"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"product_id": args.product_id, "saved": False, "published": False,
              "result": "not_checked"}
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
            report["url"] = page.url
            if "login" in page.url.lower() or "passport" in page.url.lower():
                raise RuntimeError("抖店专用浏览器尚未登录")
            label = page.get_by_text("修改规格信息", exact=True)
            if label.count() != 1:
                raise RuntimeError(f"修改规格信息标签不唯一：{label.count()}")
            switch = label.locator("xpath=..").get_by_role("switch")
            if switch.count() != 1:
                raise RuntimeError(f"修改规格信息开关不唯一：{switch.count()}")
            switch.scroll_into_view_if_needed()
            page.wait_for_timeout(400)
            prompt = page.get_by_text("我知道了", exact=True)
            report["upgrade_prompt_count"] = prompt.count()
            if prompt.count() == 1 and prompt.is_visible():
                prompt.click(timeout=5000)
                report["upgrade_prompt_dismissed"] = True
            report["switch_before"] = switch.get_attribute("aria-checked")
            if report["switch_before"] == "true":
                switch.click(timeout=8000)
                page.wait_for_timeout(500)
            report["switch_after"] = switch.get_attribute("aria-checked")
            if report["switch_after"] != "false":
                raise RuntimeError("规格信息开关未能关闭")
            switch.scroll_into_view_if_needed()
            page.screenshot(path=str(output.with_suffix(".png")), full_page=False)
            report["switch_screenshot"] = str(output.with_suffix(".png"))
            model = page.get_by_text("型号", exact=True)
            report["model_label_count"] = model.count()
            if model.count() == 1:
                report["model_field_text"] = model.evaluate(
                    "el => (el.parentElement?.parentElement?.innerText || '').slice(0, 400)")
                report["model_ancestors"] = model.evaluate(
                    "el => { const out = []; let node = el; for (let i = 0; i < 9 && node; i++, node = node.parentElement) out.push({level:i,html:(node.outerHTML || '').slice(0, 5000)}); return out; }")
                model.scroll_into_view_if_needed()
                page.screenshot(path=str(output.with_name(output.name + "_model.png")),
                                full_page=False)
                report["model_screenshot"] = str(output.with_name(output.name + "_model.png"))
                model_input = page.locator('[attr-field-id="型号"] input[role="combobox"]')
                if model_input.count() == 1:
                    model_input.click()
                    model_input.fill(args.model_search)
                    page.wait_for_timeout(1100)
                    report["model_search"] = args.model_search
                    report["model_options"] = [
                        text.strip() for text in page.locator('[role="option"]').all_inner_texts()
                        if text.strip()
                    ][:30]
                    page.screenshot(path=str(output.with_name(output.name + "_model_search.png")),
                                    full_page=False)
                    report["model_search_screenshot"] = str(
                        output.with_name(output.name + "_model_search.png"))
                    page.keyboard.press("Escape")
            brand_input = page.locator('[attr-field-id="品牌"] input[role="combobox"]')
            report["brand_input_count"] = brand_input.count()
            if brand_input.count() == 1:
                brand_selector = page.locator('[attr-field-id="品牌"] .ecom-g-select-selector')
                if brand_selector.count() != 1:
                    raise RuntimeError("品牌选择框不唯一")
                brand_selector.scroll_into_view_if_needed()
                brand_selector.click(timeout=8000)
                brand_input.fill("Redmi")
                page.wait_for_timeout(1000)
                report["brand_search"] = "Redmi"
                report["brand_dropdown_text"] = [
                    text.strip() for text in page.locator('.ecom-g-select-dropdown:visible').all_inner_texts()
                    if text.strip()
                ][:5]
                page.screenshot(path=str(output.with_name(output.name + "_brand_search.png")),
                                full_page=False)
                report["brand_search_screenshot"] = str(
                    output.with_name(output.name + "_brand_search.png"))
                if args.try_redmi_selection:
                    options = page.locator('.ecom-g-select-dropdown:visible')
                    target = options.get_by_text("Redmi/红米", exact=True)
                    if target.count() != 1:
                        raise RuntimeError(f"Redmi/红米 品牌选项不唯一：{target.count()}")
                    target.click(timeout=8000)
                    page.wait_for_timeout(700)
                    report["unsaved_brand_text"] = page.locator(
                        '[attr-field-id="品牌"]').inner_text()[:300]
                    model_selector = page.locator(
                        '[attr-field-id="型号"] .ecom-g-select-selector')
                    if model_selector.count() != 1:
                        raise RuntimeError("切换品牌后型号选择框不唯一")
                    model_selector.click(timeout=8000)
                    page.locator('[attr-field-id="型号"] input[role="combobox"]').fill(
                        args.model_search)
                    page.wait_for_timeout(2200)
                    report["redmi_model_dropdown_text"] = [
                        text.strip() for text in page.locator(
                            '.ecom-g-select-dropdown:visible').all_inner_texts() if text.strip()
                    ][:5]
                    page.screenshot(path=str(output.with_name(output.name + "_redmi_model.png")),
                                    full_page=False)
                    report["redmi_model_screenshot"] = str(
                        output.with_name(output.name + "_redmi_model.png"))
                    if args.try_model_selection:
                        exact_model = page.locator(
                            '.ecom-g-select-dropdown:visible').get_by_text(
                                "REDMI Turbo 4", exact=True)
                        if exact_model.count() != 1:
                            raise RuntimeError(f"REDMI Turbo 4 型号选项不唯一：{exact_model.count()}")
                        exact_model.click(timeout=8000)
                        page.wait_for_timeout(700)
                        report["unsaved_model_text"] = page.locator(
                            '[attr-field-id="型号"]').inner_text()[:300]
                        report["unsaved_error_summary"] = page.locator(
                            "body").inner_text()[:1000]
                        page.screenshot(path=str(output.with_name(output.name + "_selected.png")),
                                        full_page=False)
                        report["selected_screenshot"] = str(
                            output.with_name(output.name + "_selected.png"))
            report["result"] = "switch_closed_in_unsaved_form"
        except Exception as exc:
            report["result"] = "blocked"
            report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            try:
                page.screenshot(path=str(output.with_name(output.name + "_blocked.png")),
                                full_page=False)
                report["blocked_screenshot"] = str(output.with_name(output.name + "_blocked.png"))
            except Exception:
                pass
        finally:
            context.close()
    output.with_suffix(".json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                             encoding="utf-8")
    print(json.dumps({"result": report["result"], "report": str(output.with_suffix(".json"))},
                     ensure_ascii=False), flush=True)
    return 0 if report["result"] == "switch_closed_in_unsaved_form" else 2


if __name__ == "__main__":
    raise SystemExit(main())
