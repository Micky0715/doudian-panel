#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试成色识别容错：验证 【95】/【99】/【9】 等简写也能识别为 95新/99新/9新"""

import sys
import os
from types import ModuleType

# 屏蔽 playwright 依赖，仅测试标题解析逻辑
fake_sync_api = ModuleType('playwright.sync_api')
fake_sync_api.sync_playwright = lambda: None
sys.modules['playwright.sync_api'] = fake_sync_api

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_doudian_batch as tdb


TEST_CASES = [
    # (标题, 期望成色)
    ("95新 Huawei/华为 【95】华为 Mate X7 典藏版 国行 全网通 二手手机", "95新"),
    ("99新 Apple/苹果 【99】iPhone 14 Pro 二手手机", "99新"),
    ("9新 小米 【9】红米 Note 12 二手手机", "9新"),
    # 原有标准格式应保持不变
    ("95新 Huawei/华为 【95新】华为 Mate X7 二手手机", "95新"),
    ("99新 Apple/苹果 【99新】iPhone 14 Pro 二手手机", "99新"),
    ("9新 Xiaomi/小米 【9新】红米 Note 12 二手手机", "9新"),
    # 双横线格式
    ("--95新-- Huawei/华为 华为 Mate X7 二手手机", "95新"),
    ("--99新-- Apple/苹果 iPhone 14 Pro 二手手机", "99新"),
    # 无前缀开头格式
    ("95新 Huawei/华为 华为 Mate X7 二手手机", "95新"),
    ("99新 Apple/苹果 iPhone 14 Pro 二手手机", "99新"),
    ("9新 Xiaomi/小米 红米 Note 12 二手手机", "9新"),
    # Redmi 分支同样应识别简写成色
    ("95新 Redmi/红米 【95】Redmi Note 12 Turbo 二手手机", "95新"),
    ("99新 Redmi/红米 【99】Redmi K70 二手手机", "99新"),
    # 不相关方括号不应误识别
    ("Huawei/华为 华为 Mate X7 典藏版 二手手机", ""),
]


def main():
    ok = 0
    fail = 0
    for title, expected in TEST_CASES:
        parsed = tdb.parse_title(title)
        actual = parsed.get("condition", "")
        if actual == expected:
            ok += 1
            print(f"✅ [{title[:50]}...] 成色={actual}")
        else:
            fail += 1
            print(f"❌ [{title[:50]}...] 期望={expected!r}, 实际={actual!r}")
            print(f"   完整解析: {parsed}")

    # 验证 get_detail_image_paths 对简写成色也能找到真实文件
    print("\n--- 验证 get_detail_image_paths 文件匹配 ---")
    detail_ok = 0
    detail_fail = 0
    for title, expected in [
        ("95新 Huawei/华为 【95】华为 Mate X7 典藏版 国行 全网通 二手手机", "95新.jpg"),
        ("99新 Apple/苹果 【99】iPhone 14 Pro 二手手机", "99新.jpg"),
        ("9新 小米 【9】红米 Note 12 二手手机", "9新.jpg"),
    ]:
        parsed = tdb.parse_title(title)
        paths = tdb.get_detail_image_paths(parsed)
        has_condition = any(expected in p for p in paths)
        count = len(paths)
        if has_condition and count == 14:
            detail_ok += 1
            print(f"✅ [{title[:40]}...] 找到 {expected}, 共 {count} 张")
        else:
            detail_fail += 1
            print(f"❌ [{title[:40]}...] 期望找到 {expected} 且共14张, 实际 {count} 张, paths={paths}")

    total_ok = ok + detail_ok
    total_fail = fail + detail_fail
    total = len(TEST_CASES) + 3
    print(f"\n测试结果: 通过 {total_ok}/{total}, 失败 {total_fail}/{total}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
