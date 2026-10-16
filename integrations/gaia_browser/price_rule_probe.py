"""Read-only survey of Gaia price-increase rule names and paging."""

from __future__ import annotations

import json
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.gaia_listing.application.price_uplift import (
    PRICE_RULE_NAMES, PRICE_RULE_RATES, PriceUpliftError, rule_name_matches_shop_term,
)


RULES_URL = "https://gaiya.jd.com/product/liveStreamManagement/liveProductRules"


def locate_price_rule(page, shop_id: str, installments: int, scene: int = 2):
    """Scan every page for one enabled rule matching ID, term and scene."""
    shop_id = str(shop_id)
    if shop_id not in PRICE_RULE_NAMES or int(installments) not in {6, 12, 24} or scene not in {1, 2}:
        raise PriceUpliftError("店铺、期数或场域不在允许范围")
    page.locator(".ant-spin-blur").wait_for(state="hidden", timeout=10000)
    page.locator("table tbody tr").filter(visible=True).first.wait_for(state="visible", timeout=10000)
    matches = []
    for page_number in range(1, 21):
        rows = page.locator("table tbody tr").filter(visible=True)
        for row in rows.all():
            cells = [value.strip() for value in row.locator("td").all_inner_texts()]
            if cells and rule_name_matches_shop_term(cells[0], shop_id, installments, scene):
                matches.append((page_number, cells))
        next_item = page.locator(".ant-pagination-next").filter(visible=True)
        if next_item.count() == 0 or "ant-pagination-disabled" in (next_item.first.get_attribute("class") or ""):
            break
        active = page.locator(".ant-pagination-item-active").inner_text()
        next_item.first.locator("button").click(timeout=8000)
        page.locator(".ant-spin-blur").wait_for(state="hidden", timeout=10000)
        page.wait_for_function(
            "old => document.querySelector('.ant-pagination-item-active')?.innerText !== old",
            arg=active, timeout=10000,
        )
    else:
        raise PriceUpliftError("价格规则列表超过 20 页，拒绝只看部分结果")
    if len(matches) != 1:
        raise PriceUpliftError(f"店铺 {shop_id} 的 {installments} 期价格规则命中 {len(matches)} 条，不能唯一选择")
    target_page, cells = matches[0]
    expected_rate = PRICE_RULE_RATES.get(shop_id, {}).get(int(installments)) if scene == 2 else None
    if (len(cells) < 3 or cells[1] != "启用"
            or (expected_rate and not re.search(
                rf"(?<![\d.]){re.escape(expected_rate)}(?![\d.])", cells[2]))
            or (not expected_rate and not re.search(r"浮动比例\s*[:：]\s*\d+(?:\.\d+)?%", cells[2]))):
        raise PriceUpliftError("规则启用状态或上浮比例无法核实")
    # Return to the page containing the unique ID-and-term match.
    for _ in range(page_number - target_page):
        previous = page.locator(".ant-pagination-prev").filter(visible=True)
        if previous.count() != 1 or "ant-pagination-disabled" in (previous.first.get_attribute("class") or ""):
            raise PriceUpliftError("无法返回目标价格规则所在分页")
        active = page.locator(".ant-pagination-item-active").inner_text()
        previous.first.locator("button").click(timeout=8000)
        page.locator(".ant-spin-blur").wait_for(state="hidden", timeout=10000)
        page.wait_for_function(
            "old => document.querySelector('.ant-pagination-item-active')?.innerText !== old",
            arg=active, timeout=10000,
        )
    page.get_by_text(cells[0], exact=True).wait_for(state="visible", timeout=10000)
    rows = page.locator("table tbody tr").filter(visible=True).filter(has_text=cells[0])
    if rows.count() != 1 or rows.first.locator("td").first.inner_text().strip() != cells[0]:
        raise PriceUpliftError("目标价格规则重新定位后不唯一")
    return rows.first, cells


