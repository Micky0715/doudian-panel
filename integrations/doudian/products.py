"""Validated product read operations for Gaia-to-DouDian reconciliation."""

from __future__ import annotations

from typing import Any, Mapping

from doudian_api import DoudianConfigError

from .gateway import DoudianReadOnlyGateway


LIST_FILTERS = frozenset({
    "status", "check_status", "product_type", "start_time", "end_time",
    "update_start_time", "update_end_time", "store_id", "name", "product_id",
    "use_cursor", "cursor_id", "can_combine_product", "lookup_option",
    "need_rectification_info", "query_options", "need_check_out", "sku_codes",
})


class DoudianProductReader:
    def __init__(self, gateway: DoudianReadOnlyGateway):
        self.gateway = gateway

    def list_products(self, *, page: int = 1, size: int = 20, **filters) -> Mapping[str, Any]:
        unknown = sorted(set(filters) - LIST_FILTERS)
        if unknown:
            raise DoudianConfigError("unsupported product.listV2 filters: " + ", ".join(unknown))
        if int(page) < 1 or not 1 <= int(size) <= 100:
            raise DoudianConfigError("page must be >= 1 and size must be between 1 and 100")
        params = {"page": int(page), "size": int(size)}
        params.update({key: value for key, value in filters.items() if value is not None})
        return self._data_dict(self.gateway.call("product.listV2", params))

    def get_product_detail(self, product_id: str | int, *, show_draft: bool = True,
                           store_id: str | int | None = None) -> Mapping[str, Any]:
        product = str(product_id).strip()
        if not product.isdigit():
            raise DoudianConfigError("product id must be numeric")
        params: dict[str, Any] = {
            "product_id": product,
            "show_draft": "true" if show_draft else "false",
        }
        if store_id is not None:
            params["store_id"] = str(store_id)
        return self._data_dict(self.gateway.call("product.detail", params))

    def get_sku_detail(self, *, sku_id: str | int | None = None,
                       code: str | None = None) -> Mapping[str, Any]:
        params: dict[str, Any] = {}
        if sku_id is not None:
            value = str(sku_id).strip()
            if not value.isdigit():
                raise DoudianConfigError("sku id must be numeric")
            params["sku_id"] = value
        if code:
            params["code"] = str(code).strip()
        if len(params) != 1:
            raise DoudianConfigError("sku.detail requires exactly one of sku_id or code")
        return self._data_dict(self.gateway.call("sku.detail", params))

    @staticmethod
    def _data_dict(response: Mapping[str, Any]) -> Mapping[str, Any]:
        data = response.get("data", {})
        return data if isinstance(data, dict) else {}
