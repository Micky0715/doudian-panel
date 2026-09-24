#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本：打开荣耀 200 编辑页，诊断价格库存表格滚动容器和现货库存输入框结构。
"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright

WORK_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME_USER_DATA = os.path.join(WORK_DIR, "chrome_user_data")

URL = "https://fxg.jinritemai.com/ffa/g/create?product_id=3832638943254413756&entrance=draft"


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

        def snapshot_table(label):
            return page.evaluate(r"""
                (label) => {
                    function findMainTable() {
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
                        return { el: best, dataRows: bestRows };
                    }
                    let tableInfo = findMainTable();
                    let t = tableInfo.el;
                    if (!t) return { label, error: 'table not found' };

                    // 找表头索引
                    let theadRow = t.querySelector('thead tr') || t.querySelector('tr');
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                    let headerTexts = headers.map(h => (h.innerText || '').trim());
                    let spotIdx = headerTexts.findIndex(h => h.includes('现货库存'));
                    let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                    let reservedIdx = headerTexts.findIndex(h => h.includes('预留库存'));

                    // 找滚动容器
                    let container = t;
                    let containerChain = [];
                    while (container && container !== document.body) {
                        let style = window.getComputedStyle(container);
                        containerChain.push({
                            tag: container.tagName,
                            className: (container.className || '').substring(0, 120),
                            overflowY: style.overflowY,
                            overflow: style.overflow,
                            scrollHeight: container.scrollHeight,
                            clientHeight: container.clientHeight,
                            scrollTop: container.scrollTop
                        });
                        container = container.parentElement;
                    }

                    // 找 ecom-g-table-container
                    let ecomContainer = t.closest('.ecom-g-table-container');
                    let ecomContainerInfo = null;
                    if (ecomContainer) {
                        let style = window.getComputedStyle(ecomContainer);
                        ecomContainerInfo = {
                            tag: ecomContainer.tagName,
                            className: ecomContainer.className,
                            overflowY: style.overflowY,
                            overflow: style.overflow,
                            scrollHeight: ecomContainer.scrollHeight,
                            clientHeight: ecomContainer.clientHeight,
                            scrollTop: ecomContainer.scrollTop
                        };
                    }

                    // 遍历可见数据行
                    let rows = [];
                    t.querySelectorAll('tr').forEach(row => {
                        if (!row.querySelector('td')) return;
                        let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                        if (cells.length <= Math.max(spotIdx, sellableIdx)) return;
                        let spotCell = cells[spotIdx];
                        let sellableCell = cells[sellableIdx];
                        let reservedCell = reservedIdx >= 0 ? cells[reservedIdx] : null;

                        // 现货库存输入框详情
                        let inputs = [];
                        spotCell.querySelectorAll('input').forEach(inp => {
                            inputs.push({
                                type: inp.type,
                                className: (inp.className || '').substring(0, 100),
                                value: inp.value,
                                placeholder: inp.placeholder,
                                role: inp.getAttribute('role'),
                                outerHTML: inp.outerHTML.substring(0, 300)
                            });
                        });

                        // 增/减按钮
                        let buttons = [];
                        spotCell.querySelectorAll('button').forEach(b => {
                            buttons.push({ text: (b.innerText || '').trim(), className: (b.className || '').substring(0, 100) });
                        });

                        rows.push({
                            rowKey: row.getAttribute('data-row-key') || '',
                            rowText: row.innerText.trim().substring(0, 200),
                            spotCellHTML: spotCell.outerHTML.substring(0, 800),
                            spotCellText: (spotCell.innerText || '').trim(),
                            sellableText: (sellableCell.innerText || '').trim(),
                            reservedText: reservedCell ? (reservedCell.innerText || '').trim() : '',
                            inputs: inputs,
                            buttons: buttons,
                            rect: row.getBoundingClientRect ? {
                                top: row.getBoundingClientRect().top,
                                height: row.getBoundingClientRect().height
                            } : null
                        });
                    });

                    return {
                        label,
                        headerTexts,
                        spotIdx,
                        sellableIdx,
                        reservedIdx,
                        dataRows: tableInfo.dataRows,
                        containerChain,
                        ecomContainerInfo,
                        rows
                    };
                }
            """, label)

        def list_scroll_candidates():
            return page.evaluate(r"""
                () => {
                    let candidates = [];
                    document.querySelectorAll('*').forEach(el => {
                        if (el === document.body || el === document.documentElement) return;
                        let style = window.getComputedStyle(el);
                        let overflowY = style.overflowY;
                        let overflow = style.overflow;
                        let isScrollable = (overflowY === 'auto' || overflowY === 'scroll' || overflow === 'auto' || overflow === 'scroll');
                        if (!isScrollable) return;
                        if (el.scrollHeight <= el.clientHeight + 5) return;
                        let rect = el.getBoundingClientRect();
                        candidates.push({
                            tag: el.tagName,
                            className: (el.className || '').substring(0, 200),
                            overflowY,
                            overflow,
                            scrollHeight: el.scrollHeight,
                            clientHeight: el.clientHeight,
                            scrollTop: el.scrollTop,
                            rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height }
                        });
                    });
                    candidates.sort((a, b) => (b.clientHeight * b.scrollHeight) - (a.clientHeight * a.scrollHeight));
                    return candidates.slice(0, 15);
                }
            """)

        # 初始快照
        snap1 = snapshot_table("initial")
        print(f"[INFO] 初始行数: {len(snap1.get('rows', []))}")

        # 候选滚动容器
        candidates = list_scroll_candidates()
        print(f"[INFO] 找到 {len(candidates)} 个可滚动容器")

        # 测试每个候选容器滚动
        scroll_test_results = []
        for i, cand in enumerate(candidates[:5]):
            cls = cand.get('className', '')
            tag = cand.get('tag', '')
            print(f"[INFO] 测试容器 {i+1}: {tag}.{cls[:60]} height={cand.get('clientHeight')} scrollHeight={cand.get('scrollHeight')}")

            # 尝试滚动该容器
            before = len(snapshot_table(f"before_{i}").get('rows', []))
            changed = page.evaluate(r"""
                (className) => {
                    function findByClass(cls) {
                        let parts = cls.split(' ').filter(Boolean);
                        if (parts.length === 0) return null;
                        for (let el of document.querySelectorAll('.' + parts[0])) {
                            let elClasses = (el.className || '').split(' ').filter(Boolean);
                            if (parts.every(p => elClasses.includes(p))) return el;
                        }
                        return null;
                    }
                    let el = findByClass(className);
                    if (!el) return { ok: false, reason: 'not found' };
                    let oldTop = el.scrollTop;
                    el.scrollTop += el.clientHeight * 0.7;
                    return { ok: true, oldTop, newTop: el.scrollTop, maxScroll: el.scrollHeight - el.clientHeight };
                }
            """, cls)
            time.sleep(0.8)
            after_snap = snapshot_table(f"after_{i}")
            after = len(after_snap.get('rows', []))

            # 比较行内容是否变化
            before_texts = [r.get('rowText', '') for r in snap1.get('rows', [])]
            after_texts = [r.get('rowText', '') for r in after_snap.get('rows', [])]
            row_changed = before_texts != after_texts

            scroll_test_results.append({
                'candidate': cand,
                'scrollChange': changed,
                'beforeRows': before,
                'afterRows': after,
                'rowContentChanged': row_changed,
                'afterTopRowText': after_snap.get('rows', [{}])[0].get('rowText', '') if after_snap.get('rows') else ''
            })
            print(f"[INFO]   滚动结果: {changed}, 行内容变化: {row_changed}")

            # 滚回顶部
            page.evaluate(r"""
                (className) => {
                    function findByClass(cls) {
                        let parts = cls.split(' ').filter(Boolean);
                        if (parts.length === 0) return null;
                        for (let el of document.querySelectorAll('.' + parts[0])) {
                            let elClasses = (el.className || '').split(' ').filter(Boolean);
                            if (parts.every(p => elClasses.includes(p))) return el;
                        }
                        return null;
                    }
                    let el = findByClass(className);
                    if (el) el.scrollTop = 0;
                }
            """, cls)
            time.sleep(0.5)

        output_path = os.path.join(WORK_DIR, "tools", "diag_honor200_scroll_result.json")
        result = {
            'url': page.url,
            'initialSnapshot': snap1,
            'scrollCandidates': candidates,
            'scrollTests': scroll_test_results
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 诊断结果已保存: {output_path}")

        browser.close()


if __name__ == "__main__":
    main()
