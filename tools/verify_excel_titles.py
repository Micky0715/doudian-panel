"""用真实 Excel 商品名测试型号识别（parse_title）+ 校验门抗截断能力。

数据源：抖音直播货架货品价格规则数据导出(货品维度).xlsx
- 列结构：前3行是标题/空行/('品牌','型号')，之后是真实数据 [品牌, 型号/商品名]
- 列B 即"商品名称"（如 "苹果 iPhone 11"、"华为 Mate 70 Pro+"）

检查两类问题（对应"真我GT Pro 漏掉 Pro"这一类 bug）：
  1) 后缀保留：名称里含 Pro/Ultra/Pro Max/Pro+/Max/Plus/Neo/GT/+ 等后缀词时，
     parse_title 解析出的 model 是否也包含它（防止被截断成 GT / Mate 70 等）
  2) 校验门抗截断：对含后缀的 model，模拟下拉出现"品牌+短前缀"选项（如"真我GT"），
     确认 _is_acceptable_model_match 返回 False（拒绝截断），而完整选项返回 True
"""
import sys
import os
import re
import openpyxl

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_doudian_batch as dd

XLSX = "/Users/atrenew/Downloads/抖音直播货架货品价格规则数据导出(货品维度)-生产20260724174344.xlsx"

# 关键后缀词（整词匹配，大小写不敏感）—— 这些都是"漏掉就错"的后缀
SUFFIX_WORDS = ["Pro Max", "Pro+", "Pro", "Ultra", "Neo", "GT", "Max", "Plus"]


def read_names():
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    names = []
    started = False
    for r in rows:
        if not r:
            continue
        a = r[0]
        b = r[1] if len(r) > 1 else None
        # 找到 ('品牌','型号') 表头后，后面的非空行才是数据
        if a == "品牌" and b == "型号":
            started = True
            continue
        if not started:
            continue
        if a and b and str(a).strip() and str(b).strip():
            names.append((str(a).strip(), str(b).strip()))
    return names


def find_name_suffixes(name: str):
    """返回名称中（去掉品牌后）出现的关键后缀词列表"""
    found = []
    low = name
    for w in SUFFIX_WORDS:
        if re.search(r'(?i)\b' + re.escape(w) + r'\b', low):
            found.append(w)
    return found


def model_has_suffixes(model: str, suffixes):
    low = model
    miss = []
    for w in suffixes:
        if not re.search(r'(?i)\b' + re.escape(w) + r'\b', low):
            miss.append(w)
    return miss


def main():
    names = read_names()
    print(f"===== 读取到 {len(names)} 个商品（品牌, 名称） =====\n")

    # ---------- 1) 后缀保留检查 ----------
    print("===== 1) 后缀保留检查（名称含关键后缀但 model 缺失 → 标记异常） =====")
    anomalies = []
    suffix_hit = 0
    for brand, name in names:
        suffixes = find_name_suffixes(name)
        if not suffixes:
            continue
        suffix_hit += 1
        r = dd.parse_title(name)
        model = r.get("model", "")
        miss = model_has_suffixes(model, suffixes)
        if miss:
            anomalies.append((brand, name, model, suffixes, miss))
            print(f"  ❌ 品牌={brand:8} 名称={name!r:42} -> model={model!r:22} 缺失后缀 {miss} (名称含 {suffixes})")
    if not anomalies:
        print(f"  ✅ 全部 {suffix_hit} 个含关键后缀的商品，parse_title 均完整保留了后缀")
    else:
        print(f"  ⚠ 共 {len(anomalies)} 个商品存在后缀丢失")

    # ---------- 2) 校验门抗截断模拟 ----------
    print("\n===== 2) 校验门抗截断模拟（用真实解析出的 品牌+型号） =====")
    # 重建每个商品的 (brand_cn, brand_en, model)，模拟下拉出现"品牌+短前缀"选项
    gate_ok = 0
    gate_bad = 0
    gate_samples = []
    for brand, name in names:
        r = dd.parse_title(name)
        model = r.get("model", "")
        brand_cn = r.get("brand_cn", "")
        brand_en = (r.get("brand_normalized", "") or "").split("/")[0]
        if not model or " " not in model:
            continue  # 只看含后缀(有空格)的型号
        # 由长到短的关键字（与 select_model 的拆分一致）
        parts = model.split()
        for i in range(1, len(parts)):  # 跳过完整 model，取各截断前缀
            short = " ".join(parts[:len(parts) - i])
            if not short:
                continue
            # 模拟下拉选项 = 品牌前缀 + 截断前缀（如 "真我GT"）
            for prefix_brand in ([brand_cn] if brand_cn else []) + ([brand_en] if brand_en else []):
                if not prefix_brand:
                    continue
                selected = f"{prefix_brand}{short}" if not short.startswith(prefix_brand) else short
                if selected == model:
                    continue
                acceptable, _ = dd._is_acceptable_model_match(selected, model, brand_cn, brand_en)
                if acceptable:
                    gate_bad += 1
                    if len(gate_samples) < 10:
                        gate_samples.append((selected, model, brand_cn))
                else:
                    gate_ok += 1
    print(f"  截断选项被校验门正确拒绝: {gate_ok} 次")
    print(f"  截断选项被误接受(异常):   {gate_bad} 次")
    for s in gate_samples:
        print(f"    ❌ selected={s[0]!r} 对 model={s[1]!r} (品牌 {s[2]}) 被误接受")

    # ---------- 3) 完整匹配接受检查（抽样） ----------
    print("\n===== 3) 完整匹配接受检查（抽样含后缀商品） =====")
    shown = 0
    for brand, name in names:
        suffixes = find_name_suffixes(name)
        if not suffixes:
            continue
        r = dd.parse_title(name)
        model = r.get("model", "")
        brand_cn = r.get("brand_cn", "")
        brand_en = (r.get("brand_normalized", "") or "").split("/")[0]
        # 完整下拉选项（带品牌前缀）
        for prefix_brand in ([brand_cn] if brand_cn else []) + ([brand_en] if brand_en else []):
            if not prefix_brand:
                continue
            selected_full = f"{prefix_brand}{model}" if not model.startswith(prefix_brand) else model
            acc, _ = dd._is_acceptable_model_match(selected_full, model, brand_cn, brand_en)
            if shown < 15:
                print(f"  {'✅' if acc else '❌'} 名称={name!r:38} model={model!r:20} 完整选项={selected_full!r:24} -> 接受={acc}")
                shown += 1
            break
        if shown >= 15:
            break

    print("\n===== 总结 =====")
    print(f"  含关键后缀商品数: {suffix_hit}")
    print(f"  后缀丢失异常数:   {len(anomalies)}")
    print(f"  截断校验 拒绝/误接受: {gate_ok}/{gate_bad}")
    if not anomalies and gate_bad == 0:
        print("  ✅ 型号识别与校验门在真实商品名上均通过")
        return 0
    print("  ⚠ 存在需要关注的项")
    return 1


if __name__ == "__main__":
    sys.exit(main())
