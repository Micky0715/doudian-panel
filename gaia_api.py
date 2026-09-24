"""Read-only Gaia API gateway client and brand/model catalogue helpers."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


GAIA_PROD_BASE_URL = "https://pp-components.jd.com"
GAIA_GATEWAY_PATH = "/api-gateway/api"
STOCK_API_NAME = "sale.bigdata.itemgoods.stock.data"
PRICE_API_NAME = "sale.bigdata.goods.price.info.detail"
DEFAULT_REQUEST_ID = "0ccd9bcc-9207-44da-b304-92cdfcb7bfd7"


class GaiaConfigError(ValueError):
    pass


class GaiaAPIError(RuntimeError):
    def __init__(self, api_name: str, code: Any, message: Any):
        self.api_name = api_name
        self.code = code
        super().__init__(f"{api_name} failed: code={code} message={message or 'unknown error'}")


@dataclass(frozen=True, repr=False)
class GaiaConfig:
    base_url: str
    user: str
    stock_token: str = ""
    price_token: str = ""

    def __repr__(self) -> str:
        return f"GaiaConfig(base_url={self.base_url!r}, user={self.user!r}, tokens=<redacted>)"

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "GaiaConfig":
        base_url = values.get("GAIA_API_GATEWAY_PROD_BASE_URL", GAIA_PROD_BASE_URL).rstrip("/")
        user = values.get("GAIA_API_GATEWAY_USER", "").strip()
        if not user:
            raise GaiaConfigError("missing configuration: GAIA_API_GATEWAY_USER")
        return cls(
            base_url=base_url,
            user=user,
            stock_token=values.get("GAIA_API_GATEWAY_AUTH_TOKEN_PROD", "").strip(),
            price_token=values.get("GAIA_API_GATEWAY_PRICE_AUTH_TOKEN_PROD", "").strip(),
        )


def _as_object(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GaiaAPIError("gateway", "invalid_backend_response", "backendResponse is not JSON") from exc
        if isinstance(decoded, Mapping):
            return decoded
    raise GaiaAPIError("gateway", "invalid_backend_response", "backendResponse is not an object")


class GaiaClient:
    def __init__(self, config: GaiaConfig, *, timeout: float = 60.0, opener=None):
        self.config = config
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def call(self, api_name: str, auth_token: str, json_params: Mapping[str, Any]) -> Mapping[str, Any]:
        if not auth_token:
            raise GaiaConfigError(f"no auth token configured for {api_name}")
        form = urllib.parse.urlencode({
            "apiName": api_name,
            "authToken": auth_token,
            "jsonParams": json.dumps(dict(json_params), ensure_ascii=False, separators=(",", ":")),
            "user": self.config.user,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.config.base_url + GAIA_GATEWAY_PATH,
            data=form,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with self._opener(request, timeout=self.timeout) as response:
            outer = json.loads(response.read().decode("utf-8"))
        if outer.get("code") != 200 or outer.get("success") is False:
            raise GaiaAPIError(api_name, outer.get("code"), outer.get("message"))
        backend = _as_object(outer.get("backendResponse", outer))
        if backend.get("status") != 200:
            raise GaiaAPIError(api_name, backend.get("status"), backend.get("message"))
        return backend

    def iter_stock(self, *, category: str = "手机", page_size: int = 5000,
                   request_id: str = DEFAULT_REQUEST_ID, max_pages: int | None = None):
        page = 0
        while max_pages is None or page < max_pages:
            payload = self.call(STOCK_API_NAME, self.config.stock_token, {
                "requestId": request_id,
                "pageNumber": page,
                "pageSize": min(max(int(page_size), 1), 5000),
                "params": {"cate3Name": category},
            })
            content = payload.get("content") or []
            if not isinstance(content, list):
                raise GaiaAPIError(STOCK_API_NAME, "invalid_content", "content is not a list")
            if not content:
                break
            yield from content
            page += 1

    def iter_price_detail(self, *, data_type: str = "出库", start_date: str = "",
                          end_date: str = "", channel: str = "", page_size: int = 5000,
                          request_id: str = DEFAULT_REQUEST_ID, max_pages: int | None = None):
        subs = {"dataType": data_type}
        if start_date:
            subs["startDate"] = start_date
        if end_date:
            subs["endDate"] = end_date
        if channel:
            subs["yxChannelName"] = channel
        page = 0
        while max_pages is None or page < max_pages:
            payload = self.call(PRICE_API_NAME, self.config.price_token, {
                "requestId": request_id,
                "pageNumber": page,
                "pageSize": min(max(int(page_size), 1), 5000),
                "params": {},
                "stringSubs": subs,
            })
            content = payload.get("content") or []
            if not isinstance(content, list):
                raise GaiaAPIError(PRICE_API_NAME, "invalid_content", "content is not a list")
            if not content:
                break
            yield from content
            page += 1


def build_model_catalogue(rows: Iterable[Mapping[str, Any]], source: str) -> list[dict[str, str]]:
    """Return unique source-preserving brand/model pairs from either documented API."""
    if source not in {"stock", "price"}:
        raise ValueError("source must be 'stock' or 'price'")
    brand_key, model_key = ("品牌名称", "型号名称") if source == "stock" else ("品牌", "型号")
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        brand = str(row.get(brand_key, "") or "").strip()
        model = str(row.get(model_key, "") or "").strip()
        if not model:
            continue
        key = (brand.casefold(), model.casefold())
        unique.setdefault(key, {"brand": brand, "model": model, "source": source})
    return sorted(unique.values(), key=lambda item: (item["brand"].casefold(), item["model"].casefold()))
