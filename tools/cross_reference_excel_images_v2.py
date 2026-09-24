#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
改进版：以货品表为标准，重点发现同品牌下的命名差异。
排除跨品牌误匹配，按置信度分三档输出：
  A. 高置信度命名差异（同品牌、同系列、仅后缀/空格/别名不同）
  B. 中置信度（同品牌、型号相近但可能不同代）
  C. 真正缺图（同品牌无相近目录）
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
    """统一空格、去括号、转小写，用于比较。"""
    text = str(text)
    # 全角括号转半角
    text = text.replace('（', '(').replace('）', ')')
    # 去掉括号及内容
    text = re.sub(r'\s*\([^)]*\)', '', text)
    # 去掉常见无关词
    for w in ['二手手机', '二手', '国行', '全网通', '5G版', '4G版', '5G', '4G', '系列']:
        text = text.replace(w, '')
    # 空格归一
    text = re.sub(r'\s+', ' ', text).strip()
    return text.lower()


def extract_core(model):
    """提取型号核心标识：保留字母+数字组合。"""
    norm = normalize(model)
    # 保留有意义的 token：字母数字混合
    tokens = re.findall(r'[a-z]+\d+|\d+[a-z]+|\d+|pro|max|plus|ultra|turbo|se|gt|neo|rsr', norm)
    return ' '.join(tokens)


def brand_normalized(brand):
    """品牌名归一。"""
    if not brand:
        return ''
    b = brand.lower()
    aliases = {
        'apple': '苹果',
        'huawei': '华为',
        'honor': '荣耀',
        'xiaomi': '小米',
        'redmi': '红米',
        'realme': '真我',
        'oppo': 'oppo',
        'vivo': 'vivo',
        'oneplus': '一加',
        'samsung': '三星',
        'iqoo': 'iqoo',
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
    return aliases.get(b, brand)


def find_candidates(excel_brand, parsed_model, brand_dirs):
    """只在同品牌目录内找可能候选。"""
    brand_key = brand_normalized(excel_brand)
    candidates = []

    # 优先在同品牌目录查找
    dirs_to_check = []
    if brand_key and brand_key in brand_dirs:
        dirs_to_check.extend([(brand_key, d) for d in brand_dirs[brand_key]])

    # 如果 Excel 品牌是英文（如 Apple），也查中文别名
    for bk, dirs in brand_dirs.items():
        if bk != brand_key:
            dirs_to_check.extend([(bk, d) for d in dirs])

    norm_model = normalize(parsed_model)
    model_core = extract_core(parsed_model)

    for bk, d in dirs_to_check:
        # 去掉目录中的品牌前缀
        d_clean = d
        for prefix in [bk, '华为', '荣耀', '小米', '红米', 'Redmi', '真我', 'realme', 'vivo', 'OPPO', '一加', '三星', '苹果', 'iPhone', 'Apple', 'iQOO', '魅族', '努比亚']:
            if d_clean.startswith(prefix + ' '):
                d_clean = d_clean[len(prefix)+1:].strip()
                break

        norm_dir = normalize(d_clean)
        dir_core = extract_core(d_clean)

        # A级：去掉空格后完全相同
        if norm_model.replace(' ', '') == norm_dir.replace(' ', ''):
            candidates.append(('A', '空格/括号差异', bk, d, d_clean))
            continue

        # A级：核心相同，仅常见后缀差异
        if model_core and model_core == dir_core:
            candidates.append(('A', '核心相同', bk, d, d_clean))
            continue

        # B级：核心相近（用于发现可能的系列别名/数字后缀差异）
        if model_core and dir_core:
            m_tokens = set(model_core.split())
            d_tokens = set(dir_core.split())
            common = m_tokens & d_tokens
            if len(common) >= max(1, min(len(m_tokens), len(d_tokens)) - 1):
                candidates.append(('B', '核心相近', bk, d, d_clean))
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
    missing_A = []  # 高置信度命名差异
    missing_B = []  # 中置信度
    missing_C = []  # 真正缺图

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

        candidates = find_candidates(brand, parsed.get('model', ''), brand_dirs)
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

    report_path = "/Users/atrenew/doudian-panel/outputs/excel_image_cross_reference_v2.txt"
    os.makedirs(os.path.dirname(report_path), exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("=" * 80 + "\n")
        f.write(f"Excel 货品表 vs 本地商品图片目录交叉检查报告（改进版）\n")
        f.write(f"Excel: {xlsx_path}\n")
        f.write(f"本地目录根：{IMAGE_BASE_DIR}\n")
        f.write(f"总计：{len(models)} | 匹配：{len(matched)} | A级命名差异：{len(missing_A)} | B级可能差异：{len(missing_B)} | C级缺图：{len(missing_C)}\n")
        f.write("=" * 80 + "\n")

        f.write("\n\n")
        f.write("=" * 80 + "\n")
        f.write("【A级：高置信度命名差异（建议加别名/后缀规则）】\n")
        f.write("=" * 80 + "\n")
        for r in missing_A:
            f.write(f"\n- 表格：{r['brand']} / {r['model_text']}\n")
            f.write(f"  解析：{r['parsed_brand']} {r['parsed_model']}\n")
            f.write(f"  期望目录：{r['img_dir']}\n")
            f.write(f"  发现本地候选：\n")
            for level, reason, bk, d, d_clean in r['candidates']:
                if level == 'A':
                    f.write(f"    → {bk}/{d}  （{reason}，去品牌后：{d_clean}）\n")

        f.write("\n\n")
        f.write("=" * 80 + "\n")
        f.write("【B级：中置信度可能差异（需人工判断是否同机型）】\n")
        f.write("=" * 80 + "\n")
        for r in missing_B:
            f.write(f"\n- 表格：{r['brand']} / {r['model_text']}\n")
            f.write(f"  解析：{r['parsed_brand']} {r['parsed_model']}\n")
            f.write(f"  期望目录：{r['img_dir']}\n")
            f.write(f"  相似候选：\n")
            for level, reason, bk, d, d_clean in r['candidates']:
                if level == 'B':
                    f.write(f"    → {bk}/{d}  （{reason}，去品牌后：{d_clean}）\n")

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

    # 控制台摘要
    print("\n===== A级命名差异摘要 =====")
    for r in missing_A[:30]:
        cands = [f"{c[2]}/{c[3]}" for c in r['candidates'] if c[0] == 'A']
        print(f"{r['parsed_brand']} {r['parsed_model']}  ←→  {', '.join(cands)}")
    if len(missing_A) > 30:
        print(f"... 等共 {len(missing_A)} 条，详见报告")


if __name__ == "__main__":
    main()
