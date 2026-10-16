import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from app.gaia_listing.application.rejected_skus import (
    missing_goods_pair,
    rejected_sku_evidence, rejected_skus_in_workbook,
    rejected_variant_evidence, rejected_variants_in_workbook,
)


class RejectedSkuTests(unittest.TestCase):
    def test_missing_goods_pair_requires_one_explicit_pair(self):
        self.assertEqual(("123", "95"), missing_goods_pair(
            "第1行：标品ID[123]和抖音成色ID[95]未找到对应的goodsId"))
        self.assertIsNone(missing_goods_pair("没有对应的goodsId"))
        self.assertIsNone(missing_goods_pair(
            "标品ID[123]和抖音成色ID[95]未找到对应的goodsId；"
            "标品ID[456]和抖音成色ID[99]未找到对应的goodsId"))

    def test_successful_parent_keeps_proven_retry_rejections(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "accepted.json"
            path.write_text(json.dumps({
                "id": "parent", "state": "gaia_accepted", "mapping_version_id": "v1",
                "gaia_receipt": {"submit_intent": True, "retry_history": [
                    {"batch_id": "parent", "sku_id": "123", "quality_id": "95",
                     "zero_new_products_verified": True},
                    {"batch_id": "unsafe", "sku_id": "456", "quality_id": "99",
                     "zero_new_products_verified": False},
                ]},
            }), encoding="utf-8")
            self.assertEqual({}, rejected_sku_evidence(root, "v1", ["95"]))
            self.assertEqual({("123", "95"): "parent"}, rejected_variant_evidence(root, "v1"))

    def test_only_explicit_submitted_category_rejections_are_quarantined(self):
        with tempfile.TemporaryDirectory() as root:
            batch_dir = Path(root) / "batches"
            batch_dir.mkdir()
            examples = [
                ("confirmed", "第19行：标品ID[111]根据标品skuId未查询到类目信息失败", True, "v1"),
                ("gaia_rejected", "第19行：标品ID[222]根据标品skuId未查询到类目信息失败", False, "v1"),
                ("gaia_rejected", "第19行：标品ID[333]根据标品skuId未查询到类目信息失败", True, "v2"),
                ("gaia_rejected", "第19行：标品ID[444]库存不足", True, "v1"),
                ("gaia_rejected", "第19行：标品ID[555]和抖音成色ID[99]查询失败：根据标品skuId未查询到类目信息失败", True, "v1"),
                ("gaia_rejected", "第11行：标品ID[666]和抖音成色ID[100]未找到对应的goodsId", True, "v1"),
                ("gaia_rejected", "第12行：标品ID[777]和抖音成色ID[90]没有对应的 goodsId", True, "v1"),
            ]
            for number, (state, feedback, intent, version) in enumerate(examples):
                (batch_dir / f"{number}.json").write_text(json.dumps({
                    "id": str(number), "state": state, "mapping_version_id": version,
                    "gaia_receipt": {"submit_intent": intent, "result": {"feedback": feedback}},
                }), encoding="utf-8")
            evidence = rejected_sku_evidence(batch_dir, "v1")
            self.assertEqual({"555": "4"}, evidence)
            self.assertEqual({"555": "4"},
                             rejected_sku_evidence(batch_dir, "v1", ["100"]))
            self.assertEqual({"555": "4"},
                             rejected_sku_evidence(batch_dir, "v1", ["90"]))
            self.assertEqual({"555": "4"}, rejected_sku_evidence(batch_dir, "v1", ["99"]))
            self.assertEqual({("666", "100"): "5", ("777", "90"): "6"},
                             rejected_variant_evidence(batch_dir, "v1"))

            workbook = Workbook()
            workbook.active.append(["a", "b", "c", "标品ID"])
            workbook.active.append(["1", "2", "3", "555"])
            workbook.active.append(["1", "2", "3", "777"])
            workbook.active.cell(2, 5, "99")
            workbook.active.append(["1", "2", "3", "666", "100"])
            path = Path(root) / "file.xlsx"
            workbook.save(path)
            self.assertEqual(["555"], rejected_skus_in_workbook(path, evidence))
            self.assertEqual([("666", "100")], rejected_variants_in_workbook(
                path, rejected_variant_evidence(batch_dir, "v1")))


if __name__ == "__main__":
    unittest.main()
