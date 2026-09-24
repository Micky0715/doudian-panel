#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析 run_7970406_20260804_193433_3d493400.log，把"真正的运行报错"分类统计并抽样。

聚焦非图片缺失的运行时错误：
- 型号选择失败（Playwright 30s 超时 + JS 兜底）
- 发布失败 → 兜底保存草稿
- OPPO 属性值错误
- 致命异常 FATAL/Traceback/Exception
- 库存修复 fixed 计数
"""
import sys, re
from collections import Counter

LOG = "/Users/atrenew/Downloads/run_7970406_20260804_193433_3d493400.log"

def main():
    with open(LOG, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    total = len(lines)
    cnt = Counter()
    fail_reasons = Counter()      # [BATCH] ❌ 失败: <reason> 中的 reason
    opo_attr = []                # 属性值相关行
    fatal = []                   # 致命异常
    model_fail_samples = []
    publish_fail_samples = []
    fixed_counts = Counter()     # 库存修复 fixed=N 统计
    publish_success = 0
    draft_saved = 0

    model_fail_seen = 0
    for line in lines:
        if "[MODEL] Playwright locator 失败" in line:
            cnt["MODEL_PW_TIMEOUT"] += 1
        if "[CHECK] 型号选择失败" in line:
            cnt["MODEL_SELECT_FAIL"] += 1
            if len(model_fail_samples) < 5:
                model_fail_samples.append(line.strip())
        if "发布失败，兜底保存草稿" in line:
            cnt["PUBLISH_FALLBACK_DRAFT"] += 1
            if len(publish_fail_samples) < 5:
                publish_fail_samples.append(line.strip())
        if "发布商品: 成功" in line:
            publish_success += 1
        if "兜底保存草稿: 成功" in line or "保存草稿: 成功" in line:
            draft_saved += 1
        if "[BATCH] ❌ 失败" in line:
            cnt["BATCH_FAIL"] += 1
            m = re.search(r"❌ 失败: (.+?)(?: \(|$)", line)
            if m:
                fail_reasons[m.group(1)] += 1
        if "属性值" in line:
            opo_attr.append(line.strip())
        if "FATAL" in line or "Traceback" in line or "Exception" in line:
            fatal.append(line.strip())
        m = re.search(r"fixed=(\d+)", line)
        if m:
            fixed_counts[int(m.group(1))] += 1

    print(f"日志总行数: {total}")
    print("="*60)
    print("【运行时报错分类统计】")
    print(f"  MODEL Playwright 30s 超时(次数): {cnt['MODEL_PW_TIMEOUT']}")
    print(f"  [CHECK] 型号选择失败:            {cnt['MODEL_SELECT_FAIL']}")
    print(f"  发布失败→兜底保存草稿:          {cnt['PUBLISH_FALLBACK_DRAFT']}")
    print(f"  [BATCH] ❌ 失败(最终):          {cnt['BATCH_FAIL']}")
    print(f"  发布商品成功(标记):            {publish_success}")
    print(f"  兜底保存草稿成功(标记):        {draft_saved}")
    print(f"  属性值相关行:                  {len(opo_attr)}")
    print(f"  致命异常 FATAL/Traceback/Exc:  {len(fatal)}")
    print("="*60)

    print("\n【最终失败原因分布 (❌ 失败: <reason>)】")
    for reason, n in fail_reasons.most_common():
        print(f"  {n:4d}  {reason}")

    print("\n【库存修复 fixed 计数分布】")
    for k in sorted(fixed_counts):
        print(f"  fixed={k}: {fixed_counts[k]} 次")

    print("\n【型号选择失败 样例】")
    for s in model_fail_samples:
        print("  " + s)

    print("\n【发布失败兜底 样例】")
    for s in publish_fail_samples:
        print("  " + s)

    if opo_attr:
        print("\n【属性值错误 样例(前15)】")
        for s in opo_attr[:15]:
            print("  " + s)
    if fatal:
        print("\n【致命异常 样例(前15)】")
        for s in fatal[:15]:
            print("  " + s)

if __name__ == "__main__":
    main()
