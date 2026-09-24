#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
验证 test_doudian_batch.py 修复后的型号识别与图片目录匹配。
使用 mock 绕过 playwright 依赖，只测 parse_title + get_image_dir。
"""
import sys
import os
from unittest.mock import MagicMock

# mock playwright，避免安装浏览器
sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_doudian_batch as dd

IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"
dd.IMAGE_BASE_DIR = IMAGE_BASE_DIR

def check(title, expected_exists=None):
    parsed = dd.parse_title(title)
    img_dir = dd.get_image_dir(parsed)
    exists = os.path.isdir(img_dir)
    status = "OK" if (expected_exists is None or exists == expected_exists) else "FAIL"
    print(f"[{status}] {title}")
    print(f"       brand={parsed['brand_cn']!r} model={parsed['model']!r}")
    print(f"       dir={img_dir} exists={exists}")
    if expected_exists is not None and exists != expected_exists:
        return False
    return True


def main():
    cases = [
        # ===== 本次修复的关键用例（应该匹配到目录） =====
        ("【95新】红米 Redmi Note 13 国行 全网通", True),
        ("【95新】Huawei/华为 华为 畅享90m Plus 二手手", False),
        ("荣耀 Power 二手手机", False),
        ("【95】荣耀Magic7 RSR 保时捷 国行 全网通", True),
        ("【95新】realme/真我 真我 GT Neo5 240W 二手手", False),
        ("【95新】realme/真我 真我 GT5 150W 二手手", True),

        # ===== 华为 Mate 系列（用户关心的） =====
        ("华为 Mate 60 Pro 二手手机", True),
        ("华为 Mate 70 Pro+ 二手手机", True),
        ("华为 Mate 50E 二手手机", True),
        ("华为 Mate 60 二手手机", True),
        ("华为 Mate 70 Pro 二手手机", True),

        # ===== 真我品牌其他机型 =====
        ("【95新】realme/真我 真我GT6 二手手机", True),
        ("【95新】realme/真我 真我 GT Neo5 150W 二手手机", True),
        ("【95新】realme/真我 真我GT Neo5 SE 二手手机", True),

        # ===== 之前前缀回归测试的关键用例 =====
        ("荣耀 X60 Pro 二手手机", True),
        ("荣耀 Power 2 二手手机", True),
        ("荣耀 Magic7 RSR 保时捷设计 二手手机", True),

        # ===== 本次日志中确实缺失图片的机型（应返回 False） =====
        ("【95】vivo vivo Y55s (2021款) 二手手机", False),
        ("【95】vivo vivo Y51s 二手手机", False),
        ("【95】vivo vivo Y500s 二手手机", False),
        ("【95】vivo vivo Y500i 二手手机", False),
        ("【95】vivo vivo Y500 Pro 二手手机", False),
        ("【95】vivo vivo Y500 二手手机", False),
        ("【95】vivo vivo Y31s标准版 二手手机", False),
        ("【95新】realme/真我 真我 Neo7 SE 二手手机", False),
        ("【95新】realme/真我 真我 Neo8 二手手机", False),
        ("【95新】realme/真我 真我 GT7 Pro 竞速版 二手", False),
        ("【95新】realme/真我 真我GT7 Pro 二手手机", False),
        ("【95新】realme/真我 真我 GT7 二手手机", False),
        ("【95新】realme/真我 真我 Neo7 Turbo 二手手机", False),
        ("【95新】realme/真我 真我 Neo7 二手手机", False),
        ("荣耀 X60 GT 二手手机", False),
        ("【95新】Huawei/华为 华为 畅享 MAX 二手手机", False),
    ]

    ok = 0
    fail = 0
    for title, expected in cases:
        if check(title, expected):
            ok += 1
        else:
            fail += 1
        print()

    print(f"===== 结果：通过 {ok} / 失败 {fail} =====")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
