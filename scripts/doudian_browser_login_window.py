"""Open the dedicated DouDian Playwright profile for a human to sign in.

This helper does not inspect credentials or product data and never edits a product.
Keep it running until the operator has signed in, then stop it to release the
profile lock before running a read-only audit.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> int:
    parser = argparse.ArgumentParser(description="Open dedicated DouDian sign-in window")
    parser.add_argument("--profile", default="chrome_user_data")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 1800:
        parser.error("timeout must be between 30 and 1800 seconds")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(args.profile).resolve()),
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://fxg.jinritemai.com/ffa/g/draft",
                          wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"页面导航暂未完成，可在已打开窗口中手动继续：{type(exc).__name__}", flush=True)
            print("抖店专用浏览器已打开；请在窗口内登录并切换目标店铺。", flush=True)
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            context.close()
    print("专用浏览器窗口已关闭。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
