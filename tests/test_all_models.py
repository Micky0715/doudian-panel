#!/usr/bin/env python3
"""全量表格机型 parse_title 识别测试 — 甄别后缀 i/x/z/+/Pro/Max/5G/4G 等差异"""

import re, os, sys
from collections import defaultdict

# ===== 从 test_doudian_batch.py 完整复制的常量和函数 =====

BRAND_MAP = {
    "小米": "Xiaomi/小米", "华为": "Huawei/华为", "苹果": "Apple/苹果",
    "荣耀": "Honor/荣耀", "三星": "Samsung/三星", "OPPO": "OPPO/OPPO",
    "vivo": "vivo/vivo", "一加": "OnePlus/一加", "真我": "realme/真我",
    "iQOO": "iQOO/iQOO", "努比亚": "nubia/努比亚", "魅族": "Meizu/魅族",
    "中兴": "ZTE/中兴", "索尼": "Sony/索尼", "诺基亚": "Nokia/诺基亚",
    "摩托罗拉": "Motorola/摩托罗拉", "谷歌": "Google/谷歌", "黑莓": "BlackBerry/黑莓",
    "传音": "TECNO/传音", "联想": "Lenovo/联想", "Hi nova": "Hi nova/Hi nova",
    "华硕": "ASUS/华硕", "锤子": "smartisan/锤子", "麦芒": "麦芒/麦芒",
    "鼎桥": "鼎桥/鼎桥", "欧悦美": "REALMERRY/欧悦美", "黑鲨": "黑鲨/黑鲨",
}

CATEGORY_PATTERNS = [
    r'\s*二手手机\s*', r'\s*智能手机\s*',
    r'\s*二手(?:手)?\s*',
    r'(?<!\S)手机(?!\S)', r'\s*智能\s*',
    r'\s*系列\s*',
    r'\s+二\s*$',
]

SEO_KEYWORD_PATTERNS = [
    r'\s*全网通\s*', r'\s*国行\s*', r'\s*北斗卫星定位\s*',
    r'\s*双卡双待\s*', r'\s*移动联通电信\s*',
    r'\s*港版\s*', r'\s*美版\s*', r'\s*日版\s*',
    r'\s*韩版\s*', r'\s*欧版\s*',
    r'\s*全新\s*', r'\s*未拆封\s*', r'\s*未激活\s*',
    r'\s*包邮\s*', r'\s*现货\s*',
    r'\s*正品\s*', r'\s*原装\s*', r'\s*全国联保\s*',
    r'\b5G\b', r'\b5g\b', r'\b4G\b', r'\b4g\b',
    r'\bLTE\b', r'\blte\b', r'\bWiFi\b', r'\bWi-Fi\b',
    r'\bNFC\b', r'\bnfc\b',
    r'\s*全网\b',
]

_5G_MODEL_PROTECT = [
    (re.compile(r'\bOPPO\s+A3\s*\(?5G\)?\b', re.I), 'OPPO A3 (5G)'),
    (re.compile(r'\bREDMI\s+15R\s+5G\b', re.I),      'REDMI 15R 5G'),
    (re.compile(r'\bRedmi\s+13C\s+5G\b', re.I),       'Redmi 13C 5G'),
    (re.compile(r'\bGalaxy\s+A53\s+5G\b', re.I),       'Galaxy A53 5G'),
    (re.compile(r'\bGalaxy\s+A54\s+5G\b', re.I),       'Galaxy A54 5G'),
    (re.compile(r'\bGalaxy\s+A55\s+5G\b', re.I),       'Galaxy A55 5G'),
]

def _cleanup_model(raw_model: str) -> str:
    model = raw_model.strip()
    model = re.sub(r'\(\s*\)', '', model)
    model = re.sub(r'（\s*）', '', model)
    model = re.sub(r'\s+[全国网通]通?$', '', model)
    model = model.strip()
    if model != raw_model.strip():
        model = re.sub(r'\s+[全国网通]通?$', '', model).strip()
        model = re.sub(r'\(\s*\)', '', model).strip()
        model = re.sub(r'（\s*）', '', model).strip()
    return model

