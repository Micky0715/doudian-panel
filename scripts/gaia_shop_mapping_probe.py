"""CLI for the read-only Gaia shop-name/ID audit."""

import argparse
import json
from pathlib import Path

from integrations.gaia_browser.shop_mapping_probe import probe_gaia_shop_mapping


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="只读核验盖亚店铺选项对应的店铺 ID")
    parser.add_argument("option_name")
    parser.add_argument("--profile", default=str(root / "gaia_browser_data"))
    parser.add_argument("--artifacts", default=str(
        root / "panel_data" / "gaia_listing" / "browser_shop_mapping_probe"))
    parser.add_argument("--login-wait", type=int, default=120)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    try:
        result = probe_gaia_shop_mapping(
            option_name=args.option_name, profile_dir=args.profile,
            artifact_dir=args.artifacts, login_wait=args.login_wait,
            headless=args.headless,
        )
    except Exception as exc:
        print(json.dumps({"success": False, "message": str(exc).splitlines()[0][:800],
                          "submitted": False}, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

