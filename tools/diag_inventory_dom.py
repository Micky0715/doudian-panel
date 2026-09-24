#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本：打开指定抖店编辑页，读取“价格库存”表格 DOM 结构。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORAGE_STATE = os.path.join(WORK_DIR, "storage_state.json")
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
        page = browser.new_page()

        print(f"[INFO] 正在打开: {URL}")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print(f"[WARN] goto domcontentloaded 超时或失败: {e}")
        time.sleep(8)

        # 关闭可能弹出的"我知道了"
        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        # 确保在“价格库存”标签页
        try:
            page.locator('[role="tab"]:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换价格库存标签可能失败: {e}")

        # 滚动到表格区域
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        diag = page.evaluate(r"""
            () => {
                let result = {
                    url: window.location.href,
                    title: document.title,
                    tabs: [],
                    tables: [],
                    negativeTexts: []
                };

                // 1. 标签信息
                document.querySelectorAll('[role="tab"], .ant-tabs-tab, [class*="tab"]').forEach(t => {
                    let txt = (t.innerText || '').trim();
                    if (txt.includes('价格库存') || txt.includes('基础信息') || txt.includes('图文信息')) {
                        let style = window.getComputedStyle(t);
                        result.tabs.push({
                            text: txt,
                            className: t.className || '',
                            ariaSelected: t.getAttribute('aria-selected'),
                            borderBottom: style.borderBottom,
                            color: style.color
                        });
                    }
                });

                // 2. 找到包含“现货库存”和“可售库存”的表格
                function findTables() {
                    let candidates = [];
                    document.querySelectorAll('table').forEach(t => candidates.push(t));
                    document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                        if (!candidates.includes(el)) candidates.push(el);
                    });
                    return candidates;
                }

                let candidates = findTables();
                for (let t of candidates) {
                    let headers = [];
                    t.querySelectorAll('th, thead td, [class*="th"], [class*="header"]').forEach(h => headers.push(h));
                    if (headers.length === 0) {
                        let firstRow = t.querySelector('[class*="row"], [class*="Row"], tr');
                        if (firstRow) {
                            Array.from(firstRow.children).forEach(h => headers.push(h));
                        }
                    }
                    let headerTexts = headers.map(h => (h.innerText || '').trim());
                    let hasSpot = headerTexts.some(h => h.includes('现货库存'));
                    let hasSellable = headerTexts.some(h => h.includes('可售库存'));
                    if (hasSpot && hasSellable) {
                        let rows = [];
                        t.querySelectorAll('tbody tr, tr, [class*="row"], [class*="Row"]').forEach(r => rows.push(r));
                        let rowSamples = [];
                        for (let i = 0; i < Math.min(rows.length, 4); i++) {
                            let r = rows[i];
                            let cells = Array.from(r.children).map(c => ({
                                text: (c.innerText || '').trim().substring(0, 80),
                                tag: c.tagName,
                                className: (c.className || '').substring(0, 100),
                                firstInput: (() => {
                                    let inp = c.querySelector('input');
                                    if (!inp) return null;
                                    return {
                                        type: inp.type,
                                        value: inp.value,
                                        placeholder: inp.placeholder,
                                        className: (inp.className || '').substring(0, 100),
                                        outerHTML: inp.outerHTML.substring(0, 300)
                                    };
                                })()
                            }));
                            rowSamples.push({
                                index: i,
                                outerHTML: r.outerHTML.substring(0, 800),
                                cells: cells
                            });
                        }
                        result.tables.push({
                            tag: t.tagName,
                            className: (t.className || '').substring(0, 200),
                            headerTexts: headerTexts,
                            rowCount: rows.length,
                            outerHTML: t.outerHTML.substring(0, 2000),
                            rowSamples: rowSamples
                        });
                    }
                }

                // 3. 直接搜索所有文本为负整数的可见元素
                document.querySelectorAll('*').forEach(el => {
                    let txt = (el.innerText || '').trim();
                    if (/^-?\d+$/.test(txt)) {
                        let num = parseInt(txt, 10);
                        if (num < 0) {
                            result.negativeTexts.push({
                                text: txt,
                                tag: el.tagName,
                                className: (el.className || '').substring(0, 100),
                                parentText: (el.parentElement ? (el.parentElement.innerText || '').trim().substring(0, 120) : '')
                            });
                        }
                    }
                });

                return result;
            }
        """)

        output_path = os.path.join(WORK_DIR, "tools", "diag_inventory_dom_result.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(diag, f, ensure_ascii=False, indent=2)

        print(f"[INFO] 诊断结果已保存: {output_path}")
        print(json.dumps(diag, ensure_ascii=False, indent=2)[:3000])

        time.sleep(2)
        browser.close()


if __name__ == "__main__":
    main()
