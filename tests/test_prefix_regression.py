#!/usr/bin/env python3
"""
前缀匹配回归测试：遍历目录中所有机型，检测 _find_in_dir 的误匹配。
问题机：荣耀 Power → 荣耀 Power 2（反向prefix），荣耀 X60 GT → 荣耀 X60（反向prefix）
"""

import os
import re
import sys

# ============================================================
# 从 test_doudian_batch.py 精确复制关键函数
# ============================================================

IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"

# ========== parse_title 核心逻辑（精简版，仅提取品牌+型号） ==========

BRAND_MAP = {
    # Apple - 中英文品牌
    "苹果": "苹果", "apple": "Apple",
    # 华为/荣耀
    "华为": "华为", "honor": "荣耀",
    "荣耀": "荣耀", "huawei": "华为",
    # 小米
    "小米": "小米", "xiaomi": "小米", "红米": "红米", "redmi": "Redmi",
    # OPPO/vivo
    "oppo": "OPPO", "vivo": "vivo", "一加": "一加", "oneplus": "一加",
    # 三星
    "三星": "三星", "samsung": "三星",
    # iQOO
    "iqoo": "iQOO",
    # 魅族
    "魅族": "魅族", "meizu": "魅族",
    # 真我
    "真我": "真我", "realme": "真我",
}

# 品牌推断：无前缀时从型号推断
BRAND_INFER = {
    "galaxy": "三星",
    "magic": "荣耀",
    "mate": "华为",
    "nova": "华为",
    "pura": "华为",
    "pocket": "华为",
    "畅享": "华为",
    "redmi": "红米",
}

SEO_KEYWORD_PATTERNS = [
    (r'^\[.*?\]\s*', ''),
    (r'^\s*【.*?】\s*', ''),
    (r'\s*👍.*$', ''),
    (r'\s*💥.*$', ''),
    (r'\s*🎉.*$', ''),
    (r'\b5G\b', ''),
    (r'\s*(95新|9新|99新|准新机)\s*', ''),
    (r'\s*全国联保\s*', ''),
    (r'\s*国行\s*', ''),
    (r'\s*原装正品\s*', ''),
    (r'\s*正品\s*', ''),
    (r'\s*原封\s*', ''),
]

PARENTHESIS_PATTERNS = [
    (r'\(.*?\)', ''),
    (r'（.*?）', ''),
]

CATEGORY_PATTERNS = [
    (r'\s*二手(?:手)?\s*手机\s*', ''),
    (r'\s*二手(?:手)?\s*', ''),
    (r'\s*手机\s*', ''),
    (r'\s*系列\s*', ''),
    (r'\s*二$', ''),
]

def _cleanup_model(model: str) -> str:
    """统一清理型号字符串"""
    model = re.sub(r'\(\s*\)', '', model)
    model = re.sub(r'（\s*）', '', model)
    model = re.sub(r'\s+[全国网通]通?$', '', model)
    model = model.strip()
    if re.search(r'[全国网通]通?$', model):
        model = re.sub(r'\s+[全国网通]通?$', '', model).strip()
    return model

def parse_title(title: str) -> dict:
    """精简版 parse_title，只提取 brand_cn 和 model"""
    title = title.strip()

    # Step 1: 剥离 SEO 关键词
    for pattern, repl in SEO_KEYWORD_PATTERNS:
        title = re.sub(pattern, repl, title)

    # Step 2: 剥离括号及内容（先收集，后面可能会用到）
    paren_content = []
    found = re.findall(r'[（(](.+?)[）)]', title)
    for f in found:
        paren_content.append(f.strip().lower())
    for pattern, repl in PARENTHESIS_PATTERNS:
        title = re.sub(pattern, repl, title)

    # Step 3: 剥离品类关键词
    for pattern, repl in CATEGORY_PATTERNS:
        title = re.sub(pattern, repl, title)

    title = title.strip()
    if title.endswith(('）', ')')):
        title = title[:-1].strip()

    # Step 4: 品牌识别
    brand_cn = ""
    brand_en = ""
    brand_prefix = ""
    for prefix in sorted(BRAND_MAP.keys(), key=lambda x: -len(x)):
        pl = prefix.lower()
        if title.lower().startswith(pl):
            mapped = BRAND_MAP[prefix]
            if mapped in ("苹果", "华为", "荣耀", "小米", "红米", "三星", "一加", "魅族", "真我"):
                brand_cn = mapped
            else:
                brand_en = mapped
            brand_prefix = prefix
            break

    # Step 5: 提取型号
    if brand_prefix:
        raw_model = title[len(brand_prefix):].strip()
    else:
        raw_model = title.strip()

    # 移除型号中残余的括号
    raw_model = re.sub(r'[（(].*?[）)]', '', raw_model).strip()

    model = _cleanup_model(raw_model)

    # 品牌推断
    if not brand_cn and not brand_en:
        model_lower = model.lower()
        for keyword, bcn in BRAND_INFER.items():
            if model_lower.startswith(keyword):
                brand_cn = bcn
                break

    return {"brand_cn": brand_cn, "brand_en": brand_en, "model": model}


