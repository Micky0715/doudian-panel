"""Submit one previously verified full-media draft; never retry an API write.

This is deliberately locked to the reviewed 156450040 / REDMI Turbo 4 canary.
The preceding full-media report and its post-write snapshot must match a fresh
product.detail read before product.editV2(commit=true) can be called.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.write_gateway import DoudianSingleProductPublisher, submission_payload_sha256
from scripts.doudian_apply_local_images import _pic_urls, detail_urls
from scripts.doudian_submit_verified_drafts import _atomic_json, _payload


SHOP_ID = "156450040"
PRODUCT_ID = "3842662503586857408"
SOURCE_REPORT = Path(
    "panel_data/doudian_full_media_canary/156450040/"
    "1789390433_3842662503586857408.report.json"
)
BRAND_ID = "663234468"  # Live, manually verified Redmi/红米 standard brand.


def _id(detail: Mapping[str, Any]) -> str:
    return str(detail.get("product_id_str") or detail.get("product_id") or "").strip()


def _props(detail: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_product_format_for_edit(parse_product_format(detail.get("product_format_new")))


def _sku_drift(saved: Mapping[str, Any], live: Mapping[str, Any]) -> dict[str, Any]:
    """Explain SKU changes without printing 360 full price/stock records."""
    old_rows = saved.get("spec_prices") or []
    new_rows = live.get("spec_prices") or []
    old = {str(row.get("sku_id")): row for row in old_rows if isinstance(row, Mapping)}
    new = {str(row.get("sku_id")): row for row in new_rows if isinstance(row, Mapping)}
    changed = {
        sku_id: sorted(key for key in set(old[sku_id]) | set(new[sku_id])
                       if old[sku_id].get(key) != new[sku_id].get(key))
        for sku_id in old.keys() & new.keys() if old[sku_id] != new[sku_id]
    }
    field_counts: dict[str, int] = {}
    for fields in changed.values():
        for field in fields:
            field_counts[field] = field_counts.get(field, 0) + 1
    return {
        "saved_count": len(old_rows), "live_count": len(new_rows),
        "same_sku_set": old.keys() == new.keys(),
        "order_changed": list(old) != list(new),
        "changed_sku_count": len(changed),
        "changed_field_counts": field_counts,
        "sample_changed_sku_ids": list(changed)[:5],
        "stock_changes": [
            {"sku_id": sku_id, "before": old[sku_id].get("stock_num"),
             "current": new[sku_id].get("stock_num")}
            for sku_id, fields in list(changed.items())[:5] if fields == ["stock_num"]
        ],
    }


def _allowed_stock_increment(drift: Mapping[str, Any]) -> bool:
    """Allow only the observed 0→1 stock sync; never accept price/SKU changes."""
    return (
        drift.get("saved_count") == 360 and drift.get("live_count") == 360
        and drift.get("same_sku_set") is True and drift.get("order_changed") is False
        and drift.get("changed_sku_count") == 1
        and drift.get("changed_field_counts") == {"stock_num": 1}
        and len(drift.get("stock_changes") or []) == 1
        and drift["stock_changes"][0].get("before") == 0
        and drift["stock_changes"][0].get("current") == 1
    )


def _snapshot_issues(report: Mapping[str, Any], saved: Mapping[str, Any],
                     live: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    if (str(report.get("shop_id")) != SHOP_ID or str(report.get("product_id")) != PRODUCT_ID
            or report.get("result") != "verified_draft" or report.get("commit") is not False
            or report.get("publish") is not False or report.get("verification_errors") != []):
        issues.append("source_report_not_verified_for_locked_product")
    if _id(saved) != PRODUCT_ID or _id(live) != PRODUCT_ID:
        issues.append("product_id_mismatch")
    if (live.get("status"), live.get("check_status"), live.get("draft_status")) != (0, 1, 1):
        issues.append("product_not_editable_draft")
    if str(live.get("standard_brand_id")) != BRAND_ID:
        issues.append("standard_brand_not_redmi")
    if "REDMI Turbo 4" not in str(live.get("name") or ""):
        issues.append("title_model_changed")
    for field in (
        "name", "name_prefix", "name_suffix", "standard_brand_id", "status",
        "check_status", "draft_status", "pic", "description", "spec_pics",
        "specs", "freight_id", "product_type", "spec_id",
    ):
        if live.get(field) != saved.get(field):
            issues.append(field + "_changed_since_media_verification")
    if _props(live) != _props(saved):
        issues.append("properties_changed_since_media_verification")
    if saved.get("spec_prices") != live.get("spec_prices"):
        drift = _sku_drift(saved, live)
        if not _allowed_stock_increment(drift):
            issues.append("spec_prices_changed_since_media_verification")
    models = _props(live).get("1493") or []
    if (len(models) != 1 or not isinstance(models[0], Mapping)
            or str(models[0].get("value")) != "488936"
            or models[0].get("name") != "REDMI Turbo 4"
            or models[0].get("diy_type") != 0):
        issues.append("official_model_missing")
    if len(_pic_urls(live.get("pic"))) != 4:
        issues.append("expected_one_main_plus_three_white")
    if len(detail_urls(live.get("description"))) != 16:
        issues.append("expected_sixteen_detail_images")
    if len(live.get("spec_pics") or []) != 3:
        issues.append("expected_three_color_images")
    if len(live.get("spec_prices") or []) != 360:
        issues.append("sku_count_changed")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Locked one-product full-media API submission")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output-root", default="panel_data/doudian_full_chain_canary")
    args = parser.parse_args()

    source_report = json.loads(SOURCE_REPORT.read_text(encoding="utf-8"))
    saved_path = SOURCE_REPORT.with_name(SOURCE_REPORT.name.replace(".report.json", ".after.json"))
    saved = json.loads(saved_path.read_text(encoding="utf-8"))["detail"]
    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, SHOP_ID))
    live = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
    issues = _snapshot_issues(source_report, saved, live)
    stamp = int(time.time())
    plan_path = Path(args.output_root) / SHOP_ID / f"{stamp}_{PRODUCT_ID}.json"
    plan: dict[str, Any] = {
        "shop_id": SHOP_ID, "product_id": PRODUCT_ID, "created_at": stamp,
        "source_report": str(SOURCE_REPORT.resolve()),
        "source_snapshot": str(saved_path.resolve()),
        "mode": "execute" if args.execute else "dry_run",
        "preflight_issues": issues,
        "before": {
            "status": live.get("status"), "check_status": live.get("check_status"),
            "draft_status": live.get("draft_status"), "standard_brand_id": live.get("standard_brand_id"),
            "main_count": len(_pic_urls(live.get("pic"))),
            "detail_count": len(detail_urls(live.get("description"))),
            "color_count": len(live.get("spec_pics") or []),
            "sku_count": len(live.get("spec_prices") or []),
        },
        "result": "blocked" if issues else "ready",
    }
    if saved.get("spec_prices") != live.get("spec_prices"):
        plan["sku_drift"] = _sku_drift(saved, live)
    _atomic_json(plan_path, plan)
    if issues or not args.execute:
        print(json.dumps({"result": plan["result"], "issues": issues,
                          "plan": str(plan_path)}, ensure_ascii=False))
        return 2 if issues else 0

    # Re-read at the write boundary.  If a person edited the draft after the
    # first preflight, no submission occurs and no browser fallback is tried.
    latest = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
    issues = _snapshot_issues(source_report, saved, latest)
    if latest.get("spec_prices") != live.get("spec_prices"):
        issues.append("sku_stock_changed_during_preflight")
    if issues:
        plan.update({"result": "blocked_before_write", "pre_submit_issues": issues})
        _atomic_json(plan_path, plan)
        print(json.dumps({"result": plan["result"], "issues": issues,
                          "plan": str(plan_path)}, ensure_ascii=False))
        return 2

    payload = _payload(latest)
    digest = submission_payload_sha256(payload)
    plan.update({"result": "submit_pending", "payload_sha256": digest})
    _atomic_json(plan_path, plan)
    try:
        publisher = DoudianSingleProductPublisher(args.env, SHOP_ID, PRODUCT_ID, digest)
        response = publisher.submit_planned_draft(payload)
        plan.update({"result": "api_accepted", "api_code": response.get("code")})
    except Exception as exc:
        # Unknown write outcomes must never be retried automatically.
        plan.update({"result": "submit_unknown_or_rejected", "error": str(exc)[:300]})
        _atomic_json(plan_path, plan)
        print(json.dumps({"result": plan["result"], "plan": str(plan_path)}, ensure_ascii=False))
        return 2
    _atomic_json(plan_path, plan)

    for attempt in range(1, 5):
        try:
            after = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
            plan["after"] = {
                "status": after.get("status"), "check_status": after.get("check_status"),
                "draft_status": after.get("draft_status"),
                "main_count": len(_pic_urls(after.get("pic"))),
                "detail_count": len(detail_urls(after.get("description"))),
                "color_count": len(after.get("spec_pics") or []),
                "sku_count": len(after.get("spec_prices") or []),
                "standard_brand_id": after.get("standard_brand_id"),
            }
            # product.listV2/product.detail use status=0 for online and
            # check_status=3 for audit passed.
            if after.get("status") == 0 and after.get("check_status") == 3:
                break
        except Exception as exc:
            plan["readback_error"] = str(exc)[:300]
        if attempt < 4:
            time.sleep(2)
    post_issues = [
        field + "_changed_after_submit"
        for field in ("pic", "description", "spec_pics", "specs", "spec_prices",
                      "standard_brand_id", "freight_id")
        if "after" in locals() and after.get(field) != latest.get(field)
    ]
    if "after" in locals() and _props(after) != _props(latest):
        post_issues.append("properties_changed_after_submit")
    plan["post_submit_issues"] = post_issues
    plan["result"] = (
        "published_readback" if plan.get("after", {}).get("status") == 0
        and plan.get("after", {}).get("check_status") == 3 and not post_issues
        else "api_accepted_pending_or_needs_review"
    )
    _atomic_json(plan_path, plan)
    print(json.dumps({"result": plan["result"], "api_code": plan.get("api_code"),
                      "after": plan.get("after"), "plan": str(plan_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
