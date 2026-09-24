"""Read-only DouDian Open API client used by the API migration PoC."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


API_BASE_URL = "https://openapi-fxg.jinritemai.com"
SECOND_HAND_PHONE_CATEGORY_ID = 1000007628
MODEL_PROPERTY_ID = 1493


class DoudianConfigError(ValueError):
    pass


class DoudianAPIError(RuntimeError):
    def __init__(self, method: str, response: Mapping[str, Any]):
        self.method = method
        self.code = response.get("code")
        self.sub_code = response.get("sub_code", "")
        message = response.get("sub_msg") or response.get("msg") or "unknown API error"
        self.message = str(message)
        super().__init__(
            f"{method} failed: code={self.code} sub_code={self.sub_code} message={message}"
        )


@dataclass(frozen=True, repr=False)
class DoudianConfig:
    app_key: str
    app_secret: str
    shop_tokens: Mapping[str, str]
    shop_refresh_tokens: Mapping[str, str]
    app_type: str
    shop_token_meta: Mapping[str, Mapping[str, Any]]

    def __repr__(self) -> str:
        return (
            f"DoudianConfig(app_key=<redacted>, app_type={self.app_type!r}, "
            f"shops={len(self.shop_tokens)})"
        )

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "DoudianConfig":
        required = ("DOUDIAN_APP_KEY", "DOUDIAN_APP_SECRET", "DOUDIAN_SHOP_TOKENS_JSON")
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise DoudianConfigError("missing configuration: " + ", ".join(missing))
        try:
            tokens = json.loads(values["DOUDIAN_SHOP_TOKENS_JSON"])
            refresh = json.loads(values.get("DOUDIAN_SHOP_REFRESH_TOKENS_JSON", "{}") or "{}")
            metadata = json.loads(values.get("DOUDIAN_SHOP_TOKEN_META_JSON", "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise DoudianConfigError("shop token JSON is invalid") from exc
        if not isinstance(tokens, dict) or not tokens:
            raise DoudianConfigError("shop tokens must be a non-empty JSON object")
        if not isinstance(refresh, dict):
            raise DoudianConfigError("refresh tokens must be a JSON object")
        if not isinstance(metadata, dict):
            raise DoudianConfigError("shop token metadata must be a JSON object")
        if any(not str(k).isdigit() or not isinstance(v, str) or not v for k, v in tokens.items()):
            raise DoudianConfigError("shop token mapping contains an invalid entry")
        app_type = str(values.get("DOUDIAN_APP_TYPE") or "self_use").strip().casefold()
        if app_type not in {"self_use", "tool"}:
            raise DoudianConfigError("DOUDIAN_APP_TYPE must be self_use or tool")
        return cls(
            values["DOUDIAN_APP_KEY"], values["DOUDIAN_APP_SECRET"], tokens, refresh,
            app_type, metadata,
        )

    @classmethod
    def from_env_file(cls, path: str | Path) -> "DoudianConfig":
        return cls.from_mapping(read_env_file(path))


def read_env_file(path: str | Path) -> dict[str, str]:
    values = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key.strip()] = value
    return values


def _request_token(app_key: str, app_secret: str, method: str,
                   params: Mapping[str, Any], *, base_url: str = API_BASE_URL,
                   timeout: float = 30.0, opener=None) -> Mapping[str, Any]:
    body = serialize_params(params)
    timestamp = int(time.time())
    sign = calculate_sign(app_key, app_secret, method, timestamp, body)
    query = urllib.parse.urlencode({
        "method": method,
        "app_key": app_key,
        "timestamp": str(timestamp),
        "v": "2",
        "sign": sign,
        "sign_method": "hmac-sha256",
    })
    request = urllib.request.Request(
        base_url.rstrip("/") + "/" + method.replace(".", "/") + "?" + query,
        data=body.encode("utf-8"),
        headers={"Accept": "application/json", "Content-Type": "application/json;charset=UTF-8"},
        method="POST",
    )
    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 10000:
        raise DoudianAPIError(method, result)
    data = result.get("data") or {}
    if not data.get("access_token") or not data.get("refresh_token"):
        raise DoudianConfigError(f"{method} response has no token pair")
    return data


def create_self_use_access_token(app_key: str, app_secret: str, shop_id: str | int, *,
                                 base_url: str = API_BASE_URL, timeout: float = 30.0,
                                 opener=None) -> Mapping[str, Any]:
    """Create a token pair for a self-use application bound to one shop."""
    shop = str(shop_id).strip()
    if not shop.isdigit():
        raise DoudianConfigError("shop id must be numeric")
    return _request_token(
        app_key,
        app_secret,
        "token.create",
        {"code": "", "grant_type": "authorization_self", "shop_id": shop},
        base_url=base_url,
        timeout=timeout,
        opener=opener,
    )


def refresh_access_token(app_key: str, app_secret: str, refresh_token: str, *,
                         base_url: str = API_BASE_URL, timeout: float = 30.0,
                         opener=None) -> Mapping[str, Any]:
    """Refresh one tool-application token pair through token.refresh."""
    return _request_token(
        app_key,
        app_secret,
        "token.refresh",
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        base_url=base_url,
        timeout=timeout,
        opener=opener,
    )


def canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: canonicalize(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonicalize(item) for item in value]
    return value


def serialize_params(params: Mapping[str, Any]) -> str:
    return json.dumps(canonicalize(dict(params)), ensure_ascii=False, separators=(",", ":"))


def calculate_sign(app_key: str, app_secret: str, method: str,
                   timestamp: int | str, param_json: str) -> str:
    pattern = (
        f"app_key{app_key}method{method}param_json{param_json}"
        f"timestamp{timestamp}v2"
    )
    payload = f"{app_secret}{pattern}{app_secret}"
    return hmac.new(app_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class DoudianClient:
    def __init__(self, config: DoudianConfig, shop_id: str, *,
                 base_url: str = API_BASE_URL, timeout: float = 30.0, opener=None):
        self.shop_id = str(shop_id)
        if self.shop_id not in config.shop_tokens:
            raise DoudianConfigError(f"no access token configured for shop {self.shop_id}")
        self._config = config
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def call(self, method: str, params: Mapping[str, Any], *,
             headers: Mapping[str, str] | None = None) -> Mapping[str, Any]:
        """Call one Open API method.

        ``headers`` is deliberately an additive escape hatch for the few
        platform SDK-backed gateways (currently material binary upload).  The
        business parameters and the signed request shape remain identical to
        ordinary Open API calls; callers cannot replace authentication or the
        JSON body through this argument.
        """
        body = serialize_params(params)
        timestamp = int(time.time())
        sign = calculate_sign(
            self._config.app_key, self._config.app_secret, method, timestamp, body
        )
        query = urllib.parse.urlencode({
            "method": method,
            "app_key": self._config.app_key,
            "access_token": self._config.shop_tokens[self.shop_id],
            "timestamp": str(timestamp),
            "v": "2",
            "sign": sign,
            "sign_method": "hmac-sha256",
        })
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json;charset=UTF-8",
        }
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})
        request = urllib.request.Request(
            f"{self.base_url}/{method.replace('.', '/')}?{query}",
            data=body.encode("utf-8"),
            headers=request_headers,
            method="POST",
        )
        with self._opener(request, timeout=self.timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if result.get("code") != 10000:
            raise DoudianAPIError(method, result)
        return result

    def get_shop_categories(self, parent_id: int = 0) -> list[Mapping[str, Any]]:
        data = self.call("shop.getShopCategory", {"cid": int(parent_id)}).get("data", [])
        return data if isinstance(data, list) else []

    def get_category_properties(self, category_leaf_id: int = SECOND_HAND_PHONE_CATEGORY_ID
                                ) -> list[Mapping[str, Any]]:
        data = self.call(
            "product.getCatePropertyV2", {"category_leaf_id": int(category_leaf_id)}
        ).get("data", {})
        if isinstance(data, dict):
            data = data.get("data", [])
        return data if isinstance(data, list) else []

    def list_products(self, page: int = 1, size: int = 50, **filters) -> Mapping[str, Any]:
        """Read products through the auth2-compatible V2 endpoint."""
        params = {"page": int(page), "size": int(size)}
        params.update({key: value for key, value in filters.items() if value is not None})
        data = self.call("product.listV2", params).get(
            "data", {}
        )
        return data if isinstance(data, dict) else {}

    def get_product_detail(self, product_id: str | int, *, show_draft: bool = False,
                           store_id: str | int | None = None) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "product_id": str(product_id),
            "show_draft": "true" if show_draft else "false",
        }
        if store_id is not None:
            params["store_id"] = str(store_id)
        data = self.call("product.detail", params).get("data", {})
        return data if isinstance(data, dict) else {}

    def get_sku_detail(self, *, sku_id: str | int | None = None,
                       code: str | None = None) -> Mapping[str, Any]:
        params: dict[str, Any] = {}
        if sku_id is not None:
            params["sku_id"] = str(sku_id)
        if code:
            params["code"] = str(code)
        if len(params) != 1:
            raise DoudianConfigError("sku.detail requires exactly one of sku_id or code")
        data = self.call("sku.detail", params).get("data", {})
        return data if isinstance(data, dict) else {}


def find_property(properties, property_id: int):
    return next(
        (
            item
            for item in properties
            if str(item.get("property_id", item.get("id"))) == str(property_id)
        ),
        None,
    )


def find_exact_option(prop: Mapping[str, Any], value_name: str):
    expected = value_name.strip().casefold()
    return next(
        (item for item in prop.get("options", [])
         if str(item.get("name", "")).strip().casefold() == expected),
        None,
    )


def normalize_model_name(value: Any, brand_names=()) -> str:
    """Build a conservative key while retaining meaningful SKU suffixes."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    for brand in sorted((str(x) for x in brand_names if x), key=len, reverse=True):
        token = unicodedata.normalize("NFKC", brand).strip().casefold()
        text = re.sub(rf"^\s*{re.escape(token)}\s*", "", text)
    # Ignore platform formatting only. Keep +, 5G, Pro, Max and edition text.
    return re.sub(r"[\s()（）·._/／\-]+", "", text)


