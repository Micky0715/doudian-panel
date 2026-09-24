#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复验证脚本 v2：对 25595926 旧日志中的 156 条失败记录重新验证。
内置 parse_title / get_image_dir 等核心逻辑（从 test_doudian_batch.py 复制），无需 playwright。
"""
import os
import re
import csv
import glob
from datetime import datetime

# ========== 配置 (从 test_doudian_batch.py 复制) ==========
PRODUCT_IMAGE_ROOT = "/Users/atrenew/商品图片"
IMAGE_BASE_DIR = "/Users/atrenew/商品图片/25595926/3C数码及配件/二手手机"
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

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "failure_reanalysis_report.csv")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", f"verify_fixes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)


def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
        f.flush()


# ========== 核心函数 (从 test_doudian_batch.py 复制) ==========

def _cleanup_model(raw_model: str) -> str:
    """清理 parse_title 提取的型号字符串中的残留词和空括号。"""
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


def parse_title(title: str) -> dict:
    """从商品标题解析品牌、型号、成色"""
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
        result["brand_cn"] = "小米"
        result["brand_normalized"] = BRAND_MAP["小米"]
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
        elif any(k in title_lower2 for k in ["mate", "nova", "pura", "畅享"]):
            inferred_brand = "华为"
        elif "iphone" in title_lower2 or "mac" in title_lower2:
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

    return result


def get_image_dir(parsed: dict) -> str:
    """根据品牌和型号构建图片目录路径。"""
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
        model_lower = model.lower().strip()
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
                    remaining_no_space = remaining.lower().replace(' ', '')
                    if model_lower.startswith(prefix_lower):
                        model_remaining_no_space = model_lower[len(prefix_lower):].replace(' ', '').strip()
                        if remaining_no_space == model_remaining_no_space:
                            return entry_path
                    model_stripped = model_lower
                    for rp in ["redmi ", "redmi", "红米 ", "红米"]:
                        if model_stripped.startswith(rp):
                            model_stripped = model_stripped[len(rp):].strip()
                            break
                    if model_stripped != model_lower and remaining.lower() == model_stripped:
                        return entry_path
                    if model_stripped != model_lower and remaining_no_space == model_stripped.replace(' ', ''):
                        return entry_path
                    if remaining.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    remaining_no_suffix = re.sub(r'\s*[（(][^)）]*[)）]\s*$', '', remaining).strip()
                    if remaining_no_suffix.lower() == model_lower:
                        return entry_path
                    if remaining_no_suffix.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    remaining_no_parens = remaining.replace('(', '').replace(')', '').replace('（', '').replace('）', '').strip()
                    if remaining_no_parens.lower() == model_lower:
                        return entry_path
                    if remaining_no_parens.lower().replace(' ', '') == model_lower.replace(' ', ''):
                        return entry_path
                    rem_lower = remaining.lower().strip()
                    if rem_lower.startswith(model_lower + ' ') or rem_lower.startswith(model_lower + '(') or rem_lower.startswith(model_lower + '（'):
                        return entry_path
                    if model_lower.startswith(rem_lower + ' ') or model_lower.startswith(rem_lower + '(') or model_lower.startswith(rem_lower + '（'):
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


def check_images(img_dir: str):
    """检查主图和白底图是否存在"""
    main_dir = os.path.join(img_dir, "主图")
    aux_dir = os.path.join(img_dir, "白底图")
    main_exists, main_count = False, 0
    aux_exists, aux_count = False, 0

    if os.path.isdir(main_dir):
        for f in os.listdir(main_dir):
            if f.startswith("主图"):
                main_exists = True
                main_count += 1
    if os.path.isdir(aux_dir):
        for f in os.listdir(aux_dir):
            if f.startswith("白底图"):
                aux_exists = True
                aux_count += 1
    return main_exists, main_count, aux_exists, aux_count


def main():
    log("=" * 80)
    log("抖店批量处理 - 故障修复验证 v2")
    log(f"店铺: 25595926 (拍拍二手悦玥手机专卖店)")
    log(f"IMAGE_BASE_DIR: {IMAGE_BASE_DIR}")
    log(f"CSV: {CSV_PATH}")
    log(f"日志: {LOG_PATH}")
    log("=" * 80)

    rows = []
    with open(CSV_PATH, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)

    log(f"读取到 {len(rows)} 条记录\n")

    total = len(rows)
    ok_count, fail_count = 0, 0
    # 分类统计
    regressions = []  # 旧=OK 现在失败
    improvements = []  # 旧=失败 现在OK
    still_ok = []
    still_fail = []
    detail_fail = {}

    for idx, row in enumerate(rows):
        raw_title = row['原始标题'].strip()
        old_status = row.get('修复后状态', row.get('旧报错', '')).strip()
        old_root = row.get('根因', '').strip()

        parsed = parse_title(raw_title)
        img_dir = get_image_dir(parsed)
        dir_exists = os.path.isdir(img_dir)
        main_exists, main_count, aux_exists, aux_count = check_images(img_dir)

        if dir_exists and main_exists and aux_exists:
            current_status = "OK"
            ok_count += 1
        elif dir_exists and (not main_exists or not aux_exists):
            current_status = "主图白底图缺失"
            fail_count += 1
        else:
            current_status = "型号未匹配"
            fail_count += 1

        # 状态变化
        if old_status == "OK" and current_status != "OK":
            regressions.append(raw_title)
            flag = "⚠️ 回归"
        elif old_status != "OK" and current_status == "OK":
            improvements.append(raw_title)
            flag = "✅ 修复确认"
        elif current_status == "OK":
            still_ok.append(raw_title)
            flag = "✅"
        else:
            still_fail.append(raw_title)
            flag = "❌"

        # 失败根因
        if current_status != "OK":
            if not dir_exists:
                root = "本地无此目录"
            elif not main_exists and aux_exists:
                root = "缺主图"
            elif main_exists and not aux_exists:
                root = "缺白底图"
            else:
                root = "主图白底图都缺"
            detail_fail.setdefault(root, []).append(raw_title)

        log(f"[{idx+1:03d}] {flag} 旧={old_status} → 新={current_status}")
        log(f"  标题: {raw_title}")
        log(f"  解析: 品牌={parsed['brand_cn']}, 型号={parsed['model']}, 成色={parsed['condition']}")
        log(f"  目录: {img_dir}")
        log(f"  存在: {'是' if dir_exists else '否'} | 主图({'是' if main_exists else '否'}/{main_count}) | 白底图({'是' if aux_exists else '否'}/{aux_count})")
        log("")

    # ==== 汇总报告 ====
    log("=" * 80)
    log(" 验证结果汇总")
    log("=" * 80)
    log(f"总记录: {total}")
    log(f"  OK: {ok_count} ({100*ok_count/total:.0f}%)")
    log(f"  失败: {fail_count} ({100*fail_count/total:.0f}%)")
    log("")

    log(f"状态变化:")
    log(f"  ✅ 修复确认(旧失败→新OK): {len(improvements)} 条")
    log(f"  ✅ 保持OK: {len(still_ok)} 条")
    log(f"  ❌ 持续失败: {len(still_fail)} 条")
    log(f"  ⚠️ 回归(旧OK→新失败): {len(regressions)} 条")
    log("")

    if improvements:
        log("【修复确认详情】")
        for t in improvements:
            log(f"  ✅ {t}")
        log("")

    if regressions:
        log("【⚠️ 回归详情】旧报告说OK但验证失败：")
        for t in regressions:
            log(f"  ⚠️ {t}")
        log("")

    if detail_fail:
        log("【失败根因分布】")
        for root_cause, titles in sorted(detail_fail.items(), key=lambda x: -len(x[1])):
            log(f"  - {root_cause}: {len(titles)} 条")
            for t in titles:
                log(f"      {t}")
        log("")

    log(f"详细日志: {LOG_PATH}")


if __name__ == "__main__":
    main()
