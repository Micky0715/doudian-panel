import json
import os
import tempfile
import unittest
import zipfile

from app.gaia_listing.infrastructure.excel_inspector import (
    ExcelInspectionError,
    discover_excel_files,
    ensure_allowed_path,
    inspect_xlsx,
)


WORKBOOK = '''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <sheets><sheet name="数据1" sheetId="1" r:id="rId1"/></sheets></workbook>'''
RELS = '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="worksheet"/>
</Relationships>'''


def write_minimal_xlsx(path, headers):
    cells = ''.join(
        '<c r="%s1" t="inlineStr"><is><t>%s</t></is></c>' % (chr(65 + i), value)
        for i, value in enumerate(headers)
    )
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData><row r="1">%s</row></sheetData></worksheet>') % cells
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('xl/workbook.xml', WORKBOOK)
        archive.writestr('xl/_rels/workbook.xml.rels', RELS)
        archive.writestr('xl/worksheets/sheet1.xml', sheet)


class GaiaExcelInspectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profiles = [{
            'id': 'pricing_detail', 'name': '价格明细',
            'filename_patterns': ['价格'],
            'required_headers': ['规则编码', '拍拍标品SKUID', '品牌名称', '型号名称', '成色名称', '生效销售价'],
        }]

    def tearDown(self):
        self.tmp.cleanup()

    def test_inspects_and_recognizes_schema(self):
        path = os.path.join(self.tmp.name, '严选价格.xlsx')
        write_minimal_xlsx(path, self.profiles[0]['required_headers'])
        result = inspect_xlsx(path, self.profiles)
        self.assertEqual('recognized', result['status'])
        self.assertEqual('pricing_detail', result['profile_matches'][0]['id'])

    def test_unknown_schema_is_not_guessed(self):
        path = os.path.join(self.tmp.name, '其他.xlsx')
        write_minimal_xlsx(path, ['姓名', '部门'])
        self.assertEqual('unknown', inspect_xlsx(path, self.profiles)['status'])

    def test_discovery_orders_newest_first(self):
        older = os.path.join(self.tmp.name, 'a.xlsx')
        newer = os.path.join(self.tmp.name, 'b.xlsx')
        write_minimal_xlsx(older, ['A'])
        write_minimal_xlsx(newer, ['B'])
        os.utime(older, (1, 1)); os.utime(newer, (2, 2))
        self.assertEqual('b.xlsx', discover_excel_files(self.tmp.name)[0].name)

    def test_discovery_ignores_excel_lock_files(self):
        normal = os.path.join(self.tmp.name, '模板.xlsx')
        locked = os.path.join(self.tmp.name, '~$模板.xlsx')
        write_minimal_xlsx(normal, ['A'])
        with open(locked, 'wb') as handle:
            handle.write(b'lock')
        self.assertEqual(['模板.xlsx'], [item.name for item in discover_excel_files(self.tmp.name)])

    def test_path_cannot_escape_allowed_root(self):
        with self.assertRaises(ExcelInspectionError):
            ensure_allowed_path('/etc/passwd', [self.tmp.name])


if __name__ == '__main__':
    unittest.main()
