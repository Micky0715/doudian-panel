"""Single data-backed source of truth for brand/model/image-boundary rules."""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

RULES_PATH = Path(__file__).with_name("brand_model_rules.json")


@lru_cache(maxsize=1)
def load_rules() -> dict[str, Any]:
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported brand/model rules schema")
    for key in ("platform_brands", "catalogue_groups", "model_exact_overrides", "image_brand_boundaries"):
        if not isinstance(payload.get(key), dict):
            raise ValueError("missing rules section: %s" % key)
    return payload


def platform_brand_map() -> dict[str, str]:
    return {name: row["value"] for name, row in load_rules()["platform_brands"].items()}


def platform_brand_key(value: str) -> str:
    """Return the platform identity without merging independent sub-brands."""
    return _platform_brand_key(value)


def platform_brand_aliases(value: str) -> tuple[str, ...]:
    """Return reviewed spellings for one platform brand identity."""
    key = _platform_brand_key(value)
    row = load_rules()["platform_brands"].get(key)
    if not row:
        return (str(value or "").strip(),)
    aliases = (key, row.get("value", ""), *row.get("aliases", []))
    return tuple(dict.fromkeys(str(item).strip() for item in aliases if str(item).strip()))


def catalogue_groups() -> dict[str, set[str]]:
    return {name: set(values) for name, values in load_rules()["catalogue_groups"].items()}


def model_exact_overrides() -> dict[str, str]:
    return dict(load_rules()["model_exact_overrides"])


def platform_brand_for_title(title: str) -> str:
    text = str(title or "")
    lowered = text.casefold()
    brands = load_rules()["platform_brands"]
    # 子品牌必须先于父品牌判定；独立身份来自规则数据，不写业务特判。
    ordered = sorted(brands.items(), key=lambda item: bool(item[1].get("independent_from")), reverse=True)
    for name, row in ordered:
        if any(re.search(r"(?<![a-z])" + re.escape(alias.casefold()) + r"(?![a-z])", lowered)
               for alias in row.get("aliases", [])):
            return name
    return ""


def image_directory_allowed(brand: str, top_level_dir: str) -> bool:
    brand_key = _platform_brand_key(brand)
    allowed = load_rules()["image_brand_boundaries"].get(brand_key, [])
    normalized = re.sub(r"\s+", "", str(top_level_dir or "")).casefold()
    return normalized in {re.sub(r"\s+", "", value).casefold() for value in allowed}


def image_leaf_allowed(brand: str, leaf_name: str) -> bool:
    """Whether a legacy root-level model folder starts with an allowed brand."""
    normalized = re.sub(r"\s+", "", str(leaf_name or "")).casefold()
    allowed = load_rules()["image_brand_boundaries"].get(_platform_brand_key(brand), [])
    return any(normalized.startswith(re.sub(r"\s+", "", value).casefold()) for value in allowed)


def image_model_leaf_allowed(brand: str, leaf_name: str) -> bool:
    """Reject any model leaf carrying a different explicit platform brand.

    Operator libraries contain both neutral leaves such as ``X200`` and
    brand-prefixed leaves such as ``vivo X200``.  A neutral leaf is still
    eligible after its parent path has been scoped, but an explicit prefix is
    identity evidence and must agree with the requested brand.  This applies
    to every reviewed platform brand, not only the historical Xiaomi/Redmi
    split: Xiaomi ``15`` must never resolve to ``OnePlus 15`` or ``iQOO 15``
    merely because their model keys all reduce to ``15``.

    Prefixes are compared after presentation normalisation.  When aliases
    overlap, the longest alias wins (for example ``REALMERRY`` must not be
    classified as the shorter ``realme`` prefix).
    """
    brand_key = _platform_brand_key(brand)
    normalized = re.sub(
        r"[\s._\-/·，,（）()]+", "", str(leaf_name or ""),
    ).casefold()
    matches: list[tuple[int, str]] = []
    for candidate, row in load_rules()["platform_brands"].items():
        aliases = (candidate, *row.get("aliases", []))
        for alias in aliases:
            prefix = re.sub(
                r"[\s._\-/·，,（）()]+", "", str(alias or ""),
            ).casefold()
            if prefix and normalized.startswith(prefix):
                matches.append((len(prefix), candidate))
    if not matches:
        return True
    longest = max(length for length, _candidate in matches)
    explicit_brands = {
        candidate for length, candidate in matches if length == longest
    }
    return brand_key in explicit_brands


def _platform_brand_key(value: str) -> str:
    normalized = re.sub(r"\s+", "", str(value or "")).casefold()
    for name, row in load_rules()["platform_brands"].items():
        choices = [name, row.get("value", "")] + list(row.get("aliases", []))
        if normalized in {re.sub(r"\s+", "", str(item)).casefold() for item in choices}:
            return name
    return str(value or "")
