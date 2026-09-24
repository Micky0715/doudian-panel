#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本 v2：深度诊断荣耀 200 价格库存表格滚动机制。
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

        try:
            page.locator("button:has-text('我知道了')").click(timeout=2000)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            page.locator('[role="tab"]:has-text("价格库存")').first.click(timeout=5000)
            time.sleep(2)
        except Exception as e:
            print(f"[WARN] 切换价格库存标签可能失败: {e}")

        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        def get_table_state(label):
            return page.evaluate(r"""
                (label) => {
                    // 找到价格库存区域
                    let tableArea = null;
                    document.querySelectorAll('[class*="skuTable"]').forEach(el => { tableArea = el; });
                    if (!tableArea) {
                        document.querySelectorAll('table, [class*="table"], [class*="Table"]').forEach(el => {
                            if ((el.innerText || '').includes('现货库存') && (el.innerText || '').includes('可售库存')) {
                                tableArea = el;
                            }
                        });
                    }

                    function extractRows(root) {
                        let rows = [];
                        root.querySelectorAll('tr').forEach(row => {
                            if (!row.querySelector('td')) return;
                            let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                            let headerRow = row.closest('table')?.querySelector('thead tr') || row.closest('table')?.querySelector('tr');
                            let headers = headerRow ? Array.from(headerRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD').map(h => (h.innerText || '').trim()) : [];
                            let spotIdx = headers.findIndex(h => h.includes('现货库存'));
                            let sellableIdx = headers.findIndex(h => h.includes('可售库存'));
                            if (spotIdx < 0 || sellableIdx < 0) {
                                // 尝试用固定索引
                                spotIdx = 4; sellableIdx = 5;
                            }
                            if (cells.length <= Math.max(spotIdx, sellableIdx)) return;
                            let spotText = (cells[spotIdx]?.innerText || '').trim();
                            let sellableText = (cells[sellableIdx]?.innerText || '').trim();
                            let rowKey = row.getAttribute('data-row-key') || '';
                            rows.push({
                                rowKey,
                                spotText: spotText.substring(0, 50),
                                sellableText: sellableText.substring(0, 50),
                                rowText: row.innerText.trim().substring(0, 150),
                                rect: row.getBoundingClientRect ? { top: row.getBoundingClientRect().top, height: row.getBoundingClientRect().height } : null
                            });
                        });
                        return rows;
                    }

                    // 找所有相关容器
                    let containers = [];
                    let selectors = ['.ecom-g-table-container', '.ecom-g-table-body', '.ecom-g-table-wrapper', '.ecom-g-table', '.styles_skuTable__oiABK'];
                    selectors.forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => {
                            let style = window.getComputedStyle(el);
                            let rect = el.getBoundingClientRect();
                            containers.push({
                                selector: sel,
                                tag: el.tagName,
                                className: (el.className || '').substring(0, 200),
                                overflowY: style.overflowY,
                                overflow: style.overflow,
                                overflowX: style.overflowX,
                                scrollHeight: el.scrollHeight,
                                clientHeight: el.clientHeight,
                                scrollTop: el.scrollTop,
                                rect: { top: rect.top, left: rect.left, width: rect.width, height: rect.height },
                                hasTable: !!el.querySelector('table'),
                                tableCount: el.querySelectorAll('table').length,
                                childOverflows: Array.from(el.children).map(c => {
                                    let cs = window.getComputedStyle(c);
                                    return { tag: c.tagName, className: (c.className || '').substring(0, 80), overflowY: cs.overflowY, overflow: cs.overflow, scrollHeight: c.scrollHeight, clientHeight: c.clientHeight };
                                })
                            });
                        });
                    });

                    return {
                        label,
                        tableAreaTag: tableArea?.tagName,
                        tableAreaClass: tableArea?.className?.substring(0, 200),
                        tableAreaHTML: tableArea?.outerHTML?.substring(0, 2000),
                        containers,
                        rows: tableArea ? extractRows(tableArea) : []
                    };
                }
            """, label)

        state1 = get_table_state("initial")
        print(f"[INFO] 初始状态: {len(state1.get('rows', []))} 行")
        print(f"[INFO] 找到 {len(state1.get('containers', []))} 个相关容器")

        # 测试每种滚动方式
        scroll_tests = []

        # 1. 滚动 ecom-g-table-container
        containers = state1.get('containers', [])
        for i, c in enumerate(containers):
            cls = c.get('className', '')
            sel = c.get('selector', '')
            print(f"[INFO] 测试容器 {i+1}: {sel} class={cls[:80]}")

            # 方法 A: 直接改 scrollTop
            changed = page.evaluate(r"""
                (args) => {
                    let { selector, className } = args;
                    let els = Array.from(document.querySelectorAll(selector));
                    let el = els.find(e => (e.className || '').split(' ').filter(Boolean).join(' ') === className);
                    if (!el && className) {
                        let parts = className.split(' ').filter(Boolean);
                        if (parts.length > 0) {
                            for (let candidate of document.querySelectorAll('.' + parts[0])) {
                                let cns = (candidate.className || '').split(' ').filter(Boolean);
                                if (parts.every(p => cns.includes(p))) { el = candidate; break; }
                            }
                        }
                    }
                    if (!el) return { ok: false, reason: 'not found' };
                    let oldTop = el.scrollTop;
                    el.scrollTop += 500;
                    return { ok: true, oldTop, newTop: el.scrollTop, delta: el.scrollTop - oldTop, maxScroll: el.scrollHeight - el.clientHeight };
                }
            """, {'selector': sel, 'className': cls})
            time.sleep(0.8)
            state_after = get_table_state(f"after_scroll_{i}")
            row_changed = [r.get('rowKey') for r in state1.get('rows', [])] != [r.get('rowKey') for r in state_after.get('rows', [])]
            scroll_tests.append({'method': 'scrollTop', 'container': c, 'scrollResult': changed, 'rowChanged': row_changed, 'afterRows': len(state_after.get('rows', [])), 'firstAfterRow': state_after.get('rows', [{}])[0].get('rowKey', '')})
            print(f"[INFO]   scrollTop 结果: {changed}, 行变化: {row_changed}")

            # 方法 B: dispatch wheel event
            changed2 = page.evaluate(r"""
                (args) => {
                    let { selector, className } = args;
                    let els = Array.from(document.querySelectorAll(selector));
                    let el = els.find(e => (e.className || '').split(' ').filter(Boolean).join(' ') === className);
                    if (!el && className) {
                        let parts = className.split(' ').filter(Boolean);
                        if (parts.length > 0) {
                            for (let candidate of document.querySelectorAll('.' + parts[0])) {
                                let cns = (candidate.className || '').split(' ').filter(Boolean);
                                if (parts.every(p => cns.includes(p))) { el = candidate; break; }
                            }
                        }
                    }
                    if (!el) return { ok: false, reason: 'not found' };
                    let oldTop = el.scrollTop;
                    el.dispatchEvent(new WheelEvent('wheel', { bubbles: true, cancelable: true, deltaY: 500 }));
                    return { ok: true, oldTop, newTop: el.scrollTop, delta: el.scrollTop - oldTop };
                }
            """, {'selector': sel, 'className': cls})
            time.sleep(0.8)
            state_after2 = get_table_state(f"after_wheel_{i}")
            row_changed2 = [r.get('rowKey') for r in state1.get('rows', [])] != [r.get('rowKey') for r in state_after2.get('rows', [])]
            scroll_tests.append({'method': 'wheel', 'container': c, 'scrollResult': changed2, 'rowChanged': row_changed2, 'afterRows': len(state_after2.get('rows', [])), 'firstAfterRow': state_after2.get('rows', [{}])[0].get('rowKey', '')})
            print(f"[INFO]   wheel 结果: {changed2}, 行变化: {row_changed2}")

            # 滚回顶部
            page.evaluate(r"""
                (args) => {
                    let { selector, className } = args;
                    let els = Array.from(document.querySelectorAll(selector));
                    let el = els.find(e => (e.className || '').split(' ').filter(Boolean).join(' ') === className);
                    if (!el && className) {
                        let parts = className.split(' ').filter(Boolean);
                        if (parts.length > 0) {
                            for (let candidate of document.querySelectorAll('.' + parts[0])) {
                                let cns = (candidate.className || '').split(' ').filter(Boolean);
                                if (parts.every(p => cns.includes(p))) { el = candidate; break; }
                            }
                        }
                    }
                    if (el) el.scrollTop = 0;
                }
            """, {'selector': sel, 'className': cls})
            time.sleep(0.3)

        # 2. 测试 outer form 滚动
        print("[INFO] 测试外层 form 滚动")
        form_scroll = page.evaluate(r"""
            () => {
                let el = document.querySelector('.styles_form__Ew_RZ');
                if (!el) return { ok: false };
                let oldTop = el.scrollTop;
                el.scrollTop += 500;
                return { ok: true, oldTop, newTop: el.scrollTop, delta: el.scrollTop - oldTop };
            }
        """)
        time.sleep(0.8)
        state_after_form = get_table_state("after_form_scroll")
        row_changed_form = [r.get('rowKey') for r in state1.get('rows', [])] != [r.get('rowKey') for r in state_after_form.get('rows', [])]
        scroll_tests.append({'method': 'form_scrollTop', 'scrollResult': form_scroll, 'rowChanged': row_changed_form, 'afterRows': len(state_after_form.get('rows', [])), 'firstAfterRow': state_after_form.get('rows', [{}])[0].get('rowKey', '')})
        print(f"[INFO]   form scroll 结果: {form_scroll}, 行变化: {row_changed_form}")

        # 3. 测试 page wheel on table area
        print("[INFO] 测试表格区域 wheel")
        table_area = page.locator('.styles_skuTable__oiABK, .ecom-g-table').first
        try:
            table_area.scroll_into_view_if_needed(timeout=5000)
            time.sleep(0.3)
        except Exception as e:
            print(f"[WARN] scroll_into_view_if_needed 失败: {e}")

        # 鼠标滚轮
        page.mouse.wheel(0, 500)
        time.sleep(0.8)
        state_after_mouse = get_table_state("after_mouse_wheel")
        row_changed_mouse = [r.get('rowKey') for r in state1.get('rows', [])] != [r.get('rowKey') for r in state_after_mouse.get('rows', [])]
        scroll_tests.append({'method': 'mouse_wheel', 'rowChanged': row_changed_mouse, 'afterRows': len(state_after_mouse.get('rows', [])), 'firstAfterRow': state_after_mouse.get('rows', [{}])[0].get('rowKey', '')})
        print(f"[INFO]   mouse wheel 行变化: {row_changed_mouse}")

        # 4. 尝试点击最后一个可见行，看是否能滚动
        print("[INFO] 测试 focus/click 最后一行")
        last_row_key = state1.get('rows', []) and state1.get('rows', [])[-1].get('rowKey')
        if last_row_key:
            page.evaluate(r"""
                (rowKey) => {
                    let row = document.querySelector('tr[data-row-key="' + rowKey + '"]');
                    if (row) {
                        row.scrollIntoView({ behavior: 'auto', block: 'center' });
                        return { ok: true };
                    }
                    return { ok: false };
                }
            """, last_row_key)
            time.sleep(0.8)
            state_after_focus = get_table_state("after_focus_last_row")
            row_changed_focus = [r.get('rowKey') for r in state1.get('rows', [])] != [r.get('rowKey') for r in state_after_focus.get('rows', [])]
            scroll_tests.append({'method': 'focus_last_row', 'rowChanged': row_changed_focus, 'afterRows': len(state_after_focus.get('rows', [])), 'firstAfterRow': state_after_focus.get('rows', [{}])[0].get('rowKey', '')})
            print(f"[INFO]   focus last row 行变化: {row_changed_focus}")

        output_path = os.path.join(WORK_DIR, "tools", "diag_honor200_scroll_v2_result.json")
        result = {
            'url': page.url,
            'initial': state1,
            'scrollTests': scroll_tests
        }
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 诊断结果已保存: {output_path}")

        browser.close()


if __name__ == "__main__":
    main()
