#!/usr/bin/env python3
"""Read-only live test. Never creates/edits products or refreshes tokens."""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from doudian_api import (
    DoudianClient, DoudianConfig, DoudianAPIError, DoudianConfigError,
    MODEL_PROPERTY_ID, SECOND_HAND_PHONE_CATEGORY_ID, find_exact_option, find_property,
)


def args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--shop-id")
    parser.add_argument("--all-shops", action="store_true")
    parser.add_argument("--model", default="iPhone 16 Pro")
    return parser.parse_args()


def has_phone_category(client):
    root = client.get_shop_categories(0)
    digital = next((x for x in root if x.get("id") == 1000007020), None)
    if not digital:
        return False
    children = client.get_shop_categories(digital["id"])
    return any(
        x.get("id") == SECOND_HAND_PHONE_CATEGORY_ID
        and x.get("is_leaf") is True and x.get("enable") is True
        for x in children
    )


def main():
    options = args()
    config = DoudianConfig.from_env_file(Path(options.env_file))
    if options.shop_id:
        shops = [options.shop_id]
    elif options.all_shops:
        shops = sorted(config.shop_tokens)
    else:
        shops = ["7970406" if "7970406" in config.shop_tokens else sorted(config.shop_tokens)[0]]
    failed = 0
    for shop_id in shops:
        try:
            client = DoudianClient(config, shop_id)
            category_ok = has_phone_category(client)
            properties = client.get_category_properties() if category_ok else []
            model = find_property(properties, MODEL_PROPERTY_ID)
            match = find_exact_option(model or {}, options.model)
            print(
                f"shop={shop_id} auth=ok second_hand_phone={'yes' if category_ok else 'no'} "
                f"properties={len(properties)} model={'exact' if match else 'missing'} "
                f"model_value={match.get('value', '-') if match else '-'}"
            )
            failed += int(not category_ok or not match)
        except (DoudianAPIError, DoudianConfigError, OSError, ValueError) as exc:
            failed += 1
            print(f"shop={shop_id} status=failed reason={exc}")
    print(f"summary shops={len(shops)} passed={len(shops)-failed} failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
