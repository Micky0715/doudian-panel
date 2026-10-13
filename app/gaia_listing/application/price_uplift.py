"""Strict handoff from a reconciled Gaia batch to a price-uplift import."""

from __future__ import annotations

import re


class PriceUpliftError(ValueError):
    pass


# Previously observed names are only an allowlist and display reference.
# The live rule is selected by its embedded shop ID and installment term,
# because operators may rename the human-readable shop portion.
PRICE_RULE_NAMES = {
    "129675602": {12: "爱回收官方旗舰店【129675602】-12期", 24: "爱回收官方旗舰店【129675602】-24期"},
    "25565842": {12: "爱回收严选二手手机（小马哥）【25565842】-12期", 24: "爱回收严选二手手机（小马哥）【25565842】-24期"},
    "636781": {12: "爱回收二手数码旗舰店【636781】—12期", 24: "爱回收二手数码旗舰店【636781】—24期"},
    "7970406": {12: "爱回收严选二手数码【7970406】-12期", 24: "爱回收严选二手数码【7970406】-24期"},
    "51165770": {12: "爱回收严选二手优品【51165770】-12期", 24: "爱回收严选二手优品【51165770】-24期"},
    "168976032": {12: "拍拍严选手机优品【168976032】-12期", 24: "拍拍严选手机优品【168976032】-24期"},
    "5576543": {12: "拍拍严选二手手机【5576543】—12期", 24: "拍拍严选二手手机【5576543】—24期"},
    "156450040": {12: "拍拍二手数码（深圳）【156450040】-12期", 24: "拍拍二手数码（深圳）【156450040】-24期"},
    "25595926": {12: "拍拍二手悦玥手机专卖店【25595926】—12期", 24: "拍拍二手悦玥手机专卖店【25595926】—24期"},
    "208501840": {12: "返航新生二手手机旗舰店【208501840】-12期", 24: "返航新生二手手机旗舰店【208501840】-24期"},
}


def rule_name_matches_shop_term(name: str, shop_id: str, installments: int) -> bool:
    """Require one bracketed shop ID and term, excluding livestream-room rules."""
    if str(installments) not in {"12", "24"}:
        return False
    text = str(name or "").strip()
    ids = re.findall(r"【\s*([0-9]+)\s*】", text)
    term = re.search(r"(?:】|[-—–－])\s*(12|24)\s*期\s*$", text)
    return ("直播间" not in text and len(ids) == 1 and ids[0] == str(shop_id)
            and bool(term) and term.group(1) == str(installments))

# Exact enabled rates read back from the current Gaia rule list.
PRICE_RULE_RATES = {
    shop_id: {12: "4.3%", 24: "9%"}
    for shop_id in PRICE_RULE_NAMES
}


def prepare_price_uplift(batch: dict, *, shop_id: str, installments: int) -> dict:
    """Return only DouDian product IDs proven by the batch reconciliation.

    Gaia upload acceptance alone does not prove that any product was created.
    Standard SKU IDs from the price-export mapping must never enter this list.
    """
    if str(shop_id) != str(batch.get("shop_id")):
        raise PriceUpliftError("盖亚批次与价格规则目标店铺不一致")
    if str(installments) not in {"12", "24"}:
        raise PriceUpliftError("分期数只能选择 12 期或 24 期")
    if batch.get("scene") != 2:
        raise PriceUpliftError("价格上浮规则仅关联直播货架场域的盖亚批次")
    rule_name = PRICE_RULE_NAMES.get(str(shop_id), {}).get(int(installments))
    if not rule_name:
        raise PriceUpliftError("该店铺尚无经核实的 12/24 期价格上浮规则对应关系")
    if batch.get("state") not in {"doudian_drafts_matched", "handed_off"}:
        raise PriceUpliftError("盖亚受理不等于商品创建成功；请先完成逐行抖店商品 ID 关联")
    ids = batch.get("doudian_draft_ids")
    if not isinstance(ids, list) or not ids:
        raise PriceUpliftError("批次没有已核实的抖店商品 ID")
    if any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{9,19}", value) for value in ids):
        raise PriceUpliftError("抖店商品 ID 格式无效，拒绝导入")
    if len(ids) != len(set(ids)):
        raise PriceUpliftError("抖店商品 ID 重复，拒绝导入")
    expected = batch.get("summary", {}).get("expected_product_count")
    if not isinstance(expected, int) or expected != len(ids):
        raise PriceUpliftError("已核实商品 ID 数量与本批预计商品数不一致")
    return {
        "batch_id": batch["id"],
        "shop_id": str(shop_id),
        "installments": int(installments),
        "rule_name": f"店铺 ID {shop_id} · {installments} 期（实际规则名以盖亚页面为准）",
        "product_ids": ids,
        "count": len(ids),
    }
