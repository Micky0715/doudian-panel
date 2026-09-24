#!/usr/bin/env python3
"""验证标题解析修复：无空格/小写后缀型号能正确规范化"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_doudian_batch import parse_title

CASES = [
    ("荣耀90pro 二手手机", "荣耀", "90 Pro"),
    ("荣耀300pro 二手手机", "荣耀", "300 Pro"),
    ("荣耀90GT 二手手机", "荣耀", "90 GT"),
    ("荣耀x60gt 二手手机", "荣耀", "X60 GT"),
    ("荣耀100 Pro 二手手机", "荣耀", "100 Pro"),  # 已规范，应保持
    ("荣耀 X60 GT 二手手机", "荣耀", "X60 GT"),   # 已规范，应保持
    ("华为Pura80Pro 二手手机", "华为", "Pura80Pro"),  # 不符合纯数字/单字母+数字+后缀模式，保持原样
]

print("=" * 80)
print("标题解析型号规范化验证")
print("=" * 80)

all_ok = True
for title, expected_brand, expected_model in CASES:
    parsed = parse_title(title)
    brand = parsed.get("brand_cn", "")
    model = parsed.get("model", "")
    ok = (brand == expected_brand and model == expected_model)
    status = "✅ PASS" if ok else "❌ FAIL"
    print(f"{status} | {title}")
    print(f"       期望: {expected_brand} {expected_model}")
    print(f"       实际: {brand} {model}")
    if not ok:
        all_ok = False
    print()

print("=" * 80)
print("全部通过" if all_ok else "存在失败")
print("=" * 80)
sys.exit(0 if all_ok else 1)
