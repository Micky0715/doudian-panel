"""One scoped Gaia channel-list read-only probe; never opens the upload form."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from integrations.gaia_browser.channel_reconcile import (
    _matching_search_response, _select_exact_shop_filter, snapshot_channel_products,
)
from integrations.gaia_browser.probe import GAIA_CHANNEL_URL, _has_text, _visible_unique
from integrations.gaia_browser.upload_form import GAIA_SHOP_NAMES


def main():
    shop_id, model, brand = sys.argv[1:4]
    if shop_id not in GAIA_SHOP_NAMES:
        raise ValueError("unknown scoped shop")
    profile = Path(__file__).resolve().parents[1] / "gaia_browser_data"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            print("Gaia login window opened; waiting for channel page", flush=True)
            deadline = time.monotonic() + 300
            while not (_has_text(page, "抖音渠道") or _has_text(page, "双维成色发品")):
                if time.monotonic() > deadline:
                    raise RuntimeError("login wait expired; no query sent")
                page.wait_for_timeout(1000)
            if _has_text(page, "抖音渠道"):
                _visible_unique(page, "抖音渠道", roles=("tab", "button", "link")).click(timeout=8000)
                page.get_by_text("双维成色发品", exact=True).wait_for(state="visible", timeout=20000)
            _select_exact_shop_filter(page, shop_id, GAIA_SHOP_NAMES[shop_id])
            page.locator("#advanced_search_channelSpuName").fill(model)
            search = page.get_by_role("button", name=re.compile(r"搜\s*索")).filter(visible=True)
            if search.count() != 1:
                raise RuntimeError("search button is not unique")
            with page.expect_response(lambda r: _matching_search_response(r, shop_id, model), timeout=25000) as received:
                search.click(timeout=8000)
            response = received.value
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            safe = {"http_status": response.status, "business_code": payload.get("code"),
                    "data_keys": sorted(data) if isinstance(data, dict) else None,
                    "total_count": data.get("totalCount") if isinstance(data, dict) else None,
                    "data_types": {k: type(v).__name__ for k, v in data.items()} if isinstance(data, dict) else None,
                    "array_item_keys": {k: sorted(v[0]) for k, v in data.items()
                                        if isinstance(v, list) and v and isinstance(v[0], dict)} if isinstance(data, dict) else None}
            page_items = data.get("pageList") if isinstance(data, dict) else None
            if isinstance(page_items, list) and page_items:
                sku_items = page_items[0].get("productSkuList")
                safe["sku_item_keys"] = (sorted(sku_items[0]) if isinstance(sku_items, list)
                                         and sku_items and isinstance(sku_items[0], dict) else None)
            print("api_shape " + json.dumps(safe, ensure_ascii=False), flush=True)
            for ms in (0, 1000, 5000, 12000):
                page.wait_for_timeout(ms)
                totals = page.locator(".ant-pagination-total-text").filter(visible=True).all_inner_texts()
                print("ui_total " + json.dumps({"after_ms": ms, "totals": totals[:2]}, ensure_ascii=False), flush=True)
            snapshot = snapshot_channel_products(
                page, shop_id, [{"brand": brand, "model": model}])
            items = next(iter(snapshot.values()))["items"]
            print("production_snapshot_items " + str(len(items)), flush=True)
            if "--items" in sys.argv[4:]:
                print("scoped_items " + json.dumps([
                    {key: item.get(key) for key in
                     ("product_id", "title", "created_at", "scene", "status", "strategy", "quality_dimension")}
                    for item in items
                ], ensure_ascii=False), flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()
