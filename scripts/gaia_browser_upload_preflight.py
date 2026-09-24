"""CLI for real Gaia file attachment without final confirmation."""

import argparse
import json
from pathlib import Path

from integrations.gaia_browser.upload_preflight import preflight_gaia_upload


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="盖亚真实选店/选文件验收；绝不点击最终确定")
    parser.add_argument("batch_id")
    parser.add_argument("--shop-id", required=True)
    parser.add_argument("--profile", default=str(root / "gaia_browser_data"))
    parser.add_argument("--artifacts")
    parser.add_argument("--login-wait", type=int, default=120)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    artifacts = args.artifacts or str(
        root / "panel_data" / "gaia_listing" / "browser_upload_preflight" / args.batch_id
    )
    try:
        result = preflight_gaia_upload(
            work_dir=str(root), batch_id=args.batch_id, shop_id=args.shop_id,
            profile_dir=args.profile, artifact_dir=artifacts,
            login_wait=args.login_wait, headless=args.headless,
        )
    except Exception as exc:
        print(json.dumps({
            "success": False,
            "message": str(exc).splitlines()[0][:800],
            "batch_id": args.batch_id,
            "shop_id": args.shop_id,
            "submitted": False,
        }, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

