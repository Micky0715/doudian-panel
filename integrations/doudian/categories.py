"""Read-only category metadata operations for DouDian."""

from __future__ import annotations

from typing import Any, Mapping

from doudian_api import DoudianConfigError

from .gateway import DoudianReadOnlyGateway


class DoudianCategoryReader:
    """Read category properties with a per-process cache.

    The cache avoids repeating the same platform request for every product in a
    batch. ``refresh=True`` is reserved for explicit catalogue refresh jobs.
    """

    def __init__(self, gateway: DoudianReadOnlyGateway):
        self.gateway = gateway
        self._properties: dict[str, tuple[Mapping[str, Any], ...]] = {}

    def get_properties(
        self, category_leaf_id: str | int, *, refresh: bool = False,
    ) -> tuple[Mapping[str, Any], ...]:
        category = str(category_leaf_id).strip()
        if not category.isdigit():
            raise DoudianConfigError("category leaf id must be numeric")
        if not refresh and category in self._properties:
            return self._properties[category]

        response = self.gateway.call(
            "product.getCatePropertyV2", {"category_leaf_id": int(category)},
        )
        data: Any = response.get("data", [])
        if isinstance(data, Mapping):
            data = data.get("data", data.get("properties", []))
        if not isinstance(data, list):
            raise DoudianConfigError("category property response must contain a list")
        properties = tuple(item for item in data if isinstance(item, Mapping))
        self._properties[category] = properties
        return properties

    def find_property(
        self, category_leaf_id: str | int, property_name: str, *, refresh: bool = False,
    ) -> Mapping[str, Any] | None:
        expected = str(property_name).strip()
        if not expected:
            raise DoudianConfigError("property name must not be empty")
        matches = [
            item for item in self.get_properties(category_leaf_id, refresh=refresh)
            if str(item.get("property_name") or item.get("name") or "").strip() == expected
        ]
        if len(matches) > 1:
            raise DoudianConfigError(f"duplicate category property name: {expected}")
        return matches[0] if matches else None
