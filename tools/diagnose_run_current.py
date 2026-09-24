#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
诊断 run_current_20260805_092048_325cfd19.log 中 50 个图片异常商品。

对每个被标记的商品：
1. 用脚本逻辑 parse_title + get_image_dir + 检查主图/白底图
2. 比对本地产真实情况，区分三类：
   - 真缺失: 目录本身就不存在（型号未匹配）
   - 缺主图: 目录存在、白底图齐全，但主图目录为空
   - 误判:   本地其实有可匹配目录/图片，但 get_image_dir 或检查逻辑漏掉了
"""
import sys, os, re, glob
from unittest.mock import MagicMock

sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_doudian_batch as dd

IMAGE_BASE_DIR = "/Users/atrenew/商品图片/7970406/3C数码及配件/二手手机"
dd.IMAGE_BASE_DIR = IMAGE_BASE_DIR

LOG = "/Users/atrenew/doudian-panel/panel_data/logs/run_current_20260805_092048_325cfd19.log"

def main():
    with open(LOG, encoding="utf-8") as f:
        text = f.read()
    # 提取 ⚠ [类型] 名称（型号：X）| 原始名称: 'Y'
    rows = []
    for m in re.finditer(r"⚠ \[(型号未匹配|主图白底图缺失)\] (.+?)（型号：(.+?)）\| 原始名称: '(.+)'", text):
        etype, name, model_reported, raw = m.groups()
        rows.append((etype, raw, model_reported))
    print(f"从日志提取到 {len(rows)} 个图片异常商品\n")

    # 预先缓存每个品牌子目录下的机型列表
    brand_subdirs = {}
    if os.path.isdir(IMAGE_BASE_DIR):
        for b in os.listdir(IMAGE_BASE_DIR):
            bp = os.path.join(IMAGE_BASE_DIR, b)
            if os.path.isdir(bp):
                brand_subdirs[b] = [d for d in os.listdir(bp) if os.path.isdir(os.path.join(bp, d))]

    summary = {"真缺失_型号未匹配": 0, "缺主图": 0, "缺白底图": 0, "误判_有目录漏匹配": 0, "误判_有主图漏检": 0}
    misjudge = []

    for etype, raw, model_reported in rows:
        parsed = dd.parse_title(raw)
        brand_cn = parsed.get("brand_cn", "")
        model = parsed.get("model", "")
        img_dir = dd.get_image_dir(parsed)
        dir_exists = os.path.isdir(img_dir)
        main_path = dd.get_main_image_path(parsed)
        has_main = os.path.isfile(main_path)
        aux = dd.get_aux_image_paths(parsed)
        has_aux = len(aux) > 0

        # 误判检测：若脚本认为目录不存在，看品牌子目录下是否有能匹配 model 的目录
        missed_dir = ""
        if not dir_exists and brand_cn:
            cand = brand_subdirs.get(brand_cn, [])
            ml = model.lower().replace(" ", "")
            for d in cand:
                dl = d.lower().replace(" ", "")
                if ml == dl or ml in dl or dl in ml:
                    missed_dir = d
                    break

        # 误判检测：若目录存在但被判缺主图，确认主图到底有没有
        main_in_dir = ""
        if dir_exists and not has_main:
            main_dir = os.path.join(img_dir, "主图")
            if os.path.isdir(main_dir):
                fs = [f for f in os.listdir(main_dir) if f.lower().endswith((".png", ".jpg", ".jpeg"))]
                if fs:
                    main_in_dir = fs[0]
            # 也看看机型根目录是否直接放了主图
            root_imgs = [f for f in os.listdir(img_dir) if f.lower().endswith((".png", ".jpg", ".jpeg")) and "白底" not in f]
            if not main_in_dir and root_imgs:
                main_in_dir = "根目录:" + root_imgs[0]

        if etype == "型号未匹配":
            if missed_dir:
                summary["误判_有目录漏匹配"] += 1
                misjudge.append((raw, brand_cn, model, img_dir, missed_dir))
                verdict = "误判(本地有目录但漏匹配)"
            else:
                summary["真缺失_型号未匹配"] += 1
                verdict = "真缺失"
        else:  # 主图白底图缺失
            if main_in_dir:
                summary["误判_有主图漏检"] += 1
                misjudge.append((raw, brand_cn, model, img_dir, "主图实际有:" + main_in_dir))
                verdict = "误判(主图实际存在)"
            elif not has_aux:
                summary["缺白底图"] += 1
                verdict = "缺白底图(真)"
            else:
                summary["缺主图"] += 1
                verdict = "缺主图(真)"

        print(f"[{verdict}] {raw}")
        print(f"    品牌={brand_cn!r} 型号={model!r} | 脚本目录={img_dir}")
        print(f"    目录存在={dir_exists} 主图={has_main} 白底图数={len(aux)}"
              + (f" | 漏匹配目录={missed_dir}" if missed_dir else "")
              + (f" | 主图实际={main_in_dir}" if main_in_dir else ""))
        print()

    print("="*60)
    print("汇总:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if misjudge:
        print("\n误判明细:")
        for raw, brand_cn, model, img_dir, detail in misjudge:
            print(f"  - {raw}  [{brand_cn}/{model}] {detail}")

if __name__ == "__main__":
    main()
