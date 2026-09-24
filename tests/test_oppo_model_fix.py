#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 OPPO 专用型号前缀修复的「纯逻辑」部分（不启动浏览器）：

背景：OPPO 型号下拉选项/草稿箱预填常带 "OPPO " 前缀（如 "OPPO Reno15 Pro"），
而抖店"型号"标品库只认纯型号。通用校验 _is_acceptable_model_match 在剥离前缀后
能匹配通过，但框里实际仍是带前缀值，平台发布校验会拒绝。

本测试覆盖：
1. _is_oppo_brand 对 OPPO 命中、对其余品牌（vivo/荣耀/真我）不命中；
2. _strip_brand_tokens 能把 "OPPO Reno15 Pro" 正确剥离为 "Reno15 Pro"；
3. OPPO 触发条件：品牌是 OPPO 且 选中值剥离前缀 == 纯 model → 应走改写分支；
4. 关键防护：非 OPPO 品牌即便选中值形如 "OPPO Reno15 Pro" 也绝不触发改写
   （保证"不动别的品牌"的约束）。
"""
import sys
import os
from unittest.mock import MagicMock

sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import test_doudian_batch as dd


def _oppo_should_rewrite(brand_cn, brand_en, selected, model):
    """复刻 select_model 中 OPPO 分支的触发判定，便于单测验证。"""
    if not dd._is_oppo_brand(brand_cn, brand_en):
        return False
    return dd._strip_brand_tokens(selected, brand_cn, brand_en) == model


def test_is_oppo_brand():
    assert dd._is_oppo_brand("OPPO", "OPPO") is True
    assert dd._is_oppo_brand("OPPO", "") is True
    assert dd._is_oppo_brand("", "OPPO") is True
    # 大小写 / 空格容错
    assert dd._is_oppo_brand(" oppo ", "") is True
    # 其它品牌不命中
    for b in ["vivo", "荣耀", "真我", "realme", "苹果", "Apple", ""]:
        assert dd._is_oppo_brand(b, b) is False, f"品牌 {b} 不应被识别为 OPPO"


def test_strip_oppo_prefix():
    # 核心：剥离 "OPPO " 前缀后得到纯型号
    assert dd._strip_brand_tokens("OPPO Reno15 Pro", "OPPO", "OPPO") == "Reno15 Pro"
    assert dd._strip_brand_tokens("OPPO Reno12", "OPPO", "OPPO") == "Reno12"
    # 不带前缀时保持不变
    assert dd._strip_brand_tokens("Reno15 Pro", "OPPO", "OPPO") == "Reno15 Pro"


def test_oppo_triggers_rewrite():
    # OPPO + 带前缀选中值 + 纯 model → 触发
    assert _oppo_should_rewrite("OPPO", "OPPO", "OPPO Reno15 Pro", "Reno15 Pro") is True
    # OPPO + 已经纯型号 → 同样满足（剥离后相等），但框里本就纯，改写无害
    assert _oppo_should_rewrite("OPPO", "OPPO", "Reno15 Pro", "Reno15 Pro") is True


def test_oppo_prefilled_already_selected_forces_reselect():
    """
    模拟草稿箱预填场景：probe_select_positions 读到型号框已选 "OPPO Reno15 Pro"，
    通用逻辑会因此跳过选择。OPPO 专用判定应识别出这是带前缀值，强制重新选择纯型号。
    """
    # 从 probe_select_positions 拿来的典型 already_selected 条目
    fake_position = {"hasPlaceholder": False, "placeholderText": "", "selectedText": "OPPO Reno15 Pro", "x": 692, "y": 561}
    model = "Reno15 Pro"
    brand_cn, brand_en = "OPPO", "OPPO"

    # 通用 already_selected 条件：model 包含在 selectedText 中
    assert model in fake_position["selectedText"]

    # OPPO 专用强制重选条件：品牌是 OPPO 且剥离前缀后等于纯 model
    should_force_reselect = (
        dd._is_oppo_brand(brand_cn, brand_en)
        and dd._strip_brand_tokens(fake_position["selectedText"], brand_cn, brand_en) == model
    )
    assert should_force_reselect is True, "草稿箱预填 OPPO 带前缀型号必须触发强制重选"

    # 非 OPPO 品牌的同样文本不应触发强制重选
    assert not (
        dd._is_oppo_brand("vivo", "vivo")
        and dd._strip_brand_tokens(fake_position["selectedText"], "vivo", "vivo") == model
    )


def _oppo_should_continue_not_finalize(selected, model, is_last_keyword):
    """复刻 select_model 内 OPPO 分支的「是否继续尝试下一关键字」判定。

    仅当：品牌 OPPO + 选中值剥离前缀后等于纯 model + 当前选中值仍带前缀
    + 不是最后一个关键字 时，才继续（返回 True）；其余情况应结束本轮选择。
    """
    if not dd._is_oppo_brand("OPPO", "OPPO"):
        return False
    if dd._strip_brand_tokens(selected, "OPPO", "OPPO") != model:
        return False
    if selected == model:
        return False
    return not is_last_keyword


def test_oppo_continue_on_prefixed_match():
    # 选中带前缀 + 不是最后一个关键字 → 继续尝试下一关键字找纯型号
    assert _oppo_should_continue_not_finalize("OPPO Reno15 Pro", "Reno15 Pro", is_last_keyword=False) is True
    # 选中已是纯型号 → 不必继续
    assert _oppo_should_continue_not_finalize("Reno15 Pro", "Reno15 Pro", is_last_keyword=False) is False
    # 已是最后一个关键字 → 不再继续（交给定稿/兜底改写）
    assert _oppo_should_continue_not_finalize("OPPO Reno15 Pro", "Reno15 Pro", is_last_keyword=True) is False
    # 非 OPPO 品牌绝不走此逻辑
    assert dd._is_oppo_brand("vivo", "vivo") is False


def test_other_brands_never_rewrite():
    # 即便选中值长得像 "OPPO Reno15 Pro"，只要品牌不是 OPPO 就绝不触发
    assert _oppo_should_rewrite("vivo", "vivo", "OPPO Reno15 Pro", "Reno15 Pro") is False
    assert _oppo_should_rewrite("荣耀", "荣耀", "OPPO Reno15 Pro", "Reno15 Pro") is False
    assert _oppo_should_rewrite("", "", "OPPO Reno15 Pro", "Reno15 Pro") is False
    # 其它品牌正常纯型号也不受影响
    assert _oppo_should_rewrite("vivo", "vivo", "X100 Pro", "X100 Pro") is False


def _global_upgrade_guard_rejects(selected, model, brand_cn="", brand_en=""):
    """复刻 select_model 内全局「升级字段截断拦截」判定：命中则返回 True（应跳过该匹配）。"""
    return dd._model_has_upgrade_field(model) and not dd._selected_retains_upgrade(selected, model, brand_cn, brand_en)


def test_global_upgrade_field_no_fallback():
    # 1) 型号含 Pro/Ultra/Max，选中值丢失升级字段 → 全局拦截（对所有品牌生效，不只 OPPO）
    assert _global_upgrade_guard_rejects("K90 Pro", "K90 Pro Max", "荣耀", "荣耀") is True      # 丢失 Max
    assert _global_upgrade_guard_rejects("Reno15", "Reno15 Pro", "OPPO", "OPPO") is True        # 丢失 Pro
    assert _global_upgrade_guard_rejects("Find X7", "Find X7 Ultra", "OPPO", "OPPO") is True    # 丢失 Ultra
    # 2) OPPO 带前缀但升级字段仍在 → 不拦截（交给 OPPO 改写分支处理）
    assert _global_upgrade_guard_rejects("OPPO Reno15 Pro", "Reno15 Pro", "OPPO", "OPPO") is False
    # 3) 选中值完整保留升级字段 → 不拦截
    assert _global_upgrade_guard_rejects("K90 Pro Max", "K90 Pro Max", "荣耀", "荣耀") is False
    # 4) 型号本身无升级字段 → 不拦截（维持原有宽松行为）
    assert _global_upgrade_guard_rejects("X100", "X100", "vivo", "vivo") is False
    assert _global_upgrade_guard_rejects("K90", "K90", "荣耀", "荣耀") is False


def test_model_has_suffix_field():
    # 带后缀：Pro / Ultra / Max
    assert dd._model_has_suffix_field("Find X7 Pro") is True
    assert dd._model_has_suffix_field("Find X7 Ultra") is True
    assert dd._model_has_suffix_field("K10 Max") is True
    # + 号
    assert dd._model_has_suffix_field("Find X8s+") is True
    assert dd._model_has_suffix_field("Mate 70 Pro+") is True
    # 单字母后缀：紧跟数字且在末尾
    assert dd._model_has_suffix_field("Find X7s") is True
    assert dd._model_has_suffix_field("Reno8 Z") is True
    assert dd._model_has_suffix_field("Note 12i") is True
    assert dd._model_has_suffix_field("A58s") is True
    # 中文版本后缀
    assert dd._model_has_suffix_field("K10 至尊版") is True
    assert dd._model_has_suffix_field("A2 活力版") is True


def test_model_has_no_suffix_field():
    # 无后缀：Find X7 这类不能升级为 Pro
    assert dd._model_has_suffix_field("Find X7") is False
    assert dd._model_has_suffix_field("Find X8") is False
    assert dd._model_has_suffix_field("Mate 70") is False
    assert dd._model_has_suffix_field("iPhone 15") is False
    assert dd._model_has_suffix_field("A5") is False
    # X 在数字前是系列名，不是后缀
    assert dd._model_has_suffix_field("X100") is False
    # 注意：这里的 'X' 是系列前缀，不是单字母后缀


def test_suffix_field_forbidden_upgrade():
    """复刻 guard_model_before_publish 里的新增校验：目标无后缀但实际带后缀 -> 拒绝。"""
    def should_reject(expected, actual):
        exp_norm = dd._strip_brand_tokens(expected, "OPPO", "OPPO")
        act_norm = dd._strip_brand_tokens(actual, "OPPO", "OPPO")
        # 原宽松比较会通过
        base_ok = (exp_norm == act_norm
                   or exp_norm in act_norm
                   or act_norm in exp_norm)
        # 新增后缀拦截
        if base_ok and not dd._model_has_suffix_field(exp_norm) and dd._model_has_suffix_field(act_norm):
            return True
        return False

    assert should_reject("Find X7", "Find X7 Pro") is True
    assert should_reject("Find X7", "OPPO Find X7 Pro") is True
    assert should_reject("Find X7", "Find X7") is False
    assert should_reject("Find X7 Pro", "Find X7 Pro") is False
    # 带后缀型号不降级仍然保持
    assert should_reject("Find X7 Pro", "Find X7") is False  # 这里原比较本就不会通过


if __name__ == "__main__":
    test_is_oppo_brand()
    test_strip_oppo_prefix()
    test_oppo_triggers_rewrite()
    test_oppo_prefilled_already_selected_forces_reselect()
    test_oppo_continue_on_prefixed_match()
    test_other_brands_never_rewrite()
    test_global_upgrade_field_no_fallback()
    test_model_has_suffix_field()
    test_model_has_no_suffix_field()
    test_suffix_field_forbidden_upgrade()
    print("===== test_oppo_model_fix 全部通过 =====")
