#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析抖店运行日志中「发布商品失败」的具体原因。
输出按品牌、错误类型、机型分布的统计报告。

直接解析 `[LOG] 已记录失败: <name> (ID:xxx) - <reason>` 行，
并结合上下文（发布结果 JSON / 异常堆栈 / 型号终检）推导子原因。
"""
import re
import sys
from collections import defaultdict, Counter

sys.path.insert(0, "/Users/atrenew/doudian-panel")
try:
    import test_doudian_batch as dd
    HAVE_DD = True
except Exception:
    HAVE_DD = False

LOG_PATH = "/Users/atrenew/doudian-panel/panel_data/logs/run_25595926_20260805_194201_46afcf20.log"

PRODUCT_START_RE = re.compile(
    r"^\d+:\d+:\d+ \[BATCH\] \[(\d+)/(\d+)\] (.+?) \(ID:(\d+)\)"
)
FAIL_LOG_RE = re.compile(
    r"^\d+:\d+:\d+ \[LOG\] 已记录失败: (.+?) \(ID:(\d+)\) - (.+)$"
)
PUBLISH_RESULT_RE = re.compile(
    r"^\d+:\d+:\d+ \[STEP10\] 发布结果\(第(\d+)次\): (.+)$"
)

# 品牌推断（兜底，覆盖旧数据无 brand 字段的情况）
BRAND_INFER = [
    ("苹果", ["苹果", "iPhone", "ipad"]),
    ("华为", ["华为", "Huawei", "huawei", "非凡大师", "Mate", "Pura", "nova", "畅享"]),
    ("荣耀", ["荣耀", "Honor", "honor", "Magic", "Play", "X", "GT"]),
    ("小米", ["小米", "Xiaomi", "xiaomi", "Redmi", "redmi", "米"]),
    ("OPPO", ["OPPO", "oppo", "Find", "Reno", "A", "K"]),
    ("vivo", ["vivo", "VIVO", "Y", "X", "S", "iQOO", "iqoo"]),
    ("realme", ["realme", "真我", "GT", "Neo"]),
    ("一加", ["一加", "OnePlus", "oneplus", "1+"]),
    ("三星", ["三星", "Samsung", "samsung", "Galaxy", "W", "Z"]),
    ("iQOO", ["iQOO", "iqoo"]),
]


def infer_brand(name: str) -> str:
    for brand, keys in BRAND_INFER:
        for k in keys:
            if k.lower() in name.lower():
                return brand
    return "未知品牌"


def parse_title_safe(name: str):
    if HAVE_DD:
        try:
            return dd.parse_title(name)
        except Exception:
            return {}
    return {}


def extract_error_lines(reason_text: str) -> list:
    """从发布反馈文本中提取关键错误行（'错误' 后紧跟的详情）。"""
    lines = reason_text.split("\n")
    errors = []
    for i, line in enumerate(lines):
        s = line.strip()
        if s == "错误":
            if i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if nxt and nxt != "错误":
                    errors.append(nxt)
        elif s.startswith("错误") and len(s) > 2:
            errors.append(s[2:].strip())
    # 去重保序
    seen = set()
    out = []
    for e in errors:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out[:4]


def derive_sub_reason(top_reason: str, window: list, last_publish_fail: str) -> str:
    """根据顶层原因 + 上下文窗口 + 最近一次发布失败反馈，推导具体子原因。"""
    t = top_reason.strip()
    win = "\n".join(window)

    # 0) 优先从上下文窗口识别「型号终检未通过」——即使顶层被误标为 反馈
    #    （clear_last_publish_error 未在终检前调用，导致沿用上一个商品的 stale 原因）
    if "发布前型号终检未通过" in win or "终检失败" in win or "型号明显更长" in win:
        return "型号终检未通过(页面型号未选上/不匹配，拒绝发布)"

    # 1) 顶层就是处理异常（可能带细节）
    if t.startswith("处理异常"):
        detail = t[len("处理异常"):].strip(" :")
        if detail:
            return "处理异常/" + detail[:60]
        if "型号选择失败" in win or "未找到匹配型号" in win:
            return "处理异常/型号未匹配(选择失败)"
        if "Timeout" in win or "timeout" in win:
            return "处理异常/页面元素超时(hover/等待)"
        if "Locator" in win:
            return "处理异常/页面元素定位失败"
        return "处理异常/未捕获详情"

    # 2) 反馈 = 发布结果 success=False（平台真实拒因）或 stale 误标
    if t == "反馈":
        if last_publish_fail:
            errs = extract_error_lines(last_publish_fail)
            if errs:
                return "平台拒因/" + errs[0][:50]
            # 没有标准错误行，用关键词粗分
            for kw, label in [
                ("标题", "标题过长"), ("库存", "库存类"), ("价格", "价格类"),
                ("规格", "规格类"), ("3:4", "主图3:4长宽比"), ("类目", "类目类"),
                ("资质", "资质类"), ("现货", "库存类"),
            ]:
                if kw in last_publish_fail:
                    return "平台拒因/" + label
            return "平台拒因/未知(见反馈原文)"
        return "型号终检未通过(页面型号未选上/不匹配，拒绝发布)[stale原因=反馈]"

    # 3) 其他顶层原因
    return t[:80]


def main():
    with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    failures = []          # (lineno, name, id, top_reason, sub_reason)
    last_publish_fail = None   # 当前商品最近一次 success=False 的发布反馈原文
    cur_product = None
    cur_start_line = None

    for idx, raw in enumerate(lines):
        line = raw.rstrip("\n")

        m = PRODUCT_START_RE.match(line)
        if m:
            cur_product = m.group(3)
            cur_start_line = idx
            last_publish_fail = None
            continue

        pm = PUBLISH_RESULT_RE.match(line)
        if pm:
            body = pm.group(2)
            if "'success': False" in body or '"success": false' in body:
                last_publish_fail = body
            elif "'success': True" in body or '"success": true' in body:
                last_publish_fail = None
            continue

        fm = FAIL_LOG_RE.match(line)
        if fm:
            name = fm.group(1)
            pid = fm.group(2)
            top = fm.group(3)
            # 取本条失败行之前的上下文窗口（同一商品块内）
            start = cur_start_line if cur_start_line is not None else max(0, idx - 80)
            window = lines[max(start, 0):idx]
            sub = derive_sub_reason(top, window, last_publish_fail or "")
            failures.append((idx + 1, name, pid, top, sub))
            continue

    # 品牌/型号解析
    parsed_cache = {}
    def brand_of(name):
        if name in parsed_cache:
            return parsed_cache[name]
        b = "未知品牌"
        p = parse_title_safe(name)
        if p and p.get("brand_cn"):
            b = p["brand_cn"]
        else:
            b = infer_brand(name)
        parsed_cache[name] = b
        return b

    def model_of(name):
        p = parse_title_safe(name)
        if p and p.get("model"):
            return p["model"]
        # 兜底：取括号内型号
        mm = re.search(r"（型号：([^）]+)）", name)
        if mm:
            return mm.group(1)
        return ""

    # 统计
    brand_errors = defaultdict(lambda: defaultdict(list))   # brand -> sub_reason -> [items]
    sub_type_counts = Counter()
    brand_counts = Counter()

    for ln, name, pid, top, sub in failures:
        brand = brand_of(name)
        model = model_of(name)
        brand_counts[brand] += 1
        brand_errors[brand][sub].append({
            "name": name, "model": model, "id": pid, "line": ln, "top": top,
        })
        sub_type_counts[sub] += 1

    # 输出
    print("# 发布商品失败深度分析报告\n")
    print(f"- 日志文件: `{LOG_PATH}`")
    print(f"- 发布失败商品总数: **{len(failures)}**\n")

    print("## 一、按品牌分布（失败数降序）\n")
    for brand, cnt in brand_counts.most_common():
        print(f"### {brand} — {cnt} 个失败\n")
        for sub, items in sorted(brand_errors[brand].items(), key=lambda x: -len(x[1])):
            print(f"- **{sub}**: {len(items)} 个")
            mc = Counter(it["model"] for it in items if it["model"])
            for model, c in mc.most_common(6):
                print(f"  - {model}: {c}")
        print()

    print("## 二、按具体子原因分布（Top 全部）\n")
    for sub, cnt in sub_type_counts.most_common():
        print(f"- **{sub}**: {cnt} 个")
    print()

    print("## 三、典型失败商品抽样（每类 3 个）\n")
    for sub, cnt in sub_type_counts.most_common(8):
        print(f"### {sub} ({cnt})\n")
        shown = 0
        for brand in brand_errors:
            for it in brand_errors[brand].get(sub, []):
                print(f"- `{it['model']}` | {it['name'][:55]}... (行 {it['line']})")
                shown += 1
                if shown >= 3:
                    break
            if shown >= 3:
                break
        print()


if __name__ == "__main__":
    main()