def _literal_model_name(value: Any) -> str:
    """Normalise Unicode and repeated whitespace while preserving case.

    Platform enums occasionally contain several IDs whose labels differ only
    by case or a missing word boundary.  The operator/official spelling is the
    only deterministic tie-breaker before broader case-insensitive matching.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def resolve_model_option(prop: Mapping[str, Any], value_name: str, *, brand_names=()):
    """Resolve one unambiguous option without dangerous substring matching."""
    literal_expected = _literal_model_name(value_name)
    literal_matches = [
        option for option in prop.get("options", [])
        if _literal_model_name(option.get("name", "")) == literal_expected
    ]
    if literal_expected and len(literal_matches) == 1:
        return literal_matches[0]
    raw_expected = literal_expected.casefold()
    raw_matches = [
        option for option in prop.get("options", [])
        if _literal_model_name(option.get("name", "")).casefold() == raw_expected
    ]
    # Prefer the platform's exact spelling when normalized aliases collide
    # (e.g. "iPhone 14 Pro" and the legacy duplicate "iPhone 14pro").
    if len(raw_matches) == 1:
        return raw_matches[0]
    expected = normalize_model_name(value_name, brand_names)
    if not expected:
        return None
    matches = [
        option for option in prop.get("options", [])
        if normalize_model_name(option.get("name", ""), brand_names) == expected
    ]
    return matches[0] if len(matches) == 1 else None
