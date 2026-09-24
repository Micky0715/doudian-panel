"""Stream a large XLSX and export unique phone brand/model pairs."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter

from inspect_xlsx import NS, cell_value, shared_strings


def column_number(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    value = 0
    for char in letters.group(0) if letters else "":
        value = value * 26 + ord(char) - 64
    return value - 1


def sheet_path(book, wanted):
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    targets = {x.get("Id"): x.get("Target") for x in rels.findall("p:Relationship", NS)}
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        if sheet.get("name") == wanted:
            target = targets[sheet.get("{%s}id" % NS["r"])]
            return target.lstrip("/") if target.startswith("/") else "xl/" + target
    raise ValueError(f"sheet not found: {wanted}")


def extract(path, sheet="数据1", category="手机"):
    with zipfile.ZipFile(path) as book:
        strings = shared_strings(book)
        source = book.open(sheet_path(book, sheet))
        headers = None
        counts = Counter()
        total_rows = category_rows = skipped = 0
        for event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag != "{%s}row" % NS["m"]:
                continue
            values = {}
            for cell in elem.findall("m:c", NS):
                values[column_number(cell.get("r"))] = cell_value(cell, strings)
            if headers is None:
                headers = {str(value): index for index, value in values.items() if value}
                required = {"三级类目名称", "品牌名称", "型号名称"}
                missing = required - headers.keys()
                if missing:
                    raise ValueError("missing columns: " + ", ".join(sorted(missing)))
            else:
                total_rows += 1
                if str(values.get(headers["三级类目名称"], "")).strip() != category:
                    elem.clear()
                    continue
                category_rows += 1
                brand = str(values.get(headers["品牌名称"], "") or "").strip()
                model = str(values.get(headers["型号名称"], "") or "").strip()
                if not model:
                    skipped += 1
                else:
                    counts[(brand, model)] += 1
            elem.clear()
    catalogue = [{"brand": brand, "model": model, "rows": count, "source": "excel"}
                 for (brand, model), count in sorted(counts.items(), key=lambda x: (x[0][0].casefold(), x[0][1].casefold()))]
    return {"sheet": sheet, "category": category, "total_rows": total_rows,
            "category_rows": category_rows, "unique_brands": len({x["brand"] for x in catalogue}),
            "unique_models": len(catalogue), "missing_model_rows": skipped,
            "catalogue": catalogue}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--sheet", default="数据1")
    parser.add_argument("--category", default="手机")
    parser.add_argument("--output")
    args = parser.parse_args()
    result = extract(args.path, args.sheet, args.category)
    summary = {key: value for key, value in result.items() if key != "catalogue"}
    print(json.dumps(summary, ensure_ascii=True, indent=2))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
