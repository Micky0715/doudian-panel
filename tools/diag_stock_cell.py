#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
读取现货库存列单元格的完整 HTML 结构，帮助理解增/减输入组件。
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
        except Exception:
            pass

        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.55); }")
        time.sleep(1)

        result = page.evaluate(r"""
            () => {
                let out = [];
                let tables = document.querySelectorAll('table');
                for (let t of tables) {
                    let theadRow = t.querySelector('thead tr');
                    if (!theadRow) {
                        let firstRow = t.querySelector('tr');
                        if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                    }
                    if (!theadRow) continue;
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH');
                    let headerTexts = headers.map(h => (h.innerText || '').trim());
                    let spotIdx = headerTexts.findIndex(h => h.includes('现货库存'));
                    let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                    if (spotIdx < 0 || sellableIdx < 0) continue;

                    let rows = [];
                    t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) rows.push(r); });
                    if (rows.length === 0) continue;

                    for (let row of rows) {
                        let cells = Array.from(row.children).filter(c => c.tagName === 'TD');
                        if (cells.length <= Math.max(spotIdx, sellableIdx)) continue;
                        let sellableText = (cells[sellableIdx].innerText || '').trim();
                        let sellableMatch = sellableText.match(/-?[0-9]+/);
                        if (!sellableMatch) continue;
                        let sellable = parseInt(sellableMatch[0], 10);
                        if (sellable >= 0) continue;

                        let spotCell = cells[spotIdx];
                        out.push({
                            sellable: sellable,
                            cellOuterHTML: spotCell.outerHTML,
                            cellInnerText: spotCell.innerText,
                            inputs: Array.from(spotCell.querySelectorAll('input, button')).map(el => ({
                                tag: el.tagName,
                                type: el.type,
                                value: el.value,
                                checked: el.checked,
                                className: el.className,
                                innerText: el.innerText,
                                outerHTML: el.outerHTML
                            }))
                        });
                    }
                }
                return out;
            }
        """)

        output_path = os.path.join(WORK_DIR, "tools", "diag_stock_cell_result.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

        print(f"[INFO] 结果已保存: {output_path}")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])

        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    main()
