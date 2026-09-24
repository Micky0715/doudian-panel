"""One-product editV2 probe with snapshot, strict verification and rollback."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_api import MODEL_PROPERTY_ID, find_property, resolve_model_option
from doudian_product_service import (
    build_model_edit_payload,
    normalize_product_format_for_edit,
    parse_product_format,
)
from integrations.doudian import DoudianReadOnlyGateway
from integrations.doudian.write_gateway import DoudianSingleProductWriter


CRITICAL_UNCHANGED_FIELDS = (
    "name", "pic", "description", "brand_id", "standard_brand_id",
    "spec_id", "specs", "spec_prices", "freight_id", "product_type",
)


def _data(response: Mapping[str, Any]) -> dict[str, Any]:
    value = response.get("data") or {}
    if not isinstance(value, dict):
        raise RuntimeError("DouDian response data is not an object")
    return value


def _property_map(detail: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_product_format_for_edit(
        parse_product_format(detail.get("product_format_new"))
    )


def _model_value(detail: Mapping[str, Any]) -> dict[str, Any]:
    entries = _property_map(detail).get(str(MODEL_PROPERTY_ID)) or []
    return dict(entries[0]) if len(entries) == 1 and isinstance(entries[0], dict) else {}


def _snapshot(path: Path, content: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(content, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify(before: Mapping[str, Any], after: Mapping[str, Any], option: Mapping[str, Any]):
    problems = []
    expected_product = str(before.get("product_id_str") or before.get("product_id") or "")
    actual_product = str(after.get("product_id_str") or after.get("product_id") or "")
    if actual_product != expected_product:
        problems.append("product_id changed")
    if after.get("status") != before.get("status") or after.get("check_status") != before.get("check_status"):
        problems.append("draft status changed")
    for key in CRITICAL_UNCHANGED_FIELDS:
        if after.get(key) != before.get(key):
            problems.append(f"{key} changed")
    before_props = _property_map(before)
    after_props = _property_map(after)
    before_props.pop(str(MODEL_PROPERTY_ID), None)
    after_props.pop(str(MODEL_PROPERTY_ID), None)
    if after_props != before_props:
        problems.append("non-model properties changed")
    model = _model_value(after)
    if str(model.get("value")) != str(option.get("value")):
        problems.append("official model value was not persisted")
    if str(model.get("name") or "").strip() != str(option.get("name") or "").strip():
        problems.append("official model name was not persisted")
    if int(model.get("diy_type") or 0) != 0:
        problems.append("model remained a custom value")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely test one product.editV2 model edit")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("model_name")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--snapshot-root", default="panel_data/doudian_write_snapshots")
    args = parser.parse_args()

    reader = DoudianReadOnlyGateway(args.env, args.shop_id)
    before = _data(reader.call("product.detail", {
        "product_id": args.product_id, "show_draft": "true",
    }))
    actual_id = str(before.get("product_id_str") or before.get("product_id") or "")
    if actual_id != str(args.product_id):
        raise RuntimeError("read-back product id does not match requested product")
    if before.get("status") != 0 or before.get("check_status") != 1:
        raise RuntimeError("target is not a current pending draft (status=0/check_status=1)")

    properties = _data(reader.call("product.getCatePropertyV2", {
        "category_leaf_id": 1000007628,
    })).get("data", [])
    model_property = find_property(properties, MODEL_PROPERTY_ID)
    if not model_property:
        raise RuntimeError("model property 1493 is missing from category catalogue")
    option = resolve_model_option(model_property, args.model_name, brand_names=("iQOO",))
    if not option:
        raise RuntimeError("requested model is not one unique platform option")
    payload = build_model_edit_payload(before, option)
    # product.editV2 requires this flag. False means save only: it must never
    # become a submission/review action in this first write probe.
    payload["commit"] = False
    preview = {
        "shop_id": str(args.shop_id), "product_id": actual_id,
        "status": before.get("status"), "check_status": before.get("check_status"),
        "before_model": _model_value(before),
        "target_model": {"name": option.get("name"), "value": option.get("value")},
        "payload_sha256": hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    print(json.dumps(preview, ensure_ascii=False, indent=2))
    if not args.execute:
        print("DRY_RUN: no write was sent")
        return 0

    stamp = int(time.time())
    snapshot_path = Path(args.snapshot_root) / str(args.shop_id) / f"{stamp}_{actual_id}.json"
    _snapshot(snapshot_path, {"captured_at": stamp, "shop_id": str(args.shop_id), "detail": before})
    print(f"SNAPSHOT: {snapshot_path}")

    writer = DoudianSingleProductWriter(args.env, args.shop_id, args.product_id)
    result = writer.edit_model_properties(payload)
    print(json.dumps({"WRITE_CODE": result.get("code"), "WRITE_MSG": result.get("msg")}, ensure_ascii=False))

    after = _data(reader.call("product.detail", {
        "product_id": args.product_id, "show_draft": "true",
    }))
    problems = verify(before, after, option)
    if not problems:
        print(json.dumps({
            "VERIFIED": True, "status": after.get("status"),
            "check_status": after.get("check_status"), "model": _model_value(after),
        }, ensure_ascii=False))
        return 0

    print(json.dumps({"VERIFIED": False, "problems": problems}, ensure_ascii=False))
    rollback_payload = {
        "product_id": actual_id,
        "product_format_new": json.dumps(
            _property_map(before), ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ),
        "commit": False,
    }
    rollback = writer.edit_model_properties(rollback_payload)
    restored = _data(reader.call("product.detail", {
        "product_id": args.product_id, "show_draft": "true",
    }))
    restored_ok = _model_value(restored) == _model_value(before)
    print(json.dumps({
        "ROLLBACK_CODE": rollback.get("code"), "ROLLBACK_MSG": rollback.get("msg"),
        "ROLLBACK_MODEL_RESTORED": restored_ok,
    }, ensure_ascii=False))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
