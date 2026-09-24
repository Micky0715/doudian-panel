"""Create missing self-use DouDian shop tokens without printing secrets."""

from __future__ import annotations

import argparse
from pathlib import Path

from doudian_api import create_self_use_access_token
from integrations.doudian.token_store import EnvTokenStore


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="为缺失店铺创建抖店自用型授权")
    parser.add_argument("shop_ids", nargs="+")
    parser.add_argument("--env", default=str(root / "env.local"))
    args = parser.parse_args()

    store = EnvTokenStore(args.env)
    config = store.load_config()
    if config.app_type != "self_use":
        raise SystemExit("当前应用不是自用型，拒绝调用 authorization_self")

    failed = 0
    for raw_shop_id in args.shop_ids:
        shop_id = str(raw_shop_id).strip()
        if not shop_id.isdigit():
            print(f"{shop_id or '<blank>'} INVALID_SHOP_ID")
            failed += 1
            continue
        try:
            _, created = store.recreate_self_use_shop(
                shop_id,
                None,
                lambda latest, current_shop: create_self_use_access_token(
                    latest.app_key, latest.app_secret, current_shop,
                ),
            )
            print(f"{shop_id} {'AUTH_CREATED' if created else 'AUTH_ALREADY_EXISTS'}")
        except Exception as exc:
            print(f"{shop_id} AUTH_FAILED {str(exc)[:240]}")
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
