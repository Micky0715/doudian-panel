import unittest

from scripts.doudian_listing_preflight import select_snapshot_items


class ListingPreflightScriptTests(unittest.TestCase):
    def test_selects_requested_products_in_requested_order(self):
        snapshot = {"items": [
            {"product_id": "1", "name": "one"},
            {"product_id": "2", "name": "two"},
        ]}
        selected = select_snapshot_items(snapshot, ["2", "1"])
        self.assertEqual([row["product_id"] for row in selected], ["2", "1"])

    def test_rejects_product_absent_from_current_snapshot(self):
        with self.assertRaisesRegex(RuntimeError, "absent"):
            select_snapshot_items({"items": [{"product_id": "1"}]}, ["2"])

    def test_rejects_duplicate_product_ids(self):
        with self.assertRaisesRegex(RuntimeError, "unique numeric"):
            select_snapshot_items({"items": [{"product_id": "1"}]}, ["1", "1"])


if __name__ == "__main__":
    unittest.main()
