"""Read-only inspection of Gaia's task-result surface."""

from __future__ import annotations

import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from .probe import GAIA_CHANNEL_URL


def probe_task_results(profile_dir: str) -> dict:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            task_link = page.get_by_role("link", name="任务列表", exact=True)
            if task_link.count() == 0:
                tools_menu = page.get_by_text("工具", exact=True).filter(visible=True)
                if tools_menu.count() == 1:
                    tools_menu.click(timeout=10000)
                task_link = page.get_by_text("任务列表", exact=True).filter(visible=True)
            if task_link.count() != 1:
                return {"ready": False, "reason": "任务列表入口不可唯一定位",
                        "count": task_link.count(), "url": page.url,
                        "visible_text": page.locator("body").inner_text()[:1500]}
            task_link.click(timeout=10000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1800)
            return {
                "ready": True,
                "url": page.url,
                "headings": page.locator("h1,h2,h3").all_text_contents()[:20],
                "table_count": page.locator("table").count(),
                "table_text": [value[:3000] for value in page.locator("table").all_text_contents()[:3]],
                "buttons": page.get_by_role("button").all_text_contents()[:50],
                "visible_text": page.locator("body").inner_text()[:4000],
                "frames": [frame.url for frame in page.frames],
            }
        finally:
            context.close()


def probe_channel_products(profile_dir: str, shop_name: str = "") -> dict:
    """Check whether Gaia's channel-product list exposes source-to-product IDs."""
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("抖音渠道", exact=True).first.wait_for(state="visible", timeout=15000)
            page.get_by_text("抖音渠道", exact=True).first.click(timeout=8000)
            page.wait_for_timeout(1000)
            dropdown_options = []
            if shop_name:
                shop_selector = page.locator(".ant-select-selector").filter(visible=True).first
                shop_selector.click(timeout=8000)
                dropdown = page.locator(".ant-select-dropdown").filter(visible=True).first
                dropdown.wait_for(state="visible", timeout=8000)
                dropdown_options = dropdown.locator('[role="option"], .ant-select-dropdown-menu-item').all_inner_texts()[:40]
                option = dropdown.get_by_text(shop_name, exact=True).filter(visible=True)
                if option.count() != 1:
                    return {"ready": False, "reason": "店铺选项不唯一", "options": dropdown_options}
                option.click(timeout=8000)
                page.get_by_role("button", name="搜 索").click(timeout=8000)
                page.wait_for_timeout(1000)
            return {
                "url": page.url,
                "visible_text": page.locator("body").inner_text()[:2500],
                "shop_id_fields": page.get_by_text("店铺ID", exact=True).evaluate_all(
                    "nodes => nodes.map(n => n.parentElement?.outerHTML.slice(0, 1000))"),
                "filter_inputs": page.locator('input[id^="advanced_search_"]').evaluate_all(
                    "nodes => nodes.map(n => ({id:n.id,placeholder:n.placeholder}))"),
                "table_headers": [value.strip() for value in page.locator("table thead th").all_inner_texts()],
                "first_rows": page.locator("table tbody tr").all_inner_texts()[:3],
                "shop_options": dropdown_options,
            }
        finally:
            context.close()


if __name__ == "__main__":
    import sys
    result = (probe_channel_products(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else "")
              if len(sys.argv) > 2 and sys.argv[2] == "channel"
              else probe_task_results(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
