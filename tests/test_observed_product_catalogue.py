import csv
import tempfile
import unittest
from pathlib import Path

from tools.build_observed_product_catalogue import build


class ObservedProductCatalogueTest(unittest.TestCase):
    def test_filters_non_phone_and_rejects_brand_conflicts(self):
        fields = ["shop_id", "model", "brand", "category", "product_count"]
        rows = [
            ["1", "OPPO Reno16", "OPPO", "手机", "2"],
            ["2", "OPPO Reno16", "OPPO", "手机", "3"],
            ["1", "荣耀 400", "Honor", "手机", "1"],
            ["2", "荣耀 400", "Huawei", "手机", "1"],
            ["1", "iPad", "Apple", "平板", "9"],
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "models.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(fields)
                writer.writerows(rows)
            payload = build(path)
        self.assertEqual([row["model"] for row in payload["catalogue"]], ["OPPO Reno16"])
        self.assertEqual(payload["catalogue"][0]["shop_count"], 2)
        self.assertEqual(payload["catalogue"][0]["rows"], 5)
        self.assertTrue(payload["catalogue"][0]["diagnostic_only"])
        self.assertEqual(payload["metadata"]["rejected_brand_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
