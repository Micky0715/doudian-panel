"""Strict local property catalogue; never guesses an option."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping


DEFAULT_PHONE_PROPERTY_CATALOGUE = Path(__file__).with_name("phone_properties.json")


class PropertyCatalogueError(ValueError):
    pass


class PhonePropertyCatalogue:
    def __init__(self, data: Mapping[str, Any]):
        self._data = dict(data)
        category = self._data.get("category")
        properties = self._data.get("properties")
        if not isinstance(category, Mapping) or not str(category.get("id") or "").isdigit():
            raise PropertyCatalogueError("catalogue category id is invalid")
        if not isinstance(properties, Mapping):
            raise PropertyCatalogueError("catalogue properties must be an object")

    @classmethod
    def from_file(
        cls, path: str | Path = DEFAULT_PHONE_PROPERTY_CATALOGUE,
    ) -> "PhonePropertyCatalogue":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PropertyCatalogueError(f"cannot read property catalogue: {exc}") from exc
        if not isinstance(data, Mapping):
            raise PropertyCatalogueError("property catalogue root must be an object")
        return cls(data)

    @property
    def category_id(self) -> int:
        return int(self._data["category"]["id"])

    def get_property(self, key: str) -> Mapping[str, Any]:
        value = self._data["properties"].get(str(key))
        if not isinstance(value, Mapping):
            raise PropertyCatalogueError(f"unknown property key: {key}")
        return value

    def resolve_option(self, property_key: str, option_name: str) -> Mapping[str, Any]:
        """Resolve by exact normalized name; similar text is intentionally rejected."""
        expected = " ".join(str(option_name).split()).casefold()
        if not expected:
            raise PropertyCatalogueError("option name must not be empty")
        prop = self.get_property(property_key)
        options = prop.get("options")
        if not isinstance(options, list):
            raise PropertyCatalogueError(f"property {property_key} has no options")
        matches = [
            option for option in options
            if isinstance(option, Mapping)
            and " ".join(str(option.get("name") or "").split()).casefold() == expected
        ]
        if len(matches) != 1:
            raise PropertyCatalogueError(
                f"property {property_key} option must resolve exactly once: {option_name}"
            )
        return matches[0]

    def build_product_format_selection(
        self, property_key: str, option_name: str,
    ) -> dict[str, list[dict[str, Any]]]:
        """Build pure data for later reviewed writes; this function performs no API call."""
        prop = self.get_property(property_key)
        option = self.resolve_option(property_key, option_name)
        property_id = prop.get("property_id")
        value_id = option.get("value_id")
        if not str(property_id or "").isdigit() or not str(value_id or "").isdigit():
            raise PropertyCatalogueError("property or option id is invalid")
        return {
            str(property_id): [{
                "value": int(value_id),
                "name": str(option["name"]),
                "diy_type": 0,
            }]
        }
