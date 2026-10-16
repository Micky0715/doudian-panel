"""Read-only confirmation of product-level installment enrollment."""

from __future__ import annotations

import time

from .gateway import DoudianReadOnlyGateway
from .cpay_terms import required_cpay_terms


METHOD = "cpaypromotion.queryProductCpayPromotionAndShopStatus"


def _periods(response: dict) -> tuple[set[int], str]:
    data = response.get("data") or {}
    if not isinstance(data.get("product_discount_and_promotion_activity_response"), dict):
        raise RuntimeError("分期资格接口未返回商品报名信息，不能确认报名状态")
    if (data.get("biz_base_response") or {}).get("success") is False:
        raise RuntimeError("分期资格接口业务回执失败，不能确认报名状态")
    enrollment = ((data.get("product_discount_and_promotion_activity_response") or {})
                  .get("discount_info") or {})
    activity_id = str(enrollment.get("promotion_activity_id") or "")
    config = enrollment.get("period_shared_proportion_config") or {}
    periods = set()
    for item in config.get("period_shared_proportion_list") or []:
        try:
            periods.add(int(item.get("period")))
        except (TypeError, ValueError, AttributeError):
            pass
    return periods, activity_id


def verify_cpay_membership(env_path: str, shop_id: str, installments,
                           product_ids: list[str], *, gateway=None,
                           attempts: int = 3, delay_seconds: float = 2) -> dict:
    client = gateway or DoudianReadOnlyGateway(env_path, shop_id)
    evidence = {}
    for attempt in range(attempts):
        evidence = {}
        for product_id in product_ids:
            result = client.call(METHOD, {"product_id": str(product_id)})
            periods, activity_id = _periods(result)
            evidence[str(product_id)] = {"periods": sorted(periods), "activity_id": activity_id}
        activity_ids = {item["activity_id"] for item in evidence.values()}
        required = set(required_cpay_terms(installments))
        if (len(activity_ids) == 1 and "" not in activity_ids
                and all(set(item["periods"]) == required for item in evidence.values())):
            return {"confirmed": True, "products": evidence}
        if attempt + 1 < attempts:
            time.sleep(delay_seconds)
    return {"confirmed": False, "products": evidence}
