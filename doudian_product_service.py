"""Pure product transformations for the DouDian API migration.

No network writes live in this module.  It creates a minimal edit payload that
can be reviewed and tested before product.editV2 is enabled.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Mapping

from doudian_api import MODEL_PROPERTY_ID


class ProductTransformError(ValueError):
    pass


def parse_product_format(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ProductTransformError("product_format_new is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProductTransformError("product_format_new must be an object")
    return deepcopy(value)


def normalize_product_format_for_edit(properties: dict) -> dict:
    """Convert product.detail's Value/Name shape to editV2's value/name shape."""
    normalized = {}
    for property_id, entries in properties.items():
        if not isinstance(entries, list):
            normalized[property_id] = entries
            continue
        normalized_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                normalized_entries.append(entry)
                continue
            item = {
                key: value
                for key, value in entry.items()
                if key not in ("Value", "Name", "PropertyId", "PropertyName")
                and not (key == "measure_info" and value is None)
            }
            if "Value" in entry:
                item["value"] = entry["Value"]
            if "Name" in entry:
                item["name"] = entry["Name"]
            normalized_entries.append(item)
        normalized[property_id] = normalized_entries
    return normalized


def replace_model_property(product_format_new: Any, model_option: Mapping[str, Any]) -> str:
    """Replace only property 1493 while preserving every other product property."""
    return replace_standard_property(product_format_new, MODEL_PROPERTY_ID, model_option)


def replace_standard_property(product_format_new: Any, property_id: int | str,
                              option: Mapping[str, Any]) -> str:
    """Replace one enum property with a platform-provided value/name pair."""
    name = str(option.get("name", "")).strip()
    value = option.get("value")
    if not name or value in (None, ""):
        raise ProductTransformError("property option must contain name and value")
    key = str(property_id).strip()
    if not key.isdigit():
        raise ProductTransformError("property id must be numeric")

    properties = normalize_product_format_for_edit(parse_product_format(product_format_new))
    properties[key] = [{
        "value": int(value) if str(value).isdigit() else value,
        "name": name,
        "diy_type": 0,
    }]
    return json.dumps(properties, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def build_model_edit_payload(detail: Mapping[str, Any], model_option: Mapping[str, Any]) -> dict:
    """Build the smallest reviewable editV2 body; this function does not send it."""
    product_id = detail.get("product_id_str") or detail.get("product_id")
    if not product_id:
        raise ProductTransformError("product detail has no product_id")
    if "product_format_new" not in detail:
        raise ProductTransformError("product detail has no product_format_new")
    return {
        "product_id": str(product_id),
        "product_format_new": replace_model_property(detail["product_format_new"], model_option),
    }


def build_property_edit_payload(detail: Mapping[str, Any],
                                replacements: Mapping[int | str, Mapping[str, Any]]) -> dict:
    """Build an editV2 payload replacing only reviewed enum properties."""
    product_id = detail.get("product_id_str") or detail.get("product_id")
    if not product_id:
        raise ProductTransformError("product detail has no product_id")
    if "product_format_new" not in detail:
        raise ProductTransformError("product detail has no product_format_new")
    properties = normalize_product_format_for_edit(parse_product_format(detail["product_format_new"]))
    for raw_id, option in replacements.items():
        property_id = str(raw_id).strip()
        name = str(option.get("name") or "").strip()
        value = option.get("value")
        if not property_id.isdigit() or not name or value in (None, ""):
            raise ProductTransformError("property replacement is invalid")
        properties[property_id] = [{
            "value": int(value) if str(value).isdigit() else value,
            "name": name,
            "diy_type": 0,
        }]
    return {
        "product_id": str(product_id),
        "product_format_new": json.dumps(
            properties, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        ),
    }