def probe_rules(profile_dir: str) -> dict:
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(RULES_URL, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("价格上浮", exact=True).first.wait_for(state="visible", timeout=15000)
            page.locator("table tbody tr").filter(visible=True).first.wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(800)
            rows = []
            for page_number in (1, 2):
                current = page.locator("table tbody tr").all_inner_texts()
                rows.extend({"page": page_number, "text": value.replace("\n", " | ")}
                            for value in current if value.strip())
                if page_number == 1:
                    next_button = page.locator(".ant-pagination-next button")
                    if next_button.count() != 1 or next_button.is_disabled():
                        break
                    next_button.click(timeout=8000)
                    page.wait_for_timeout(300)
            return {"url": page.url, "rows": rows}
        finally:
            context.close()


def probe_exact_rule(profile_dir: str, shop_id: str, installments: int,
                     inspect_import: bool = False, scene: int = 2) -> dict:
    """Open the ID-and-term-matched edit page, without uploading a file."""
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(RULES_URL, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("价格上浮", exact=True).first.wait_for(state="visible", timeout=15000)
            page.locator("table tbody tr").filter(visible=True).first.wait_for(state="visible", timeout=15000)
            matched, columns = locate_price_rule(page, shop_id, installments, scene)
            rule_name = columns[0]
            edit = matched.get_by_role("button", name="编辑", exact=True)
            if edit.count() != 1:
                raise PriceUpliftError("规则编辑按钮不唯一")
            with page.expect_popup(timeout=10000) as opened:
                edit.click(timeout=8000)
            detail = opened.value
            detail.wait_for_load_state("domcontentloaded", timeout=15000)
            detail.get_by_role("button", name="导入商品ID").wait_for(state="visible", timeout=15000)
            detail.wait_for_function(
                "expected => [...document.querySelectorAll('input')].some(input => input.value === expected)",
                arg=rule_name, timeout=10000,
            )
            if sum(value == rule_name for value in detail.locator("input").evaluate_all(
                    "inputs => inputs.map(input => input.value)")) != 1:
                raise PriceUpliftError("编辑页策略名称与所选规则不一致")
            if "ruleType=priceIncrease" not in detail.url or "type=edit" not in detail.url:
                raise PriceUpliftError("打开的不是价格上浮编辑页")
            result = {"shop_id": str(shop_id), "installments": int(installments), "scene": scene,
                      "rule_name": rule_name, "rule_url": detail.url,
                      "rate_text": columns[2], "import_button_visible": True,
                      "submitted": False}
            if inspect_import:
                detail.get_by_role("button", name="导入商品ID").click(timeout=8000)
                detail.wait_for_timeout(400)
                dialogs = detail.get_by_role("dialog").filter(visible=True)
                result["import_dialog_count"] = dialogs.count()
                result["dialog_text"] = dialogs.first.inner_text()[:1500] if dialogs.count() else ""
                result["visible_buttons"] = detail.get_by_role("button").filter(visible=True).all_inner_texts()[:30]
                result["file_inputs"] = detail.locator('input[type="file"]').count()
            detail.close()
            return result
        finally:
            context.close()


def probe_rule_membership(profile_dir: str, shop_id: str, installments: int,
                          product_ids: list[str], scene: int = 2) -> dict:
    """Read-only confirmation that a rejected import left no target IDs behind."""
    rule = probe_exact_rule(profile_dir, shop_id, installments, scene=scene)
    from .price_rule_upload import _read_rule_product_ids
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=True,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            detail = context.pages[0] if context.pages else context.new_page()
            detail.goto(rule["rule_url"], wait_until="domcontentloaded", timeout=30000)
            return _read_rule_product_ids(detail, rule["rule_name"], set(product_ids))
        finally:
            context.close()


if __name__ == "__main__":
    import sys
    result = (probe_exact_rule(sys.argv[1], sys.argv[2], int(sys.argv[3]))
              if len(sys.argv) > 3 else probe_rules(sys.argv[1]))
    print(json.dumps(result, ensure_ascii=False, indent=2))
