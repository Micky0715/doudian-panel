"""Read-only Playwright snapshot of one DouDian draft editor.

This script never clicks controls, reads browser storage, or writes product data.
It captures visible page evidence for human review before any draft edit.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def _dismiss_upgrade_prompt(page) -> None:
    prompt = page.get_by_text("我知道了", exact=True)
    if prompt.count() == 1 and prompt.is_visible():
        prompt.evaluate("el => el.closest('button')?.click()")
        page.wait_for_timeout(250)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DouDian product editor probe")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--output-root", default="panel_data/doudian_ui_product_probes")
    args = parser.parse_args()
    if not args.shop_id.isdigit() or not args.product_id.isdigit():
        parser.error("shop_id and product_id must be numeric")

    target_url = ("https://fxg.jinritemai.com/ffa/g/create?"
                  f"product_id={args.product_id}&entrance=draft")
    output = Path(args.output_root) / args.shop_id / f"{int(time.time())}_{args.product_id}"
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"shop_id_requested": args.shop_id, "product_id_requested": args.product_id,
              "read_only": True, "result": "not_checked"}
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile).resolve()), headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.locator('[attr-field-id="型号"]').wait_for(state="attached", timeout=30000)
            except Exception:
                pass
            page.wait_for_timeout(1000)
            _dismiss_upgrade_prompt(page)
            report["url"] = page.url
            report["title"] = page.title()
            body = page.locator("body").inner_text(timeout=10000)
            report["body_excerpt"] = body[:3000]
            page.screenshot(path=str(output.with_suffix(".png")), full_page=False)
            report["screenshot"] = str(output.with_suffix(".png"))
            if "login" in page.url.lower() or "passport" in page.url.lower():
                report["result"] = "login_required"
            elif args.product_id not in page.url:
                report["result"] = "unexpected_page"
            elif page.locator('[attr-field-id="型号"]').count() != 1:
                report["result"] = "editor_not_rendered"
            else:
                report["result"] = "editor_page_loaded"
                report["brand_field"] = page.locator(
                    '[attr-field-id="品牌"]').inner_text()[:200]
                report["model_field"] = page.locator(
                    '[attr-field-id="型号"]').inner_text()[:200]
                for label in ("修改规格信息", "商品规格", "添加规格图"):
                    locator = page.get_by_text(label, exact=False)
                    report[label] = {"count": locator.count()}
                    if locator.count() == 1:
                        locator.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(400)
                        _dismiss_upgrade_prompt(page)
                        page.screenshot(path=str(output.with_name(output.name + "_spec.png")),
                                        full_page=False)
                        report[label]["screenshot"] = str(
                            output.with_name(output.name + "_spec.png"))
                        report[label]["nearby_text"] = locator.evaluate(
                            "el => (el.parentElement?.parentElement?.innerText || '').slice(0, 600)")
                        if label == "修改规格信息":
                            report[label]["nearby_html"] = locator.evaluate(
                                "el => (el.parentElement?.parentElement?.outerHTML || '').slice(0, 3000)")
                            report[label]["aria_checked"] = locator.locator(
                                "xpath=..").get_by_role("switch").get_attribute("aria-checked")
                for label in ("商品正面图", "白底图", "商品详情"):
                    locator = page.get_by_text(label, exact=True)
                    report[label] = {"count": locator.count()}
                    if locator.count() == 1:
                        locator.scroll_into_view_if_needed(timeout=5000)
                        page.wait_for_timeout(500)
                        _dismiss_upgrade_prompt(page)
                        image_path = output.with_name(output.name + "_" + label + ".png")
                        page.screenshot(path=str(image_path), full_page=False)
                        report[label]["screenshot"] = str(image_path)
                        report[label]["nearby_text"] = locator.evaluate(
                            "el => (el.parentElement?.parentElement?.innerText || '').slice(0, 500)")
        except Exception as exc:
            report["result"] = "probe_failed"
            report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        finally:
            context.close()
    output.with_suffix(".json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result": report["result"], "report": str(output.with_suffix(".json"))},
                     ensure_ascii=False), flush=True)
    return 0 if report["result"] == "editor_page_loaded" else 2


if __name__ == "__main__":
    raise SystemExit(main())
