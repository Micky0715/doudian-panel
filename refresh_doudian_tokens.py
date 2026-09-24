#!/usr/bin/env python3
"""Refresh configured DouDian shop tokens without printing secret values.

This remains a manual maintenance command. Normal read calls use the narrower
single-shop refresh path in ``integrations.doudian``.
"""

import os
from pathlib import Path

from doudian_api import create_self_use_access_token, refresh_access_token
from integrations.doudian import EnvTokenStore


def main():
    path = Path(os.environ.get("DP_ENV_FILE") or Path(__file__).with_name("env.local"))
    store = EnvTokenStore(path)
    config = store.load_config()
    updated = 0
    for shop_id in sorted(config.shop_tokens):
        if config.app_type == "tool" and not config.shop_refresh_tokens.get(shop_id):
            print("%s SKIP no_refresh_token" % shop_id)
            continue
        try:
            if config.app_type == "self_use":
                _, refreshed = store.recreate_self_use_shop(
                    shop_id,
                    config.shop_tokens.get(shop_id),
                    lambda latest, current_shop: create_self_use_access_token(
                        latest.app_key, latest.app_secret, current_shop
                    ),
                )
            else:
                _, refreshed = store.refresh_shop(
                    shop_id,
                    config.shop_tokens.get(shop_id),
                    lambda latest, token: refresh_access_token(
                        latest.app_key, latest.app_secret, token
                    ),
                )
            updated += int(refreshed)
            print("%s %s" % (shop_id, "REFRESH_OK" if refreshed else "ALREADY_REFRESHED"))
        except Exception as exc:
            print("%s REFRESH_FAIL %s" % (shop_id, str(exc)[:180]))
    if not updated:
        raise SystemExit("no token was refreshed; env file unchanged")
    print("UPDATED %d/%d" % (updated, len(config.shop_tokens)))


if __name__ == "__main__":
    main()
