import json
import os
import tempfile
import unittest
from pathlib import Path

from integrations.doudian.drafts import DraftScanError, DraftScanner, DraftSnapshotStore


def row(product_id, *, status=0, check_status=1, name=None):
    return {
        "product_id": str(product_id),
        "name": name or f"商品{product_id}",
        "status": status,
        "check_status": check_status,
        "create_time": "100",
        "update_time": "200",
        "category_detail": {
            "first_cid": 1000007020,
            "first_cname": "3C数码及配件",
            "second_cid": 1000007628,
            "second_cname": "二手手机",
        },
    }


class FakeReader:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list_products(self, *, page, size, **filters):
        self.calls.append((page, size, filters))
        value = self.pages[page]
        return value() if callable(value) else value


def payload(rows, total):
    return {"data": rows, "total": total}


class DraftScannerTests(unittest.TestCase):
    def test_scans_all_pages_and_builds_stable_snapshot(self):
        reader = FakeReader({
            1: payload([row(1), row(2)], 5),
            2: payload([row(3), row(4)], 5),
            3: payload([row(5)], 5),
        })
        snapshot = DraftScanner(reader, "156450040", clock=lambda: 123).scan(page_size=2)
        self.assertEqual(snapshot.reported_total, 5)
        self.assertEqual(snapshot.page_count, 3)
        self.assertEqual([item.product_id for item in snapshot.items], ["1", "2", "3", "4", "5"])
        self.assertEqual(snapshot.items[0].category_leaf_id, "1000007628")
        self.assertEqual(reader.calls[0], (1, 2, {"status": 0, "check_status": 1}))
        self.assertEqual(snapshot.schema_version, 2)
        self.assertEqual(snapshot.status_filter, 0)
        self.assertEqual(snapshot.check_status_filter, 1)

    def test_rejects_duplicate_across_pages(self):
        reader = FakeReader({
            1: payload([row(1), row(2)], 4),
            2: payload([row(2), row(3)], 4),
        })
        with self.assertRaisesRegex(DraftScanError, "duplicate draft id"):
            DraftScanner(reader, "1").scan(page_size=2)

    def test_rejects_total_change_during_scan(self):
        reader = FakeReader({
            1: payload([row(1), row(2)], 4),
            2: payload([row(3), row(4)], 5),
        })
        with self.assertRaisesRegex(DraftScanError, "total changed"):
            DraftScanner(reader, "1").scan(page_size=2)

    def test_rejects_early_empty_or_short_page(self):
        with self.assertRaisesRegex(DraftScanError, "empty draft page"):
            DraftScanner(FakeReader({1: payload([], 1)}), "1").scan(page_size=2)
        with self.assertRaisesRegex(DraftScanError, "short draft page"):
            DraftScanner(FakeReader({1: payload([row(1)], 2)}), "1").scan(page_size=2)

    def test_rejects_non_draft_and_invalid_id(self):
        with self.assertRaisesRegex(DraftScanError, "unexpected status"):
            DraftScanner(FakeReader({1: payload([row(1, status=2)], 1)}), "1").scan()
        with self.assertRaisesRegex(DraftScanError, "unexpected check_status"):
            DraftScanner(FakeReader({1: payload([row(1, check_status=3)], 1)}), "1").scan()
        with self.assertRaisesRegex(DraftScanError, "numeric product id"):
            DraftScanner(FakeReader({1: payload([row("bad")], 1)}), "1").scan()

    def test_rejects_safety_limit_before_total(self):
        reader = FakeReader({1: payload([row(1), row(2)], 4)})
        with self.assertRaisesRegex(DraftScanError, "exceeded max_pages"):
            DraftScanner(reader, "1").scan(page_size=2, max_pages=1)

    def test_snapshot_store_is_atomic_and_idempotent(self):
        snapshot = DraftScanner(
            FakeReader({1: payload([row(1)], 1)}), "156450040", clock=lambda: 123,
        ).scan()
        with tempfile.TemporaryDirectory() as directory:
            store = DraftSnapshotStore(directory)
            first = store.save(snapshot)
            second = store.save(snapshot)
            self.assertEqual(first, second)
            self.assertEqual(os.stat(first).st_mode & 0o777, 0o600)
            data = json.loads(Path(first).read_text(encoding="utf-8"))
            self.assertEqual(data["reported_total"], 1)
            self.assertEqual(data["items"][0]["product_id"], "1")


if __name__ == "__main__":
    unittest.main()
