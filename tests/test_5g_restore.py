#!/usr/bin/env python3
"""验证 _restore_5g_model 定向修复：5 个目标 5G 机型恢复 + 对照组防回归"""

import sys, os, re

# ===== 从 test_doudian_batch.py 复制的常量 =====
BRAND_MAP = {
    "苹果": "Apple/苹果", "华为": "Huawei/华为", "小米": "Xiaomi/小米",
    "三星": "Samsung/三星", "荣耀": "Honor/荣耀", "OPPO": "OPPO/OPPO",
    "vivo": "vivo/vivo", "OnePlus": "OnePlus/一加", "一加": "OnePlus/一加",
    "魅族": "Meizu/魅族", "真我": "Realme/真我", "realme": "Realme/真我",
    "摩托罗拉": "Motorola/摩托罗拉", "中兴": "ZTE/中兴",
    "努比亚": "Nubia/努比亚", "索尼": "Sony/索尼",
    "iQOO": "iQOO/iQOO", "黑鲨": "BlackShark/黑鲨",
    "麦芒": "Maimang/麦芒", "美图": "Meitu/美图",
    "大疆": "DJI/大疆", "苹果iPad": "Apple/苹果",
}

CATEGORY_PATTERNS = [
    r'\s*手机\s*', r'\s*二手(?:手)?手机\s*', r'\s*二手(?:手)?\s*',
    r'\s*平板\s*', r'\s*充电器\s*', r'\s*耳机\s*',
    r'\s*手表\s*', r'\s*笔记本\s*', r'\s*主机\s*',
    r'\s*系列\s*', r'\s*二$',
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


# ===== 从 test_doudian_batch.py 复制的函数 =====
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
        # 计算 full_model 去掉 5G 相关部分后的 base
        base_with_brand = re.sub(r'\(?\s*5G\s*\)?', '', full_model, flags=re.I).strip()
        base_with_brand = re.sub(r'\(\s*\)|（\s*）', '', base_with_brand).strip()
        base_with_brand = base_with_brand.lower()

        # 部分 parse_title 分支会把品牌前缀从 model 中剥离（如 OPPO A3 -> A3）
        base_no_brand = base_with_brand
        if brand_en and base_no_brand.startswith(brand_en):
            base_no_brand = base_no_brand[len(brand_en):].strip()
        if brand_cn and base_no_brand.startswith(brand_cn.lower()):
            base_no_brand = base_no_brand[len(brand_cn):].strip()
        # 同时兼容 Redmi/REDMI 这种 model 中保留品牌前缀的情况
        if model_norm in (base_with_brand, base_no_brand):
            result["model"] = full_model
            break


def parse_title(title: str) -> dict:
    """从 test_doudian_batch.py 完整复制的 parse_title — 含 _restore_5g_model 调用"""
    result = {"raw": title, "brand_cn": "", "brand_normalized": "", "model": "", "condition": ""}

    # 成色提取
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
        title_lower = title.lower()
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
        title_lower = title.lower()
        inferred_brand = None
        if "galaxy" in title_lower:
            inferred_brand = "三星"
        elif "magic" in title_lower:
            inferred_brand = "荣耀"
        elif "mate" in title_lower or "nova" in title_lower or "pura" in title_lower or "畅享" in title_lower:
            inferred_brand = "华为"
        elif "iphone" in title_lower:
            inferred_brand = "苹果"
        elif "mac" in title_lower:
            inferred_brand = "苹果"
        elif "xperia" in title_lower:
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


# ===== 测试用例 =====
TARGET_TESTS = [
    # (标题, 期望model, 描述)
    ("OPPO A3 (5G) 二手手机", "OPPO A3 (5G)", "★ OPPO A3 (5G) 括号格式"),
    ("OPPO A3 5G 二手手机", "OPPO A3 (5G)", "★ OPPO A3 5G 裸5G格式→恢复括号"),
    ("REDMI 15R 5G 二手手机【95新】", "REDMI 15R 5G", "★ REDMI 15R 5G 标准标题"),
    ("REDMI 15R 5G 二手手机", "REDMI 15R 5G", "★ REDMI 15R 5G 无成色框"),
    ("小米 REDMI 15R 5G 二手手机", "REDMI 15R 5G", "★ REDMI 15R 5G 含父品牌前缀"),
    ("Redmi 13C 5G 二手手机", "Redmi 13C 5G", "★ Redmi 13C 5G"),
    ("三星 Galaxy A53 5G 二手手机", "Galaxy A53 5G", "★ Galaxy A53 5G"),
    ("三星 Galaxy A54 5G 二手手机", "Galaxy A54 5G", "★ Galaxy A54 5G"),
    ("三星 Galaxy A55 5G 二手手机", "Galaxy A55 5G", "★ Galaxy A55 5G"),
    ("Galaxy A55 5G 二手手机", "Galaxy A55 5G", "★ Galaxy A55 5G 无品牌前缀(推断)"),
]

REGRESSION_TESTS = [
    # 确保其他 5G 相关机型不受影响
    ("Redmi Note 15R 5G 二手手机", "Redmi Note 15R", "Redmi Note 15R → 不恢复5G"),
    ("荣耀 X60 GT 5G 二手手机", "X60 GT", "荣耀 X60 GT → 不恢复5G"),
    ("OPPO A1 5G 二手手机", "A1", "OPPO A1 → 不恢复5G"),
    # 确保非5G机型不受影响
    ("华为 Mate 80 Pro Max 二手手机", "Mate 80 Pro Max", "Mate 80 Pro Max 无5G"),
    ("荣耀 Magic7 RSR 保时捷 二手手机", "Magic7 RSR 保时捷", "Magic7 RSR 正常"),
    ("Redmi 13C 二手手机", "Redmi 13C", "Redmi 13C 无5G后缀"),
]


def run_tests():
    print("=" * 70)
    print("  _restore_5g_model 定向修复验证")
    print("=" * 70)
    print()

    all_pass = True
    target_pass = 0
    target_fail = 0
    regression_pass = 0
    regression_fail = 0

    # 目标测试
    print("【目标 5G 机型 — 6 种标题 × 10 条】")
    print("-" * 70)
    for title, expected, desc in TARGET_TESTS:
        result = parse_title(title)
        actual = result["model"]
        ok = actual == expected
        if ok:
            target_pass += 1
            print(f"  ✅ {desc}")
        else:
            target_fail += 1
            all_pass = False
            print(f"  ❌ {desc}")
        print(f"     标题: {title}")
        print(f"     期望: {expected}  实际: {actual}  品牌: {result['brand_cn']}")
        print()

    # 回归测试
    print("【对照组 — 确保非目标机型不受影响】")
    print("-" * 70)
    for title, expected, desc in REGRESSION_TESTS:
        result = parse_title(title)
        actual = result["model"]
        ok = actual == expected
        if ok:
            regression_pass += 1
            print(f"  ✅ {desc}")
        else:
            regression_fail += 1
            all_pass = False
            print(f"  ❌ {desc}")
        print(f"     标题: {title}")
        print(f"     期望: {expected}  实际: {actual}")
        print()

    # 总结
    print("=" * 70)
    print(f"  目标: {target_pass}/{target_pass+target_fail} 通过")
    print(f"  回归: {regression_pass}/{regression_pass+regression_fail} 通过")
    print(f"  总计: {'✅ 全部通过' if all_pass else '❌ 存在失败'}")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
