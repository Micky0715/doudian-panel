"""Read-only XLSX discovery and schema inspection using the OOXML container.

This module never writes to a source workbook. It intentionally inspects only
the leading rows needed for schema recognition, even when the workbook has
hundreds of thousands of data rows.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence
from xml.etree import ElementTree as ET


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class ExcelInspectionError(ValueError):
    pass


@dataclass(frozen=True)
class ExcelCandidate:
    path: str
    name: str
    size: int
    modified_at: str

    def to_dict(self):
        return {
            "path": self.path,
            "name": self.name,
            "size": self.size,
            "modified_at": self.modified_at,
        }


def ensure_allowed_path(path: str, allowed_roots: Sequence[str]) -> str:
    resolved = os.path.realpath(os.path.expanduser(path or ""))
    for root in allowed_roots:
        allowed = os.path.realpath(os.path.expanduser(root))
        try:
            if os.path.commonpath([resolved, allowed]) == allowed:
                return resolved
        except ValueError:
            pass
    raise ExcelInspectionError("文件不在允许的导入目录中")


def discover_excel_files(directory: str, limit: int = 30) -> List[ExcelCandidate]:
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        return []
    items = []
    for entry in root.iterdir():
        if (not entry.is_file() or entry.name.startswith("~$")
                or entry.suffix.lower() not in {".xlsx", ".xls"}):
            continue
        stat = entry.stat()
        items.append(ExcelCandidate(
            path=str(entry),
            name=entry.name,
            size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime).isoformat(),
        ))
    return sorted(items, key=lambda item: item.modified_at, reverse=True)[:max(1, min(limit, 100))]


def load_profiles(path: str) -> List[dict]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    return list(data.get("profiles") or [])


def _workbook_sheets(archive: zipfile.ZipFile) -> List[tuple[str, str]]:
    try:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise ExcelInspectionError("不是有效的 XLSX 工作簿") from exc
    targets = {
        rel.attrib.get("Id", ""): rel.attrib.get("Target", "")
        for rel in rels.findall("{%s}Relationship" % PKG_REL_NS)
    }
    result = []
    for sheet in workbook.findall(".//{%s}sheet" % MAIN_NS):
        rel_id = sheet.attrib.get("{%s}id" % DOC_REL_NS, "")
        target = targets.get(rel_id, "")
        if not target:
            continue
        target = target.lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        result.append((sheet.attrib.get("name", ""), target))
    return result


def _shared_strings(archive: zipfile.ZipFile, max_index: int) -> Dict[int, str]:
    if max_index < 0:
        return {}
    try:
        stream = archive.open("xl/sharedStrings.xml")
    except KeyError:
        return {}
    values = {}
    index = -1
    with stream:
        for event, node in ET.iterparse(stream, events=("end",)):
            if node.tag == "{%s}si" % MAIN_NS:
                index += 1
                if index <= max_index:
                    values[index] = "".join(t.text or "" for t in node.iter("{%s}t" % MAIN_NS))
                node.clear()
                if index >= max_index:
                    break
    return values


def _raw_leading_rows(archive: zipfile.ZipFile, sheet_path: str, max_rows: int):
    rows = []
    max_shared_index = -1
    try:
        stream = archive.open(sheet_path)
    except KeyError as exc:
        raise ExcelInspectionError("工作表内容缺失: %s" % sheet_path) from exc
    with stream:
        for event, node in ET.iterparse(stream, events=("end",)):
            if node.tag != "{%s}row" % MAIN_NS:
                continue
            cells = []
            for cell in node.findall("{%s}c" % MAIN_NS):
                kind = cell.attrib.get("t", "")
                value_node = cell.find("{%s}v" % MAIN_NS)
                inline = cell.find("{%s}is" % MAIN_NS)
                raw = value_node.text if value_node is not None else ""
                if kind == "s" and raw:
                    try:
                        max_shared_index = max(max_shared_index, int(raw))
                    except ValueError:
                        pass
                elif kind == "inlineStr" and inline is not None:
                    raw = "".join(t.text or "" for t in inline.iter("{%s}t" % MAIN_NS))
                cells.append({"ref": cell.attrib.get("r", ""), "kind": kind, "raw": raw})
            rows.append(cells)
            node.clear()
            if len(rows) >= max_rows:
                break
    return rows, max_shared_index


def _resolve_rows(raw_rows, shared: Mapping[int, str]):
    result = []
    for raw_row in raw_rows:
        row = []
        for cell in raw_row:
            raw = cell["raw"]
            if cell["kind"] == "s" and raw:
                try:
                    raw = shared.get(int(raw), "")
                except ValueError:
                    raw = ""
            row.append(str(raw or "").strip())
        result.append(row)
    return result


def _best_header(rows: Iterable[Sequence[str]]) -> List[str]:
    best = []
    for row in rows:
        cleaned = [str(value or "").strip() for value in row]
        score = sum(bool(value) for value in cleaned)
        if score > sum(bool(value) for value in best):
            best = cleaned
    return best


def _normal_header(value: str) -> str:
    return re.sub(r"[\s（）()_\-/]+", "", str(value or "")).casefold()


def match_profiles(filename: str, headers: Sequence[str], profiles: Sequence[Mapping]) -> List[dict]:
    actual = {_normal_header(value) for value in headers if value}
    matches = []
    for profile in profiles:
        required = {_normal_header(value) for value in profile.get("required_headers", [])}
        header_ok = bool(required) and required.issubset(actual)
        filename_ok = any(re.search(pattern, filename, re.I) for pattern in profile.get("filename_patterns", []))
        if header_ok:
            matches.append({
                "id": profile.get("id"),
                "name": profile.get("name"),
                "header_match": True,
                "filename_match": filename_ok,
            })
    return matches


def inspect_xlsx(path: str, profiles: Sequence[Mapping], max_rows: int = 8) -> dict:
    if Path(path).suffix.lower() != ".xlsx":
        raise ExcelInspectionError("当前阶段只支持 .xlsx；旧版 .xls 需要先另存为 .xlsx")
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise ExcelInspectionError("文件损坏或不是有效的 XLSX") from exc
    with archive:
        sheet_defs = _workbook_sheets(archive)
        raw_by_sheet = []
        max_shared = -1
        for name, sheet_path in sheet_defs:
            raw_rows, sheet_max = _raw_leading_rows(archive, sheet_path, max_rows)
            raw_by_sheet.append((name, raw_rows))
            max_shared = max(max_shared, sheet_max)
        shared = _shared_strings(archive, max_shared)
        sheets = []
        all_matches = []
        for name, raw_rows in raw_by_sheet:
            rows = _resolve_rows(raw_rows, shared)
            headers = _best_header(rows)
            matches = match_profiles(os.path.basename(path), headers, profiles)
            all_matches.extend(dict(item, sheet=name) for item in matches)
            sheets.append({"name": name, "headers": headers, "sample_rows": rows[:3]})
    unique_profiles = {item["id"] for item in all_matches}
    status = "recognized" if len(unique_profiles) == 1 else ("ambiguous" if unique_profiles else "unknown")
    return {
        "name": os.path.basename(path),
        "size": os.path.getsize(path),
        "status": status,
        "profile_matches": all_matches,
        "sheets": sheets,
    }
