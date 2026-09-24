"""Launch the single reviewed full-chain canary; never retry automatically."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.write_gateway import DoudianSingleProductLauncher
from scripts.doudian_apply_local_images import _pic_urls, detail_urls
from scripts.doudian_submit_verified_drafts import _atomic_json


SHOP_ID = "156450040"
PRODUCT_ID = "3842662503586857408"
BRAND_ID = "663234468"
MODEL_ID = "488936"


def _props(detail: Mapping[str, Any]) -> Mapping[str, Any]:
    return normalize_product_format_for_edit(parse_product_format(detail.get("product_format_new")))


def _content_issues(detail: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    product_id = str(detail.get("product_id_str") or detail.get("product_id") or "")
    if product_id != PRODUCT_ID:
        issues.append("product_id_mismatch")
    if str(detail.get("standard_brand_id")) != BRAND_ID:
        issues.append("standard_brand_not_redmi")
    if "REDMI Turbo 4" not in str(detail.get("name") or ""):
        issues.append("title_model_changed")
    model = _props(detail).get("1493") or []
    if not (
        len(model) == 1 and isinstance(model[0], Mapping)
        and str(model[0].get("value")) == MODEL_ID
        and model[0].get("name") == "REDMI Turbo 4"
        and model[0].get("diy_type") == 0
    ):
        issues.append("official_model_missing")
    if len(_pic_urls(detail.get("pic"))) != 4:
        issues.append("main_image_count_changed")
    if len(detail_urls(detail.get("description"))) != 16:
        issues.append("detail_image_count_changed")
    if len(detail.get("spec_pics") or []) != 3:
        issues.append("color_image_count_changed")
    if len(detail.get("spec_prices") or []) != 360:
        issues.append("sku_count_changed")
    return issues


def _is_online(detail: Mapping[str, Any]) -> bool:
    return detail.get("status") == 0 and detail.get("check_status") == 3


def _launch_state_issue(detail: Mapping[str, Any]) -> str | None:
    # status=1 is offline.  check_status=3 is approved and 7 is approved but
    # waiting to be launched; both can legitimately enter the launch path.
    if detail.get("status") == 1 and detail.get("check_status") in (3, 7):
        return None
    return "product_not_approved_and_offline"


def _view(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "draft_status": detail.get("draft_status"),
        "main_count": len(_pic_urls(detail.get("pic"))),
        "detail_count": len(detail_urls(detail.get("description"))),
        "color_count": len(detail.get("spec_pics") or []),
        "sku_count": len(detail.get("spec_prices") or []),
        "standard_brand_id": detail.get("standard_brand_id"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Locked one-product launch canary")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output-root", default="panel_data/doudian_full_chain_canary")
    args = parser.parse_args()

    reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, SHOP_ID))
    before = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
    issues = _content_issues(before)
    stamp = int(time.time())
    report_path = Path(args.output_root) / SHOP_ID / f"{stamp}_{PRODUCT_ID}.launch.json"
    report: dict[str, Any] = {
        "shop_id": SHOP_ID,
        "product_id": PRODUCT_ID,
        "mode": "execute" if args.execute else "dry_run",
        "created_at": stamp,
        "before": _view(before),
        "preflight_issues": issues,
        "result": "blocked" if issues else ("already_online" if _is_online(before) else "ready"),
    }
    _atomic_json(report_path, report)
    if _is_online(before):
        _atomic_json(report_path, report)
        print(json.dumps({"result": report["result"], "issues": issues,
                          "report": str(report_path)}, ensure_ascii=False))
        return 0
    state_issue = _launch_state_issue(before)
    if state_issue:
        issues.append(state_issue)
        report.update({"result": "blocked", "preflight_issues": issues})
        _atomic_json(report_path, report)
        print(json.dumps({"result": report["result"], "issues": issues,
                          "report": str(report_path)}, ensure_ascii=False))
        return 2
    if not args.execute:
        print(json.dumps({"result": report["result"], "issues": issues,
                          "report": str(report_path)}, ensure_ascii=False))
        return 2 if issues else 0

    latest = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
    latest_issues = _content_issues(latest)
    latest_state_issue = _launch_state_issue(latest)
    if latest_state_issue:
        latest_issues.append(latest_state_issue)
    if latest_issues:
        report.update({"result": "blocked_before_write", "pre_submit_issues": latest_issues})
        _atomic_json(report_path, report)
        print(json.dumps({"result": report["result"], "issues": latest_issues,
                          "report": str(report_path)}, ensure_ascii=False))
        return 2

    try:
        response = DoudianSingleProductLauncher(
            args.env, SHOP_ID, PRODUCT_ID
        ).launch({"product_id": PRODUCT_ID})
        report.update({"result": "launch_accepted", "api_code": response.get("code")})
    except Exception as exc:
        report.update({"result": "launch_unknown_or_rejected", "error": str(exc)[:500]})
        _atomic_json(report_path, report)
        print(json.dumps({"result": report["result"], "report": str(report_path)},
                         ensure_ascii=False))
        return 2
    _atomic_json(report_path, report)

    for attempt in range(1, 7):
        try:
            after = dict(reader.get_product_detail(PRODUCT_ID, show_draft=True))
            report["after"] = _view(after)
            if _is_online(after):
                break
        except Exception as exc:
            report["readback_error"] = str(exc)[:300]
        if attempt < 6:
            time.sleep(2)
    report["result"] = (
        "online_readback" if report.get("after", {}).get("status") == 0
        and report.get("after", {}).get("check_status") == 3
        else "launch_accepted_pending_or_needs_review"
    )
    _atomic_json(report_path, report)
    print(json.dumps({"result": report["result"], "api_code": report.get("api_code"),
                      "after": report.get("after"), "report": str(report_path)},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
