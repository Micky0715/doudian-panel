"""Fill only column A of the official price-uplift import XLSX copy."""

from __future__ import annotations

from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from ..application.price_uplift import PriceUpliftError


NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
SHEET = "xl/worksheets/sheet1.xml"
HEADER = "抖音商品ID（必填）"
FAILURE_HEADER = "导入失败原因"


def _checked_sheet(original: ZipFile):
    if SHEET not in original.namelist():
        raise PriceUpliftError("规则导入模板缺少首个工作表")
    if original.getinfo(SHEET).file_size > 2 * 1024 * 1024:
        raise PriceUpliftError("规则导入模板工作表过大")
    root = ET.fromstring(original.read(SHEET))
    data = root.find(f"{{{NS}}}sheetData")
    if data is None:
        raise PriceUpliftError("规则导入模板没有数据区域")
    rows = data.findall(f"{{{NS}}}row")
    if len(rows) != 1 or rows[0].get("r") != "1":
        raise PriceUpliftError("规则导入模板已有数据，拒绝覆盖")
    cells = rows[0].findall(f"{{{NS}}}c")
    if (len(cells) != 2 or cells[0].get("r") != "A1"
            or "".join(cells[0].itertext()) != HEADER
            or cells[1].get("r") != "B1"
            or "".join(cells[1].itertext()) != FAILURE_HEADER):
        raise PriceUpliftError("规则导入模板表头不匹配")
    return root, data


def inspect_price_uplift_template(template_path: str) -> None:
    with ZipFile(template_path) as original:
        _checked_sheet(original)


def write_price_uplift_template(template_path: str, output_path: str, product_ids: list[str]) -> str:
    source, target = Path(template_path), Path(output_path)
    if source.resolve() == target.resolve():
        raise PriceUpliftError("输出文件不得覆盖规则导入模板")
    if not product_ids:
        raise PriceUpliftError("没有可导入的抖店商品 ID")
    if any(not re.fullmatch(r"[1-9]\d{9,19}", str(product_id)) for product_id in product_ids):
        raise PriceUpliftError("待导入的抖店商品 ID 格式无效")
    with ZipFile(source) as original:
        _checked_sheet(original)
        # Keep the official workbook's original XML namespace and layout. A
        # full ElementTree serialization rewrites every tag to an s: prefix;
        # preserving the vendor template avoids a potential parser mismatch.
        sheet_xml = original.read(SHEET)
        if sheet_xml.count(b"</sheetData>") != 1:
            raise PriceUpliftError("规则导入模板工作表结构已变化")
        rows_xml = b"".join(
            f'<row r="{index}"><c r="A{index}" t="inlineStr"><is><t>{product_id}</t></is></c></row>'.encode()
            for index, product_id in enumerate(product_ids, start=2)
        )
        sheet_xml = sheet_xml.replace(b"</sheetData>", rows_xml + b"</sheetData>", 1)
        sheet_xml, changed = re.subn(
            rb'(<dimension\b[^>]*\bref=")[^"]+("[^>]*/>)',
            lambda match: match.group(1) + f"A1:B{len(product_ids) + 1}".encode() + match.group(2),
            sheet_xml, count=1,
        )
        if changed != 1:
            raise PriceUpliftError("规则导入模板尺寸标记已变化")
        target.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(target, "w") as result:
            for member in original.infolist():
                result.writestr(member, sheet_xml if member.filename == SHEET else original.read(member.filename))
    return str(target)
