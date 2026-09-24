"""Launch one explicitly approved full-media submission after strict readback checks."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.write_gateway import DoudianSingleProductLauncher
from scripts.doudian_apply_local_images import _pic_urls, detail_urls
from scripts.doudian_sku_color_draft_once import _product_id, _write_json


EXPECTED_PENDING_ISSUES = frozenset({
    "online_state_not_confirmed",
    "pic_changed_after_submit",
})


def _view(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "draft_status": detail.get("draft_status"),
        "name": detail.get("name"),
        "name_prefix": detail.get("name_prefix"),
        "main_count": len(_pic_urls(detail.get("pic"))),
        "detail_count": len(detail_urls(detail.get("description"))),
        "color_count": len(detail.get("spec_pics") or []),
        "sku_count": len(detail.get("spec_prices") or []),
    }


def _preflight_issues(report: Mapping[str, Any], live: Mapping[str, Any],
                      shop_id: str, product_id: str) -> list[str]:
    issues: list[str] = []
    if str(report.get("shop_id") or "") != shop_id:
        issues.append("source_shop_mismatch")
    if str(report.get("product_id") or "") != product_id:
        issues.append("source_product_mismatch")
    if report.get("result") != "api_accepted_pending_or_needs_review":
        issues.append("source_not_accepted_pending")
    if report.get("publish_api_code") != 10000:
        issues.append("publish_was_not_accepted")
    if frozenset(report.get("post_publish_issues") or []) != EXPECTED_PENDING_ISSUES:
        issues.append("unexpected_source_pending_reasons")
    if report.get("grade_image_strategy") != "combined_gallery":
        issues.append("source_not_combined_gallery")
    grade_uploads = [
        row for row in (report.get("uploaded") or [])
        if isinstance(row, Mapping) and row.get("kind") == "grade"
    ]
    if (len(grade_uploads) != 1
            or not str(grade_uploads[0].get("source") or "").endswith("/三合一.jpeg")):
        issues.append("combined_grade_upload_evidence_missing")
    if _product_id(live) != product_id:
        issues.append("live_product_mismatch")
    if (live.get("status"), live.get("check_status"), live.get("draft_status")) != (1, 7, 0):
        issues.append("product_not_approved_waiting_launch")
    expected = report.get("after_publish") or {}
    actual = _view(live)
    for key in ("main_count", "detail_count", "color_count", "sku_count"):
        if actual[key] != expected.get(key):
            issues.append(key + "_changed")
    expected_name = str(report.get("recommended_title") or expected.get("name") or "")
    if not expected_name or actual["name"] != expected_name:
        issues.append("title_changed")
    if report.get("target_detail_count") != actual["detail_count"]:
        issues.append("target_detail_count_changed")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch one approved full-media product")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output-root", default="panel_data/doudian_full_media_launch")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id).strip(), str(args.product_id).strip()
    if not shop_id.isdigit() or not product_id.isdigit():
        parser.error("shop_id and product_id must be numeric")

    source_path = Path(args.source_report).resolve()
    report = json.loads(source_path.read_text(encoding="utf-8"))
    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
    before = dict(reader.get_product_detail(product_id, show_draft=True))
    issues = _preflight_issues(report, before, shop_id, product_id)
    stamp = int(time.time())
    output_path = Path(args.output_root) / shop_id / f"{stamp}_{product_id}.json"
    output: dict[str, Any] = {
        "shop_id": shop_id,
        "product_id": product_id,
        "created_at": stamp,
        "source_report": str(source_path),
        "mode": "execute" if args.execute else "dry_run",
        "before": _view(before),
        "preflight_issues": issues,
        "result": "blocked" if issues else "ready",
    }
    _write_json(output_path, output)
    if issues or not args.execute:
        print(json.dumps({"result": output["result"], "issues": issues,
                          "report": str(output_path)}, ensure_ascii=False))
        return 2 if issues else 0

    latest = dict(reader.get_product_detail(product_id, show_draft=True))
    latest_issues = _preflight_issues(report, latest, shop_id, product_id)
    if latest_issues:
        output.update({"result": "blocked_before_write", "pre_submit_issues": latest_issues})
        _write_json(output_path, output)
        print(json.dumps({"result": output["result"], "issues": latest_issues,
                          "report": str(output_path)}, ensure_ascii=False))
        return 2

    try:
        response = DoudianSingleProductLauncher(
            args.env, shop_id, product_id,
        ).launch({"product_id": product_id})
        output.update({"result": "launch_accepted", "api_code": response.get("code")})
        _write_json(output_path, output)
    except Exception as exc:
        output.update({"result": "launch_unknown_or_rejected", "error": str(exc)[:500]})
        _write_json(output_path, output)
        print(json.dumps({"result": output["result"], "report": str(output_path)},
                         ensure_ascii=False))
        return 2

    after: dict[str, Any] = {}
    for attempt in range(1, 13):
        after = dict(reader.get_product_detail(product_id, show_draft=True))
        if (after.get("status"), after.get("check_status")) == (0, 3):
            break
        if attempt < 12:
            time.sleep(5)
    output["after"] = _view(after)
    output["result"] = (
        "online_readback"
        if (after.get("status"), after.get("check_status")) == (0, 3)
        else "launch_accepted_pending_or_needs_review"
    )
    _write_json(output_path, output)
    print(json.dumps({"result": output["result"], "api_code": output.get("api_code"),
                      "after": output["after"], "report": str(output_path)},
                     ensure_ascii=False))
    return 0 if output["result"] == "online_readback" else 2


if __name__ == "__main__":
    raise SystemExit(main())
