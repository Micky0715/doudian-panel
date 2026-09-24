"""本地验证：型号选择校验门修复（真我GT Pro 漏掉 Pro 后缀问题）。

不依赖浏览器，直接测试：
  1. parse_title 对真我GT系列及各类后缀（Pro/Ultra/Pro Max/+/x/z/i 等）的解析结果
  2. _is_acceptable_model_match 对"带品牌前缀的下拉选项"的接受/拒绝判定
     —— 核心修复点：确保 "真我GT" 不会被误判为 "GT Pro" 的合法匹配
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_doudian_batch as dd


def test_parse_titles():
    print("===== 1) parse_title 型号解析 =====")
    cases = [
        ("真我GT Pro 二手手机", "GT Pro"),
        ("真我GT7 Pro 二手手机", "GT7 Pro"),
        ("真我GT7 Pro+ 二手手机", "GT7 Pro+"),
        ("真我GT Neo6 二手手机", "GT Neo6"),
        ("真我GT6 二手手机", "GT6"),
        ("realme GT Pro 二手手机", "GT Pro"),
        # 其他品牌的后缀场景
        ("华为 Mate 70 Pro+ 二手手机", "Mate 70 Pro+"),
        ("荣耀 100 Pro 二手手机", "100 Pro"),
        ("小米 14 Ultra 二手手机", "14 Ultra"),
        ("苹果 iPhone 15 Pro Max 二手手机", "iPhone 15 Pro Max"),
        ("vivo X100 Pro 二手手机", "X100 Pro"),
        ("OPPO Find X7 二手手机", "Find X7"),
        ("小米 13 二手手机", "13"),
    ]
    ok = True
    for title, expect in cases:
        r = dd.parse_title(title)
        got = r.get("model", "")
        flag = "✅" if got == expect else "❌"
        if got != expect:
            ok = False
        print(f"  {flag} {title!r:40} -> brand={r.get('brand_cn'):6} model={got!r:20} (期望 {expect!r})")
    return ok


def test_match_gate():
    print("\n===== 2) _is_acceptable_model_match 校验门 =====")
    # (selected下拉选项, model解析型号, brand_cn, brand_en, 期望acceptable)
    cases = [
        # —— 核心修复：真我GT Pro 不能误接受 "GT" ——
        ("真我GT Pro", "GT Pro", "真我", "realme", True),    # 正确完整匹配
        ("真我GT", "GT Pro", "真我", "realme", False),       # 截断：漏掉 Pro → 拒绝
        ("真我GT7 Pro", "GT7 Pro", "真我", "realme", True),
        ("真我GT7", "GT7 Pro", "真我", "realme", False),     # 截断 → 拒绝
        ("真我GT Neo6", "GT Neo6", "真我", "realme", True),
        ("真我GT Neo", "GT Neo6", "真我", "realme", False),  # 截断 → 拒绝
        # 无品牌前缀的原有场景（不受影响）
        ("GT Pro", "GT Pro", "真我", "realme", True),
        ("GT", "GT Pro", "真我", "realme", False),
        ("GT7 Pro", "GT7 Pro", "真我", "realme", True),
        # 扩展匹配（选中比model更长）也应拒绝
        ("真我GT Pro 5G", "GT Pro", "真我", "realme", False),
        ("真我GT", "GT", "真我", "realme", True),            # model 本就是 GT，完整匹配
        # 其他品牌后缀
        ("Mate 70 Pro+", "Mate 70 Pro+", "华为", "Huawei", True),
        ("Mate 70", "Mate 70 Pro+", "华为", "Huawei", False),  # 截断 → 拒绝
        ("X60 Pro Max", "X60 Pro Max", "荣耀", "Honor", True),
        ("X60 Pro", "X60 Pro Max", "荣耀", "Honor", False),    # 截断 → 拒绝
        ("14 Ultra", "14 Ultra", "小米", "Xiaomi", True),
        ("14", "14 Ultra", "小米", "Xiaomi", False),            # 截断 → 拒绝
        ("iPhone 15 Pro Max", "iPhone 15 Pro Max", "苹果", "Apple", True),
        ("iPhone 15", "iPhone 15 Pro Max", "苹果", "Apple", False),  # 截断 → 拒绝
    ]
    ok = True
    for selected, model, bcn, ben, expect in cases:
        acceptable, reason = dd._is_acceptable_model_match(selected, model, bcn, ben)
        flag = "✅" if acceptable == expect else "❌"
        if acceptable != expect:
            ok = False
        print(f"  {flag} selected={selected!r:18} model={model!r:20} -> acceptable={acceptable!s:5} (期望 {expect!s})")
        if acceptable != expect:
            print(f"       原因: {reason}")
    return ok


if __name__ == "__main__":
    p1 = test_parse_titles()
    p2 = test_match_gate()
    print("\n===== 结果 =====")
    if p1 and p2:
        print("✅ 全部通过")
        sys.exit(0)
    else:
        print("❌ 存在失败项")
        sys.exit(1)
