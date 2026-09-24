"""Command-line entrypoint for safe Gaia button discovery."""

import argparse
import json
from pathlib import Path

from integrations.gaia_browser.probe import probe_gaia


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="盖亚页面只导航探测；不上传、不提交")
    parser.add_argument("--profile", default=str(root / "gaia_browser_data"))
    parser.add_argument("--artifacts", default=str(root / "panel_data" / "gaia_listing" / "browser_probe"))
    parser.add_argument("--login-wait", type=int, default=120)
    parser.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    try:
        result = probe_gaia(profile_dir=args.profile, artifact_dir=args.artifacts,
                            login_wait=args.login_wait, headless=args.headless)
    except Exception as exc:
        print(json.dumps({"success": False, "message": str(exc).splitlines()[0][:500],
                          "submitted": False},
                         ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
