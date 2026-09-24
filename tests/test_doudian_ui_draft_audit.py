import json
import os
import tempfile
import unittest
from pathlib import Path

from scripts.doudian_ui_draft_audit import _latest_api_snapshot, reconcile


def api_item(product_id, category="1000007628"):
    return {"product_id": str(product_id), "category_leaf_id": category}


def ui_item(product_id):
    return {"product_id": str(product_id), "name": f"商品{product_id}"}


class DraftUIReconciliationTests(unittest.TestCase):
    def test_latest_snapshot_ignores_obsolete_status_only_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shop = root / "156450040"
            shop.mkdir()
            current = shop / "current.json"
            obsolete = shop / "obsolete.json"
            current.write_text(json.dumps({
                "schema_version": 2, "status_filter": 0, "check_status_filter": 1,
            }))
            obsolete.write_text(json.dumps({
                "schema_version": 1, "status_filter": 2,
            }))
            os.utime(obsolete, (current.stat().st_mtime + 10, current.stat().st_mtime + 10))
            self.assertEqual(_latest_api_snapshot(root, "156450040"), current)

    def test_matches_all_categories(self):
        api = {"items": [api_item(1), api_item(2, "other")]}
        result = reconcile(api, {"items": [ui_item(1), ui_item(2)]})
        self.assertEqual(result["match_scope"], "all_categories")
        self.assertEqual(result["api_only_count"], 0)
        self.assertEqual(result["ui_only_count"], 0)

    def test_matches_phone_filter(self):
        api = {"items": [api_item(1), api_item(2, "other")]}
        result = reconcile(api, {"items": [ui_item(1)]})
        self.assertEqual(result["match_scope"], "phone_category")

    def test_mismatch_fails_closed_with_samples(self):
        api = {"items": [api_item(1), api_item(2)]}
        result = reconcile(api, {"items": [ui_item(1), ui_item(3)]})
        self.assertEqual(result["match_scope"], "mismatch")
        self.assertEqual(result["api_only_sample"], ["2"])
        self.assertEqual(result["ui_only_sample"], ["3"])


if __name__ == "__main__":
    unittest.main()