def _restore_5g_model(title: str, result: dict) -> None:
    model = result.get("model")
    if not model:
        return
    model_norm = model.strip().lower()
    brand_cn = result.get("brand_cn", "")
    brand_normalized = result.get("brand_normalized", "")
    brand_en = brand_normalized.split("/")[0].lower() if brand_normalized else ""
    for pattern, full_model in _5G_MODEL_PROTECT:
        if not pattern.search(title):
            continue
        base_with_brand = re.sub(r'\(?\s*5G\s*\)?', '', full_model, flags=re.I).strip()
        base_with_brand = re.sub(r'\(\s*\)|（\s*）', '', base_with_brand).strip()
        base_with_brand = base_with_brand.lower()
        base_no_brand = base_with_brand
        if brand_en and base_no_brand.startswith(brand_en):
            base_no_brand = base_no_brand[len(brand_en):].strip()
        if brand_cn and base_no_brand.startswith(brand_cn.lower()):
            base_no_brand = base_no_brand[len(brand_cn):].strip()
        if model_norm in (base_with_brand, base_no_brand):
            result["model"] = full_model
            break

def parse_title(title: str) -> dict:
    """从 test_doudian_batch.py 完整复制的 parse_title（含 _restore_5g_model）"""
    result = {"raw": title, "brand_cn": "", "brand_normalized": "", "model": "", "condition": ""}

    m = re.search(r'【([^】]+)】', title)
    if m:
        result["condition"] = m.group(1)
    else:
        m_dash = re.search(r'--\s*(99新|95新|9新|8成新|9成新)\s*--', title)
        if m_dash:
            result["condition"] = m_dash.group(1)
        else:
            m2 = re.match(r'(99新|95新|9新|8成新|9成新)', title)
            if m2:
                result["condition"] = m2.group(1)

    title_lower = title.lower()
    redmi_keywords = ["红米", "redmi"]
    is_redmi = any(kw in title_lower for kw in redmi_keywords)

    if is_redmi:
        cn_name = "小米"
        full_name = BRAND_MAP[cn_name]
        result["brand_cn"] = cn_name
        result["brand_normalized"] = full_name
        after_brand = re.sub(r'^【[^】]+】', '', title).strip()
        after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
        after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
        after_brand = re.sub(r'^(99新|95新|9新|8成新|9成新)\s*', '', after_brand).strip()
        after_brand = re.sub(r'^(Redmi/红米|REDMI/红米|redmi/红米|Xiaomi/小米|xiaomi/小米)\s*', '', after_brand, flags=re.I).strip()
        if "小米" in after_brand:
            after_brand = after_brand.split("小米", 1)[-1].strip()
            after_brand = re.sub(r'^(Redmi/红米|REDMI/红米|redmi/红米)\s*', '', after_brand, flags=re.I).strip()
        after_brand = after_brand.strip()
        after_brand_clean = after_brand
        for pat in CATEGORY_PATTERNS + SEO_KEYWORD_PATTERNS:
            after_brand_clean = re.sub(pat, ' ', after_brand_clean)
        after_brand_clean = after_brand_clean.strip()
        model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
        if model_match:
            result["model"] = _cleanup_model(model_match.group(1).strip())
        _restore_5g_model(title, result)
        return result

    for cn_name, full_name in BRAND_MAP.items():
        if cn_name in title:
            result["brand_cn"] = cn_name
            result["brand_normalized"] = full_name
            if cn_name in ('麦芒', '黑鲨') and not re.search(rf'{cn_name}\s', title):
                after_brand = title
            else:
                after_brand = title.split(cn_name, 1)[-1].strip()
            en_name = full_name.split("/")[0]
            if after_brand.lower().startswith(en_name.lower()):
                after_brand = re.split(en_name, after_brand, flags=re.I, maxsplit=1)[-1].strip()
                after_brand = after_brand.lstrip('/').strip()
            if after_brand.startswith(cn_name):
                after_brand = after_brand.split(cn_name, 1)[-1].strip()
            after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
            after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
            after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
            after_brand_clean = after_brand
            for pat in CATEGORY_PATTERNS + SEO_KEYWORD_PATTERNS:
                after_brand_clean = re.sub(pat, ' ', after_brand_clean)
            after_brand_clean = after_brand_clean.strip()
            model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
            if model_match:
                result["model"] = _cleanup_model(model_match.group(1).strip())
            break

    if not result["brand_cn"]:
        for cn_name, full_name in BRAND_MAP.items():
            en_name = full_name.split("/")[0]
            if en_name.lower() in title_lower:
                result["brand_cn"] = cn_name
                result["brand_normalized"] = full_name
                after_brand = re.split(en_name, title, flags=re.I)[-1].strip()
                if after_brand.lower().startswith(en_name.lower()):
                    after_brand = re.split(en_name, after_brand, flags=re.I, maxsplit=1)[-1].strip()
                    after_brand = after_brand.lstrip('/').strip()
                if after_brand.startswith(cn_name):
                    after_brand = after_brand.split(cn_name, 1)[-1].strip()
                after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
                after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
                after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
                after_brand_clean = after_brand
                for pat in CATEGORY_PATTERNS + SEO_KEYWORD_PATTERNS:
                    after_brand_clean = re.sub(pat, ' ', after_brand_clean)
                after_brand_clean = after_brand_clean.strip()
                model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
                if model_match:
                    result["model"] = _cleanup_model(model_match.group(1).strip())
                break

    if not result["brand_cn"]:
        title_lower2 = title.lower()
        inferred_brand = None
        if "galaxy" in title_lower2:
            inferred_brand = "三星"
        elif "magic" in title_lower2:
            inferred_brand = "荣耀"
        elif "mate" in title_lower2 or "nova" in title_lower2 or "pura" in title_lower2 or "畅享" in title_lower2:
            inferred_brand = "华为"
        elif "iphone" in title_lower2:
            inferred_brand = "苹果"
        elif "mac" in title_lower2:
            inferred_brand = "苹果"
        elif "xperia" in title_lower2:
            inferred_brand = "索尼"
        if inferred_brand:
            result["brand_cn"] = inferred_brand
            result["brand_normalized"] = BRAND_MAP[inferred_brand]
            after_brand = title
            after_brand = re.sub(r'^【[^】]+】', '', after_brand).strip()
            after_brand = re.sub(r'^--\s*\d+[新成][新新]?\s*--', '', after_brand).strip()
            after_brand = re.sub(r'--\s*\d+[新成][新新]?\s*--\s*$', '', after_brand).strip()
            after_brand = re.sub(r'^(99新|95新|9新|8成新|9成新)\s*', '', after_brand).strip()
            after_brand_clean = after_brand
            for pat in CATEGORY_PATTERNS + SEO_KEYWORD_PATTERNS:
                after_brand_clean = re.sub(pat, ' ', after_brand_clean)
            after_brand_clean = after_brand_clean.strip()
            model_match = re.match(r'([\u4e00-\u9fa5A-Za-z0-9\s+\-().·／/（）]+)', after_brand_clean)
            if model_match:
                result["model"] = _cleanup_model(model_match.group(1).strip())

    if result["brand_cn"] and not result["model"]:
        en_name = result["brand_normalized"].split("/")[0] if result["brand_normalized"] else ""
        if en_name:
            result["model"] = en_name

    _restore_5g_model(title, result)
    return result

