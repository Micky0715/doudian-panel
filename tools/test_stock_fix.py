#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试 _try_fix_negative_stock 是否能正确处理多行负库存。
从草稿箱找一个有负库存的商品（或直接指定 ID），切换到价格库存页，
模拟发布失败（让平台显示负库存），然后调用修复逻辑验证。
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

        # 关弹窗
        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        # 切换到价格库存
        try:
            page.locator('[role="tab"]:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换价格库存: {e}")

        # 滚动到表格区域
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        # 检查当前负库存情况
        before = page.evaluate(r"""
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

                let negativeRows = [];
                let visibleRowKeys = [];
                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    let rowKey = row.getAttribute('data-row-key') || '';
                    visibleRowKeys.push(rowKey);
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, sellableIdx)) return;
                    let sellableMatch = (cells[sellableIdx].innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellableMatch) return;
                    let sellable = parseInt(sellableMatch[0], 10);
                    if (isNaN(sellable) || sellable >= 0) return;
                    let reservedText = reservedIdx >= 0 ? (cells[reservedIdx].innerText || '').trim() : '';
                    let spotText = (cells[spotIdx].innerText || '').trim();
                    negativeRows.push({ rowKey, sellable, spotText: spotText.substring(0, 50), reservedText });
                });

                return {
                    spotIdx, sellableIdx, reservedIdx,
                    dataRows: visibleRowKeys.length,
                    visibleRowKeys: visibleRowKeys,
                    negativeCount: negativeRows.length,
                    negativeRows: negativeRows
                };
            }
        """)
        print(f"[INFO] 当前可见行数: {before.get('dataRows')}")
        print(f"[INFO] 当前可见负库存行数: {before.get('negativeCount')}")
        if before.get('negativeRows'):
            for r in before['negativeRows'][:10]:
                print(f"  rowKey={r['rowKey']} sellable={r['sellable']} spot={r['spotText'][:30]} reserved={r['reservedText']}")

        if before.get('negativeCount', 0) == 0:
            print("[WARN] 当前没有负库存行，无法测试修复流程")
            print("[INFO] 如需测试，请先用其他工具将部分现货库存改小使可售库存变负")
            browser.close()
            return

        # 调用 _try_fix_negative_stock 验证
        print("\n[INFO] 调用 _try_fix_negative_stock 测试修复逻辑...")
        result = dd._try_fix_negative_stock(page)
        print(f"\n[RESULT] fixed={result.get('fixed')}, reason={result.get('reason')}")
        print(f"[RESULT] 修复详情:")
        for r in result.get('rows', []):
            print(f"  rowKey={r.get('rowKey')} sellable={r.get('sellable')} target={r.get('targetValue')}")

        # 修复后再次检查
        time.sleep(1)
        after = page.evaluate(r"""
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

                let negativeRows = [];
                let visibleRowKeys = [];
                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    let rowKey = row.getAttribute('data-row-key') || '';
                    visibleRowKeys.push(rowKey);
                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, sellableIdx)) return;
                    let sellableMatch = (cells[sellableIdx].innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellableMatch) return;
                    let sellable = parseInt(sellableMatch[0], 10);
                    if (isNaN(sellable) || sellable >= 0) return;
                    let spotText = (cells[spotIdx].innerText || '').trim();
                    negativeRows.push({ rowKey, sellable, spotText: spotText.substring(0, 50) });
                });
                return { dataRows: visibleRowKeys.length, negativeCount: negativeRows.length, negativeRows };
            }
        """)
        print(f"\n[INFO] 修复后当前可见行数: {after.get('dataRows')}")
        print(f"[INFO] 修复后当前可见负库存行数: {after.get('negativeCount')}")

        browser.close()


if __name__ == "__main__":
    main()