"""Atomic multi-shop token storage without exposing credential values."""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from doudian_api import DoudianConfig, DoudianConfigError, read_env_file

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback for copied deployments
    fcntl = None


ACCESS_KEY = "DOUDIAN_SHOP_TOKENS_JSON"
REFRESH_KEY = "DOUDIAN_SHOP_REFRESH_TOKENS_JSON"
META_KEY = "DOUDIAN_SHOP_TOKEN_META_JSON"
DOCUMENTED_REFRESH_LIFETIME_SECONDS = 14 * 24 * 60 * 60


class EnvTokenStore:
    """Read and atomically update only the two token mappings in an env file."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()

    def load_config(self) -> DoudianConfig:
        return DoudianConfig.from_mapping(read_env_file(self.path))

    @contextmanager
    def _locked(self):
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def update_shop_tokens(self, shop_id: str | int, access_token: str,
                           refresh_token: str, *, token_data: Mapping[str, Any] | None = None,
                           issued_at: int | None = None) -> None:
        shop = str(shop_id).strip()
        if not shop.isdigit() or not access_token or not refresh_token:
            raise DoudianConfigError("invalid shop token update")

        with self._locked():
            self._update_shop_tokens_unlocked(
                shop, access_token, refresh_token,
                token_data=token_data, issued_at=issued_at,
            )

    def recreate_self_use_shop(
        self,
        shop_id: str | int,
        expected_access_token: str | None,
        create_fn: Callable[[DoudianConfig, str], Mapping[str, Any]],
    ) -> tuple[DoudianConfig, bool]:
        """Recreate one self-use shop token, serialized across local workers."""
        shop = str(shop_id).strip()
        with self._locked():
            latest = self.load_config()
            current_access = latest.shop_tokens.get(shop)
            if current_access and current_access != expected_access_token:
                return latest, False
            data = create_fn(latest, shop)
            returned_shop = str(data.get("shop_id") or "").strip()
            if returned_shop != shop:
                raise DoudianConfigError(
                    f"token.create returned unexpected shop id {returned_shop or '<missing>'}"
                )
            access_token = str(data.get("access_token") or "")
            refresh_token = str(data.get("refresh_token") or "")
            if not access_token or not refresh_token:
                raise DoudianConfigError("token.create response has no token pair")
            self._update_shop_tokens_unlocked(
                shop, access_token, refresh_token, token_data=data,
            )
            return self.load_config(), True

    def refresh_shop(
        self,
        shop_id: str | int,
        expected_access_token: str | None,
        refresh_fn: Callable[[DoudianConfig, str], Mapping[str, Any]],
    ) -> tuple[DoudianConfig, bool]:
        """Refresh at most once across concurrent workers using this env file.

        Returns ``(latest_config, refreshed_here)``. If another process already
        rotated the access token, no refresh call is made.
        """
        shop = str(shop_id).strip()
        with self._locked():
            latest = self.load_config()
            current_access = latest.shop_tokens.get(shop)
            if current_access and current_access != expected_access_token:
                return latest, False
            refresh_token = latest.shop_refresh_tokens.get(shop)
            if not refresh_token:
                raise DoudianConfigError(f"shop {shop} has no refresh token")
            data = refresh_fn(latest, refresh_token)
            access_token = str(data.get("access_token") or "")
            next_refresh = str(data.get("refresh_token") or "")
            if not access_token or not next_refresh:
                raise DoudianConfigError("token.refresh response has no token pair")
            self._update_shop_tokens_unlocked(
                shop, access_token, next_refresh, token_data=data,
            )
            return self.load_config(), True

    def _update_shop_tokens_unlocked(self, shop: str, access_token: str,
                                     refresh_token: str, *,
                                     token_data: Mapping[str, Any] | None = None,
                                     issued_at: int | None = None) -> None:
        values = read_env_file(self.path)
        try:
            access = json.loads(values.get(ACCESS_KEY, "{}") or "{}")
            refresh = json.loads(values.get(REFRESH_KEY, "{}") or "{}")
            metadata = json.loads(values.get(META_KEY, "{}") or "{}")
        except json.JSONDecodeError as exc:
            raise DoudianConfigError("shop token JSON is invalid") from exc
        if not isinstance(access, dict) or not isinstance(refresh, dict) or not isinstance(metadata, dict):
            raise DoudianConfigError("shop token mappings must be JSON objects")

        access[shop] = str(access_token)
        refresh[shop] = str(refresh_token)
        if token_data is not None:
            now = int(issued_at if issued_at is not None else time.time())
            try:
                expires_in = max(0, int(token_data.get("expires_in") or 0))
            except (TypeError, ValueError) as exc:
                raise DoudianConfigError("token expires_in is invalid") from exc
            refresh_expires_raw = token_data.get("refresh_expires_in")
            try:
                refresh_expires_in = (
                    max(0, int(refresh_expires_raw))
                    if refresh_expires_raw is not None
                    else DOCUMENTED_REFRESH_LIFETIME_SECONDS
                )
            except (TypeError, ValueError) as exc:
                raise DoudianConfigError("refresh token expiry is invalid") from exc
            metadata[shop] = {
                "issued_at": now,
                "expires_in": expires_in,
                "access_expires_at": now + expires_in,
                "refresh_expires_in": refresh_expires_in,
                "refresh_expires_at": now + refresh_expires_in,
                "shop_name": str(token_data.get("shop_name") or ""),
            }
        replacements = {
            ACCESS_KEY: json.dumps(access, ensure_ascii=False, separators=(",", ":")),
            REFRESH_KEY: json.dumps(refresh, ensure_ascii=False, separators=(",", ":")),
            META_KEY: json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        }
        original = self.path.read_text(encoding="utf-8-sig")
        rendered = self._replace_values(original, replacements)
        self._atomic_write(rendered)

    @staticmethod
    def _replace_values(original: str, replacements: dict[str, str]) -> str:
        lines = original.splitlines(keepends=True)
        seen: set[str] = set()
        output: list[str] = []
        for line in lines:
            stripped = line.lstrip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key in replacements and not stripped.startswith("#"):
                ending = "\r\n" if line.endswith("\r\n") else "\n"
                output.append(f"{key}={replacements[key]}{ending}")
                seen.add(key)
            else:
                output.append(line)
        if output and not output[-1].endswith(("\n", "\r")):
            output[-1] += "\n"
        for key, value in replacements.items():
            if key not in seen:
                output.append(f"{key}={value}\n")
        return "".join(output)

    def _atomic_write(self, content: str) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
