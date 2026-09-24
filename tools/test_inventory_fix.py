#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在真实抖店编辑页测试 _try_fix_negative_stock 是否能正确把负库存行现货库存改为 0。
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright
import test_doudian_batch as dd

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
        time.sleep(8)

        # 关闭可能弹出的"我知道了"
        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        # 确保在“价格库存”标签页
        try:
            page.locator('.ecom-g-tabs-tab:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换标签可能失败: {e}")

        # 滚动到表格区域
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        print("[INFO] 调用 _try_fix_negative_stock...")
        result = dd._try_fix_negative_stock(page)
        print("[INFO] 结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2))

        diag = result.get("diag", {})
        print(f"\n[SUMMARY] onPriceStock={diag.get('onPriceStock')}, rowCount={diag.get('rowCount')}, "
              f"spotIdx={diag.get('spotIdx')}, sellableIdx={diag.get('sellableIdx')}, fixed={diag.get('fixed')}")

        # 验证修改后的值
        time.sleep(1)
        verify = page.evaluate("""
            () => {
                let out = [];
                document.querySelectorAll('tbody tr').forEach(row => {
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD');
                    if (cells.length < 6) return;
                    let sellableText = (cells[5].innerText || '').trim();
                    let sellableMatch = sellableText.match(/-?[0-9]+/);
                    if (sellableMatch) {
                        let sellable = parseInt(sellableMatch[0], 10);
                        if (sellable < 0) {
                            let spotCell = cells[4];
                            let input = spotCell.querySelector('input.ecom-g-input-number-input, input[role="spinbutton"]');
                            out.push({
                                sellable: sellable,
                                spotValue: input ? input.value : null,
                                rowText: row.innerText.trim().substring(0, 80)
                            });
                        }
                    }
                });
                return out;
            }
        """)
        print("\n[VERIFY] 负库存行修改后现货库存值:")
        print(json.dumps(verify, ensure_ascii=False, indent=2))

        # 截图
        screenshot_path = os.path.join(WORK_DIR, "tools", "test_inventory_fix.png")
        page.screenshot(path=screenshot_path, full_page=False)
        print(f"\n[INFO] 截图已保存: {screenshot_path}")

        time.sleep(3)
        browser.close()


if __name__ == "__main__":
    main()
