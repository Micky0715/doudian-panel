#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
以抖音货品价格规则表(Excel)为标准，逐条检测本地商品图片目录匹配情况。
输出：
  1. 匹配成功（含通过别名/后缀容错匹配到的）
  2. 本地无目录（真正缺图）
  3. 可能命名相似的候选目录（用于补图或加别名）
"""
import sys
import os
import re
from difflib import SequenceMatcher
from unittest.mock import MagicMock

# mock playwright，避免安装浏览器
sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

import test_doudian_batch as dd
import openpyxl

IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"
dd.IMAGE_BASE_DIR = IMAGE_BASE_DIR


def load_excel_models(xlsx_path):
    """从 Excel 读取品牌/型号，填充 brand 空值，返回 (brand, model_text) 列表。"""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb['Sheet1']
    rows = list(ws.iter_rows(values_only=True))
    models = []
    last_brand = None
    for row in rows[3:]:  # 第4行开始是数据
        brand = row[0]
        model_text = row[1]
        if brand:
            last_brand = brand.strip()
        if model_text:
            models.append((last_brand, str(model_text).strip()))
    return models


def similar(a, b):
    return SequenceMatcher(None, a, b).ratio()


def find_similar_dirs(target_model, brand_dirs, top_n=3):
    """在所有品牌目录里，找与 target_model 名称最相近的目录名。"""
    candidates = []
    target_clean = re.sub(r'[^\w\u4e00-\u9fff]+', '', target_model.lower())
    for brand, dirs in brand_dirs.items():
        for d in dirs:
            # 去掉品牌前缀
            d_name = d
            for prefix in [brand, '华为', '荣耀', '小米', '红米', 'Redmi', '真我', 'realme', 'vivo', 'OPPO', '一加', '三星', '苹果', 'iPhone']:
                if d_name.startswith(prefix + ' '):
                    d_name = d_name[len(prefix)+1:].strip()
                    break
            d_clean = re.sub(r'[^\w\u4e00-\u9fff]+', '', d_name.lower())
            if not d_clean:
                continue
            ratio = similar(target_clean, d_clean)
            candidates.append((ratio, brand, d, d_name))
    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[:top_n]


def classify_match(model_text, parsed, img_dir, brand_dirs):
    """判断一条记录的匹配类型。"""
    exists = os.path.isdir(img_dir)
    brand_cn = parsed.get('brand_cn') or ''
    model = parsed.get('model') or ''

    if exists:
        return 'MATCHED', None

    # 未匹配，尝试找相似目录
    similar_dirs = find_similar_dirs(model, brand_dirs, top_n=3)

    # 检查是否是因为别名/后缀容错本可以匹配到但实际目录不存在
    return 'MISSING', similar_dirs


def main():
    xlsx_path = "/Users/atrenew/Downloads/抖音直播货架货品价格规则数据导出(货品维度)-生产20260724174344.xlsx"
    models = load_excel_models(xlsx_path)
    print(f"共读取 {len(models)} 条机型记录")

    # 预扫描本地目录结构：brand -> [dir1, dir2, ...]
    brand_dirs = {}
    if os.path.isdir(IMAGE_BASE_DIR):
        for entry in os.listdir(IMAGE_BASE_DIR):
            entry_path = os.path.join(IMAGE_BASE_DIR, entry)
            if os.path.isdir(entry_path):
                subdirs = []
                for sub in os.listdir(entry_path):
                    sub_path = os.path.join(entry_path, sub)
                    if os.path.isdir(sub_path):
                        subdirs.append(sub)
                brand_dirs[entry] = subdirs
    print(f"本地品牌目录数：{len(brand_dirs)}")

    matched = []
    missing = []
    parse_empty = []

    for brand, model_text in models:
        # 模拟一个常见抖店标题
        title = f"【95新】{model_text} 二手手机"
        parsed = dd.parse_title(title)
        img_dir = dd.get_image_dir(parsed)

        status, similar_dirs = classify_match(model_text, parsed, img_dir, brand_dirs)

        record = {
            'brand': brand,
            'model_text': model_text,
            'parsed_brand': parsed.get('brand_cn', ''),
            'parsed_model': parsed.get('model', ''),
            'img_dir': img_dir,
            'similar_dirs': similar_dirs,
        }

        if not parsed.get('model'):
            parse_empty.append(record)
        elif status == 'MATCHED':
            matched.append(record)
        else:
            missing.append(record)

    print(f"\n===== 统计 =====")
    print(f"匹配成功：{len(matched)}")
    print(f"本地无目录：{len(missing)}")
    print(f"解析为空：{len(parse_empty)}")

    # 输出详细报告文件
    report_path = "/Users/atrenew/doudian-panel/outputs/excel_image_cross_reference.txt"
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(f"Excel 货品表 vs 本地商品图片目录交叉检查报告\n")
        f.write(f"Excel: {xlsx_path}\n")
        f.write(f"本地目录根：{IMAGE_BASE_DIR}\n")
        f.write(f"总计机型：{len(models)} | 匹配：{len(matched)} | 缺图：{len(missing)} | 解析失败：{len(parse_empty)}\n")
        f.write("=" * 80 + "\n\n")

        f.write("\n" + "=" * 80 + "\n")
        f.write("【一、解析为空的机型】\n")
        f.write("=" * 80 + "\n")
        for r in parse_empty:
            f.write(f"- 表格：{r['brand']} / {r['model_text']}\n")
            f.write(f"  解析：brand={r['parsed_brand']!r}, model={r['parsed_model']!r}\n")
            f.write(f"  目录：{r['img_dir']}\n\n")

        f.write("\n" + "=" * 80 + "\n")
        f.write("【二、本地无目录的机型（真正缺图）】\n")
        f.write("=" * 80 + "\n")
        # 按品牌分组
        missing_by_brand = {}
        for r in missing:
            missing_by_brand.setdefault(r['parsed_brand'] or r['brand'], []).append(r)
        for b in sorted(missing_by_brand.keys()):
            f.write(f"\n--- {b} ({len(missing_by_brand[b])} 条) ---\n")
            for r in missing_by_brand[b]:
                f.write(f"- 表格型号：{r['model_text']}\n")
                f.write(f"  解析型号：{r['parsed_model']}\n")
                f.write(f"  期望目录：{r['img_dir']}\n")
                if r['similar_dirs']:
                    f.write(f"  相似候选：\n")
                    for ratio, sim_brand, sim_dir, sim_name in r['similar_dirs']:
                        f.write(f"    [{ratio:.2f}] {sim_brand}/{sim_dir}  (去品牌后:{sim_name})\n")
                f.write("\n")

        f.write("\n" + "=" * 80 + "\n")
        f.write("【三、匹配成功的机型（含别名/后缀容错）】\n")
        f.write("=" * 80 + "\n")
        matched_by_brand = {}
        for r in matched:
            matched_by_brand.setdefault(r['parsed_brand'] or r['brand'], []).append(r)
        for b in sorted(matched_by_brand.keys()):
            f.write(f"\n--- {b} ({len(matched_by_brand[b])} 条) ---\n")
            for r in matched_by_brand[b]:
                f.write(f"- {r['model_text']} → {r['parsed_model']} → {r['img_dir']}\n")

    print(f"\n详细报告已保存：{report_path}")

    # 控制台额外输出：重点展示可能命名差异的情况（相似度>0.5）
    print("\n===== 疑似命名差异（缺图但有高相似度本地目录）=====")
    count = 0
    for r in missing:
        if r['similar_dirs'] and r['similar_dirs'][0][0] >= 0.5:
            ratio, sim_brand, sim_dir, sim_name = r['similar_dirs'][0]
            print(f"[{ratio:.2f}] {r['parsed_brand']} {r['parsed_model']}  ←可能对应→  {sim_brand}/{sim_dir}")
            count += 1
    print(f"共 {count} 条疑似命名差异")


if __name__ == "__main__":
    main()