# ===== 后缀分类 =====

def classify_suffixes(model: str) -> list:
    """返回型号的各类后缀标签"""
    tags = []
    model_lower = model.lower()
    # 5G / 4G
    if re.search(r'\b5G\b', model, re.I):
        if re.search(r'\(5G[^)]*\)', model, re.I):
            tags.append("5G(括号内)")
        else:
            tags.append("5G(裸)")
    if re.search(r'\b4G\b', model, re.I):
        tags.append("4G")
    # 后缀特征
    for label, pattern in [
        ("Pro", r'\bPro\b'), ("Pro+", r'\bPro\+\b'), ("Max", r'\bMax\b'),
        ("Ultra", r'\bUltra\b'), ("Plus", r'\bPlus\b|Plus$'),
        ("Mini", r'\bMini\b'), ("Lite", r'\bLite\b'), ("SE", r'\bSE\b'),
        ("Turbo", r'\bTurbo\b'), ("Neo", r'\bNeo\b'), ("GT", r'\bGT\b'),
        ("RSR", r'\bRSR\b'), ("PE", r'\bPE\b'),
        ("Art", r'\bArt\b'), ("至尊", r'至尊'), ("至臻", r'至臻'),
        ("典藏", r'典藏'), ("先锋", r'先锋'), ("风驰", r'风驰'),
        ("优享", r'优享'), ("活力", r'活力'), ("青春", r'青春'),
        ("透明", r'透明'), ("大师", r'大师'), ("探索", r'探索'),
        ("旗舰", r'旗舰'), ("冠军", r'冠军'), ("竞速", r'竞速'),
        ("闪速", r'闪速'), ("玩家", r'玩家'), ("限量", r'限量'),
        ("电竞", r'电竞'), ("游戏", r'游戏'), ("折叠", r'折叠'),
        ("卫星", r'卫星'),
    ]:
        if re.search(pattern, model):
            tags.append(label)
    # 数字后缀检测 (如 i/x/z/+ 作为字母后缀)
    for label, pattern in [
        ("+ 后缀", r'[A-Za-z0-9]\+\s*$'),
        ("i 后缀", r'\bi\s*$'),
        ("x 后缀", r'\bx\s*$'),
        ("z 后缀", r'\bz\s*$'),
        ("s 后缀", r'\bs\s*$'),
        ("t 后缀", r'\bt\s*$'),
    ]:
        if re.search(pattern, model):
            tags.append(label)
    return tags if tags else ["无特殊后缀"]


