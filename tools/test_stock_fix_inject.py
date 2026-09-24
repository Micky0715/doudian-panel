#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 _try_fix_negative_stock：先人为给几行设置预留库存（让可售库存变负），
再调用修复函数，验证是否能修复所有行（特别是虚拟滚动下面的行）。
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

        # 给前 N 行的预留库存填入大值，使可售库存变负
        # 现货库存是 spotIdx=4, 预留库存是 reservedIdx=6
        print("\n[INFO] 给前 5 行的预留库存填入值，使可售库存变负...")

        setup = page.evaluate(r"""
            () => {
                function findTable() {
                    let candidates = [];
                    document.querySelectorAll('table').forEach(t => candidates.push(t));
                    document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                        if (!candidates.includes(el)) candidates.push(el);
                    });
                    let best = null, bestRows = 0;
                    for (let t of candidates) {
                        let theadRow = t.querySelector('thead tr');
                        if (!theadRow) {
                            let firstRow = t.querySelector('tr');
                            if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                        }
                        if (!theadRow) continue;
                        let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                        let texts = headers.map(h => (h.innerText || '').trim());
                        if (!texts.some(h => h.includes('现货库存')) || !texts.some(h => h.includes('可售库存'))) continue;
                        let dataRows = 0;
                        t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) dataRows++; });
                        if (dataRows > bestRows) { best = t; bestRows = dataRows; }
                    }
                    return best;
                }
                let t = findTable();
                if (!t) return { error: 'table not found' };

                let theadRow = t.querySelector('thead tr') || t.querySelector('tr');
                let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                let headerTexts = headers.map(h => (h.innerText || '').trim());
                let spotIdx = headerTexts.findIndex(h => h.includes('现货库存'));
                let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                let reservedIdx = headerTexts.findIndex(h => h.includes('预留库存'));
                if (reservedIdx < 0) return { error: 'reserved column not found' };

                // 找所有数据行（含 rowspan 的颜色行用第一个有 spotIdx 的子行代表）
                let dataRows = [];
                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    if (!row.querySelector('td')) return;
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, reservedIdx)) return;
                    dataRows.push({ row, cells, rowKey: row.getAttribute('data-row-key') });
                });

                let reservedInputs = [];
                let reservedCells = [];
                for (let i = 0; i < Math.min(5, dataRows.length); i++) {
                    let dr = dataRows[i];
                    let reservedCell = dr.cells[reservedIdx];
                    reservedCells.push(reservedCell);
                    // 预留库存 cell 中找 input
                    let inp = reservedCell.querySelector('input[type="number"], input.ecom-g-input-number-input, input[role="spinbutton"]');
                    if (!inp) {
                        let cands = reservedCell.querySelectorAll('input:not([type=radio]):not([type=hidden])');
                        for (let c of cands) {
                            let rect = c.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) { inp = c; break; }
                        }
                    }
                    if (inp) reservedInputs.push(inp);
                }

                return {
                    spotIdx, sellableIdx, reservedIdx,
                    dataRowCount: dataRows.length,
                    reservedInputCount: reservedInputs.length,
                    reservedCellSampleHTML: reservedCells[0] ? reservedCells[0].outerHTML.substring(0, 600) : null
                };
            }
        """)
        print(f"[INFO] 表头: spotIdx={setup.get('spotIdx')}, reservedIdx={setup.get('reservedIdx')}")
        print(f"[INFO] 数据行数: {setup.get('dataRowCount')}")
        print(f"[INFO] 预留库存 input 数: {setup.get('reservedInputCount')}")
        if setup.get('reservedCellSampleHTML'):
            print(f"[INFO] 预留库存 cell HTML 片段: {setup['reservedCellSampleHTML'][:400]}")

        # 用 Playwright 填预留库存
        print("\n[INFO] 用 Playwright 给前 5 行的预留库存填入 10...")

        # 滚到顶部确保前 5 行可见
        page.evaluate("""
            () => {
                let row = document.querySelector('tr[data-row-key]');
                if (row) row.scrollIntoView({ behavior: 'auto', block: 'start' });
            }
        """)
        time.sleep(0.5)

        for i in range(5):
            try:
                # 用 selector 找第 i 个数据行的预留库存 input
                inp_selector = f"tr[data-row-key]:nth-child({i+2}) td[attr-column-field_reserved_stock_info] input"
                # 简化：找所有预留库存 input，按顺序填
                inputs = page.locator("tr[data-row-key] td[attr-column-field_reserved_stock_info] input").all()
                if i < len(inputs):
                    inp = inputs[i]
                    try:
                        inp.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    inp.click()
                    time.sleep(0.1)
                    inp.fill('')
                    inp.fill('10')
                    time.sleep(0.2)
                    inp.blur()
                    print(f"  row {i}: 预留库存设为 10")
            except Exception as e:
                print(f"  row {i}: 失败 {e}")

        time.sleep(1)

        # 检查可售库存是否变负
        check = page.evaluate(r"""
            () => {
                function findTable() {
                    let candidates = [];
                    document.querySelectorAll('table').forEach(t => candidates.push(t));
                    document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                        if (!candidates.includes(el)) candidates.push(el);
                    });
                    let best = null, bestRows = 0;
                    for (let t of candidates) {
                        let theadRow = t.querySelector('thead tr');
                        if (!theadRow) {
                            let firstRow = t.querySelector('tr');
                            if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                        }
                        if (!theadRow) continue;
                        let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                        let texts = headers.map(h => (h.innerText || '').trim());
                        if (!texts.some(h => h.includes('现货库存')) || !texts.some(h => h.includes('可售库存'))) continue;
                        let dataRows = 0;
                        t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) dataRows++; });
                        if (dataRows > bestRows) { best = t; bestRows = dataRows; }
                    }
                    return best;
                }
                let t = findTable();
                if (!t) return { error: 'table not found' };
                let theadRow = t.querySelector('thead tr') || t.querySelector('tr');
                let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                let headerTexts = headers.map(h => (h.innerText || '').trim());
                let spotIdx = headerTexts.findIndex(h => h.includes('现货库存'));
                let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                let reservedIdx = headerTexts.findIndex(h => h.includes('预留库存'));

                let negative = 0;
                let visibleKeys = [];
                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    let rowKey = row.getAttribute('data-row-key') || '';
                    visibleKeys.push(rowKey);
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, sellableIdx)) return;
                    let sellableMatch = (cells[sellableIdx].innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellableMatch) return;
                    let sellable = parseInt(sellableMatch[0], 10);
                    if (!isNaN(sellable) && sellable < 0) negative++;
                });
                return { negative, visibleKeys, visibleCount: visibleKeys.length };
            }
        """)
        print(f"\n[INFO] 设置后当前可见负库存行数: {check.get('negative')}")

        if check.get('negative', 0) == 0:
            print("[WARN] 未能成功创建负库存，可能因为 input 没接受 fill。")
            print("[INFO] 尝试用 JS 触发 React 事件...")
            # 用 JS 设置 React 内部状态（通过 input.value + dispatchEvent）
            page.evaluate(r"""
                () => {
                    let inputs = document.querySelectorAll('tr[data-row-key] td[attr-column-field_reserved_stock_info] input');
                    let count = 0;
                    inputs.forEach((inp, i) => {
                        if (i < 5) {
                            let nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                            nativeInputValueSetter.call(inp, '10');
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            count++;
                        }
                    });
                    return count;
                }
            """)
            time.sleep(1)
            check2 = page.evaluate(r"""
                () => {
                    function findTable() {
                        let candidates = [];
                        document.querySelectorAll('table').forEach(t => candidates.push(t));
                        document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                            if (!candidates.includes(el)) candidates.push(el);
                        });
                        let best = null, bestRows = 0;
                        for (let t of candidates) {
                            let theadRow = t.querySelector('thead tr');
                            if (!theadRow) {
                                let firstRow = t.querySelector('tr');
                                if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                            }
                            if (!theadRow) continue;
                            let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                            let texts = headers.map(h => (h.innerText || '').trim());
                            if (!texts.some(h => h.includes('现货库存')) || !texts.some(h => h.includes('可售库存'))) continue;
                            let dataRows = 0;
                            t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) dataRows++; });
                            if (dataRows > bestRows) { best = t; bestRows = dataRows; }
                        }
                        return best;
                    }
                    let t = findTable();
                    if (!t) return { error: 'table not found' };
                    let theadRow = t.querySelector('thead tr') || t.querySelector('tr');
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                    let headerTexts = headers.map(h => (h.innerText || '').trim());
                    let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                    let negative = 0;
                    t.querySelectorAll('tr[data-row-key]').forEach(row => {
                        let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                        if (cells.length <= sellableIdx) return;
                        let sellableMatch = (cells[sellableIdx].innerText || '').trim().match(/-?[0-9]+/);
                        if (!sellableMatch) return;
                        let sellable = parseInt(sellableMatch[0], 10);
                        if (!isNaN(sellable) && sellable < 0) negative++;
                    });
                    return { negative };
                }
            """)
            print(f"[INFO] JS 设置后负库存行数: {check2.get('negative')}")
            if check2.get('negative', 0) == 0:
                print("[ERROR] 无法创建负库存，跳过测试")
                browser.close()
                return

        # 调用修复函数
        print("\n[INFO] ===== 调用 _try_fix_negative_stock =====")
        result = dd._try_fix_negative_stock(page)
        print(f"\n[RESULT] fixed={result.get('fixed')}, reason={result.get('reason')}")
        for r in result.get('rows', []):
            print(f"  rowKey={r.get('rowKey')} sellable={r.get('sellable')} target={r.get('targetValue')} oldValue={r.get('oldValue')}")

        browser.close()


if __name__ == "__main__":
    main()