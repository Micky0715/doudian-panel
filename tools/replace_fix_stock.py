#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
替换 test_doudian_batch.py 中的 _try_fix_negative_stock 函数。
使用鼠标滚轮触发虚拟表格更新（ecom-g-table-virtual）。
"""
import re

PATH = "/Users/atrenew/doudian-panel/test_doudian_batch.py"

NEW_FUNC = '''def _try_fix_negative_stock(page) -> dict:
    """
    检查"价格库存"页中是否存在"可售库存"为负数的行。
    若存在，把对应行的"现货库存"输入框直接设为合适的值（不走增/减按钮）。

    由于抖店校验"库存数量不能低于预留库存"，因此目标值为：
        target = max(0, 预留库存)
    这样可售库存 = 现货库存 - 预留库存 >= 0，且满足平台校验。

    抖店的"价格库存"表格使用虚拟滚动（ecom-g-table-virtual）：
      - DOM 中只渲染当前可视行（约 10-12 行），其余行未渲染
      - .ecom-g-table-container 自身不可滚动（maxScroll=0）
      - 只有真实鼠标滚轮事件才会触发虚拟表格重新渲染
      - 直接设置 scrollTop 或 dispatchEvent('wheel') 都不生效

    本函数采用：
      1. 把鼠标移动到表格区域中心；
      2. 调用 page.mouse.wheel 触发虚拟滚动；
      3. 每轮扫描当前可见行中"可售库存"为负的行；
      4. 给未处理过的 input 加 TARGET_CLASS 并记录目标值；
      5. Python 侧 Playwright fill 设值；
      6. 用 data-row-key 跟踪已处理的行，避免虚拟滚动来回滚时重复处理；
      7. 完整滚到底（连续多轮无可修复行）时停止。

    返回 {fixed: 修复行数, reason: 说明, rows: [...], diag: {...}}。
    """
    TARGET_CLASS = '__fix_stock_target__'
    DONE_CLASS = '__fix_stock_done__'
    ROW_DONE_CLASS = '__fix_stock_row_done__'
    MAX_ROUNDS = 30           # 每轮滚一行，最多 30 轮（足够覆盖 30+ 行规格）
    IDLE_LIMIT = 4             # 连续多少轮无新负库存视为到底

    total_fixed = 0
    all_details = []
    last_diag = {}

    def _table_center() -> dict:
        return page.evaluate(r"""
            () => {
                let el = document.querySelector('.styles_skuTable__oiABK, .ecom-g-table-wrapper, .ecom-g-table');
                if (!el) return null;
                let rect = el.getBoundingClientRect();
                if (rect.width === 0) return null;
                return { x: Math.round(rect.left + rect.width / 2), y: Math.round(rect.top + Math.min(rect.height / 2, 300)) };
            }
        """)

    def _scroll_table(delta_y: int) -> dict:
        """用真实鼠标滚轮触发虚拟表格重新渲染。"""
        pos = _table_center()
        if not pos:
            return {'ok': False, 'reason': 'table area not found'}
        try:
            page.mouse.move(pos['x'], pos['y'])
            page.mouse.wheel(0, delta_y)
        except Exception as e:
            return {'ok': False, 'reason': f'mouse wheel failed: {e}'}
        time.sleep(0.6)
        return {'ok': True, 'reason': 'mouse wheel', 'deltaY': delta_y, 'pos': pos}

    def _scroll_to_top() -> dict:
        """滚到表格顶部（用 scrollIntoView 触发虚拟表格跳回首行）。"""
        return page.evaluate(r"""
            () => {
                let rows = document.querySelectorAll('tr[data-row-key]');
                if (rows.length === 0) return { ok: false, reason: 'no rows' };
                rows[0].scrollIntoView({ behavior: 'auto', block: 'start' });
                return { ok: true };
            }
        """)

    def _scan_and_mark() -> dict:
        """扫描当前可见行，给未修复的负库存 input 加标记并记录目标值。"""
        return page.evaluate(r"""
            () => {
                let diag = { steps: [] };

                // 关闭可能遮挡的"保存成功"等弹窗
                let modals = document.querySelectorAll('.ant-modal, [class*="modal"], [role="dialog"], [class*="mask"]');
                for (let m of modals) {
                    let txt = (m.innerText || '').trim();
                    if (txt.includes('保存成功') || txt.includes('我知道了')) {
                        let confirmBtns = m.querySelectorAll('button');
                        for (let b of confirmBtns) {
                            if (b.innerText.includes('我知道了') || b.innerText.includes('确定') || b.innerText.includes('确认')) {
                                b.click();
                                diag.steps.push('closed save-success modal');
                                break;
                            }
                        }
                    }
                }

                // 找表格
                function findTableWithColumns() {
                    let candidates = [];
                    document.querySelectorAll('table').forEach(t => candidates.push(t));
                    document.querySelectorAll('[class*="table"], [class*="Table"]').forEach(el => {
                        if (!candidates.includes(el)) candidates.push(el);
                    });
                    let best = null, bestDataRows = 0;
                    for (let t of candidates) {
                        let theadRow = t.querySelector('thead tr');
                        if (!theadRow) {
                            let firstRow = t.querySelector('tr');
                            if (firstRow && firstRow.querySelector('th')) theadRow = firstRow;
                        }
                        if (!theadRow) continue;
                        let headers = Array.from(theadRow.children).filter(c => c.tagName === 'TH' || c.tagName === 'TD');
                        let headerTexts = headers.map(h => (h.innerText || '').trim());
                        if (!headerTexts.some(h => h.includes('现货库存')) || !headerTexts.some(h => h.includes('可售库存'))) continue;
                        let dataRows = 0;
                        t.querySelectorAll('tr').forEach(r => { if (r.querySelector('td')) dataRows++; });
                        if (dataRows > bestDataRows) { best = { el: t, headers: headers, headerTexts: headerTexts, dataRows: dataRows }; bestDataRows = dataRows; }
                    }
                    return best;
                }

                let tableInfo = findTableWithColumns();
                if (!tableInfo) {
                    diag.reason = 'price-stock table not found';
                    return { fixed: 0, reason: diag.reason, rows: [], diag: diag };
                }
                diag.tableHeaderTexts = tableInfo.headerTexts;
                diag.rowCount = tableInfo.dataRows;

                let headers = tableInfo.headers;
                let spotIdx = -1, sellableIdx = -1, reservedIdx = -1;
                for (let i = 0; i < headers.length; i++) {
                    let txt = (headers[i].innerText || '').trim();
                    if (txt.includes('现货库存')) spotIdx = i;
                    if (txt.includes('可售库存')) sellableIdx = i;
                    if (txt.includes('预留库存')) reservedIdx = i;
                }
                diag.spotIdx = spotIdx;
                diag.sellableIdx = sellableIdx;
                diag.reservedIdx = reservedIdx;
                if (spotIdx < 0 || sellableIdx < 0) {
                    diag.reason = 'required columns not found';
                    return { fixed: 0, reason: diag.reason, rows: [], diag: diag };
                }

                // 清掉过期的 TARGET_CLASS（保留 DONE_CLASS）
                document.querySelectorAll('.' + '__fix_stock_target__').forEach(el => el.classList.remove('__fix_stock_target__'));

                let t = tableInfo.el;
                let fixed = 0;
                let details = [];
                let visibleRowKeys = [];

                t.querySelectorAll('tr[data-row-key]').forEach(row => {
                    if (!row.querySelector('td')) return;
                    let rowKey = row.getAttribute('data-row-key') || '';
                    visibleRowKeys.push(rowKey);

                    // 已修复的行直接跳过（row-level 标记）
                    if (row.classList.contains('__fix_stock_row_done__')) return;

                    let cells = Array.from(row.children).filter(c => c.tagName === 'TD' || c.tagName === 'TH');
                    if (cells.length <= Math.max(spotIdx, sellableIdx)) return;

                    let sellableCell = cells[sellableIdx];
                    let sellableMatch = (sellableCell.innerText || '').trim().match(/-?[0-9]+/);
                    if (!sellableMatch) return;
                    let sellable = parseInt(sellableMatch[0], 10);
                    if (isNaN(sellable) || sellable >= 0) return;

                    let spotCell = cells[spotIdx];
                    // 现货库存输入框选择器：
                    // 优先 .ecom-g-input-number-input，其次 [role=spinbutton]，
                    // 再 type=number，再 type=text，最后任何可见 input
                    let input = spotCell.querySelector(
                        'input.ecom-g-input-number-input, input[role="spinbutton"], input[type="number"], input[type="text"]:not([disabled])'
                    );
                    if (!input) {
                        let cands = spotCell.querySelectorAll('input:not([type=radio]):not([type=hidden])');
                        for (let inp of cands) {
                            let rect = inp.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) { input = inp; break; }
                        }
                    }
                    if (!input || input.classList.contains('__fix_stock_done__')) return;

                    // 读取预留库存，计算目标现货库存
                    let target = '0';
                    if (reservedIdx >= 0 && cells.length > reservedIdx) {
                        let reservedMatch = (cells[reservedIdx].innerText || '').trim().match(/[0-9]+/);
                        if (reservedMatch) {
                            let reserved = parseInt(reservedMatch[0], 10);
                            if (!isNaN(reserved) && reserved > 0) target = String(reserved);
                        }
                    }

                    let currentValue = (input.value || '').trim();
                    // 也参考现货库存显示值（有些 input 为空但显示有值）
                    if (!currentValue) {
                        let disp = spotCell.querySelector('.style_stockNum__NtDzx');
                        if (disp) currentValue = (disp.innerText || '').trim();
                    }
                    if (currentValue === target) {
                        row.classList.add('__fix_stock_row_done__');
                        input.classList.add('__fix_stock_done__');
                        return;
                    }

                    input.classList.add('__fix_stock_target__');
                    input.setAttribute('data-fix-stock-target', target);
                    input.setAttribute('data-fix-stock-rowkey', rowKey);
                    fixed++;
                    details.push({
                        rowKey: rowKey,
                        sellable: sellable,
                        oldValue: currentValue,
                        targetValue: target,
                        rowText: row.innerText.trim().substring(0, 120)
                    });
                });

                diag.fixed = fixed;
                diag.visibleRowKeys = visibleRowKeys.slice(0, 5);
                diag.visibleRowCount = visibleRowKeys.length;
                diag.reason = fixed > 0 ? 'marked negative sellable stock inputs' : 'no negative sellable stock in current view';
                return { fixed: fixed, reason: diag.reason, rows: details, diag: diag };
            }
        """)

    def _fill_target_inputs() -> int:
        """填充所有标记的 input 为目标值，返回成功数量。"""
        count = 0
        try:
            inputs = page.locator(f"input.{TARGET_CLASS}").all()
            for inp in inputs:
                row_key = ''
                target = '0'
                try:
                    row_key = inp.evaluate("el => el.getAttribute('data-fix-stock-rowkey') || ''")
                    target = inp.evaluate("el => el.getAttribute('data-fix-stock-target') || '0'")
                except Exception:
                    target = '0'
                try:
                    try:
                        inp.scroll_into_view_if_needed(timeout=5000)
                    except Exception:
                        try:
                            inp.evaluate("el => el.scrollIntoView({block: 'center', behavior: 'auto'})")
                            time.sleep(0.2)
                        except Exception:
                            pass
                    inp.click()
                    time.sleep(0.1)
                    # 清空再填
                    try:
                        inp.fill('')
                        time.sleep(0.05)
                    except Exception:
                        try:
                            inp.evaluate("el => { el.value = ''; el.dispatchEvent(new Event('input', { bubbles: true })); }")
                        except Exception:
                            pass
                    inp.fill(target)
                    time.sleep(0.1)
                    # 触发 blur 让 React 保存
                    try:
                        inp.blur()
                    except Exception:
                        try:
                            inp.evaluate("el => el.dispatchEvent(new Event('blur', { bubbles: true }))")
                        except Exception:
                            pass
                    time.sleep(0.1)
                    inp.evaluate("el => { el.classList.add('__fix_stock_done__'); let r=el.closest('tr[data-row-key]'); if(r) r.classList.add('__fix_stock_row_done__'); }")
                    count += 1
                except Exception as e:
                    log("STEP10", f"库存修复 fill 异常 (target={target}): {e}")
                    try:
                        inp.evaluate("el => { el.classList.add('__fix_stock_done__'); let r=el.closest('tr[data-row-key]'); if(r) r.classList.add('__fix_stock_row_done__'); }")
                    except Exception:
                        pass
            time.sleep(0.4)
        except Exception as e:
            log("STEP10", f"库存修复 fill 批次异常: {e}")
        return count

    # 0. 滚到表格顶部
    _scroll_to_top()
    time.sleep(0.5)

    idle_rounds = 0
    last_row_keys_sig = ''

    for round_num in range(1, MAX_ROUNDS + 1):
        # 1. 扫描并标记
        scan_result = _scan_and_mark()
        diag = scan_result.get("diag") or {}
        last_diag = diag
        fixed_this_round = scan_result.get("fixed", 0)
        visible_keys = diag.get('visibleRowKeys', [])

        if fixed_this_round > 0:
            idle_rounds = 0
            log("STEP10", f"库存修复第 {round_num} 轮: 发现 {fixed_this_round} 行负库存 (可见行 keys={visible_keys[:3]})")
            for d in scan_result.get("rows", [])[:5]:
                log("STEP10", f"  修复行: rowKey={d.get('rowKey')}, 可售={d.get('sellable')}, 目标={d.get('targetValue')}, 内容={d.get('rowText', '')[:80]}")

            # 2. Python 侧 fill
            filled_count = _fill_target_inputs()
            total_fixed += filled_count
            all_details.extend(scan_result.get("rows", []))
        else:
            idle_rounds += 1
            if idle_rounds <= 2:
                log("STEP10", f"库存修复第 {round_num} 轮: 当前视图无新负库存 (idle={idle_rounds})")

        # 3. 检查可视行是否变化（虚拟表格是否真的换了内容）
        cur_sig = ','.join(visible_keys)
        row_changed = (cur_sig != last_row_keys_sig and len(cur_sig) > 0)
        last_row_keys_sig = cur_sig

        # 4. 连续空闲达到上限，认为已到底
        if idle_rounds >= IDLE_LIMIT:
            log("STEP10", f"库存修复连续 {idle_rounds} 轮无新负库存，判定已到底")
            break

        # 5. 滚到下一行（一次滚 1 行高度，约 60-70px）
        scroll_info = _scroll_table(70)
        if round_num <= 3 or round_num % 5 == 0:
            log("STEP10", f"库存修复第 {round_num} 轮滚动: {scroll_info} | row_changed={row_changed}")

    # 6. 滚回顶部再扫一次，确认是否漏了开头的行
    _scroll_to_top()
    time.sleep(0.8)
    final_scan = _scan_and_mark()
    final_fixed = final_scan.get("fixed", 0)
    if final_fixed > 0:
        log("STEP10", f"库存修复最终轮: 发现 {final_fixed} 行负库存（开头漏掉的）")
        for d in final_scan.get("rows", [])[:5]:
            log("STEP10", f"  最终行: rowKey={d.get('rowKey')}, 可售={d.get('sellable')}, 目标={d.get('targetValue')}")
        filled_count = _fill_target_inputs()
        total_fixed += filled_count
        all_details.extend(final_scan.get("rows", []))

    # 清理临时标记（保留 ROW_DONE_CLASS / DONE_CLASS 用于调试）
    try:
        page.evaluate("() => { document.querySelectorAll('.__fix_stock_target__').forEach(el => el.classList.remove('__fix_stock_target__')); }")
    except Exception:
        pass

    result = {
        "fixed": total_fixed,
        "reason": "scrolled via mouse wheel and fixed all negative sellable stock rows" if total_fixed > 0 else "no negative sellable stock found",
        "rows": all_details,
        "diag": last_diag
    }
    if total_fixed > 0:
        result["diag"]["rounds"] = round_num
    log("STEP10", f"库存修复总计: fixed={total_fixed}, rounds={round_num}")
    return result
'''

def main():
    with open(PATH, "r", encoding="utf-8") as f:
        src = f.read()

    # 用 def _try_fix_negative_stock 开头，匹配到下一个 def 开头
    pattern = re.compile(
        r"def _try_fix_negative_stock\(page\) -> dict:.*?(?=\ndef [A-Za-z_])",
        re.DOTALL,
    )
    matches = list(pattern.finditer(src))
    if len(matches) != 1:
        print(f"[ERROR] 找到 {len(matches)} 个匹配，期望 1 个")
        return

    start, end = matches[0].span()
    new_src = src[:start] + NEW_FUNC + src[end:]

    with open(PATH, "w", encoding="utf-8") as f:
        f.write(new_src)

    print(f"[OK] 已替换 _try_fix_negative_stock (长度: {end - start} -> {len(NEW_FUNC)})")


if __name__ == "__main__":
    main()