# ===== 标题构建 =====

def strip_brand_parens(table_brand: str) -> str:
    """从表格品牌列提取纯中文/英文品牌名"""
    return re.sub(r'[（(][^)）]*[)）]', '', table_brand).strip()

def build_title(table_brand: str, table_model: str) -> str:
    """根据表格品牌+机型模拟抖店标题"""
    brand_cn = strip_brand_parens(table_brand)
    return f"{brand_cn} {table_model} 二手手机"


# ===== 主测试流程 =====

def run():
    import openpyxl
    xlsx_path = "/Users/atrenew/Downloads/抖音直播货架货品价格规则数据导出(货品维度)-生产20260724174344.xlsx"
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb["Sheet1"]

    current_brand = ""
    all_rows = []
    for row in ws.iter_rows(min_row=4, values_only=True):
        brand_cell, model_cell = row[0], row[1]
        if brand_cell is not None:
            current_brand = str(brand_cell).strip()
        model = str(model_cell).strip() if model_cell is not None else ""
        if model:
            all_rows.append((current_brand, model))
    wb.close()

    print(f"表格总机型数: {len(all_rows)}")
    print()

    # 分组统计
    ok_list = []          # 完全匹配
    mismatch_list = []    # 有差异
    no_model_list = []    # model 为空
    no_brand_list = []    # brand 为空
    suffix_stats = defaultdict(lambda: {"total": 0, "ok": 0, "mismatch": 0, "examples": []})

    for table_brand, table_model in all_rows:
        title = build_title(table_brand, table_model)
        result = parse_title(title)
        actual_model = result["model"]
        brand_cn = result["brand_cn"]

        # 分类后缀
        tags = classify_suffixes(table_model)
        for tag in tags:
            suffix_stats[tag]["total"] += 1

        # 判断结果 — parse_title 设计上会剥离品牌前缀，比较时需同步处理
        # 计算「期望」：表格机型去掉品牌前缀后的纯型号
        # 使用与 parse_title 一致的逻辑：从表格品牌提取中文名，如果模型以该品牌名开头则剥离
        brand_cn_raw = strip_brand_parens(table_brand)
        expected_model = table_model
        for strip_order in [brand_cn_raw] + [en for _, f in BRAND_MAP.items() 
                                              for en in [f.split("/")[0], f.split("/")[-1]]
                                              if en != brand_cn_raw and en in table_model]:
            if expected_model.startswith(strip_order + " ") or expected_model == strip_order:
                expected_model = expected_model[len(strip_order):].strip()
                break
        # normalize both for comparison
        exp_norm = expected_model.strip().lower().replace(" ", "")
        act_norm = actual_model.strip().lower().replace(" ", "") if actual_model else ""

        if not brand_cn:
            no_brand_list.append((table_brand, table_model, title))
            for tag in tags:
                suffix_stats[tag]["mismatch"] += 1
                if len(suffix_stats[tag]["examples"]) < 3:
                    suffix_stats[tag]["examples"].append(f"品牌未识别: {table_model}")
        elif not actual_model:
            no_model_list.append((table_brand, table_model, result))
            for tag in tags:
                suffix_stats[tag]["mismatch"] += 1
                if len(suffix_stats[tag]["examples"]) < 3:
                    suffix_stats[tag]["examples"].append(f"型号为空: {table_model}")
        elif exp_norm == act_norm:
            ok_list.append((table_brand, expected_model, actual_model))
            for tag in tags:
                suffix_stats[tag]["ok"] += 1
        else:
            mismatch_list.append((table_brand, expected_model, actual_model, result, table_model))
            for tag in tags:
                suffix_stats[tag]["mismatch"] += 1
                if len(suffix_stats[tag]["examples"]) < 5:
                    suffix_stats[tag]["examples"].append(
                        f"原={table_model} → 期望={expected_model} → 实际={actual_model}"
                    )

    # ===== 输出 =====

    print("=" * 90)
    print("  测试总览")
    print("=" * 90)
    print(f"  总机型数:             {len(all_rows)}")
    print(f"  ✅ 完全匹配 (忽略空格): {len(ok_list)}")
    print(f"  ❌ 有差异:              {len(mismatch_list)}")
    print(f"  ⚠️  model 为空:         {len(no_model_list)}")
    print(f"  ⚠️  brand 未识别:       {len(no_brand_list)}")
    print()

    # ===== brand 未识别 =====
    if no_brand_list:
        print("=" * 90)
        print(f"  ⚠️ 品牌未识别 ({len(no_brand_list)} 条)")
        print("=" * 90)
        for b, m, t in no_brand_list:
            print(f"  表格品牌={b:20s} | 机型={m:30s}")
        print()

    # ===== model 为空 =====
    if no_model_list:
        print("=" * 90)
        print(f"  ⚠️ model 为空 ({len(no_model_list)} 条)")
        print("=" * 90)
        for b, m, r in no_model_list:
            print(f"  表格品牌={b:20s} | 机型={m:30s} | brand_cn={r['brand_cn']}")
        print()

    # ===== 差异详情 =====
    if mismatch_list:
        print("=" * 90)
        print(f"  ❌ 型号差异 ({len(mismatch_list)} 条)")
        print("=" * 90)

        # 按差异类型分
        by_type = defaultdict(list)
        for b, m_exp, m_act, r, m_orig in mismatch_list:
            exp_tags = classify_suffixes(m_exp)
            reason = ""
            # 5G 差异
            has_exp_5g = any('5G' in t for t in exp_tags)
            has_act_5g = '5G' in m_act.upper()
            if has_exp_5g and not has_act_5g:
                reason = "5G被剥离"
            elif '4G' in m_exp.upper() and '4G' not in m_act.upper():
                reason = "4G被剥离"
            elif re.search(r'\b(Pro|Max|Ultra|Plus|Mini|Lite|SE|Turbo|Neo|GT|RSR|PE|Art)\b', m_exp) and \
                 any(t in m_exp.lower() for t in ['pro', 'max', 'ultra', 'plus', 'mini', 'lite', 'se', 'turbo', 'neo', 'gt', 'rsr', 'pe', 'art']) and \
                 not any(t in m_act.lower() for t in ['pro', 'max', 'ultra', 'plus', 'mini', 'lite', 'se', 'turbo', 'neo', 'gt', 'rsr', 'pe', 'art']):
                reason = "英文后缀被剥离"
            elif re.search(r'[+\-]', m_exp) and not re.search(r'[+\-]', m_act):
                reason = "符号被剥离"
            else:
                # 其他差异
                reason = f"其他差异 (exp={m_exp}, act={m_act})"
            by_type[reason].append((b, m_exp, m_act))

        for reason, items in sorted(by_type.items(), key=lambda x: -len(x[1])):
            print(f"\n  --- {reason} ({len(items)} 条) ---")
            for b, m_e, m_a in items:
                print(f"    {b:16s} | {m_e:35s} → {m_a}")

    # ===== 后缀统计 =====
    print()
    print("=" * 90)
    print("  按后缀/特征分类统计")
    print("=" * 90)
    print(f"  {'特征':<16s} {'总数':>5s} {'✅OK':>5s} {'❌差异':>5s} {'通过率':>7s}  差异示例")
    print(f"  {'-'*16} {'-'*5} {'-'*5} {'-'*5} {'-'*7}   {'-'*40}")

    # 按重要性排序：差异多的在前
    sorted_suffixes = sorted(suffix_stats.items(), key=lambda x: (-x[1]["mismatch"], -x[1]["total"]))

    for tag, stats in sorted_suffixes:
        total, ok, mismatch = stats["total"], stats["ok"], stats["mismatch"]
        rate = f"{ok/total*100:.0f}%" if total > 0 else "  -  "
        examples = "; ".join(stats["examples"][:2]) if stats["examples"] else ""
        print(f"  {tag:<16s} {total:>5d} {ok:>5d} {mismatch:>5d} {rate:>7s}   {examples}")

    # ===== 5G 专项 =====
    print()
    print("=" * 90)
    print("  5G/4G 专项交叉分析")
    print("=" * 90)

    # 表格中官方带 5G 的机型
    five_g_official = []
    for b, m in all_rows:
        if re.search(r'\b5G\b', m, re.I):
            five_g_official.append((b, m))
    print(f"  官方含 5G: {len(five_g_official)} 条")

    # 解析后正确保留了 5G 的
    five_g_correct = []
    five_g_stripped = []
    for b, m in all_rows:
        if re.search(r'\b5G\b', m, re.I):
            title = build_title(b, m)
            result = parse_title(title)
            actual = result["model"]
            if actual and re.search(r'\b5G\b', actual, re.I):
                five_g_correct.append((b, m, actual))
            else:
                five_g_stripped.append((b, m, actual if actual else "(空)"))

    print(f"  解析后保留 5G: {len(five_g_correct)} / {len(five_g_official)}")
    print(f"  解析后丢失 5G: {len(five_g_stripped)} / {len(five_g_official)}")
    if five_g_stripped:
        print(f"\n  ❌ 5G 被错误剥离的机型:")
        for b, m, a in five_g_stripped:
            print(f"    {b:16s} | 期望={m:35s} → 实际={a}")

    # 意外恢复了原本没有 5G 的机型
    five_g_false_positive = []
    for b, m in all_rows:
        if not re.search(r'\b5G\b', m, re.I):
            title = build_title(b, m)
            result = parse_title(title)
            actual = result["model"]
            if actual and re.search(r'\b5G\b', actual, re.I):
                five_g_false_positive.append((b, m, actual))
    if five_g_false_positive:
        print(f"\n  ⚠️ 误恢复 5G（表格无5G但解析后有了）:")
        for b, m, a in five_g_false_positive:
            print(f"    {b:16s} | {m:35s} → {a}")
    else:
        print(f"  ✅ 无误恢复 5G 的情况")

    print()
    return len(mismatch_list) == 0 and len(no_model_list) == 0 and len(no_brand_list) == 0


if __name__ == "__main__":
    ok = run()
    print(f"  结论: {'✅ 全部通过' if ok else '❌ 存在差异，见上述详情'}")
