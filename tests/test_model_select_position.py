#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 select_model / _try_select_model_with_keyword 的定位与验证修复。

核心问题（run_7970406 日志）：
- 品牌框在 y=505，型号框应在同行右侧；旧逻辑 rightmostPlaceholder 只按 x 取最右，
  结果定位到 y=681 的错位下拉框（划痕/电池等属性），导致反复搜索/验证失败。
- 验证逻辑遍历所有 .ecom-g-select 取第一个有值的，点错框后返回品牌框的 'vivo'。

本测试用纯 Python 模拟 JS 中的筛选与验证逻辑，不依赖 Playwright。
"""


def pick_model_candidate(candidates, brand_x, brand_y, y_tolerance=8):
    """模拟修复后的 rightmostPlaceholder 选择逻辑"""
    if brand_x < 0 or brand_y < 0:
        return None
    rightmost = None
    for c in candidates:
        if c["type"] == "placeholder" and c["x"] > brand_x and abs(c["y"] - brand_y) <= y_tolerance:
            if rightmost is None or c["x"] > rightmost["x"]:
                rightmost = c
    return rightmost


def verify_selected(selects, model_x, model_y):
    """模拟修复后的验证逻辑：取距离目标坐标最近的已选下拉框"""
    best = None
    best_dist = float("inf")
    for s in selects:
        x, y = s["x"], s["y"]
        text = s.get("selectedText", "")
        if not text:
            continue
        if model_x >= 0 and model_y >= 0:
            dist = abs(x - model_x) + abs(y - model_y)
            if dist < best_dist:
                best_dist = dist
                best = text
        elif best is None:
            best = text
    return best


def test_model_candidate_y_constraint():
    """型号框定位必须和品牌框在同一行，避免跨行选到 y=681 的错位框"""
    brand_x, brand_y = 458, 505
    candidates = [
        {"x": 458, "y": 505, "type": "placeholder", "selectedText": ""},   # 品牌框
        {"x": 1160, "y": 505, "type": "placeholder", "selectedText": ""},  # 正确的型号框
        {"x": 1160, "y": 681, "type": "placeholder", "selectedText": ""},  # 错位的属性框（如划痕）
    ]
    picked = pick_model_candidate(candidates, brand_x, brand_y)
    assert picked is not None, "应该能选到型号框"
    assert picked["y"] == 505, f"应该选到同行型号框，实际 y={picked['y']}"
    assert picked["x"] == 1160, f"应该选到最右侧同行型号框，实际 x={picked['x']}"


def test_model_candidate_y_constraint_not_too_loose():
    """y 容差不能过宽，否则仍会跨行"""
    brand_x, brand_y = 458, 505
    candidates = [
        {"x": 1160, "y": 505, "type": "placeholder", "selectedText": ""},
        {"x": 1200, "y": 681, "type": "placeholder", "selectedText": ""},
    ]
    # 默认容差 8px，681 与 505 差 176，应被排除
    picked = pick_model_candidate(candidates, brand_x, brand_y)
    assert picked["y"] == 505


def test_verify_prefers_target_select():
    """验证时应返回目标型号框内的值，而不是品牌框"""
    # 模拟：错点到了 y=681 的框，但该框选中值可能是 vivo X300 Ultra
    selects = [
        {"x": 458, "y": 505, "selectedText": "vivo"},          # 品牌框
        {"x": 1160, "y": 681, "selectedText": "vivo X300 Ultra"},  # 错位的属性框（但确实选中了）
    ]
    # 如果目标是 y=681 的框，验证应返回该框值
    assert verify_selected(selects, 1160, 681) == "vivo X300 Ultra"
    # 如果目标是正确的 y=505 型号框，但品牌框更近/页面上只有品牌框有值时，
    # 旧逻辑会返回 'vivo'；新逻辑按坐标最近应返回型号框值
    selects2 = [
        {"x": 458, "y": 505, "selectedText": "vivo"},
        {"x": 1160, "y": 505, "selectedText": "X300 Ultra"},
    ]
    assert verify_selected(selects2, 1160, 505) == "X300 Ultra"


def test_verify_falls_back_when_no_coords():
    """未传入坐标时取第一个有值的兜底"""
    selects = [
        {"x": 458, "y": 505, "selectedText": "vivo"},
        {"x": 1160, "y": 505, "selectedText": "X300 Ultra"},
    ]
    assert verify_selected(selects, -1, -1) == "vivo"


if __name__ == "__main__":
    test_model_candidate_y_constraint()
    test_model_candidate_y_constraint_not_too_loose()
    test_verify_prefers_target_select()
    test_verify_falls_back_when_no_coords()
    print("===== test_model_select_position 全部通过 =====")
