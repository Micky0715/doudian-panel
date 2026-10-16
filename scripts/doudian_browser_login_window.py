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
from integrations.doudian.browser_profiles import browser_profile_for_shop


def main() -> int:
    parser = argparse.ArgumentParser(description="Open dedicated DouDian sign-in window")
    profile_args = parser.add_mutually_exclusive_group()
    profile_args.add_argument("--profile", help="旧版或诊断用的显式目录")
    profile_args.add_argument("--shop-id", help="为该店铺打开独立登录资料（推荐）")
    parser.add_argument("--purpose", choices=("cpay", "listing"), default="cpay",
                        help="店铺资料用途；默认为分期免息")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    if not 30 <= args.timeout <= 1800:
        parser.error("timeout must be between 30 and 1800 seconds")
    if args.shop_id:
        try:
            profile = browser_profile_for_shop(Path(__file__).resolve().parents[1],
                                               args.purpose, args.shop_id)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        profile = Path(args.profile or "chrome_user_data").resolve()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            viewport={"width": 1440, "height": 900},
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://fxg.jinritemai.com/ffa/cpay/home" if args.shop_id and args.purpose == "cpay"
                          else "https://fxg.jinritemai.com/ffa/g/draft",
                          wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                print(f"页面导航暂未完成，可在已打开窗口中手动继续：{type(exc).__name__}", flush=True)
            target = f"短 ID {args.shop_id}" if args.shop_id else "目标店铺"
            print(f"抖店专用浏览器已打开；请在窗口内登录并确认当前店铺为{target}。", flush=True)
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
