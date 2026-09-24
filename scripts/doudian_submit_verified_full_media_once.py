"""Publish one already-verified full-media draft without rewriting its media."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import (
    DoudianProductReader, DoudianReadOnlyGateway, recommend_title_for_submission,
)
from integrations.doudian.write_gateway import DoudianSingleProductPublisher, submission_payload_sha256
from scripts.doudian_apply_local_images import _pic_urls, detail_urls
from scripts.doudian_full_media_draft_once import (
    _post_publish_issues, _property_entry, _publish_with_retry,
)
from scripts.doudian_sku_color_draft_once import _product_id, _source_fingerprint, _write_json
from scripts.doudian_edit_phone_properties_batch import SCREEN_PROPERTY_ID
from doudian_api import MODEL_PROPERTY_ID


def _only_unit_stock_increments(original: Mapping[str, Any], saved: Mapping[str, Any]) -> bool:
    """Accept only platform stock syncs of +1; reject every other SKU mutation."""
    old_rows = original.get("spec_prices") or []
    new_rows = saved.get("spec_prices") or []
    if not isinstance(old_rows, list) or not isinstance(new_rows, list) or len(old_rows) != len(new_rows):
        return False
    changed_any = False
    for old, new in zip(old_rows, new_rows):
        if not isinstance(old, Mapping) or not isinstance(new, Mapping):
            return False
        if str(old.get("sku_id") or "") != str(new.get("sku_id") or ""):
            return False
        changed = {
            key for key in set(old) | set(new) if old.get(key) != new.get(key)
        }
        if not changed:
            continue
        before_stock, after_stock = old.get("stock_num"), new.get("stock_num")
        if (changed != {"stock_num"}
                or not isinstance(before_stock, int) or isinstance(before_stock, bool)
                or not isinstance(after_stock, int) or isinstance(after_stock, bool)
                or after_stock != before_stock + 1):
            return False
        changed_any = True
    return changed_any


def _verified_report_issues(report: Mapping[str, Any], saved: Mapping[str, Any],
                            live: Mapping[str, Any], shop_id: str,
                            product_id: str, *,
                            allow_rate_limit_retry: bool = False,
                            allow_verified_read_timeout_recovery: bool = False,
                            allow_unit_stock_sync_recovery: bool = False,
                            original: Mapping[str, Any] | None = None) -> list[str]:
    issues: list[str] = []
    retryable_rate_limit_report = bool(
        allow_rate_limit_retry
        and report.get("result") == "publish_rejected_or_unknown_needs_manual_review"
        and report.get("verification_errors") == []
        and report.get("publish") is True
        and "code=60000" in str(report.get("error") or "")
        and "request-limited" in str(report.get("error") or "")
    )
    recoverable_verified_read_timeout_report = bool(
        allow_verified_read_timeout_recovery
        and report.get("result") == "failed_after_draft_write_needs_manual_review"
        and report.get("verification_errors") == []
        and report.get("publish") is True
        and report.get("api_code") == 10000
        and not report.get("publish_api_code")
        and not report.get("publish_payload_sha256")
        and "timed out" in str(report.get("error") or "").casefold()
    )
    recoverable_unit_stock_sync_report = bool(
        allow_unit_stock_sync_recovery
        and isinstance(original, Mapping)
        and report.get("result") == "unverified_needs_manual_review"
        and report.get("verification_errors") == ["spec_prices 改变"]
        and report.get("publish") is True
        and report.get("api_code") == 10000
        and not report.get("publish_api_code")
        and not report.get("publish_payload_sha256")
        and _only_unit_stock_increments(original, saved)
    )
    permitted_source_report = bool(
        report.get("result") == "verified_draft"
        or retryable_rate_limit_report
        or recoverable_verified_read_timeout_report
        or recoverable_unit_stock_sync_report
    )
    permitted_verification_errors = bool(
        report.get("verification_errors") == []
        or recoverable_unit_stock_sync_report
    )
    if (str(report.get("shop_id") or "") != shop_id
            or str(report.get("product_id") or "") != product_id
            or not permitted_source_report
            or not permitted_verification_errors
            or (report.get("publish") is not False
                and not retryable_rate_limit_report
                and not recoverable_verified_read_timeout_report
                and not recoverable_unit_stock_sync_report)):
        issues.append("source_report_not_verified")
    if _product_id(saved) != product_id or _product_id(live) != product_id:
        issues.append("product_id_mismatch")
    if (live.get("status"), live.get("check_status"), live.get("draft_status")) != (0, 1, 1):
        issues.append("product_not_editable_draft")
    if _source_fingerprint(saved) != _source_fingerprint(live):
        issues.append("draft_changed_since_verification")
    if len(_pic_urls(live.get("pic"))) != len(_pic_urls(saved.get("pic"))):
        issues.append("main_area_count_changed")
    if len(detail_urls(live.get("description"))) != len(detail_urls(saved.get("description"))):
        issues.append("detail_count_changed")
    if len(live.get("spec_pics") or []) != len(saved.get("spec_pics") or []):
        issues.append("color_count_changed")
    if len(live.get("spec_prices") or []) != len(saved.get("spec_prices") or []):
        issues.append("sku_count_changed")
    properties = normalize_product_format_for_edit(parse_product_format(live.get("product_format_new")))
    for property_id, expected, label in (
        (MODEL_PROPERTY_ID, report.get("model_option") or {}, "model"),
        (SCREEN_PROPERTY_ID, report.get("screen_shape") or {}, "screen"),
    ):
        actual = _property_entry(properties, property_id)
        if (str(actual.get("value")) != str(expected.get("value"))
                or actual.get("name") != expected.get("name")
                or actual.get("diy_type") != 0):
            issues.append(label + "_changed")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish one verified full-media draft")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output-root", default="panel_data/doudian_full_media_publish")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--publish-retries", type=int, default=2,
        help="safe retries after the initial publish submission (default: 2)",
    )
    parser.add_argument(
        "--allow-rate-limit-retry", action="store_true",
        help="retry one previously verified commit rejected specifically with code 60000",
    )
    parser.add_argument(
        "--allow-verified-read-timeout-recovery", action="store_true",
        help="submit a verified saved draft when the pre-submit reread alone timed out",
    )
    parser.add_argument(
        "--allow-unit-stock-sync-recovery", action="store_true",
        help="submit when draft verification found only platform stock increments of exactly one",
    )
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit():
        parser.error("shop_id and product_id must be numeric")
    if not 0 <= args.publish_retries <= 5:
        parser.error("--publish-retries must be between 0 and 5")
    source_path = Path(args.source_report).resolve()
    report = json.loads(source_path.read_text(encoding="utf-8"))
    saved_path = source_path.with_name(source_path.name.replace(".report.json", ".after.json"))
    original_path = source_path.with_name(source_path.name.replace(".report.json", ".before.json"))
    saved = json.loads(saved_path.read_text(encoding="utf-8"))["detail"]
    original = json.loads(original_path.read_text(encoding="utf-8"))["detail"]
    gateway = DoudianReadOnlyGateway(args.env, shop_id)
    reader = DoudianProductReader(gateway)
    live = dict(reader.get_product_detail(product_id, show_draft=True))
    issues = _verified_report_issues(
        report, saved, live, shop_id, product_id,
        allow_rate_limit_retry=args.allow_rate_limit_retry,
        allow_verified_read_timeout_recovery=args.allow_verified_read_timeout_recovery,
        allow_unit_stock_sync_recovery=args.allow_unit_stock_sync_recovery,
        original=original,
    )
    stamp = int(time.time())
    plan_path = Path(args.output_root) / shop_id / f"{stamp}_{product_id}.json"
    plan: dict[str, Any] = {
        "shop_id": shop_id, "product_id": product_id, "created_at": stamp,
        "source_report": str(source_path), "source_snapshot": str(saved_path),
        "mode": "execute" if args.execute else "dry_run",
        "preflight_issues": issues, "result": "blocked" if issues else "ready",
    }
    _write_json(plan_path, plan)
    if issues or not args.execute:
        print(json.dumps({"result": plan["result"], "issues": issues,
                          "report": str(plan_path)}, ensure_ascii=False))
        return 2 if issues else 0

    latest = dict(reader.get_product_detail(product_id, show_draft=True))
    issues = _verified_report_issues(
        report, saved, latest, shop_id, product_id,
        allow_rate_limit_retry=args.allow_rate_limit_retry,
        allow_verified_read_timeout_recovery=args.allow_verified_read_timeout_recovery,
        allow_unit_stock_sync_recovery=args.allow_unit_stock_sync_recovery,
        original=original,
    )
    if issues:
        plan.update({"result": "blocked_before_write", "pre_submit_issues": issues})
        _write_json(plan_path, plan)
        print(json.dumps({"result": plan["result"], "issues": issues,
                          "report": str(plan_path)}, ensure_ascii=False))
        return 2
    title_plan = recommend_title_for_submission(gateway, latest)
    payload = {
        "product_id": product_id,
        "product_format_new": json.dumps(
            normalize_product_format_for_edit(parse_product_format(latest.get("product_format_new"))),
            ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ),
        "name": title_plan.name,
        "name_prefix": title_plan.name_prefix,
        "commit": True,
    }
    white_background_url = str(latest.get("white_back_ground_pic_url") or "").strip()
    if white_background_url:
        payload["white_back_ground_pic_url"] = white_background_url
    digest = submission_payload_sha256(payload)
    plan.update({
        "result": "publish_pending", "payload_sha256": digest,
        "recommended_title": title_plan.name,
        "recommended_name_prefix": title_plan.name_prefix,
        "recommended_property_ids": list(title_plan.property_ids),
        "publish_retry_limit": args.publish_retries,
        "publish_attempts": [],
        "rate_limit_retry": bool(args.allow_rate_limit_retry),
        "verified_read_timeout_recovery": bool(args.allow_verified_read_timeout_recovery),
        "unit_stock_sync_recovery": bool(args.allow_unit_stock_sync_recovery),
    })
    _write_json(plan_path, plan)
    try:
        def record_publish_failure(attempt, delay, error, readback, accepted):
            plan["publish_attempts"].append({
                "attempt": attempt,
                "result": "accepted_by_readback" if accepted else "rejected",
                "error": f"{type(error).__name__}: {error}",
                "retry_in_seconds": delay,
                "readback_status": readback.get("status") if readback else None,
                "readback_check_status": readback.get("check_status") if readback else None,
            })
            _write_json(plan_path, plan)

        response, publish_attempt_count, accepted_readback = _publish_with_retry(
            DoudianSingleProductPublisher(args.env, shop_id, product_id, digest),
            payload,
            read_detail=lambda: dict(reader.get_product_detail(product_id, show_draft=True)),
            expected_fingerprint=_source_fingerprint(latest),
            retries=args.publish_retries,
            on_failure=record_publish_failure,
        )
        if accepted_readback is None:
            plan["publish_attempts"].append({
                "attempt": publish_attempt_count,
                "result": "api_accepted",
                "api_code": response.get("code"),
            })
        plan.update({"result": "api_accepted_readback_pending",
                     "publish_api_code": response.get("code"),
                     "publish_attempt_count": publish_attempt_count,
                     "publish_retry_count": max(0, publish_attempt_count - 1)})
        _write_json(plan_path, plan)
    except Exception as exc:
        plan.update({"result": "submit_unknown_or_rejected", "error": str(exc)[:500]})
        _write_json(plan_path, plan)
        print(json.dumps({"result": plan["result"], "error": plan["error"],
                          "report": str(plan_path)}, ensure_ascii=False))
        return 2

    published: dict[str, Any] = dict(accepted_readback or {})
    for attempt in range(1, 13):
        if not published:
            published = dict(reader.get_product_detail(product_id, show_draft=True))
        if published.get("status") == 0 and published.get("check_status") == 3:
            break
        published = {}
        if attempt < 12:
            time.sleep(5)
    post_issues, stock_sync = _post_publish_issues(
        latest, published, expected_name=title_plan.name,
        expected_prefix=title_plan.name_prefix,
    )
    plan.update({
        "post_publish_issues": post_issues,
        "stock_sync": stock_sync,
        "after_publish": {
            "status": published.get("status"),
            "check_status": published.get("check_status"),
            "draft_status": published.get("draft_status"),
            "name": published.get("name"),
            "name_prefix": published.get("name_prefix"),
            "main_count": len(_pic_urls(published.get("pic"))),
            "detail_count": len(detail_urls(published.get("description"))),
            "color_count": len(published.get("spec_pics") or []),
            "sku_count": len(published.get("spec_prices") or []),
        },
        "result": "published_readback" if not post_issues
        else "api_accepted_pending_or_needs_review",
    })
    _write_json(plan_path, plan)
    print(json.dumps({"result": plan["result"], "post_publish_issues": post_issues,
                      "report": str(plan_path)}, ensure_ascii=False))
    return 0 if plan["result"] == "published_readback" else 2


if __name__ == "__main__":
    raise SystemExit(main())
