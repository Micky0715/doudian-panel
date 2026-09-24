"""Submit a small, immutable API-only batch of already verified drafts.

This is intentionally *not* a creation or browser fallback script.  It only
submits existing DouDian drafts that have already passed the API property-save
phase.  The only write is ``product.editV2`` with ``commit=true``.  A failed
submission is recorded for manual handling and is never retried by a browser,
so an item cannot be submitted twice by two different paths.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import (
    DoudianProductReader, DoudianReadOnlyGateway, recommend_title_for_submission,
)
from integrations.doudian.write_gateway import (
    DoudianSingleProductPublisher,
    submission_payload_sha256,
)


MODEL_PROPERTY_ID = "1493"
SCREEN_PROPERTY_ID = "6448"
REQUIRED_ASSET_FIELDS = ("pic", "description", "spec_prices", "freight_id")
PLAN_VERSION = 1


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _properties(detail: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_product_format_for_edit(parse_product_format(detail["product_format_new"]))


def _single_standard_selection(properties: Mapping[str, Any], property_id: str) -> dict[str, Any]:
    values = properties.get(property_id)
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], Mapping):
        return {}
    return dict(values[0])


def _text_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return value not in (None, "", 0, "0")


def _detail_fingerprint(detail: Mapping[str, Any]) -> str:
    # This is a plan integrity check only.  It intentionally excludes no field
    # from the returned API payload because that payload contains only the
    # existing product_format_new; the rest is checked before submission.
    material = {
        "product_id": str(detail.get("product_id_str") or detail.get("product_id") or ""),
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "name": detail.get("name"),
        "pic": detail.get("pic"),
        "description": detail.get("description"),
        "spec_prices": detail.get("spec_prices"),
        "freight_id": detail.get("freight_id"),
        "product_format_new": _properties(detail),
    }
    return hashlib.sha256(json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _preflight(detail: Mapping[str, Any], source_item: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    issues: list[str] = []
    if detail.get("status") != 0 or detail.get("check_status") != 1:
        issues.append("not_pending_draft")
    if not str(detail.get("name") or "").strip():
        issues.append("missing_name")
    for field in REQUIRED_ASSET_FIELDS:
        if not _text_present(detail.get(field)):
            issues.append("missing_" + field)
    props = _properties(detail)
    checks = {}
    for property_id, source_key, label in (
        (MODEL_PROPERTY_ID, "model", "model"),
        (SCREEN_PROPERTY_ID, "screen_shape", "screen"),
    ):
        actual = _single_standard_selection(props, property_id)
        planned = source_item.get(source_key) if isinstance(source_item.get(source_key), Mapping) else {}
        checks[label] = {"value": actual.get("value"), "name": actual.get("name"), "diy_type": actual.get("diy_type")}
        if not actual:
            issues.append("missing_" + label)
        elif str(actual.get("value")) != str(planned.get("value")) or str(actual.get("name") or "").strip() != str(planned.get("name") or "").strip():
            issues.append(label + "_changed_since_verified")
        elif int(actual.get("diy_type") or 0) != 0:
            issues.append(label + "_not_standard")
    return issues, checks


def _payload(detail: Mapping[str, Any], gateway: DoudianReadOnlyGateway) -> dict[str, Any]:
    product_id = str(detail.get("product_id_str") or detail.get("product_id") or "").strip()
    if not product_id.isdigit():
        raise ValueError("detail has no numeric product id")
    title_plan = recommend_title_for_submission(gateway, detail)
    return {
        "product_id": product_id,
        "product_format_new": json.dumps(
            _properties(detail), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
        "name": title_plan.name,
        "name_prefix": title_plan.name_prefix,
        "commit": True,
    }


def _status_view(detail: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "status": detail.get("status"),
        "check_status": detail.get("check_status"),
        "name": detail.get("name"),
        "has_main_image": _text_present(detail.get("pic")),
        "has_detail_images": _text_present(detail.get("description")),
        "has_sku_price": _text_present(detail.get("spec_prices")),
        "has_freight": _text_present(detail.get("freight_id")),
    }


def _load_candidates(report: Mapping[str, Any], shop_id: str, limit: int) -> list[Mapping[str, Any]]:
    if str(report.get("shop_id") or "") != shop_id:
        raise ValueError("source report shop does not match requested shop")
    if report.get("commit") is not False:
        raise ValueError("source report must be a verified commit=false API save report")
    items = [item for item in report.get("items", [])
             if isinstance(item, Mapping) and item.get("result") == "verified"]
    if len(items) < limit:
        raise ValueError("source report has fewer than requested verified drafts")
    return items[:limit]


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit verified DouDian drafts through API only")
    parser.add_argument("shop_id")
    parser.add_argument("--source-report", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-root", default="panel_data/doudian_api_publish_plans")
    parser.add_argument("--snapshot-root", default="panel_data/doudian_api_publish_snapshots")
    args = parser.parse_args()
    if not 1 <= args.limit <= 10:
        raise SystemExit("limit must be between 1 and 10")
    shop_id = str(args.shop_id).strip()
    if not shop_id.isdigit():
        raise SystemExit("shop id must be numeric")

    source_path = Path(args.source_report)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    candidates = _load_candidates(source, shop_id, args.limit)
    gateway = DoudianReadOnlyGateway(args.env, shop_id)
    reader = DoudianProductReader(gateway)
    plan: dict[str, Any] = {
        "schema_version": PLAN_VERSION,
        "shop_id": shop_id,
        "source_report": str(source_path.resolve()),
        "source_report_checksum": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "created_at": int(time.time()),
        "mode": "execute" if args.execute else "dry_run",
        "operation": "product.editV2(commit=true)",
        "items": [],
        "manual_queue": [],
    }
    prepared: list[tuple[dict[str, Any], Mapping[str, Any], dict[str, Any]]] = []
    for source_item in candidates:
        product_id = str(source_item.get("product_id") or "")
        try:
            detail = dict(reader.get_product_detail(product_id, show_draft=True))
            issues, property_checks = _preflight(detail, source_item)
            if issues:
                raise ValueError(",".join(issues))
            payload = _payload(detail, gateway)
            item = {
                "product_id": product_id,
                "name": str(detail.get("name") or ""),
                "before_fingerprint": _detail_fingerprint(detail),
                "payload_sha256": submission_payload_sha256(payload),
                "preflight": _status_view(detail),
                "property_checks": property_checks,
            }
            plan["items"].append(item)
            prepared.append((item, detail, payload))
        except Exception as exc:
            plan["manual_queue"].append({"product_id": product_id, "reason": str(exc)})

    # Persist the plan before the first network write.  The publisher compares
    # each payload with the digest held in this file's item record.
    plan_path = Path(args.plan_root) / shop_id / f"{plan['created_at']}.json"
    _atomic_json(plan_path, plan)

    if args.execute and not plan["manual_queue"] and len(prepared) == args.limit:
        for item, before, payload in prepared:
            product_id = item["product_id"]
            stamp = int(time.time())
            snapshot_path = Path(args.snapshot_root) / shop_id / f"{stamp}_{product_id}.json"
            _atomic_json(snapshot_path, {
                "captured_at": stamp, "shop_id": shop_id, "detail": before,
                "plan": str(plan_path), "payload_sha256": item["payload_sha256"],
            })
            try:
                publisher = DoudianSingleProductPublisher(
                    args.env, shop_id, product_id, item["payload_sha256"]
                )
                response = publisher.submit_planned_draft(payload)
                item.update({"result": "accepted", "api_code": response.get("code"), "snapshot": str(snapshot_path)})
                # The platform can move state asynchronously.  Read once; the
                # API acknowledgement remains the only submission-success claim.
                try:
                    after = dict(reader.get_product_detail(product_id, show_draft=True))
                    item["after_submit"] = _status_view(after)
                except Exception as exc:
                    item["after_submit_read_error"] = str(exc)
            except Exception as exc:
                item.update({"result": "api_rejected", "error": str(exc), "snapshot": str(snapshot_path)})
                plan["manual_queue"].append({"product_id": product_id, "reason": "api_rejected:" + str(exc)})
            # Submission is not reversible through this script.  Persist each
            # outcome immediately so an interrupted terminal cannot erase the
            # audit trail or invite an unsafe duplicate submission.
            _atomic_json(plan_path, plan)
    elif args.execute:
        plan["execution_blocked"] = "all requested items must pass preflight before any submission"

    plan["summary"] = {
        "requested": args.limit,
        "prepared": len(prepared),
        "accepted": sum(1 for item in plan["items"] if item.get("result") == "accepted"),
        "manual_queue": len(plan["manual_queue"]),
    }
    _atomic_json(plan_path, plan)
    print(json.dumps({"summary": plan["summary"], "plan": str(plan_path)}, ensure_ascii=False))
    return 0 if not plan["manual_queue"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
