"""Inspect XLSX sheet names, dimensions and leading rows using only stdlib."""

from __future__ import annotations

import argparse
import json
import re
import zipfile
import xml.etree.ElementTree as ET


NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "p": "http://schemas.openxmlformats.org/package/2006/relationships"}


def shared_strings(book):
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return ["".join(node.text or "" for node in item.findall(".//m:t", NS))
            for item in root.findall("m:si", NS)]


def cell_value(cell, strings):
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", NS))
    node = cell.find("m:v", NS)
    if node is None:
        return None
    value = node.text or ""
    if kind == "s" and value.isdigit():
        return strings[int(value)]
    return value


def inspect(path, rows=5):
    with zipfile.ZipFile(path) as book:
        strings = shared_strings(book)
        workbook = ET.fromstring(book.read("xl/workbook.xml"))
        rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
        targets = {x.get("Id"): x.get("Target") for x in rels.findall("p:Relationship", NS)}
        result = []
        for sheet in workbook.findall("m:sheets/m:sheet", NS):
            target = targets[sheet.get("{%s}id" % NS["r"])]
            xml_path = target.lstrip("/") if target.startswith("/") else "xl/" + target
            root = ET.fromstring(book.read(xml_path))
            dimension = root.find("m:dimension", NS)
            samples = []
            for row in root.findall("m:sheetData/m:row", NS)[:rows]:
                values = {}
                for cell in row.findall("m:c", NS):
                    ref = cell.get("r", "")
                    column = re.match(r"[A-Z]+", ref)
                    values[column.group(0) if column else ref] = cell_value(cell, strings)
                samples.append(values)
            result.append({"name": sheet.get("name"),
                           "dimension": dimension.get("ref") if dimension is not None else "",
                           "rows": samples})
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument("--rows", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(inspect(args.path, args.rows), ensure_ascii=True, indent=2))
