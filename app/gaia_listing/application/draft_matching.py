"""Conservative reconciliation of newly created DouDian drafts.

The list snapshot contains only a title and product ID. It is not enough to
infer a model or condition, so new items require verified detail fields.
"""

from __future__ import annotations

import re
import unicodedata
import hashlib
import json
from collections import Counter


def _key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _integer(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _invalid(expected_rows: list, reason: str) -> dict:
    return {
        "matched": {},
        "unresolved": [
            {"row_id": str(row.get("row_id") or "") if isinstance(row, dict) else "",
             "candidate_count": 0, "reason": reason}
            for row in expected_rows
        ],
        "errors": [reason],
        "new_draft_count": 0,
    }


def match_new_drafts(expected_rows, before_snapshot, after_snapshot, *,
                     shop_id: str, submitted_at: int, details_by_id: dict,
                     receipt_links_by_row: dict | None = None) -> dict:
    """Match only new, same-shop, post-submission, uniquely enriched drafts.

    Both snapshots use ``{shop_id, captured_at, items}``. ``details_by_id``
    supplies verified brand/model/condition from separate product-detail reads.
    ``receipt_links_by_row`` is a verified Gaia receipt mapping source row ID to
    DouDian product ID. Without it, even a unique model match remains unresolved:
    another operator may have created that product during the same time window.
    Title parsing and fuzzy matching are deliberately excluded.
    """
    rows = list(expected_rows)
    if not isinstance(before_snapshot, dict) or not isinstance(after_snapshot, dict):
        return _invalid(rows, "草稿快照格式无效")
    if (not str(shop_id).isdigit()
            or str(before_snapshot.get("shop_id")) != str(shop_id)
            or str(after_snapshot.get("shop_id")) != str(shop_id)):
        return _invalid(rows, "草稿快照店铺与目标店铺不一致")
    if (before_snapshot.get("status_filter") != 0 or after_snapshot.get("status_filter") != 0
            or before_snapshot.get("check_status_filter") != 1
            or after_snapshot.get("check_status_filter") != 1):
        return _invalid(rows, "草稿快照不是完整草稿筛选结果")
    before_at = _integer(before_snapshot.get("captured_at"))
    after_at = _integer(after_snapshot.get("captured_at"))
    submitted_at = _integer(submitted_at)
    if not before_at or not after_at or not submitted_at or not before_at <= submitted_at <= after_at:
        return _invalid(rows, "草稿快照与盖亚提交时间顺序无效")
    before_items = before_snapshot.get("items")
    after_items = after_snapshot.get("items")
    if not isinstance(before_items, list) or not isinstance(after_items, list):
        return _invalid(rows, "草稿快照缺少商品列表")
    for snapshot in (before_snapshot, after_snapshot):
        items = snapshot["items"]
        if snapshot.get("schema_version") != 2 or _integer(snapshot.get("reported_total")) != len(items):
            return _invalid(rows, "草稿快照未完整采集所有分页")
        checksum_source = {key: snapshot.get(key) for key in
                           ("shop_id", "status_filter", "check_status_filter", "reported_total", "items")}
        digest = hashlib.sha256(json.dumps(
            checksum_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        if digest != snapshot.get("checksum"):
            return _invalid(rows, "草稿快照校验和不一致")
    if not isinstance(details_by_id, dict):
        return _invalid(rows, "缺少商品详情回读结果")
    if any(not isinstance(row, dict) for row in rows):
        return _invalid(rows, "来源行结构无效")
    row_ids = [str(row.get("row_id") or "") for row in rows]
    if not rows or any(not value for value in row_ids) or len(row_ids) != len(set(row_ids)):
        return _invalid(rows, "来源行 ID 为空或重复")
    if any(not all(_key(row.get(field)) for field in ("brand", "model", "condition"))
           or str(row.get("shop_id") or shop_id) != str(shop_id) for row in rows):
        return _invalid(rows, "来源行缺少品牌、型号、成色或店铺不一致")

    def item_ids(items):
        return [str(item.get("product_id") or item.get("id") or "") for item in items
                if isinstance(item, dict)]

    before_ids = item_ids(before_items)
    after_ids = item_ids(after_items)
    if (len(before_ids) != len(before_items) or len(after_ids) != len(after_items)
            or any(not value for value in before_ids + after_ids)
            or len(before_ids) != len(set(before_ids))
            or len(after_ids) != len(set(after_ids))):
        return _invalid(rows, "草稿快照商品 ID 为空或重复")

    old_ids = set(before_ids)
    new_items = [item for item in after_items
                 if str(item.get("product_id") or item.get("id")) not in old_ids]
    eligible = []
    for item in new_items:
        created = _integer(item.get("create_time"))
        if created is None or created > after_at:
            return _invalid(rows, "新增草稿创建时间无效")
        if created < submitted_at:
            continue
        if item.get("status") != 0 or str(item.get("check_status")) != "1":
            return _invalid(rows, "新增商品不是草稿状态")
        product_id = str(item.get("product_id") or item.get("id"))
        detail = details_by_id.get(product_id)
        if (not isinstance(detail, dict)
                or not all(_key(detail.get(field)) for field in ("brand", "model", "condition"))
                or str(detail.get("shop_id") or shop_id) != str(shop_id)):
            return _invalid(rows, "新增草稿缺少已核实的同店品牌、型号或成色详情")
        eligible.append((item, detail))

    candidates = {}
    for row in rows:
        row_key = tuple(_key(row[field]) for field in ("brand", "model", "condition"))
        candidates[str(row["row_id"])] = [item for item, detail in eligible
            if tuple(_key(detail[field]) for field in ("brand", "model", "condition")) == row_key]
    unique_claims = [str(items[0].get("product_id") or items[0].get("id"))
                     for row_id, items in candidates.items()
                     if len(items) == 1 and isinstance(receipt_links_by_row, dict)
                     and str(receipt_links_by_row.get(row_id) or "")
                     == str(items[0].get("product_id") or items[0].get("id"))]
    claim_counts = Counter(unique_claims)
    matched, unresolved = {}, []
    for row in rows:
        row_id = str(row["row_id"])
        items = candidates[row_id]
        receipt_id = (str(receipt_links_by_row.get(row_id) or "")
                      if isinstance(receipt_links_by_row, dict) else "")
        if not receipt_id:
            reason = "缺少盖亚回执的逐行商品 ID 关联，需人工确认"
        elif len(items) == 1:
            product_id = str(items[0].get("product_id") or items[0].get("id"))
            if product_id == receipt_id and claim_counts[product_id] == 1:
                matched[row_id] = product_id
                continue
            reason = ("盖亚回执商品 ID 与候选草稿不一致" if product_id != receipt_id
                      else "多行竞争同一新增草稿")
        else:
            reason = "没有唯一新增草稿" if not items else "新增草稿候选不唯一"
        unresolved.append({"row_id": row_id, "candidate_count": len(items), "reason": reason})
    return {"matched": matched, "unresolved": unresolved, "errors": [],
            "new_draft_count": len(eligible)}
