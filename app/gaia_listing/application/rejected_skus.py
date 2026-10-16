"""Learn only explicit SKU or SKU/quality Gaia rejections from prior batches.

This is a pre-submit exclusion for *new* batches. It never retries or changes
an already submitted batch, whose partial platform effects may be unknown.
"""

import json
import re
from pathlib import Path

from openpyxl import load_workbook


_UNKNOWN_CATEGORY = re.compile(
    r"标品ID\[(\d+)\].*根据标品skuId未查询到类目信息"
)
_MISSING_GOODS = re.compile(
    r"标品ID\[(\d+)\]\s*和\s*抖音成色ID\[(\d+)\]"
    r"[^\n]{0,40}?(?:(?:未找到|没有)对应的?\s*goods\s*id|goods\s*id\s*不存在)",
    re.IGNORECASE,
)


def missing_goods_pair(feedback):
    """Only one explicit SKU/quality rejection is safe to isolate."""
    matches = set(_MISSING_GOODS.findall(str(feedback or "")))
    return next(iter(matches)) if len(matches) == 1 else None


def rejected_sku_evidence(batches_dir, mapping_version_id, quality_ids=()):
    """Return SKU-wide category rejections; grade-specific failures stay pairs."""
    evidence = {}
    if not mapping_version_id:
        return evidence
    for path in Path(batches_dir).glob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                batch = json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        if (batch.get("mapping_version_id") != mapping_version_id
                or not (batch.get("gaia_receipt") or {}).get("submit_intent")):
            continue
        receipt = batch.get("gaia_receipt") or {}
        if batch.get("state") == "gaia_rejected":
            feedback = (receipt.get("result") or {}).get("feedback") or receipt.get("message") or ""
            for sku_id in _UNKNOWN_CATEGORY.findall(str(feedback)):
                evidence[sku_id] = str(batch.get("id") or path.stem)
    return evidence


def rejected_variant_evidence(batches_dir, mapping_version_id):
    """Return only explicitly rejected (SKU, dyQualityId) pairs."""
    evidence = {}
    if not mapping_version_id:
        return evidence
    for path in Path(batches_dir).glob("*.json"):
        try:
            with path.open(encoding="utf-8") as handle:
                batch = json.load(handle)
        except (OSError, ValueError, TypeError):
            continue
        if (batch.get("mapping_version_id") != mapping_version_id
                or not (batch.get("gaia_receipt") or {}).get("submit_intent")):
            continue
        receipt = batch["gaia_receipt"]
        if batch.get("state") == "gaia_rejected":
            feedback = (receipt.get("result") or {}).get("feedback") or receipt.get("message") or ""
            for sku_id, quality_id in _MISSING_GOODS.findall(str(feedback)):
                evidence[(sku_id, quality_id)] = str(batch.get("id") or path.stem)
        for item in receipt.get("retry_history") or []:
            if item.get("zero_new_products_verified") is True:
                evidence[(str(item["sku_id"]), str(item["quality_id"]))] = str(
                    item.get("batch_id") or path.stem)
    return evidence


def rejected_skus_in_workbook(path, evidence):
    """Read-only check that a previously generated file has no quarantined SKU."""
    if not evidence:
        return []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.active
        return sorted({str(value).strip() for (value,) in sheet.iter_rows(
            min_row=2, min_col=4, max_col=4, values_only=True)
            if value is not None and str(value).strip() in evidence})
    finally:
        workbook.close()


def rejected_variants_in_workbook(path, evidence):
    """Find known-bad SKU/quality pairs in an already generated workbook."""
    if not evidence:
        return []
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.active
        return sorted({(str(sku).strip(), str(quality).strip())
                       for sku, quality in sheet.iter_rows(
                           min_row=2, min_col=4, max_col=5, values_only=True)
                       if sku is not None and quality is not None
                       and (str(sku).strip(), str(quality).strip()) in evidence})
    finally:
        workbook.close()
