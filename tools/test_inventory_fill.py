#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在真实抖店编辑页测试：用 Playwright fill 把现货库存 input 设为 0 的效果。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME_USER_DATA = os.path.join(WORK_DIR, "chrome_user_data")
URL = "https://fxg.jinritemai.com/ffa/g/create?product_id=3832641000510193842&entrance=draft"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            CHROME_USER_DATA,
            headless=False,
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()

        print(f"[INFO] 打开: {URL}")
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(12)

        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            page.locator('.ecom-g-tabs-tab:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换标签可能失败: {e}")

        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.55); }")
        time.sleep(1)

        # 修改前截图
        page.screenshot(path=os.path.join(WORK_DIR, "tools", "test_inventory_fill_before.png"), full_page=False)

        # 找第一个可见的现货库存 number input
        inputs = page.locator('input.ecom-g-input-number-input').all()
        print(f"[INFO] 找到 {len(inputs)} 个现货库存 number input")
        if len(inputs) == 0:
            print("[ERROR] 未找到 input")
            browser.close()
            return

        target = inputs[0]
        old_val = target.input_value()
        print(f"[INFO] 第一个 input 当前值: {old_val}")

        target.scroll_into_view_if_needed()
        time.sleep(0.3)
        target.click()
        time.sleep(0.2)
        target.fill('0')
        target.blur()
        time.sleep(2)

        new_val = target.input_value()
        print(f"[INFO] fill('0') 后值: {new_val}")

        # 修改后截图
        page.screenshot(path=os.path.join(WORK_DIR, "tools", "test_inventory_fill_after.png"), full_page=False)

        time.sleep(3)
        browser.close()


if __name__ == "__main__":
    main()
