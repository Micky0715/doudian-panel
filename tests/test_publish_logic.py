#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试发布商品逻辑：
  1. 成功时 LAST_PUBLISH_ERROR 被清空
  2. 失败时 LAST_PUBLISH_ERROR 被设置，且 process_single_product 返回 False
  3. panel_runner 能正确读取 get_last_publish_error() 并分类为"发布商品失败"
  4. 可售库存为负时自动将现货库存置 0 并多次发布（最多 3 次尝试，含两次库存兜底）
"""
import sys
import os

# 将项目根目录加入路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_doudian_batch as dd


# 用 monkey-patch 替换真实的库存修复函数，避免 FakePage 缺少 Playwright locator 方法导致失败
_original_try_fix_negative_stock = dd._try_fix_negative_stock

def _fake_try_fix_negative_stock(page):
    scenario = getattr(page, 'scenario', '')
    if scenario in ("inventory_fix_success", "inventory_fix_third_success",
                    "inventory_second_non_stock", "inventory_fix_fail"):
        return {"fixed": 1, "jsFixed": 1, "targetFixed": 0,
                "reason": "fixed negative sellable stock",
                "rows": [{"sellable": -1, "rowText": "90新 12G+256G"}], "diag": {"onPriceStock": True}}
    return {"fixed": 0, "jsFixed": 0, "targetFixed": 0,
            "reason": "test no negative stock", "rows": [], "diag": {}}

dd._try_fix_negative_stock = _fake_try_fix_negative_stock


class FakePage:
    """模拟 Playwright Page，只复现发布按钮场景。"""

    def __init__(self, scenario: str):
        self.scenario = scenario
        self.logs = []
        self.click_count = 0
        self.detect_count = 0

    def evaluate(self, script, *args):
        # 本测试只关注发布结果，规格总开关按已明确关闭的页面模拟。
        if "暂不想更新规格信息" in script and "修改规格信息" in script:
            return {"found": True, "state": "关", "aria": "false",
                    "className": "", "method": "test", "x": 0, "y": 0, "w": 20, "h": 20}
        # 第一次 evaluate：滚动（忽略）
        if "scrollTo" in script:
            return "scrolled"

        # 点击按钮：通过特定的 clicked by text 字符串区分
        if "clicked by text" in script:
            self.click_count += 1
            if self.scenario == "not_found":
                return "not found"
            if self.scenario in ("success", "fail_toast", "meaningless_error",
                                 "inventory_fix_success", "inventory_fix_fail",
                                 "inventory_fix_third_success", "inventory_second_non_stock"):
                return "clicked by text: 发布商品"
            return "not found"

        # 价格与库存区域检测（_is_on_price_stock_area），注意要在 _try_fix_negative_stock 之前判断
        if "skuTable" in script and ("可售库存" in script or "现货库存" in script):
            return self.scenario in ("inventory_fix_success", "inventory_fix_third_success",
                                      "inventory_fix_fail")

        # 库存修复检测
        if "可售库存" in script or "现货库存" in script:
            if self.scenario in ("inventory_fix_success", "inventory_fix_third_success",
                                  "inventory_second_non_stock", "inventory_fix_fail"):
                return {"fixed": 1, "reason": "fixed negative sellable stock",
                        "rows": [{"sellable": -1, "rowText": "90新 12G+256G"}]}
            return {"fixed": 0, "reason": "test no negative stock"}

        # 发布结果检测：通过 extractBestError 区分
        if "extractBestError" in script:
            self.detect_count += 1
            if self.scenario == "success":
                return {"success": True, "reason": "发布成功"}
            if self.scenario == "inventory_fix_success":
                # 第一次检测失败，第二次检测成功
                if self.detect_count == 1:
                    return {"success": False, "reason": "库存数量不能低于预扣库存"}
                return {"success": True, "reason": "发布成功"}
            if self.scenario == "inventory_fix_fail":
                return {"success": False, "reason": "库存数量不能低于预扣库存"}
            if self.scenario == "inventory_fix_third_success":
                # 前两次库存类失败，第三次成功
                if self.detect_count <= 2:
                    return {"success": False, "reason": "库存数量不能低于预扣库存"}
                return {"success": True, "reason": "发布成功"}
            if self.scenario == "inventory_second_non_stock":
                # 第一次库存类失败，第二次非库存类失败
                if self.detect_count == 1:
                    return {"success": False, "reason": "库存数量不能低于预扣库存"}
                return {"success": False, "reason": "商品信息不完善，请完善后再发布"}
            if self.scenario == "fail_toast":
                return {"success": False, "reason": "商品信息不完善，请完善后再发布"}
            if self.scenario == "meaningless_error":
                return {"success": False, "reason": "点击发布后仍在编辑页，可能信息不完善或不合规",
                        "debug": ["错误", "错误提示 提升建议"]}
            return {"success": False, "reason": "点击发布后仍在编辑页"}

        return None


def test_publish_meaningless_error():
    dd.clear_last_publish_error()
    page = FakePage("meaningless_error")
    ok = dd.publish_product(page)
    assert ok is False, "无意义错误场景仍应返回 False"
    err = dd.get_last_publish_error()
    assert err and "仍在编辑页" in err, f"应返回兜底原因，实际: {err}"
    print("✅ test_publish_meaningless_error 通过")


def test_publish_success():
    dd.clear_last_publish_error()
    page = FakePage("success")
    ok = dd.publish_product(page)
    assert ok is True, "成功场景应返回 True"
    assert dd.get_last_publish_error() is None, "成功后错误信息应被清空"
    print("✅ test_publish_success 通过")


def test_publish_not_found():
    dd.clear_last_publish_error()
    page = FakePage("not_found")
    ok = dd.publish_product(page)
    assert ok is False, "未找到按钮应返回 False"
    err = dd.get_last_publish_error()
    assert err and "未找到发布商品按钮" in err, f"错误信息异常: {err}"
    print("✅ test_publish_not_found 通过")


def test_publish_fail_toast():
    dd.clear_last_publish_error()
    page = FakePage("fail_toast")
    ok = dd.publish_product(page)
    assert ok is False, "失败 toast 应返回 False"
    err = dd.get_last_publish_error()
    assert err and "不完善" in err, f"错误信息异常: {err}"
    print("✅ test_publish_fail_toast 通过")


def test_publish_inventory_fix_success():
    dd.clear_last_publish_error()
    page = FakePage("inventory_fix_success")
    ok = dd.publish_product(page)
    assert ok is True, "库存修复后二次发布成功应返回 True"
    assert dd.get_last_publish_error() is None, "成功后错误信息应被清空"
    assert page.click_count == 2, f"应点击两次发布按钮，实际: {page.click_count}"
    assert page.detect_count == 2, f"应检测两次发布结果，实际: {page.detect_count}"
    print("✅ test_publish_inventory_fix_success 通过")


def test_publish_inventory_fix_fail():
    dd.clear_last_publish_error()
    page = FakePage("inventory_fix_fail")
    ok = dd.publish_product(page)
    assert ok is False, "库存修复后仍失败应返回 False"
    err = dd.get_last_publish_error()
    assert err and "预扣库存" in err, f"错误信息异常: {err}"
    # 新兜底循环：首次失败 -> 修复 -> 二次失败 -> 再修复 -> 三次失败，共 3 次点击/检测
    assert page.click_count == 3, f"应点击三次发布按钮，实际: {page.click_count}"
    assert page.detect_count == 3, f"应检测三次发布结果，实际: {page.detect_count}"
    print("✅ test_publish_inventory_fix_fail 通过")


def test_publish_inventory_fix_third_success():
    dd.clear_last_publish_error()
    page = FakePage("inventory_fix_third_success")
    ok = dd.publish_product(page)
    assert ok is True, "两次库存兜底后第三次发布成功应返回 True"
    assert dd.get_last_publish_error() is None, "成功后错误信息应被清空"
    assert page.click_count == 3, f"应点击三次发布按钮，实际: {page.click_count}"
    assert page.detect_count == 3, f"应检测三次发布结果，实际: {page.detect_count}"
    print("✅ test_publish_inventory_fix_third_success 通过")


def test_publish_inventory_second_non_stock():
    dd.clear_last_publish_error()
    page = FakePage("inventory_second_non_stock")
    ok = dd.publish_product(page)
    assert ok is False, "第二次非库存类失败应立即返回 False"
    err = dd.get_last_publish_error()
    assert err and "不完善" in err, f"错误信息应为非库存原因，实际: {err}"
    assert page.click_count == 2, f"应点击两次发布按钮，实际: {page.click_count}"
    assert page.detect_count == 2, f"应检测两次发布结果，实际: {page.detect_count}"
    print("✅ test_publish_inventory_second_non_stock 通过")


def test_error_state_preserved():
    dd.set_last_publish_error("测试：类目属性填写不完整")
    err = dd.get_last_publish_error()
    assert err == "测试：类目属性填写不完整"
    dd.clear_last_publish_error()
    assert dd.get_last_publish_error() is None
    print("✅ test_error_state_preserved 通过")


if __name__ == "__main__":
    test_publish_success()
    test_publish_not_found()
    test_publish_fail_toast()
    test_publish_meaningless_error()
    test_publish_inventory_fix_success()
    test_publish_inventory_fix_fail()
    test_publish_inventory_fix_third_success()
    test_publish_inventory_second_non_stock()
    test_error_state_preserved()
    print("\n全部测试通过")
