#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 OPPO 专用「从草稿全名反推纯型号」逻辑（纯 Python，不启动浏览器）：

用户方案：OPPO 机型型号下拉选不上，改为从草稿全名剔除 成色/品牌名/商品类目，
得到正确的纯型号。关键约束：绝不能误删型号关键字
（Pro/Ultra/Max/+/z/i/s/x/至尊版/活力版/竞速版/元气版 等）。
"""
import sys
import os
from unittest.mock import MagicMock

sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import test_doudian_batch as dd


def test_user_example():
    # 用户给的真实样例
    raw = "【95新】OPPO OPPO Find X9s Pro 二手手机"
    assert dd.extract_oppo_pure_model(raw, "Find X9s Pro") == "Find X9s Pro"


def test_strip_brand_only():
    assert dd.extract_oppo_pure_model("OPPO Reno14 Pro", "Reno14 Pro") == "Reno14 Pro"
    assert dd.extract_oppo_pure_model("OPPO Find X7 Ultra", "Find X7 Ultra") == "Find X7 Ultra"
    assert dd.extract_oppo_pure_model("OPPO A5", "A5") == "A5"


def test_keep_upgrade_keywords():
    # Pro / Ultra / Max 必须保留
    assert dd.extract_oppo_pure_model("OPPO Reno14 Pro", "Reno14 Pro") == "Reno14 Pro"
    assert dd.extract_oppo_pure_model("OPPO Find X7 Ultra", "Find X7 Ultra") == "Find X7 Ultra"
    assert dd.extract_oppo_pure_model("OPPO K10 Max", "K10 Max") == "K10 Max"


def test_keep_trailing_letters():
    # 型号末尾的 s / x / i / z 不能误删
    assert dd.extract_oppo_pure_model("OPPO A58s", "A58s") == "A58s"
    assert dd.extract_oppo_pure_model("OPPO Find X9s", "Find X9s") == "Find X9s"
    assert dd.extract_oppo_pure_model("OPPO A56s", "A56s") == "A56s"


def test_keep_special_edition_suffix():
    # 至尊版/活力版/竞速版/元气版 等版本后缀必须保留
    assert dd.extract_oppo_pure_model("OPPO K10 至尊版", "K10 至尊版") == "K10 至尊版"
    assert dd.extract_oppo_pure_model("OPPO A2 活力版", "A2 活力版") == "A2 活力版"


def test_double_brand_stripped():
    # 重复 OPPO OPPO 也要清掉
    assert dd.extract_oppo_pure_model("OPPO OPPO Reno12", "Reno12") == "Reno12"


def test_no_brackets_no_category():
    # 没有成色括号、没有类目词时也能仅去品牌
    assert dd.extract_oppo_pure_model("OPPO A3 (5G)", "A3 (5G)") == "A3 (5G)"


def test_fallback_when_empty():
    # 剔除后为空/过短 → 回退解析型号
    assert dd.extract_oppo_pure_model("OPPO", "Reno12") == "Reno12"
    assert dd.extract_oppo_pure_model("", "Find X9s Pro") == "Find X9s Pro"


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-q"]))
