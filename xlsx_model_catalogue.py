"""Read DouDian attribute-detail XLSX exports using only the Python standard library."""

from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

BRAND_MARKERS = (
    ("Redmi", ("redmi/红米", "redmi", "红米")),
    ("Apple", ("apple/苹果", "iphone", "苹果")),
    ("Huawei", ("huawei/华为", "华为")),
    ("Honor", ("honor/荣耀", "荣耀")),
    ("Samsung", ("samsung/三星", "三星", "galaxy")),
    ("Xiaomi", ("xiaomi/小米", "小米")),
    ("OPPO", ("oppo",)),
    ("vivo", ("vivo",)),
    ("iQOO", ("iqoo",)),
    ("OnePlus", ("oneplus", "一加")),
    ("realme", ("realme", "真我")),
)


def infer_brand(title: str, model: str = "") -> str:
    text = f"{title} {model}".casefold()
    for brand, markers in BRAND_MARKERS:
        if any(marker.casefold() in text for marker in markers):
            return brand
    return ""


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference).group(0)
    number = 0
    for char in letters:
        number = number * 26 + ord(char) - 64
    return number - 1


def _rows(path: Path):
    with ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{{{MAIN}}}si"):
                strings.append("".join(node.text or "" for node in item.iter(f"{{{MAIN}}}t")))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        targets = {item.attrib["Id"]: item.attrib["Target"]
                   for item in relationships.findall(f"{{{PKG}}}Relationship")}
        first_sheet = workbook.find(f"{{{MAIN}}}sheets")[0]
        target = targets[first_sheet.attrib[f"{{{REL}}}id"]].lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheet = ET.fromstring(archive.read(target))
        for row in sheet.findall(f".//{{{MAIN}}}sheetData/{{{MAIN}}}row"):
            values = []
            for cell in row.findall(f"{{{MAIN}}}c"):
                index = _column_index(cell.attrib["r"])
                values.extend([""] * (index - len(values) + 1))
                value = cell.find(f"{{{MAIN}}}v")
                raw = value.text if value is not None else ""
                if cell.attrib.get("t") == "s" and raw:
                    raw = strings[int(raw)]
                elif cell.attrib.get("t") == "inlineStr":
                    raw = "".join(node.text or "" for node in cell.iter(f"{{{MAIN}}}t"))
                values[index] = raw
            yield values


def read_attribute_models(paths):
    catalogue = {}
    for raw_path in paths:
        path = Path(raw_path)
        rows = iter(_rows(path))
        header = None
        for row in rows:
            if "商品名称" in row and "型号" in row:
                header = row
                break
        if not header:
            continue
        title_index, model_index = header.index("商品名称"), header.index("型号")
        for row in rows:
            model = str(row[model_index] if model_index < len(row) else "").strip()
            title = str(row[title_index] if title_index < len(row) else "").strip()
            brand = infer_brand(title, model)
            if not brand or not model:
                continue
            key = (brand.casefold(), re.sub(r"\s+", "", model).casefold())
            entry = catalogue.setdefault(key, {
                "brand": brand, "model": model, "source": "doudian_attribute_detail",
                "rows": 0, "files": set(),
            })
            entry["rows"] += 1
            entry["files"].add(path.name)
    result = []
    for entry in catalogue.values():
        entry["files"] = sorted(entry["files"])
        result.append(entry)
    return sorted(result, key=lambda item: (item["brand"].casefold(), item["model"].casefold()))
