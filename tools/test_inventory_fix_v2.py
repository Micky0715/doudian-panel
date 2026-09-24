#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
在真实抖店编辑页测试多种设置现货库存为 0 的方法。
"""
import os
import sys
import time
import json

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

        # 先截图：修改前
        page.screenshot(path=os.path.join(WORK_DIR, "tools", "test_inventory_fix_v2_before.png"), full_page=False)

        # 方法：用 Playwright 的 fill 直接填写第一个负库存行的现货库存 input
        # 先找到目标 input
        target_info = page.evaluate(r"""
            () => {
                let tables = document.querySelectorAll('table');
                let best = null, bestRows = 0;
                for (let t of tables) {
                    let theadRow = t.querySelector('thead tr') || t.querySelector('tr:has(th)');
                    if (!theadRow) continue;
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH');
                    let texts = headers.map(h => (h.innerText || '').trim());
                    let spotIdx = texts.findIndex(x => x.includes('现货库存'));
                    let sellIdx = texts.findIndex(x => x.includes('可售库存'));
                    if (spotIdx < 0 || sellIdx < 0) continue;
                    let rows = [];
                    t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) rows.push(r); });
                    if (rows.length > bestRows) {
                        bestRows = rows.length;
                        best = { t, spotIdx, sellIdx, rows };
                    }
                }
                if (!best) return null;
                for (let row of best.rows) {
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD');
                    if (cells.length <= Math.max(best.spotIdx, best.sellIdx)) continue;
                    let sellMatch = (cells[best.sellIdx].innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellMatch) continue;
                    let sellable = parseInt(sellMatch[0], 10);
                    if (sellable >= 0) continue;
                    let spotCell = cells[best.spotIdx];
                    let input = spotCell.querySelector('input.ecom-g-input-number-input, input[role="spinbutton"]');
                    if (!input) continue;
                    return {
                        sellable: sellable,
                        oldValue: input.value,
                        selectorHint: input.className,
                        rowText: row.innerText.trim().substring(0, 80)
                    };
                }
                return { bestRows, msg: 'no negative row' };
            }
        """)
        print("[INFO] 目标行信息:", json.dumps(target_info, ensure_ascii=False, indent=2))

        if target_info and 'oldValue' in target_info:
            # 用 Playwright locator 找到这个 input 并 fill 0
            try:
                # 找第一个负库存行的现货库存 input
                input_locator = page.locator('table tbody tr:has(td)').filter(has_text=re.compile(r'-1')).first.locator('input.ecom-g-input-number-input').first
                # 更简单：直接用 nth 找到可见的 number input
                all_inputs = page.locator('input.ecom-g-input-number-input').all()
                print(f"[INFO] 找到 {len(all_inputs)} 个 number input")
                # 找到值为 oldValue 的那个
                for inp in all_inputs:
                    val = inp.input_value()
                    if val == target_info['oldValue']:
                        print(f"[INFO] 对 input value={val} 执行 fill('0')")
                        inp.scroll_into_view_if_needed()
                        inp.click()
                        time.sleep(0.2)
                        inp.fill('0')
                        inp.blur()
                        time.sleep(1)
                        print(f"[INFO] fill 后 value={inp.input_value()}")
                        break
            except Exception as e:
                print(f"[ERROR] fill 失败: {e}")

        # 方法对比：用 JS 模拟输入
        js_result = page.evaluate(r"""
            () => {
                let tables = document.querySelectorAll('table');
                let best = null, bestRows = 0;
                for (let t of tables) {
                    let theadRow = t.querySelector('thead tr') || t.querySelector('tr:has(th)');
                    if (!theadRow) continue;
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH');
                    let texts = headers.map(h => (h.innerText || '').trim());
                    let spotIdx = texts.findIndex(x => x.includes('现货库存'));
                    let sellIdx = texts.findIndex(x => x.includes('可售库存'));
                    if (spotIdx < 0 || sellIdx < 0) continue;
                    let rows = [];
                    t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) rows.push(r); });
                    if (rows.length > bestRows) {
                        bestRows = rows.length;
                        best = { t, spotIdx, sellIdx, rows };
                    }
                }
                if (!best) return { error: 'no table' };
                let results = [];
                for (let row of best.rows) {
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD');
                    if (cells.length <= Math.max(best.spotIdx, best.sellIdx)) continue;
                    let sellMatch = (cells[best.sellIdx].innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellMatch) continue;
                    let sellable = parseInt(sellMatch[0], 10);
                    if (sellable >= 0) continue;
                    let spotCell = cells[best.spotIdx];
                    let input = spotCell.querySelector('input.ecom-g-input-number-input, input[role="spinbutton"]');
                    if (!input) continue;

                    // 模拟真实输入：focus, select, type 0, blur
                    input.focus();
                    input.select();
                    document.execCommand('insertText', false, '0');
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                    input.dispatchEvent(new Event('change', { bubbles: true }));
                    input.blur();

                    results.push({
                        sellable: sellable,
                        valueAfterJS: input.value,
                        rowText: row.innerText.trim().substring(0, 80)
                    });
                    break; // 只试第一行
                }
                return results;
            }
        """)
        print("[INFO] JS 模拟输入结果:", json.dumps(js_result, ensure_ascii=False, indent=2))

        time.sleep(2)
        page.screenshot(path=os.path.join(WORK_DIR, "tools", "test_inventory_fix_v2_after.png"), full_page=False)
        print(f"[INFO] 修改后截图已保存")

        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    import re
    main()
