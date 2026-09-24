#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
更严格的版本：
  - A/B 级候选仅限同品牌
  - 核心模型包含系列前缀（避免 iPhone 11 匹配 Reno 11）
  - 专门识别常见命名差异模式
"""
import sys
import os
import re
from unittest.mock import MagicMock

sys.modules['playwright'] = MagicMock()
sys.modules['playwright.sync_api'] = MagicMock()

import test_doudian_batch as dd
import openpyxl

IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"
dd.IMAGE_BASE_DIR = IMAGE_BASE_DIR

# 品牌英→中映射
BRAND_EN_TO_CN = {
    'apple': '苹果',
    'huawei': '华为',
    'honor': '荣耀',
    'xiaomi': '小米',
    'redmi': '红米',
    'realme': '真我',
    'oppo': 'OPPO',
    'vivo': 'vivo',
    'oneplus': '一加',
    'samsung': '三星',
    'iqoo': 'iQOO',
    'meizu': '魅族',
    'nubia': '努比亚',
    'asus': '华硕',
    'motorola': '摩托罗拉',
    'sony': '索尼',
    'zte': '中兴',
    'smartisan': '锤子',
    'lenovo': '联想',
    'nokia': '诺基亚',
    'blackshark': '黑鲨',
}


def load_excel_models(xlsx_path):
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb['Sheet1']
    rows = list(ws.iter_rows(values_only=True))
    models = []
    last_brand = None
    for row in rows[3:]:
        brand = row[0]
        model_text = row[1]
        if brand:
            last_brand = brand.strip()
        if model_text:
            models.append((last_brand, str(model_text).strip()))
    return models


def normalize(text):
    text = str(text)
    text = text.replace('（', '(').replace('）', ')')
    text = re.sub(r'\s*\([^)]*\)', '', text)
    text = re.sub(r'\s+', ' ', text).strip().lower()
    return text


def extract_series_core(model):
    """
    提取系列核心：保留第一个系列词 + 数字 + 主要后缀。
    例如：
      'GT Neo5 240W' → 'gt neo5 240w'
      'GT5 240W'     → 'gt5 240w'
      'iPhone 11 Pro'→ 'iphone 11 pro'
      'Reno 11 Pro'  → 'reno 11 pro'
    """
    norm = normalize(model)
    # 去掉无关词
    for w in ['二手手机', '二手', '国行', '全网通', '5g版', '4g版', '5g', '4g', '系列']:
        norm = norm.replace(w, '')
    norm = re.sub(r'\s+', ' ', norm).strip()

    # 第一个词通常是系列名
    tokens = norm.split()
    if not tokens:
        return ''

    series_tokens = [tokens[0]]
    for t in tokens[1:]:
        # 保留数字、带数字的词、主要后缀
        if re.search(r'\d', t) or t in ('pro', 'max', 'plus', 'ultra', 'turbo', 'se', 'mini', 'rsr', '至臻版', '典藏版', '保时捷设计', '保时捷', '风驰版', '活力版', '竞速版', '摄影师版'):
            series_tokens.append(t)
        elif t in ('neo', 'gt', 'ace', 'reno', 'find', 'mate', 'nova', 'pura', 'galaxy', 'note', 'x', 'y', 'z', 'a', 's', 'k'):
            series_tokens.append(t)

    return ' '.join(series_tokens)


def strip_brand_prefix(dir_name, brand):
    """去掉目录名中的品牌前缀。"""
    d = dir_name
    prefixes = []
    if brand and brand in BRAND_EN_TO_CN.values():
        prefixes.append(brand)
    # 也尝试一些常见前缀
    for p in ['华为', '荣耀', '小米', '红米', 'Redmi', '真我', 'realme', 'vivo', 'OPPO', '一加', '三星', '苹果', 'iPhone', 'Apple', 'iQOO', 'IQOO', '魅族', '努比亚']:
        prefixes.append(p)
    for p in prefixes:
        if d.startswith(p + ' '):
            d = d[len(p)+1:].strip()
            break
    return d


def is_chinese_only(text):
    """是否仅包含中文字符（可能含空格）。"""
    return bool(re.match(r'^[\u4e00-\u9fff\s]+$', text.strip()))


def find_candidates(excel_brand, parsed_brand, parsed_model, brand_dirs):
    """严格同品牌查找候选。"""
    candidates = []

    # 确定要查找的品牌目录
    target_brands = set()
    if parsed_brand:
        target_brands.add(parsed_brand)
    if excel_brand:
        target_brands.add(excel_brand)
        cn = BRAND_EN_TO_CN.get(excel_brand.lower())
        if cn:
            target_brands.add(cn)

    dirs_to_check = []
    for tb in target_brands:
        if tb in brand_dirs:
            dirs_to_check.extend([(tb, d) for d in brand_dirs[tb]])

    norm_model = normalize(parsed_model)
    model_core = extract_series_core(parsed_model)
    model_tokens = set(model_core.split()) if model_core else set()

    for bk, d in dirs_to_check:
        d_clean = strip_brand_prefix(d, bk)
        norm_dir = normalize(d_clean)
        dir_core = extract_series_core(d_clean)
        dir_tokens = set(dir_core.split()) if dir_core else set()

        # A1: 去掉空格后完全相同
        if norm_model.replace(' ', '') == norm_dir.replace(' ', ''):
            candidates.append(('A', '空格/括号差异', bk, d, d_clean))
            continue

        # A2: 核心完全相同
        if model_core and model_core == dir_core:
            candidates.append(('A', '核心完全相同', bk, d, d_clean))
            continue

        # A3: 一方是另一方的中文-only 前缀/后缀扩展
        if norm_model.startswith(norm_dir) or norm_dir.startswith(norm_model):
            extra = (norm_model[len(norm_dir):] if norm_dir.startswith(norm_model) else norm_dir[len(norm_model):]).strip()
            if extra and is_chinese_only(extra):
                candidates.append(('A', f'中文前后缀差异（{extra}）', bk, d, d_clean))
                continue

        # B1: 核心 token 大部分相同（用于发现系列别名如 GT Neo5 vs GT5）
        if model_tokens and dir_tokens:
            common = model_tokens & dir_tokens
            # 要求第一个系列 token相同，且数字 token 相同
            first_same = (norm_model.split()[0] == norm_dir.split()[0]) if norm_model and norm_dir else False
            model_nums = set(re.findall(r'\d+', model_core))
            dir_nums = set(re.findall(r'\d+', dir_core))
            nums_same = bool(model_nums & dir_nums) or (not model_nums and not dir_nums)

            if first_same and nums_same and len(common) >= min(len(model_tokens), len(dir_tokens)) - 1:
                candidates.append(('B', '系列内命名差异', bk, d, d_clean))
                continue

    return candidates


def main():
    xlsx_path = "/Users/atrenew/Downloads/抖音直播货架货品价格规则数据导出(货品维度)-生产20260724174344.xlsx"
    models = load_excel_models(xlsx_path)
    print(f"共读取 {len(models)} 条机型记录")

    brand_dirs = {}
    for entry in os.listdir(IMAGE_BASE_DIR):
        entry_path = os.path.join(IMAGE_BASE_DIR, entry)
        if os.path.isdir(entry_path):
            brand_dirs[entry] = [sub for sub in os.listdir(entry_path)
                                  if os.path.isdir(os.path.join(entry_path, sub))]
    print(f"本地品牌目录数：{len(brand_dirs)}")

    matched = []
    missing_A = []
    missing_B = []
    missing_C = []

    for brand, model_text in models:
        title = f"【95新】{model_text} 二手手机"
        parsed = dd.parse_title(title)
        img_dir = dd.get_image_dir(parsed)
        exists = os.path.isdir(img_dir)

        record = {
            'brand': brand,
            'model_text': model_text,
            'parsed_brand': parsed.get('brand_cn', ''),
            'parsed_model': parsed.get('model', ''),
            'img_dir': img_dir,
        }

        if exists:
            matched.append(record)
            continue

        candidates = find_candidates(brand, parsed.get('brand_cn', ''), parsed.get('model', ''), brand_dirs)
        record['candidates'] = candidates

        if any(c[0] == 'A' for c in candidates):
            missing_A.append(record)
        elif any(c[0] == 'B' for c in candidates):
            missing_B.append(record)
        else:
            missing_C.append(record)

    print(f"\n===== 统计 =====")
    print(f"匹配成功：{len(matched)}")
    print(f"A级-高置信度命名差异：{len(missing_A)}")
    print(f"B级-中置信度可能差异：{len(missing_B)}")
    print(f"C级-真正缺图：{len(missing_C)}")

    report_path = "/Users/atrenew/doudian-panel/outputs/excel_image_cross_reference_v3.txt"
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(f"Excel 货品表 vs 本地商品图片目录交叉检查报告（严格同品牌版）\n")
        f.write(f"Excel: {xlsx_path}\n")
        f.write(f"本地目录根：{IMAGE_BASE_DIR}\n")
        f.write(f"总计：{len(models)} | 匹配：{len(matched)} | A级命名差异：{len(missing_A)} | B级可能差异：{len(missing_B)} | C级缺图：{len(missing_C)}\n")
        f.write("=" * 80 + "\n")

        f.write("\n\n")
        f.write("=" * 80 + "\n")
        f.write("【A级：高置信度命名差异（建议加规则/别名）】\n")
        f.write("=" * 80 + "\n")
        for r in missing_A:
            f.write(f"\n- 表格：{r['brand']} / {r['model_text']}\n")
            f.write(f"  解析：{r['parsed_brand']} {r['parsed_model']}\n")
            f.write(f"  期望目录：{r['img_dir']}\n")
            f.write(f"  本地实际目录：\n")
            for level, reason, bk, d, d_clean in r['candidates']:
                if level == 'A':
                    f.write(f"    → {bk}/{d}  （{reason}）\n")

        f.write("\n\n")
        f.write("=" * 80 + "\n")
        f.write("【B级：中置信度可能差异（需人工判断是否同机型）】\n")
        f.write("=" * 80 + "\n")
        for r in missing_B:
            f.write(f"\n- 表格：{r['brand']} / {r['model_text']}\n")
            f.write(f"  解析：{r['parsed_brand']} {r['parsed_model']}\n")
            f.write(f"  期望目录：{r['img_dir']}\n")
            f.write(f"  本地相似目录：\n")
            for level, reason, bk, d, d_clean in r['candidates']:
                if level == 'B':
                    f.write(f"    → {bk}/{d}  （{reason}）\n")

        f.write("\n\n")
        f.write("=" * 80 + "\n")
        f.write(f"【C级：真正缺图（共 {len(missing_C)} 条，按品牌分组）】\n")
        f.write("=" * 80 + "\n")
        by_brand = {}
        for r in missing_C:
            by_brand.setdefault(r['parsed_brand'] or r['brand'], []).append(r)
        for b in sorted(by_brand.keys()):
            f.write(f"\n--- {b} ({len(by_brand[b])} 条) ---\n")
            for r in by_brand[b]:
                f.write(f"  {r['model_text']}  →  {r['parsed_model']}\n")

    print(f"\n详细报告已保存：{report_path}")

    print("\n===== A级命名差异摘要 =====")
    for r in missing_A[:40]:
        cands = [f"{c[2]}/{c[3]}" for c in r['candidates'] if c[0] == 'A']
        print(f"{r['parsed_brand']} {r['parsed_model']}  ←→  {', '.join(cands)}")
    if len(missing_A) > 40:
        print(f"... 等共 {len(missing_A)} 条，详见报告")

    print("\n===== B级可能差异摘要 =====")
    for r in missing_B[:20]:
        cands = [f"{c[2]}/{c[3]}" for c in r['candidates'] if c[0] == 'B']
        print(f"{r['parsed_brand']} {r['parsed_model']}  ←→  {', '.join(cands)}")
    if len(missing_B) > 20:
        print(f"... 等共 {len(missing_B)} 条，详见报告")


if __name__ == "__main__":
    main()
