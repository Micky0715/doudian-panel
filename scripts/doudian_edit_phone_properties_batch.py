"""API-only save-draft test for model and screen-shape properties.

This intentionally stops at product.editV2(commit=false).  It never uploads
assets, changes SKU/price, or publishes.  Any item whose model cannot be tied
to exactly one platform option is recorded for the browser fallback queue.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_api import (
    MODEL_PROPERTY_ID, find_property, normalize_model_name, resolve_model_option,
)
from doudian_product_service import (
    build_property_edit_payload,
    normalize_product_format_for_edit,
    parse_product_format,
)
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.write_gateway import DoudianSingleProductWriter
from app.listing_preflight.legacy_resolver import resolve_brand_model
from app.material_pipeline import OfficialModelRegistry
from model_catalogue import versionless_model_candidates


PHONE_CATEGORY_ID = "1000007628"
SCREEN_PROPERTY_ID = 6448
DEFAULT_SCREEN_NAME = "直屏"
CRITICAL_UNCHANGED_FIELDS = (
    "name", "pic", "description", "brand_id", "standard_brand_id",
    "spec_id", "specs", "spec_prices", "freight_id", "product_type",
)


# Human-reviewed exceptions are authoritative only when both the platform ID
# and platform spelling still exist in the live category enum.  They never
# create a free-form value locally.
VERIFIED_MODEL_OPTION_OVERRIDES = {
    ("苹果", "iphone17promax"): {"value": "559043", "name": "iPhone 17 Pro Max"},
    # 素材库/官方销售名保留 5G 后缀，抖店手机类目枚举只提供 14R。
    # 该 ID 与名称已由 2026-09-23 实时枚举复核；仍要求二者同时存在才采用。
    ("红米", "14r5g"): {"value": "468434", "name": "14R"},
    # HONOR's official product name includes “保时捷设计”; DouDian's enum uses
    # the shortened family name below.
    ("荣耀", "magic8rsr保时捷设计"): {"value": "629505", "name": "Magic8 RSR"},
}

OFFICIAL_MODEL_REGISTRY = OfficialModelRegistry.bundled()

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


def _format(detail: Mapping[str, Any]) -> dict[str, Any]:
    return normalize_product_format_for_edit(parse_product_format(detail["product_format_new"]))


def _selection(detail: Mapping[str, Any], property_id: int | str) -> dict[str, Any]:
    values = _format(detail).get(str(property_id)) or []
    return dict(values[0]) if len(values) == 1 and isinstance(values[0], Mapping) else {}


def _option_value(option: Mapping[str, Any]) -> Any:
    return option.get("value", option.get("value_id"))


def _selection_option(prop: Mapping[str, Any], selection: Mapping[str, Any]):
    """Resolve an existing selection by exact value first, then exact name."""
    value = str(selection.get("value") or "").strip()
    name = " ".join(str(selection.get("name") or "").split()).casefold()
    options = [item for item in prop.get("options", []) if isinstance(item, Mapping)]
    value_matches = [item for item in options if value and str(_option_value(item)) == value]
    if len(value_matches) == 1:
        return value_matches[0]
    name_matches = [item for item in options if name and " ".join(
        str(item.get("name") or "").split()).casefold() == name]
    return name_matches[0] if len(name_matches) == 1 else None


def _exact_named_option(prop: Mapping[str, Any], name: str):
    wanted = " ".join(str(name).split()).casefold()
    matches = [item for item in prop.get("options", []) if isinstance(item, Mapping)
               and " ".join(str(item.get("name") or "").split()).casefold() == wanted]
    return matches[0] if len(matches) == 1 else None


def _standard_option(option: Mapping[str, Any]) -> dict[str, Any]:
    value = _option_value(option)
    name = str(option.get("name") or "").strip()
    if value in (None, "") or not name:
        raise ValueError("platform option has no value/name")
    return {"value": value, "name": name}


def _reviewed_model_option(prop: Mapping[str, Any], brand_cn: str,
                           model: str) -> dict[str, Any] | None:
    key = (str(brand_cn or "").strip(), normalize_model_name(model))
    reviewed = VERIFIED_MODEL_OPTION_OVERRIDES.get(key)
    if not reviewed:
        return None
    matches = [
        option for option in prop.get("options", []) if isinstance(option, Mapping)
        and str(_option_value(option)) == str(reviewed["value"])
        and str(option.get("name") or "").strip() == reviewed["name"]
    ]
    return _standard_option(matches[0]) if len(matches) == 1 else None


def _versionless_model_candidates(value: str) -> tuple[str, ...]:
    """Compatibility wrapper for the shared conservative suffix policy."""
    return versionless_model_candidates(value)


def model_option_suggestions(prop: Mapping[str, Any], model: str, *,
                             brand_names=(), limit: int = 4) -> list[dict[str, Any]]:
    """Return diagnostic platform candidates; never use them as a selection."""
    expected = normalize_model_name(model, brand_names)
    if not expected:
        return []
    ranked: list[tuple[float, dict[str, Any]]] = []
    for option in prop.get("options", []):
        if not isinstance(option, Mapping):
            continue
        candidate = normalize_model_name(option.get("name", ""), brand_names)
        if not candidate:
            continue
        score = difflib.SequenceMatcher(None, expected, candidate).ratio()
        if expected in candidate or candidate in expected:
            score = max(score, 0.9)
        if score >= 0.62:
            ranked.append((score, _standard_option(option)))
    ranked.sort(key=lambda row: (-row[0], row[1]["name"], str(row[1]["value"])))
    return [option for _, option in ranked[:limit]]


def _temporary_stable_random_model_option(
    prop: Mapping[str, Any], model: str, *, brand_names=(),
) -> dict[str, Any] | None:
    """Pick one of several *equivalent* live options in a stable random way.

    This is a temporary business rule.  It is deliberately limited to options
    whose complete normalized model key equals the requested complete model;
    fuzzy diagnostic suggestions are never eligible.  Sorting plus a SHA-256
    bucket makes the arbitrary choice stable across retries and shops, so the
    same brand/model cannot oscillate between platform IDs.
    """
    expected = normalize_model_name(model, brand_names)
    if not expected:
        return None
    matches = [
        _standard_option(option)
        for option in prop.get("options", [])
        if isinstance(option, Mapping)
        and normalize_model_name(option.get("name", ""), brand_names) == expected
    ]
    unique = {
        (str(option["value"]), option["name"]): option
        for option in matches
    }
    choices = sorted(unique.values(), key=lambda item: (item["name"], str(item["value"])))
    if len(choices) < 2:
        return None
    brand_key = "|".join(sorted(
        normalize_model_name(value) for value in brand_names if str(value).strip()
    ))
    digest = hashlib.sha256(f"{brand_key}|{expected}".encode("utf-8")).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def _model_option(detail: Mapping[str, Any], title: str, prop: Mapping[str, Any]):
    parsed = dict(resolve_brand_model(title))
    aliases = tuple(value for value in (
        parsed.get("target_brand_en"), parsed.get("target_brand_cn"), parsed.get("brand"),
    ) if value)
    reviewed = _reviewed_model_option(
        prop, str(parsed.get("target_brand_cn") or ""), str(parsed.get("model") or ""),
    )
    if reviewed:
        return reviewed, "reviewed_override"

    current = _selection(detail, MODEL_PROPERTY_ID)
    selected = _selection_option(prop, current)
    if selected:
        return _standard_option(selected), "existing_platform"

    # Audited manufacturer spelling takes precedence over presentation
    # variants in the platform enum.  It still must resolve to exactly one
    # live option; the registry never invents a platform value.
    official = OFFICIAL_MODEL_REGISTRY.resolve(
        str(parsed.get("target_brand_cn") or ""),
        str(parsed.get("model") or ""),
    )
    if official:
        hit = resolve_model_option(prop, official.official_name, brand_names=aliases)
        if hit:
            return _standard_option(hit), "official_registry"

    candidates = [str(current.get("name") or "").strip(),
                  str(parsed.get("target_model_exact") or "").strip(),
                  str(parsed.get("model") or "").strip()]
    # Marketplace titles commonly use “(5G版)”, while the platform enum is
    # “... 5G”. This is a format conversion only; the result must still have
    # exactly one platform option before it can be written.
    candidates.extend(re.sub(r"[（(]\s*5g版\s*[）)]", " 5G", value,
                             flags=re.IGNORECASE).strip()
                      for value in list(candidates) if value)
    seen = set()
    for candidate in candidates:
        key = " ".join(candidate.split()).casefold()
        if not candidate or key in seen:
            continue
        seen.add(key)
        hit = resolve_model_option(prop, candidate, brand_names=aliases)
        if hit:
            return _standard_option(hit), "resolved"
        temporary = _temporary_stable_random_model_option(
            prop, candidate, brand_names=aliases,
        )
        if temporary:
            return temporary, "temporary_stable_random"
    for candidate in tuple(candidates):
        for base_model in _versionless_model_candidates(candidate):
            hit = resolve_model_option(prop, base_model, brand_names=aliases)
            if hit:
                return _standard_option(hit), "version_suffix_removed"
    return None, "unresolved"


def _screen_option(detail: Mapping[str, Any], prop: Mapping[str, Any]):
    current = _selection(detail, SCREEN_PROPERTY_ID)
    selected = _selection_option(prop, current)
    if selected:
        return _standard_option(selected), "existing"
    default = _exact_named_option(prop, DEFAULT_SCREEN_NAME)
    if not default:
        return None, "default_missing_from_platform"
    return _standard_option(default), "default_straight"


def _detail_fingerprint(detail: Mapping[str, Any]) -> str:
    value = {
        "status": detail.get("status"), "check_status": detail.get("check_status"),
        "fields": {key: detail.get(key) for key in CRITICAL_UNCHANGED_FIELDS},
        "properties": _format(detail),
    }
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _verify(before: Mapping[str, Any], after: Mapping[str, Any],
            model: Mapping[str, Any], screen: Mapping[str, Any]) -> list[str]:
    issues = []
    if after.get("status") != before.get("status") or after.get("check_status") != before.get("check_status"):
        issues.append("draft_status_changed")
    for key in CRITICAL_UNCHANGED_FIELDS:
        if after.get(key) != before.get(key):
            issues.append(f"{key}_changed")
    before_props, after_props = _format(before), _format(after)
    for key in (str(MODEL_PROPERTY_ID), str(SCREEN_PROPERTY_ID)):
        before_props.pop(key, None)
        after_props.pop(key, None)
    if before_props != after_props:
        issues.append("non_target_properties_changed")
    for property_id, expected, label in (
        (MODEL_PROPERTY_ID, model, "model"), (SCREEN_PROPERTY_ID, screen, "screen"),
    ):
        actual = _selection(after, property_id)
        if str(actual.get("value")) != str(expected.get("value")):
            issues.append(f"{label}_value_not_persisted")
        if str(actual.get("name") or "").strip() != str(expected.get("name") or "").strip():
            issues.append(f"{label}_name_not_persisted")
        if int(actual.get("diy_type") or 0) != 0:
            issues.append(f"{label}_remained_custom")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="Batch API-only draft property save test")
    parser.add_argument("shop_id")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report-root", default="panel_data/doudian_api_write_reports")
    parser.add_argument("--snapshot-root", default="panel_data/doudian_write_snapshots")
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        raise SystemExit("limit must be between 1 and 20")

    source = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    if str(source.get("shop_id") or "") != str(args.shop_id):
        raise SystemExit("snapshot shop does not match requested shop")
    candidates = [item for item in source.get("items", [])
                  if str(item.get("category_leaf_id") or "") == PHONE_CATEGORY_ID][:args.limit]
    if len(candidates) != args.limit:
        raise SystemExit("snapshot has fewer than requested phone drafts")

    gateway = DoudianReadOnlyGateway(args.env, args.shop_id)
    reader = DoudianProductReader(gateway)
    raw_properties = gateway.call("product.getCatePropertyV2", {"category_leaf_id": int(PHONE_CATEGORY_ID)})
    property_data = raw_properties.get("data") or {}
    properties = property_data.get("data", []) if isinstance(property_data, Mapping) else []
    model_prop = find_property(properties, MODEL_PROPERTY_ID)
    screen_prop = find_property(properties, SCREEN_PROPERTY_ID)
    if not model_prop or not screen_prop:
        raise SystemExit("platform model or screen-shape property is unavailable")

    report: dict[str, Any] = {
        "schema_version": 1, "shop_id": str(args.shop_id), "source_snapshot": str(Path(args.snapshot).resolve()),
        "source_checksum": source.get("checksum"), "created_at": int(time.time()),
        "mode": "execute" if args.execute else "dry_run", "commit": False,
        "items": [], "browser_fallback": [],
    }
    prepared = []
    for row in candidates:
        product_id, title = str(row["product_id"]), str(row.get("name") or "")
        try:
            detail = dict(reader.get_product_detail(product_id, show_draft=True))
            if detail.get("status") != 0 or detail.get("check_status") != 1:
                raise ValueError("not_pending_draft")
            model, model_source = _model_option(detail, title, model_prop)
            screen, screen_source = _screen_option(detail, screen_prop)
            if not model:
                raise ValueError("model_unresolved")
            if not screen:
                raise ValueError("screen_default_missing")
            payload = build_property_edit_payload(detail, {
                MODEL_PROPERTY_ID: model, SCREEN_PROPERTY_ID: screen,
            })
            payload["commit"] = False
            item = {
                "product_id": product_id, "name": title, "model": model,
                "screen_shape": screen, "model_source": model_source,
                "screen_source": screen_source, "before_fingerprint": _detail_fingerprint(detail),
                "payload_sha256": hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest(),
            }
            prepared.append((item, detail, payload))
            report["items"].append(item)
        except Exception as exc:
            report["browser_fallback"].append({"product_id": product_id, "name": title, "reason": str(exc)})

    if args.execute:
        for item, before, payload in prepared:
            product_id = item["product_id"]
            stamp = int(time.time())
            snapshot_path = Path(args.snapshot_root) / str(args.shop_id) / f"{stamp}_{product_id}.json"
            _atomic_json(snapshot_path, {"captured_at": stamp, "shop_id": str(args.shop_id), "detail": before})
            try:
                writer = DoudianSingleProductWriter(args.env, args.shop_id, product_id)
                response = writer.edit_model_properties(payload)
                after = dict(reader.get_product_detail(product_id, show_draft=True))
                issues = _verify(before, after, item["model"], item["screen_shape"])
                item.update({"write_code": response.get("code"), "snapshot": str(snapshot_path), "issues": issues})
                if issues:
                    rollback = build_property_edit_payload(before, {})
                    rollback["commit"] = False
                    rollback_response = writer.edit_model_properties(rollback)
                    item.update({"result": "rolled_back", "rollback_code": rollback_response.get("code")})
                    report["browser_fallback"].append({"product_id": product_id, "name": item["name"], "reason": "verification_failed:" + ",".join(issues)})
                else:
                    item["result"] = "verified"
            except Exception as exc:
                item.update({"result": "api_error", "error": str(exc)})
                report["browser_fallback"].append({"product_id": product_id, "name": item["name"], "reason": "api_error:" + str(exc)})

    report["summary"] = {
        "requested": args.limit, "prepared": len(prepared),
        "verified": sum(1 for item in report["items"] if item.get("result") == "verified"),
        "fallback": len(report["browser_fallback"]),
    }
    report_path = Path(args.report_root) / str(args.shop_id) / f"{report['created_at']}.json"
    _atomic_json(report_path, report)
    print(json.dumps({"summary": report["summary"], "report": str(report_path)}, ensure_ascii=False))
    return 0 if not report["browser_fallback"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
