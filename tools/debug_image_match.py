#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
独立调试脚本：不依赖 playwright，只测试 parse_title + get_image_dir
"""
import re
import os
import sys

# 直接从 test_doudian_batch.py 复制关键常量与函数（避免导入 playwright）
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCT_IMAGE_ROOT = "/Users/atrenew/商品图片"
IMAGE_BASE_DIR = ""
DETAIL_IMAGE_DIR = "/Users/atrenew/手机商详"

BRAND_MAP = {
    "小米": "Xiaomi/小米",
    "华为": "Huawei/华为",
    "苹果": "Apple/苹果",
    "荣耀": "Honor/荣耀",
    "三星": "Samsung/三星",
    "OPPO": "OPPO/OPPO",
    "vivo": "vivo/vivo",
    "一加": "OnePlus/一加",
    "真我": "realme/真我",
    "iQOO": "iQOO/iQOO",
    "努比亚": "nubia/努比亚",
    "魅族": "Meizu/魅族",
    "中兴": "ZTE/中兴",
    "索尼": "Sony/索尼",
    "诺基亚": "Nokia/诺基亚",
    "摩托罗拉": "Motorola/摩托罗拉",
    "谷歌": "Google/谷歌",
    "黑莓": "BlackBerry/黑莓",
    "传音": "TECNO/传音",
    "联想": "Lenovo/联想",
    "Hi nova": "Hi nova/Hi nova",
    "华硕": "ASUS/华硕",
    "锤子": "smartisan/锤子",
    "麦芒": "麦芒/麦芒",
    "鼎桥": "鼎桥/鼎桥",
    "欧悦美": "REALMERRY/欧悦美",
    "黑鲨": "黑鲨/黑鲨",
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
    # 1) 去掉空括号/空全角括号以及仅含空白的括号
    model = re.sub(r'\(\s*\)', '', model)
    model = re.sub(r'（\s*）', '', model)
    # 2) 去掉 SEO 关键词剥离后残留的截断孤儿字符
    model = re.sub(r'\s+[全国网通]通?$', '', model)
    # 3) 清理括号内因 5G/4G 被剥离后留下的前导空白，例如 "(  骁龙版)" → "(骁龙版)"
    model = re.sub(r'\(\s+', '(', model)
    model = re.sub(r'\s+\)', ')', model)
    model = re.sub(r'（\s+', '（', model)
    model = re.sub(r'\s+）', '）', model)
    # 4) 再清理一次多余空格
    model = model.strip()
    # 5) 递归清理（去掉括号后可能暴露出新的孤儿字符）
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


def get_image_dir(parsed: dict, base_dir: str = None) -> str:
    _image_base_dir = base_dir if base_dir is not None else IMAGE_BASE_DIR
    brand_cn = parsed.get("brand_cn", "")
    brand_normalized = parsed.get("brand_normalized", "")
    model = parsed.get("model", "")

    candidate_prefixes = []
    if brand_cn:
        candidate_prefixes.append(brand_cn)
    if brand_normalized and "/" in brand_normalized:
        en_name = brand_normalized.split("/")[0]
        if en_name and en_name not in candidate_prefixes:
            candidate_prefixes.append(en_name)
            candidate_prefixes.append(en_name.upper())
            candidate_prefixes.append(en_name.lower())

    EXTRA_BRAND_PREFIXES = {
        "苹果": ["iPhone", "IPHONE", "iphone"],
        "黑鲨": ["黑鲨游戏手机"],
        "真我": ["真我GT"],
        "荣耀": ["荣耀GT", "荣耀Magic"],
        "华为": ["华为畅享"],
        "三星": ["三星Galaxy"],
        "魅族": ["魅族20", "魅族21"],
        "小米": ["Redmi", "REDMI", "redmi", "红米"],
    }
    extra = EXTRA_BRAND_PREFIXES.get(brand_cn, [])
    for ep in extra:
        if ep not in candidate_prefixes:
            candidate_prefixes.append(ep)

    seen = set()
    unique_prefixes = []
    for p in candidate_prefixes:
        if p not in seen:
            seen.add(p)
            unique_prefixes.append(p)

    def _prefix_match(entry_lower: str, prefix_lower: str) -> bool:
        if not entry_lower.startswith(prefix_lower):
            return False
        tail = entry_lower[len(prefix_lower):]
        return tail == "" or tail[0] in (" ", "-", "_")

    def _find_in_dir(search_dir: str) -> str:
        if not os.path.isdir(search_dir):
            return None
        for prefix in unique_prefixes:
            path = os.path.join(search_dir, f"{prefix} {model}")
            if os.path.isdir(path):
                return path
        path = os.path.join(search_dir, model)
        if os.path.isdir(path):
            return path
        try:
            entries = os.listdir(search_dir)
        except OSError:
            return None

        # 构建型号别名集合，处理本地目录命名与官方/标题命名的常见差异
        model_lower = model.lower().strip()
        model_aliases = {model_lower}
        if brand_cn == "真我":
            # 真我 GT Neo5 240W / 150W 等，本地目录可能省略 Neo 写作 GT5
            m = re.sub(r'^gt\s*neo\s*5\b', 'gt5', model_lower)
            if m != model_lower:
                model_aliases.add(m)
            # 反向：标题是 GT5，目录是 GT Neo5
            m2 = re.sub(r'^gt\s*5\b', 'gt neo5', model_lower)
            if m2 != model_lower:
                model_aliases.add(m2)
        if brand_cn == "华为" and "畅享" in model_lower:
            # 畅享90m Plus 与 畅享 90 Plus 的空格/m 差异
            m1 = re.sub(r'畅享\s*(\d+)\s*m\b', r'畅享 \1', model_lower)
            m2 = re.sub(r'畅享\s*(\d+)\b', r'畅享\1m', model_lower)
            model_aliases.update({m1, m2})
        if brand_cn == "荣耀" and model_lower == "power":
            # 标题写 Power，本地只有 Power 2
            model_aliases.add("power 2")

        debug = search_dir.endswith('/荣耀') or search_dir.endswith('/Redmi') or search_dir.endswith('/真我') or search_dir.endswith('/华为')
        if debug:
            print(f'[_find_in_dir] search_dir={search_dir} model={model!r} aliases={model_aliases} prefixes={unique_prefixes}')

        def _match_alias(alias_lower: str, remaining: str, prefix_lower: str) -> bool:
            """对单个型号别名与目录剩余部分做全量匹配"""
            if remaining.lower() == alias_lower:
                return True
            remaining_no_space = remaining.lower().replace(' ', '')
            if alias_lower.startswith(prefix_lower):
                alias_remaining_no_space = alias_lower[len(prefix_lower):].replace(' ', '').strip()
                if remaining_no_space == alias_remaining_no_space:
                    return True
            # Redmi/红米 前缀不一致（支持双重前缀如 "红米 Redmi Note 13"）
            alias_stripped = alias_lower
            changed = True
            while changed:
                changed = False
                for rp in ["redmi ", "redmi", "红米 ", "红米"]:
                    if alias_stripped.startswith(rp):
                        alias_stripped = alias_stripped[len(rp):].strip()
                        changed = True
                        break
            if alias_stripped != alias_lower and remaining.lower() == alias_stripped:
                return True
            if alias_stripped != alias_lower and remaining_no_space == alias_stripped.replace(' ', ''):
                return True
            if remaining.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            remaining_no_suffix = re.sub(r'\s*[（(][^)）]*[)）]\s*$', '', remaining).strip()
            if remaining_no_suffix.lower() == alias_lower:
                return True
            if remaining_no_suffix.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            remaining_no_parens = remaining.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
            if remaining_no_parens.lower() == alias_lower:
                return True
            if remaining_no_parens.lower().replace(' ', '') == alias_lower.replace(' ', ''):
                return True
            rem_lower = remaining.lower().strip()
            # 正向：目录比标题多尾部修饰词（有空格/括号）
            if rem_lower.startswith(alias_lower + ' ') or rem_lower.startswith(alias_lower + '(') or rem_lower.startswith(alias_lower + '（'):
                extra = rem_lower[len(alias_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 正向：目录比标题多无空格中文修饰词（如 "保时捷设计"）
            if rem_lower and rem_lower.startswith(alias_lower):
                extra = rem_lower[len(alias_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 反向：标题比目录多尾部修饰词（有空格/括号）
            if alias_lower.startswith(rem_lower + ' ') or alias_lower.startswith(rem_lower + '(') or alias_lower.startswith(rem_lower + '（'):
                extra = alias_lower[len(rem_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            # 反向：标题比目录多无空格中文修饰词
            if rem_lower and alias_lower.startswith(rem_lower):
                extra = alias_lower[len(rem_lower):].strip()
                if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                    return True
            return False

        for entry in entries:
            entry_path = os.path.join(search_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            entry_lower = entry.lower()
            for prefix in unique_prefixes:
                prefix_lower = prefix.lower()
                if entry_lower.startswith(prefix_lower):
                    remaining = entry[len(prefix):].strip()
                    if debug:
                        print(f'  entry={entry!r} prefix={prefix!r} remaining={remaining!r}')
                    for alias_lower in model_aliases:
                        if _match_alias(alias_lower, remaining, prefix_lower):
                            return entry_path
        return None

    result = _find_in_dir(_image_base_dir)
    if result:
        return result

    if os.path.isdir(_image_base_dir):
        try:
            subdirs = [d for d in os.listdir(_image_base_dir)
                       if os.path.isdir(os.path.join(_image_base_dir, d))]
        except OSError:
            subdirs = []
        for subdir in subdirs:
            brand_subdir = os.path.join(_image_base_dir, subdir)
            result = _find_in_dir(brand_subdir)
            if result:
                return result

    return os.path.join(_image_base_dir, f"{brand_cn} {model}")


if __name__ == "__main__":
    IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"

    test_titles = [
        "【95】vivo vivo Y55s (2021款) 二手手机",
        "【95】vivo vivo Y51s 二手手机",
        "【95】vivo vivo Y500s 二手手机",
        "【95】vivo vivo Y500i 二手手机",
        "【95】vivo vivo Y500 Pro 二手手机",
        "【95】vivo vivo Y500 二手手机",
        "【95】vivo vivo Y31s标准版 二手手机",
        "【95新】realme/真我 真我 Neo7 SE 二手手机",
        "【95新】realme/真我 真我 Neo8 二手手机",
        "【95新】realme/真我 真我 GT7 Pro 竞速版 二手",
        "【95新】realme/真我 真我GT7 Pro 二手手机",
        "【95新】realme/真我 真我 GT7 二手手机",
        "【95新】realme/真我 真我 Neo7 Turbo 二手手机",
        "【95新】realme/真我 真我 GT Neo5 240W 二手手",
        "【95新】realme/真我 真我 Neo7 二手手机",
        "【95新】realme/真我 真我GT6 二手手机",
        "华为 Mate 60 Pro 二手手机",
        "华为 Mate 70 Pro+ 二手手机",
        "华为 Mate 50E 二手手机",
    ]

    for title in test_titles:
        parsed = parse_title(title)
        img_dir = get_image_dir(parsed)
        exists = os.path.isdir(img_dir)
        print(f"TITLE: {title}")
        print(f"  parsed: {parsed}")
        print(f"  img_dir: {img_dir}")
        print(f"  exists: {exists}")
        print()
