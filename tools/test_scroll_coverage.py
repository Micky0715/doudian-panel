#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断脚本：验证 _try_fix_negative_stock 的鼠标滚轮能否遍历虚拟表格所有行。
不修改任何数据，仅记录滚动过程中看到的所有 data-row-key。
同时导出每行的现货库存 / 可售库存 / 预留库存值。
"""
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from playwright.sync_api import sync_playwright

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

        # 滚到价格库存区域
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight * 0.5); }")
        time.sleep(1)

        # ===== 第一步：滚到表格顶部 =====
        page.evaluate(r"""
            () => {
                let rows = document.querySelectorAll('tr[data-row-key]');
                if (rows.length > 0) rows[0].scrollIntoView({ behavior: 'auto', block: 'start' });
            }
        """)
        time.sleep(1)

        # ===== 第二步：获取表头信息 =====
        header_info = page.evaluate(r"""
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
                return { headerTexts };
            }
        """)
        print(f"[INFO] 表头: {header_info.get('headerTexts', [])}")

        # ===== 第三步：模拟 _try_fix_negative_stock 的滚动循环，记录所有行 =====
        all_rows_data = {}  # rowKey -> {spot, sellable, reserved, rowText}
        all_seen_keys = []  # 按顺序记录所有看到过的 key（含重复）
        scroll_log = []

        def table_center():
            return page.evaluate(r"""
                () => {
                    let el = document.querySelector('.styles_skuTable__oiABK, .ecom-g-table-wrapper, .ecom-g-table');
                    if (!el) return null;
                    let rect = el.getBoundingClientRect();
                    if (rect.width === 0) return null;
                    return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + Math.min(rect.height / 2, 300)) };
                }
            """)

        def scan_visible():
            return page.evaluate(r"""
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
                    if (!t) return { error: 'table not found', rows: [], keys: [] };

                    let theadRow = t.querySelector('thead tr') || t.querySelector('tr');
                    let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                    let headerTexts = headers.map(h => (h.innerText || '').trim());
                    let spotIdx = headerTexts.findIndex(h => h.includes('现货库存'));
                    let sellableIdx = headerTexts.findIndex(h => h.includes('可售库存'));
                    let reservedIdx = headerTexts.findIndex(h => h.includes('预留库存'));

                    let rows = [];
                    let keys = [];
                    t.querySelectorAll('tr[data-row-key]').forEach(row => {
                        if (!row.querySelector('td')) return;
                        let rowKey = row.getAttribute('data-row-key') || '';
                        keys.push(rowKey);

                        let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                        let spot = '', sellable = '', reserved = '';
                        if (spotIdx >= 0 && cells.length > spotIdx) {
                            let disp = cells[spotIdx].querySelector('.style_stockNum__NtDzx');
                            spot = disp ? (disp.innerText || '').trim() : (cells[spotIdx].innerText || '').trim();
                        }
                        if (sellableIdx >= 0 && cells.length > sellableIdx) {
                            sellable = (cells[sellableIdx].innerText || '').trim();
                        }
                        if (reservedIdx >= 0 && cells.length > reservedIdx) {
                            reserved = (cells[reservedIdx].innerText || '').trim();
                        }
                        rows.push({
                            rowKey, spot, sellable, reserved,
                            rowText: row.innerText.trim().substring(0, 150)
                        });
                    });
                    return { rows, keys, spotIdx, sellableIdx, reservedIdx };
                }
            """)

        MAX_ROUNDS = 35
        IDLE_LIMIT = 5
        idle_rounds = 0
        last_keys_sig = ''

        print(f"\n[INFO] ===== 开始滚动扫描 (MAX_ROUNDS={MAX_ROUNDS}) =====")

        for round_num in range(1, MAX_ROUNDS + 1):
            result = scan_visible()
            if 'error' in result:
                print(f"[ERR] 第 {round_num} 轮: {result['error']}")
                break

            visible_keys = result.get('keys', [])
            visible_rows = result.get('rows', [])

            # 记录新看到的行
            new_count = 0
            for r in visible_rows:
                key = r['rowKey']
                if key not in all_rows_data:
                    new_count += 1
                    all_rows_data[key] = r
                all_seen_keys.append(key)

            # 检查可视行是否变化
            cur_sig = ','.join(visible_keys)
            row_changed = (cur_sig != last_keys_sig and len(cur_sig) > 0)
            last_keys_sig = cur_sig

            # 检查负库存
            negative_in_view = sum(1 for r in visible_rows if r['sellable'].startswith('-'))

            print(f"  轮 {round_num:2d}: 可见 {len(visible_keys)} 行, 新增 {new_count} 行, "
                  f"负库存 {negative_in_view} 行, keys={visible_keys[:3]}{'...' if len(visible_keys)>3 else ''} "
                  f"changed={row_changed}")

            scroll_log.append({
                'round': round_num,
                'visibleCount': len(visible_keys),
                'newCount': new_count,
                'negativeInView': negative_in_view,
                'keys': visible_keys,
                'rowChanged': row_changed
            })

            if new_count > 0:
                idle_rounds = 0
            else:
                idle_rounds += 1

            if idle_rounds >= IDLE_LIMIT:
                print(f"[INFO] 连续 {idle_rounds} 轮无新行，判定已到底")
                break

            # 鼠标滚轮
            pos = table_center()
            if pos:
                page.mouse.move(pos['x'], pos['y'])
                page.mouse.wheel(0, 70)
                time.sleep(0.6)

        # ===== 第四步：滚回顶部再扫一次 =====
        page.evaluate(r"""
            () => {
                let rows = document.querySelectorAll('tr[data-row-key]');
                if (rows.length > 0) rows[0].scrollIntoView({ behavior: 'auto', block: 'start' });
            }
        """)
        time.sleep(1)
        final_result = scan_visible()
        if 'rows' in final_result:
            for r in final_result['rows']:
                if r['rowKey'] not in all_rows_data:
                    all_rows_data[r['rowKey']] = r
                    print(f"  最终轮新增: rowKey={r['rowKey']}")

        # ===== 第五步：输出汇总 =====
        print(f"\n{'='*60}")
        print(f"[RESULT] 滚动扫描完成")
        print(f"  总轮数: {len(scroll_log)}")
        print(f"  唯一行数 (unique row keys): {len(all_rows_data)}")
        print(f"  所有看到过的 key (按顺序): {all_seen_keys}")
        print(f"\n[RESULT] 所有行数据:")
        print(f"{'rowKey':>8} | {'spot':>6} | {'sellable':>8} | {'reserved':>8} | rowText")
        print(f"{'-'*8}-+-{'-'*6}-+-{'-'*8}-+-{'-'*8}-+-{'-'*40}")
        negative_count = 0
        for key, r in sorted(all_rows_data.items(), key=lambda x: x[0]):
            is_negative = r['sellable'].startswith('-')
            if is_negative:
                negative_count += 1
            marker = " *** 负库存" if is_negative else ""
            print(f"{key:>8} | {r['spot']:>6} | {r['sellable']:>8} | {r['reserved']:>8} | {r['rowText'][:60]}{marker}")

        print(f"\n[RESULT] 负库存行数: {negative_count}")
        print(f"[RESULT] 唯一行数: {len(all_rows_data)}")

        # 保存完整数据
        output = {
            'product_id': product_id,
            'totalRounds': len(scroll_log),
            'uniqueRowCount': len(all_rows_data),
            'negativeCount': negative_count,
            'allRows': list(all_rows_data.values()),
            'scrollLog': scroll_log,
            'allSeenKeys': all_seen_keys
        }
        out_path = os.path.join(WORK_DIR, 'tools', 'scroll_coverage_result.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
        print(f"\n[INFO] 完整数据已保存: {out_path}")

        browser.close()


if __name__ == "__main__":
    main()