# ========== get_image_dir 核心逻辑 ==========

REDMI_CN_PREFIXES = ["红米 ", "红米"]

def get_image_dir(parsed: dict) -> str:
    brand_cn = parsed.get("brand_cn", "")
    brand_en = parsed.get("brand_en", "")
    model = parsed.get("model", "")
    model_lower = model.lower()

    # Redmi 双重前缀处理：检测 model 开头是否有 "红米 redmi" 或 "redmi 红米"
    redmi_override_cn = ""
    stripped_model = model
    for cn_pref in REDMI_CN_PREFIXES:
        if model_lower.startswith(cn_pref.lower()):
            redmi_override_cn = cn_pref.rstrip()
            stripped_model = model[len(cn_pref):].strip()
            break

    model_for_search = stripped_model

    # 构建候选前缀
    candidate_prefixes = []
    cleaned_base_brand_cn = brand_cn
    if brand_en:
        candidate_prefixes.append(brand_en)
    if brand_cn:
        # 中文品牌可能有空格（如"华为 "、"荣耀 "）
        candidate_prefixes.append(brand_cn)
        candidate_prefixes.append(brand_cn + " ")
        # Redmi 双重前缀
        if brand_cn == "小米" and redmi_override_cn:
            candidate_prefixes.append("Redmi")
            candidate_prefixes.append(redmi_override_cn)
            candidate_prefixes.append(redmi_override_cn + " ")
            cleaned_base_brand_cn = redmi_override_cn
        elif brand_cn == "荣耀":
            candidate_prefixes.append("honor")
            candidate_prefixes.append("Honor")
        elif brand_cn == "华为":
            candidate_prefixes.append("huawei")
            candidate_prefixes.append("Huawei")
        elif brand_cn == "三星":
            candidate_prefixes.append("Samsung")
            candidate_prefixes.append("samsung")
        elif brand_cn == "真我":
            candidate_prefixes.append("realme")
            candidate_prefixes.append("Realme")
    if redmi_override_cn:
        cleaned_base_brand_cn = redmi_override_cn

    # 去重
    seen = set()
    unique_prefixes = []
    for p in candidate_prefixes:
        if p not in seen:
            seen.add(p)
            unique_prefixes.append(p)

    def _find_in_dir(search_dir: str) -> str:
        if not os.path.isdir(search_dir):
            return None
        for prefix in unique_prefixes:
            path = os.path.join(search_dir, f"{prefix} {model_for_search}")
            if os.path.isdir(path):
                return path
        path = os.path.join(search_dir, model_for_search)
        if os.path.isdir(path):
            return path
        try:
            entries = os.listdir(search_dir)
        except OSError:
            return None
        model_lower = model_for_search.lower().strip()
        for entry in entries:
            entry_path = os.path.join(search_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            entry_lower = entry.lower()
            for prefix in unique_prefixes:
                prefix_lower = prefix.lower()
                if entry_lower.startswith(prefix_lower):
                    remaining = entry[len(prefix):].strip()
                    if remaining.lower() == model_lower:
                        return entry_path
                    # 无空格匹配
                    remaining_no_space = remaining.lower().replace(' ', '')
                    if model_lower.startswith(prefix_lower):
                        model_remaining_no_space = model_lower[len(prefix_lower):].replace(' ', '').strip()
                        if remaining_no_space == model_remaining_no_space:
                            return entry_path
                    # Redmi/红米 前缀不一致
                    model_stripped = model_lower
                    for rp in ["redmi ", "redmi", "红米 ", "红米"]:
                        if model_stripped.startswith(rp):
                            model_stripped = model_stripped[len(rp):].strip()
                            break
                    if model_stripped != model_lower and remaining.lower() == model_stripped:
                        return entry_path
                    if model_stripped != model_lower and remaining_no_space == model_stripped.replace(' ', ''):
                        return entry_path
                    # 空格归一化
                    if remaining.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    # 括号后缀剥离
                    remaining_no_suffix = re.sub(r'\s*[（(][^)）]*[)）]\s*$', '', remaining).strip()
                    if remaining_no_suffix.lower() == model_lower:
                        return entry_path
                    if remaining_no_suffix.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    # 全局去括号比较
                    remaining_no_parens = remaining.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
                    if remaining_no_parens.lower() == model_lower:
                        return entry_path
                    if remaining_no_parens.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    # ★★★ 模型作为 entry 前缀比较 ★★★
                    # 安全约束：extra 必须以中文或括号开头
                    rem_lower = remaining.lower().strip()
                    if rem_lower.startswith(model_lower + ' ') or rem_lower.startswith(model_lower + '(') or rem_lower.startswith(model_lower + '（'):
                        extra = rem_lower[len(model_lower):].strip()
                        if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                            return entry_path
                    # ★★★ 反方向：entry 作为 model 前缀 ★★★
                    # 安全约束：extra 必须以中文或括号开头
                    if model_lower.startswith(rem_lower + ' ') or model_lower.startswith(rem_lower + '(') or model_lower.startswith(rem_lower + '（'):
                        extra = model_lower[len(rem_lower):].strip()
                        if extra and (extra[0] == '(' or extra[0] == '（' or '\u4e00' <= extra[0] <= '\u9fff'):
                            return entry_path
        return None

    result = _find_in_dir(IMAGE_BASE_DIR)
    if result:
        return result

    if os.path.isdir(IMAGE_BASE_DIR):
        try:
            subdirs = [d for d in os.listdir(IMAGE_BASE_DIR)
                       if os.path.isdir(os.path.join(IMAGE_BASE_DIR, d))]
        except OSError:
            subdirs = []
        for subdir in subdirs:
            brand_subdir = os.path.join(IMAGE_BASE_DIR, subdir)
            result = _find_in_dir(brand_subdir)
            if result:
                return result

    return os.path.join(IMAGE_BASE_DIR, f"{brand_cn} {model}")

# ============================================================
# 测试逻辑
# ============================================================

def get_expected_dir(dir_entry: str, brand_dir: str) -> str:
    """给定目录中的条目名，返回它对应的期望路径"""
    return os.path.join(brand_dir, dir_entry)

def test_all_models():
    errors = []
    correct = []
    
    for brand_subdir in sorted(os.listdir(IMAGE_BASE_DIR)):
        brand_path = os.path.join(IMAGE_BASE_DIR, brand_subdir)
        if not os.path.isdir(brand_path):
            continue
        
        model_dirs = sorted(os.listdir(brand_path))
        for model_dir in model_dirs:
            model_path = os.path.join(brand_path, model_dir)
            if not os.path.isdir(model_path):
                continue
            
            # 用目录名作为"标题"来测试
            title = model_dir
            parsed = parse_title(title)
            result_path = get_image_dir(parsed)
            expected_path = model_path
            
            if result_path == expected_path:
                correct.append((title, result_path))
            elif os.path.isdir(result_path) if result_path else False:
                # 匹配到了别的目录
                errors.append(("WRONG_MATCH", title, result_path, expected_path,
                               f"brand_cn={parsed.get('brand_cn')}, model={parsed.get('model')}"))
            else:
                # 没匹配到任何目录
                errors.append(("NO_MATCH", title, result_path, expected_path,
                               f"brand_cn={parsed.get('brand_cn')}, model={parsed.get('model')}"))
    
    print("=" * 80)
    print("目录机型回归测试结果")
    print("=" * 80)
    print(f"\n总测试: {len(correct) + len(errors)} | ✅ 正确: {len(correct)} | ❌ 错误: {len(errors)}")
    
    if errors:
        wrong_match = [e for e in errors if e[0] == "WRONG_MATCH"]
        no_match = [e for e in errors if e[0] == "NO_MATCH"]
        
        if wrong_match:
            print(f"\n🔴 误匹配 ({len(wrong_match)} 条):")
            for _, title, result, expected, detail in wrong_match:
                print(f"  标题: {title}")
                print(f"    期望: {expected}")
                print(f"    实际: {result}")
                print(f"    详情: {detail}")
                print()
        
        if no_match:
            print(f"\n🟡 未匹配 ({len(no_match)} 条):")
            for _, title, result, expected, detail in no_match:
                print(f"  标题: {title}")
                print(f"    期望: {expected}")
                print(f"    实际: {result}")
                print(f"    详情: {detail}")
                print()
    else:
        print("\n🎉 所有机型匹配正确，无回归！")
    
    return len(errors) == 0

if __name__ == "__main__":
    ok = test_all_models()
    sys.exit(0 if ok else 1)
