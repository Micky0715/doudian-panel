import json
import tempfile
import unittest
from pathlib import Path

from app.listing_execution import (
    ExecutionPlanError,
    build_execution_plan,
    load_execution_plan,
    store_execution_plan,
)


def decision(product_id="1", state="ready", execution_mode="publish"):
    return {
        "product_id": product_id,
        "name": "99新华为 P60 二手手机",
        "decision": state,
        "brand": "华为",
        "model": "P60",
        "platform_model": "P60",
        "screen_shape": "直屏" if execution_mode == "publish" else "",
        "condition": "99新",
        "image_dir": "/images/1/华为/P60",
        "main_image_count": 1,
        "auxiliary_image_count": 4,
        "detail_image_count": 14,
        "execution_mode": execution_mode if state == "ready" else "",
        "warnings": [],
        "reasons": [],
    }


def report(decisions):
    return {
        "schema_version": 1,
        "shop_id": "156450040",
        "ready": sum(row["decision"] == "ready" for row in decisions),
        "manual_required": sum(row["decision"] == "manual_required" for row in decisions),
        "out_of_scope": sum(row["decision"] == "out_of_scope" for row in decisions),
        "decisions": decisions,
    }


class ExecutionPlanTests(unittest.TestCase):
    def write_report(self, root, value):
        path = Path(root) / "report.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_only_ready_decisions_are_handed_off(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1"), decision("2", "manual_required")])
            source = self.write_report(directory, value)
            plan = build_execution_plan(value, source, action="publish", now=100)
            self.assertEqual([item.product_id for item in plan.items], ["1"])
            self.assertEqual(plan.action, "publish")

    def test_requested_manual_product_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("2", "manual_required")])
            source = self.write_report(directory, value)
            with self.assertRaisesRegex(ExecutionPlanError, "not ready"):
                build_execution_plan(
                    value, source, requested_ids=["2"], action="publish", now=100
                )

    def test_zero_ready_is_not_an_executable_success(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("2", "manual_required")])
            source = self.write_report(directory, value)
            with self.assertRaisesRegex(ExecutionPlanError, "no ready products"):
                build_execution_plan(value, source, action="publish", now=100)

    def test_plan_round_trip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1")])
            source = self.write_report(directory, value)
            plan = build_execution_plan(value, source, action="publish", now=100)
            path = store_execution_plan(plan, Path(directory) / "plans")
            loaded = load_execution_plan(path, expected_shop_id="156450040", now=101)
            self.assertEqual(loaded.items[0].screen_shape, "直屏")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1")])
            source = self.write_report(directory, value)
            path = store_execution_plan(
                build_execution_plan(value, source, action="publish", now=100),
                Path(directory) / "plans"
            )
            payload = json.loads(path.read_text())
            payload["items"][0]["product_id"] = "9"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ExecutionPlanError, "checksum"):
                load_execution_plan(path, now=101)

    def test_expired_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1")])
            source = self.write_report(directory, value)
            path = store_execution_plan(
                build_execution_plan(
                    value, source, action="publish", now=100, ttl_seconds=5
                ),
                Path(directory) / "plans",
            )
            with self.assertRaisesRegex(ExecutionPlanError, "expired"):
                load_execution_plan(path, now=106)

    def test_save_draft_plan_allows_missing_screen_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1", execution_mode="save_draft")])
            source = self.write_report(directory, value)
            plan = build_execution_plan(value, source, action="save_draft", now=100)
            self.assertEqual(plan.action, "save_draft")
            self.assertEqual(plan.items[0].screen_shape, "")

    def test_requested_product_cannot_override_its_action(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([decision("1", execution_mode="save_draft")])
            source = self.write_report(directory, value)
            with self.assertRaisesRegex(ExecutionPlanError, "not ready for publish"):
                build_execution_plan(
                    value, source, requested_ids=["1"], action="publish", now=100
                )

    def test_mixed_action_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            value = report([
                decision("1", execution_mode="publish"),
                decision("2", execution_mode="save_draft"),
            ])
            source = self.write_report(directory, value)
            with self.assertRaisesRegex(ExecutionPlanError, "cannot mix"):
                build_execution_plan(value, source, now=100)


if __name__ == "__main__":
    unittest.main()
