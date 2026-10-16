"""Read-only audit of locally recorded Gaia zero-match channel readbacks.

Only queries the shop/model pairs present in existing zero-match batch records.
Never opens an upload dialog, submits a file, or changes a batch.
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from integrations.gaia_browser.channel_reconcile import (
    _model_search_queries, _search_channel_model, _select_exact_shop_filter,
    _title_matches,
)
from integrations.gaia_browser.probe import GAIA_CHANNEL_URL, _has_text, _visible_unique
from integrations.gaia_browser.upload_form import GAIA_SHOP_NAMES


def _cases() -> dict[tuple[str, str, str], list[dict]]:
    grouped = {}
    for path in (ROOT / "panel_data/gaia_listing/batches").glob("*.json"):
        with path.open(encoding="utf-8") as source:
            batch = json.load(source)
        error = ((batch.get("gaia_receipt") or {}).get("result") or {}).get("product_reconcile_error")
        if not isinstance(error, str) or "（0 个）" not in error:
            continue
        products = [product for product in ((batch.get("summary") or {}).get("expected_products") or [])
                    if error.startswith(f"{product.get('brand')} {product.get('model')} ")]
        if len(products) != 1:
            continue
        shop_id = str(batch.get("shop_id") or "")
        if shop_id not in GAIA_SHOP_NAMES:
            continue
        product = products[0]
        key = (shop_id, str(product.get("brand") or ""), str(product.get("model") or ""))
        if not all(key):
            continue
        submitted = next((event["at"] for event in (batch.get("events") or [])
                          if event.get("to") == "gaia_submitted"), None)
        grouped.setdefault(key, []).append({"batch_id": batch["id"], "submitted_at": submitted})
    return grouped


def _open_shop(page, shop_id: str) -> None:
    page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
    deadline = time.monotonic() + 30
    while not (_has_text(page, "抖音渠道") or _has_text(page, "双维成色发品")):
        if time.monotonic() >= deadline:
            raise RuntimeError("盖亚登录失效，未查询任何该店铺商品")
        page.wait_for_timeout(500)
    if _has_text(page, "抖音渠道"):
        _visible_unique(page, "抖音渠道", roles=("tab", "button", "link")).click(timeout=8000)
        page.get_by_text("双维成色发品", exact=True).wait_for(state="visible", timeout=20000)
    _select_exact_shop_filter(page, shop_id, GAIA_SHOP_NAMES[shop_id])


def _temporal_matches(items: list[dict], batches: list[dict]) -> list[dict]:
    results = []
    for batch in batches:
        submitted = batch["submitted_at"]
        if not submitted:
            results.append({**batch, "candidate_ids": [], "time_basis": "missing"})
            continue
        start = datetime.fromisoformat(submitted).replace(tzinfo=None)
        candidates = []
        for item in items:
            created = datetime.strptime(item["created_at"], "%Y-%m-%d %H:%M:%S")
            if start - timedelta(seconds=30) <= created <= start + timedelta(minutes=10):
                candidates.append(item["product_id"])
        results.append({**batch, "candidate_ids": candidates,
                        "time_basis": "submitted_minus_30s_to_plus_10min_not_proof"})
    return results


def _diagnostic_queries(model: str) -> list[str]:
    """Looser phrases are for diagnosis only, never automatic association."""
    compact = re.sub(r"\s+", "", model)
    without_suffix = re.sub(r"\s*[（(][^)）]*[)）]", "", model).strip()
    tokens = re.findall(r"[A-Za-z]+\d+[A-Za-z\d+]*|\d+[A-Za-z]+|[A-Za-z]+", model)
    distinctive = max(tokens, key=len) if tokens else ""
    return [value for value in dict.fromkeys((compact, without_suffix, distinctive))
            if value and value not in _model_search_queries(model)]


def main() -> None:
    cases = _cases()
    if not cases:
        print(json.dumps({"cases": 0}, ensure_ascii=False), flush=True)
        return
    profile = ROOT / "gaia_browser_data"
    print(json.dumps({"shop_model_pairs": len(cases),
                      "batches": sum(len(value) for value in cases.values()),
                      "query_type": "read_only"}, ensure_ascii=False), flush=True)
    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=False,
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as exc:
            print(json.dumps({
                "error": "盖亚浏览器资料正在被其他会话使用或无法启动；未接管会话，未做线上查询",
                "technical_type": type(exc).__name__,
            }, ensure_ascii=False), flush=True)
            return
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for shop_id in sorted({key[0] for key in cases}):
                try:
                    _open_shop(page, shop_id)
                except Exception as exc:
                    print(json.dumps({"shop_id": shop_id, "error": str(exc).splitlines()[0][:250]},
                                     ensure_ascii=False), flush=True)
                    continue
                name_input = page.locator("#advanced_search_channelSpuName")
                search = page.get_by_role("button", name=re.compile(r"搜\s*索")).filter(visible=True)
                if name_input.count() != 1 or search.count() != 1:
                    print(json.dumps({"shop_id": shop_id, "error": "搜索控件不唯一"},
                                     ensure_ascii=False), flush=True)
                    continue
                for (case_shop, brand, model), batches in sorted(cases.items()):
                    if case_shop != shop_id:
                        continue
                    try:
                        queries = []
                        merged = {}
                        for query in _model_search_queries(model):
                            rows = _search_channel_model(page, shop_id, name_input, search, query)
                            queries.append({"query": query, "count": len(rows)})
                            for row in rows:
                                merged[row["product_id"]] = row
                        exact = [row for row in merged.values()
                                 if _title_matches(row["title"], brand, model)]
                        diagnostic = []
                        if not exact:
                            for query in _diagnostic_queries(model):
                                rows = _search_channel_model(page, shop_id, name_input, search, query)
                                near = {candidate for observation in _temporal_matches(rows, batches)
                                        for candidate in observation["candidate_ids"]}
                                diagnostic.append({
                                    "query": query, "count": len(rows),
                                    "near_submission_items": [
                                        {field: row[field] for field in
                                         ("product_id", "title", "created_at", "status", "scene")}
                                        for row in rows if row["product_id"] in near
                                    ],
                                    "title_samples": [row["title"] for row in rows[:5]],
                                })
                        near_ids = {candidate for observation in _temporal_matches(
                            list(merged.values()), batches)
                            for candidate in observation["candidate_ids"]}
                        print(json.dumps({
                            "shop_id": shop_id, "brand": brand, "model": model,
                            "queries": queries,
                            "diagnostic_queries_not_used_for_association": diagnostic,
                            "exact_title_items": [
                                {field: row[field] for field in
                                 ("product_id", "title", "created_at", "status", "scene")}
                                for row in exact
                            ],
                            "near_submission_items_before_title_check": [
                                {field: row[field] for field in
                                 ("product_id", "title", "created_at", "status", "scene")}
                                for row in merged.values() if row["product_id"] in near_ids
                            ],
                            "batch_time_candidates": _temporal_matches(exact, batches),
                        }, ensure_ascii=False), flush=True)
                    except Exception as exc:
                        print(json.dumps({"shop_id": shop_id, "model": model,
                                          "error": str(exc).splitlines()[0][:250]},
                                         ensure_ascii=False), flush=True)
        finally:
            context.close()


if __name__ == "__main__":
    main()
