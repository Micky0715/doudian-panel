"""Read-only API gateway with one-shop token refresh and one retry."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Mapping

from doudian_api import (
    DoudianAPIError,
    DoudianClient,
    DoudianConfig,
    DoudianConfigError,
    create_self_use_access_token,
    refresh_access_token,
)

from .token_store import EnvTokenStore


READ_ONLY_METHODS = frozenset({
    "product.listV2",
    "product.detail",
    "sku.detail",
    "product.getCatePropertyV2",
    "product.getProductUpdateRule",
    "product.getRecommendName",
})


class ReadOnlyBoundaryError(PermissionError):
    pass


class TokenRefreshError(RuntimeError):
    pass


def is_access_token_expired(error: DoudianAPIError) -> bool:
    detail = f"{error.sub_code} {error.message}".casefold()
    markers = (
        "access-token-expired",
        "access token expired",
        "access_token expired",
        "access_token已过期",
        "access token已过期",
        "授权已过期",
    )
    return str(error.code) == "40003" and any(marker in detail for marker in markers)


class DoudianReadOnlyGateway:
    """The only allowed operations here are explicitly allowlisted read calls."""

    def __init__(
        self,
        env_path: str | Path,
        shop_id: str | int,
        *,
        client_factory: Callable[[DoudianConfig, str], DoudianClient] | None = None,
        refresh_fn: Callable[..., Mapping[str, Any]] = refresh_access_token,
        create_self_use_fn: Callable[..., Mapping[str, Any]] = create_self_use_access_token,
        clock: Callable[[], float] = time.time,
    ):
        self.shop_id = str(shop_id).strip()
        if not self.shop_id.isdigit():
            raise DoudianConfigError("shop id must be numeric")
        self.store = EnvTokenStore(env_path)
        self._client_factory = client_factory or (lambda config, shop: DoudianClient(config, shop))
        self._refresh_fn = refresh_fn
        self._create_self_use_fn = create_self_use_fn
        self._clock = clock
        self._config = self.store.load_config()
        self._client = self._client_factory(self._config, self.shop_id)

    def call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        if method not in READ_ONLY_METHODS:
            raise ReadOnlyBoundaryError(f"DouDian method is not allowed in read-only phase: {method}")
        renewal_error: TokenRefreshError | None = None
        renewal_attempted = False
        if self._needs_proactive_renewal():
            renewal_attempted = True
            try:
                self._renew_or_reload()
            except TokenRefreshError as exc:
                # The old token has up to one hour of overlap. Try the read once.
                renewal_error = exc
        try:
            return self._client.call(method, dict(params))
        except DoudianAPIError as exc:
            if not is_access_token_expired(exc):
                raise
            if renewal_attempted:
                if renewal_error is not None:
                    raise renewal_error from exc
                raise
        self._renew_or_reload()
        # Exactly one retry. A second failure is surfaced to the caller.
        return self._client.call(method, dict(params))

    def _needs_proactive_renewal(self) -> bool:
        meta = self._config.shop_token_meta.get(self.shop_id)
        if not isinstance(meta, Mapping):
            return False
        try:
            expires_at = int(meta.get("access_expires_at") or 0)
        except (TypeError, ValueError):
            return False
        return bool(expires_at) and self._clock() >= expires_at - 3600

    def _renew_or_reload(self) -> None:
        previous_access = self._config.shop_tokens.get(self.shop_id)
        try:
            if self._config.app_type == "self_use":
                latest, _ = self.store.recreate_self_use_shop(
                    self.shop_id,
                    previous_access,
                    lambda config, shop: self._create_self_use_fn(
                        config.app_key, config.app_secret, shop
                    ),
                )
            else:
                latest, _ = self.store.refresh_shop(
                    self.shop_id,
                    previous_access,
                    lambda config, refresh_token: self._refresh_fn(
                        config.app_key, config.app_secret, refresh_token
                    ),
                )
        except Exception as exc:
            if isinstance(exc, TokenRefreshError):
                raise
            raise TokenRefreshError(f"shop {self.shop_id} token renewal failed: {exc}") from exc
        self._config = latest
        self._client = self._client_factory(latest, self.shop_id)
