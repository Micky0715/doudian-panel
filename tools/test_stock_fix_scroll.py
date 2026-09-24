#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 _try_fix_negative_stock 的滚动行为。
即使没有负库存，也能验证 mouse wheel 滚动是否正确切换虚拟表格行。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

import test_doudian_batch as dd

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME_USER_DATA = os.path.join(WORK_DIR, "chrome_user_data")


def main():
    product_id = sys.argv[1] if len(sys.argv) > 1 else "3832638943254413756"

    with sync_playwright() as p:
        browser = p.chromium.launch_persistent_context(
            CHROME_USER_DATA,
            headless=False,
            viewport={"width": 1440, "height": 900},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = browser.new_page()

        url = f"https://fxg.jinritemai.com/ffa/g/create?product_id={product_id}&entrance=draft"
        print(f"[INFO] 打开: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(8)

        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            page.locator('[role="tab"]:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换价格库存: {e}")

        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        # 手动测试 mouse wheel 滚动是否改变可见行
        print("\n[INFO] ===== 手动测试 mouse wheel 滚动 =====")
        def get_visible_keys():
            return page.evaluate(r"""
                () => {
                    let keys = [];
                    document.querySelectorAll('tr[data-row-key]').forEach(r => keys.push(r.getAttribute('data-row-key')));
                    return keys;
                }
            """)

        keys0 = get_visible_keys()
        print(f"  初始可见 keys (前10): {keys0[:10]}")

        # 获取表格中心位置
        pos = page.evaluate(r"""
            () => {
                let el = document.querySelector('.styles_skuTable__oiABK, .ecom-g-table-wrapper, .ecom-g-table');
                if (!el) return null;
                let rect = el.getBoundingClientRect();
                return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + Math.min(rect.height / 2, 300)) };
            }
        """)
        if pos:
            page.mouse.move(pos['x'], pos['y'])

        for i in range(8):
            page.mouse.wheel(0, 70)
            time.sleep(0.6)
            keys = get_visible_keys()
            changed = keys != keys0
            print(f"  第 {i+1} 次 wheel: keys前5={keys[:5]}, row_changed={changed}")
            keys0 = keys
            if not changed and i > 2:
                print(f"  (连续 {i+1} 次无变化，可能已到底)")
                break

        # 调用 _try_fix_negative_stock（即使没负库存，也能看滚动行为）
        print("\n[INFO] ===== 调用 _try_fix_negative_stock =====")
        result = dd._try_fix_negative_stock(page)
        print(f"\n[RESULT] fixed={result.get('fixed')}, reason={result.get('reason')}")
        diag = result.get('diag') or {}
        print(f"[RESULT] rounds={diag.get('rounds')}, visibleRowCount={diag.get('visibleRowCount')}")

        browser.close()


if __name__ == "__main__":
    main()