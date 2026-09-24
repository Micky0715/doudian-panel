#!/usr/bin/env python3
"""Read-only reconciliation between the DouDian draft UI and an API snapshot.

This script may navigate pages and click the pagination next button.  It never
opens an editor and contains no product write/save/publish operation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any


DRAFT_URL = "https://fxg.jinritemai.com/ffa/g/draft"
PHONE_CATEGORY_ID = "1000007628"


class UIAuditError(RuntimeError):
    pass


def _latest_api_snapshot(root: Path, shop_id: str) -> Path:
    directory = root / shop_id
    candidates = sorted(
        directory.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True,
    )
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            int(value.get("schema_version") or 0) == 2
            and int(value.get("status_filter", -1)) == 0
            and int(value.get("check_status_filter", -1)) == 1
        ):
            return path
    raise UIAuditError(f"no current-draft API snapshot found for shop {shop_id}")


def _current_shop_ids(page) -> set[str]:
    values = page.evaluate("""
        () => {
            const out = new Set();
            try {
                for (let i = 0; i < localStorage.length; i++) {
                    const key = localStorage.key(i) || '';
                    if (!key.startsWith('__tea_cache_tokens_')) continue;
                    try {
                        const value = JSON.parse(localStorage.getItem(key) || '{}');
                        const match = String(value.user_unique_id || '').match(/(?:^|_)(\\d{5,12})$/);
                        if (match) out.add(match[1]);
                    } catch (_) {}
                }
            } catch (_) {}
            return [...out];
        }
    """)
    return {str(value) for value in (values or []) if str(value).isdigit()}


def _read_page(page) -> dict[str, Any]:
    return page.evaluate("""
        () => {
            const rowNodes = [...document.querySelectorAll('tr.ecom-g-table-row')];
            const rows = [];
            for (const row of rowNodes) {
                const text = row.innerText || '';
                const match = text.match(/ID[:：]?\\s*(\\d{10,22})/);
                if (!match) continue;
                const nameNode = row.querySelector('.style_productName__Q9d1M');
                let name = nameNode ? (nameNode.innerText || '').trim() : '';
                if (!name) {
                    const lines = text.split(/\\n+/).map(v => v.trim()).filter(Boolean);
                    name = lines.find(v => !/^ID[:：]/.test(v) && !/^待提交$/.test(v)) || '';
                }
                rows.push({product_id: match[1], name});
            }

            const pagination = document.querySelector('.ecom-g-pagination');
            const totalNode = pagination && pagination.querySelector('.ecom-g-pagination-total-text');
            const activeNode = pagination && pagination.querySelector('.ecom-g-pagination-item-active');
            const nextNode = pagination && pagination.querySelector('.ecom-g-pagination-next');
            const totalText = totalNode ? (totalNode.innerText || '').trim() : '';
            const totalMatch = totalText.match(/(\\d+)/);
            return {
                rows,
                pagination_found: Boolean(pagination),
                total_text: totalText,
                reported_total: totalMatch ? Number(totalMatch[1]) : null,
                current_page: activeNode ? Number((activeNode.innerText || '').trim()) : null,
                next_disabled: !nextNode || nextNode.classList.contains('ecom-g-pagination-disabled') ||
                    nextNode.getAttribute('aria-disabled') === 'true'
            };
        }
    """)


def _click_next_and_wait(page, previous_ids: tuple[str, ...]) -> None:
    clicked = page.evaluate("""
        () => {
            const pagination = document.querySelector('.ecom-g-pagination');
            const nextNode = pagination && pagination.querySelector('.ecom-g-pagination-next');
            if (!nextNode || nextNode.classList.contains('ecom-g-pagination-disabled') ||
                    nextNode.getAttribute('aria-disabled') === 'true') return false;
            const target = nextNode.querySelector('button') || nextNode;
            target.click();
            return true;
        }
    """)
    if not clicked:
        raise UIAuditError("draft next-page button could not be clicked")
    deadline = time.time() + 20
    while time.time() < deadline:
        page.wait_for_timeout(500)
        state = _read_page(page)
        current = tuple(row["product_id"] for row in state["rows"])
        if current and current != previous_ids:
            return
    raise UIAuditError("draft page did not refresh after next-page click")


def collect_ui_drafts(page, *, max_pages: int = 200) -> dict[str, Any]:
    collected: list[dict[str, str]] = []
    seen: set[str] = set()
    signatures: set[tuple[str, ...]] = set()
    reported_total: int | None = None
    total_text = ""
    page_count = 0

    for page_number in range(1, max_pages + 1):
        state = _read_page(page)
        rows = state["rows"]
        signature = tuple(row["product_id"] for row in rows)
        if not state["pagination_found"]:
            raise UIAuditError("draft pagination component was not found")
        if state["reported_total"] is None:
            raise UIAuditError(f"draft total could not be parsed: {state['total_text']!r}")
        if reported_total is None:
            reported_total = int(state["reported_total"])
            total_text = str(state["total_text"])
        elif int(state["reported_total"]) != reported_total:
            raise UIAuditError(
                f"UI draft total changed during scan: {reported_total} -> {state['reported_total']}"
            )
        if not rows and reported_total:
            raise UIAuditError(f"UI draft page {page_number} is empty before total")
        if signature in signatures:
            raise UIAuditError(f"UI draft page repeated at page {page_number}")
        signatures.add(signature)
        for row in rows:
            product_id = str(row["product_id"])
            if product_id in seen:
                raise UIAuditError(f"duplicate UI draft id: {product_id}")
            seen.add(product_id)
            collected.append({"product_id": product_id, "name": str(row.get("name") or "")})
        page_count = page_number
        if len(collected) == reported_total:
            break
        if len(collected) > reported_total:
            raise UIAuditError(
                f"UI collected more drafts than total: {len(collected)} > {reported_total}"
            )
        if state["next_disabled"]:
            raise UIAuditError(
                f"UI next page disabled at {len(collected)}/{reported_total} drafts"
            )
        _click_next_and_wait(page, signature)
    else:
        raise UIAuditError(f"UI draft scan exceeded max_pages={max_pages}")

    return {
        "reported_total": int(reported_total or 0),
        "page_count": page_count,
        "total_text": total_text,
        "items": collected,
    }


def reconcile(api_data: dict[str, Any], ui_data: dict[str, Any]) -> dict[str, Any]:
    api_items = api_data.get("items") or []
    ui_items = ui_data.get("items") or []
    api_all = {str(row["product_id"]) for row in api_items}
    api_phone = {
        str(row["product_id"])
        for row in api_items
        if str(row.get("category_leaf_id") or "") == PHONE_CATEGORY_ID
    }
    ui_ids = {str(row["product_id"]) for row in ui_items}
    if ui_ids == api_all:
        match_scope = "all_categories"
        expected = api_all
    elif ui_ids == api_phone:
        match_scope = "phone_category"
        expected = api_phone
    else:
        match_scope = "mismatch"
        expected = api_phone if len(ui_ids.symmetric_difference(api_phone)) <= len(
            ui_ids.symmetric_difference(api_all)
        ) else api_all
    return {
        "match_scope": match_scope,
        "api_all_count": len(api_all),
        "api_phone_count": len(api_phone),
        "ui_count": len(ui_ids),
        "api_only_count": len(expected - ui_ids),
        "ui_only_count": len(ui_ids - expected),
        "api_only_sample": sorted(expected - ui_ids)[:20],
        "ui_only_sample": sorted(ui_ids - expected)[:20],
    }


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    temp.write_text(content, encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only DouDian UI draft audit")
    parser.add_argument("shop_id")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--api-root", default="panel_data/doudian_draft_snapshots")
    parser.add_argument("--output-root", default="panel_data/doudian_ui_draft_audits")
    parser.add_argument("--max-pages", type=int, default=200)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    shop_id = str(args.shop_id).strip()
    if not shop_id.isdigit():
        raise UIAuditError("shop id must be numeric")

    api_path = _latest_api_snapshot(Path(args.api_root), shop_id)
    api_data = json.loads(api_path.read_text(encoding="utf-8"))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile).resolve()),
            headless=bool(args.headless),
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(DRAFT_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            if "login" in page.url.lower() or "passport" in page.url.lower():
                raise UIAuditError("profile is not logged in to the DouDian draft page")
            current_ids = _current_shop_ids(page)
            if current_ids != {shop_id}:
                raise UIAuditError(
                    f"profile shop identity mismatch: expected {shop_id}, observed {sorted(current_ids)}"
                )
            ui_data = collect_ui_drafts(page, max_pages=args.max_pages)
        finally:
            context.close()

    result = {
        "schema_version": 1,
        "shop_id": shop_id,
        "captured_at": int(time.time()),
        "api_snapshot": str(api_path.resolve()),
        "ui": ui_data,
        "reconciliation": reconcile(api_data, ui_data),
    }
    output = Path(args.output_root) / shop_id / f"{result['captured_at']}.json"
    _atomic_write(output, result)
    comparison = result["reconciliation"]
    label = "OK" if comparison["match_scope"] != "mismatch" else "MISMATCH"
    print(
        f"UI_DRAFT_AUDIT_{label} shop={shop_id} ui={comparison['ui_count']} "
        f"api_all={comparison['api_all_count']} api_phone={comparison['api_phone_count']} "
        f"scope={comparison['match_scope']} pages={ui_data['page_count']} file={output.resolve()}"
    )
    return 0 if label == "OK" else 2


if __name__ == "__main__":
    raise SystemExit(main())
