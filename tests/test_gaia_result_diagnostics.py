import tempfile
import unittest
import io
import json
import hashlib
from pathlib import Path

from flask import Flask
from openpyxl import Workbook

from app.gaia_listing.application.result_diagnostics import diagnose_gaia_result
from app.gaia_listing.api.routes import create_gaia_blueprint


def workbook(path, rows, bad_dimension=False):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    book.save(path)
    if bad_dimension:
        # Simulates Gaia result files whose worksheet declares A1:A1.
        from zipfile import ZipFile, ZIP_DEFLATED
        with ZipFile(path) as source:
            items = [(info.filename, source.read(info.filename)) for info in source.infolist()]
        with ZipFile(path, "w", ZIP_DEFLATED) as target:
            for name, content in items:
                if name == "xl/worksheets/sheet1.xml":
                    content = content.replace(b'dimension ref="A1:I3"', b'dimension ref="A1:A1"')
                target.writestr(name, content)


class GaiaResultDiagnosticsTests(unittest.TestCase):
    def test_read_only_endpoint_does_not_change_batch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'panel_data/gaia_listing/outputs/abc/gaia_upload.xlsx'
            source.parent.mkdir(parents=True)
            result = root / 'result.xlsx'
            workbook(source, [["", "", "", "standardSkuId", "dyQualityId", "", "", ""],
                              [2, "A", 0, "sku1", 99, "", "", ""]])
            workbook(result, [["", "", "goodsId", "", "", "", "错误信息", "standardSkuId", "dyQualityId"],
                              [2, "p", "4868382", "", "", "", "获取抖快渠道规则价格失败", "sku1", 99]])
            batch_path = root / 'panel_data/gaia_listing/batches/abc.json'
            batch_path.parent.mkdir(parents=True)
            batch_path.write_text(json.dumps({
                'id': 'abc', 'shop_id': '5576543', 'output_path': str(source.resolve()),
                'output_hash': hashlib.sha256(source.read_bytes()).hexdigest(),
                'mapping_version_id': '0' * 32, 'state': 'manual_required',
            }), encoding='utf-8')
            before = batch_path.read_bytes()
            app = Flask('gaia_result_diagnosis_test')
            app.register_blueprint(create_gaia_blueprint(folder, ['5576543']))
            client = app.test_client()
            response = client.post('/api/gaia/batches/abc/result-diagnosis', data={
                'shop_id': '5576543', 'file': (io.BytesIO(result.read_bytes()), 'result.xlsx'),
            }, content_type='multipart/form-data')
            self.assertEqual(200, response.status_code, response.get_json())
            self.assertEqual(1, len(response.get_json()['diagnosis']['primary_errors']))
            self.assertEqual(before, batch_path.read_bytes())
            wrong_shop = client.post('/api/gaia/batches/abc/result-diagnosis', data={
                'shop_id': '129675602', 'file': (io.BytesIO(result.read_bytes()), 'result.xlsx'),
            }, content_type='multipart/form-data')
            self.assertEqual(400, wrong_shop.status_code)

    def test_primary_rule_price_error_separated_from_group_cascade(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "upload.xlsx"
            result = Path(folder) / "result.xlsx"
            workbook(source, [
                ["scene", "mark", "quality link", "standardSkuId", "dyQualityId", "name", "strategy", "prefix"],
                [2, "A", 0, "sku1", 99, "", "D且E且G", "a"],
                [2, "A", 0, "sku2", 100, "", "D且E且G", "a"],
            ])
            workbook(result, [
                ["scene", "mark", "goodsId", "price", "strategy", "source", "错误信息", "standardSkuId", "dyQualityId"],
                [2, "p", "4868382", "", "D且E且G", "", "获取抖快渠道规则价格失败", "sku1", 99],
                [2, "p", "123", "", "D且E且G", "", "同组其他数据有错误", "sku2", 100],
            ], bad_dimension=True)
            report = diagnose_gaia_result(
                batch={"id": "abc", "shop_id": "5576543", "output_path": str(source)},
                result_path=result,
                goods_by_sku={"sku1": {"99": ["4826348"]}},
            )
            self.assertEqual(2, report["result_rows"])
            self.assertEqual(1, report["cascade_error_count"])
            self.assertEqual("rule_price_lookup_failed", report["primary_errors"][0]["category"])
            self.assertIs(False, report["primary_errors"][0]["goods_in_mapping_snapshot"])
            self.assertEqual(1, report["rule_price_error_count"])
            self.assertEqual("complete", report["pair_coverage"])
            self.assertEqual("sku_scope_only", report["result_attribution"])
            self.assertIn("抖快渠道规则价格", report["next_step"])
            self.assertEqual("unknown", report["platform_product_state"])

    def test_partial_result_and_changed_upload_are_not_overstated(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "upload.xlsx"
            result = Path(folder) / "result.xlsx"
            workbook(source, [
                ["", "", "", "standardSkuId", "dyQualityId", "", "", ""],
                [2, "A", 0, "sku1", 99, "", "", ""],
                [2, "A", 0, "sku1", 95, "", "", ""],
            ])
            workbook(result, [
                ["", "", "goodsId", "", "", "", "错误信息", "standardSkuId", "dyQualityId"],
                [2, "p", "123", "", "", "", "同组其他数据有错误", "sku1", 99],
            ])
            batch = {"output_path": str(source), "output_hash": hashlib.sha256(source.read_bytes()).hexdigest()}
            report = diagnose_gaia_result(batch=batch, result_path=result)
            self.assertEqual("partial", report["pair_coverage"])
            self.assertEqual(1, report["missing_sku_quality_pairs"])
            self.assertEqual("sku_scope_only", report["result_attribution"])
            with self.assertRaisesRegex(ValueError, "哈希不一致"):
                diagnose_gaia_result(batch=dict(batch, output_hash="0" * 64), result_path=result)

    def test_unrelated_sku_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "upload.xlsx"
            result = Path(folder) / "result.xlsx"
            workbook(source, [["", "", "", "standardSkuId", "dyQualityId", "", "", ""],
                              [2, "A", 0, "sku1", 99, "", "", ""]])
            workbook(result, [["", "", "goodsId", "", "", "", "错误信息", "standardSkuId", "dyQualityId"],
                              [2, "p", "1", "", "", "", "failed", "other", 99]])
            with self.assertRaisesRegex(ValueError, "未上传"):
                diagnose_gaia_result(batch={"output_path": str(source)}, result_path=result)


if __name__ == "__main__":
    unittest.main()
