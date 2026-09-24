import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import panel_server


def preflight(state="ready", execution_mode="publish"):
    row = {
        "product_id": "1",
        "name": "99新华为 P60 二手手机",
        "decision": state,
        "brand": "华为",
        "model": "P60",
        "platform_model": "P60",
        "screen_shape": "直屏" if execution_mode == "publish" else "",
        "condition": "99新",
        "image_dir": "/images/156450040/华为/P60",
        "main_image_count": 1,
        "auxiliary_image_count": 4,
        "detail_image_count": 14,
        "execution_mode": execution_mode if state == "ready" else "",
        "warnings": [],
        "reasons": [],
    }
    return {
        "schema_version": 1,
        "shop_id": "156450040",
        "ready": int(state == "ready"),
        "manual_required": int(state == "manual_required"),
        "out_of_scope": int(state == "out_of_scope"),
        "decisions": [row],
    }


class FakeTask:
    def brief(self):
        return {"id": "task", "state": "starting"}


class PanelExecutionApiTests(unittest.TestCase):
    def setUp(self):
        panel_server.app.config.update(TESTING=True)
        self.client = panel_server.app.test_client()

    def write_report(self, root, value):
        directory = Path(root) / "156450040"
        directory.mkdir(parents=True)
        path = directory / "report.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_create_plan_does_not_start_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            report_root = Path(directory) / "reports"
            plan_root = Path(directory) / "plans"
            self.write_report(report_root, preflight())
            with (
                patch.object(panel_server, "PREFLIGHT_REPORT_DIR", str(report_root)),
                patch.object(panel_server, "EXECUTION_PLAN_DIR", str(plan_root)),
                patch("panel_server.start_task") as start_task,
            ):
                response = self.client.post(
                    "/api/preflight/execution-plan",
                    json={
                        "shop_id": "156450040", "report_id": "report.json",
                        "action": "publish",
                    },
                )
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["product_ids"], ["1"])
            self.assertEqual(payload["action"], "publish")
            self.assertTrue((plan_root / "156450040" / payload["execution_plan_id"]).is_file())
            start_task.assert_not_called()

    def test_create_plan_rejects_manual_only_report(self):
        with tempfile.TemporaryDirectory() as directory:
            report_root = Path(directory) / "reports"
            self.write_report(report_root, preflight("manual_required"))
            with patch.object(panel_server, "PREFLIGHT_REPORT_DIR", str(report_root)):
                response = self.client.post(
                    "/api/preflight/execution-plan",
                    json={
                        "shop_id": "156450040", "report_id": "report.json",
                        "action": "publish",
                    },
                )
            self.assertEqual(response.status_code, 400)
            self.assertIn("no ready products", response.get_json()["message"])

    def test_create_plan_rejects_report_path_traversal(self):
        response = self.client.post(
            "/api/preflight/execution-plan",
            json={
                "shop_id": "156450040", "report_id": "../report.json",
                "action": "publish",
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_run_accepts_only_valid_shop_scoped_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            report_root = Path(directory) / "reports"
            plan_root = Path(directory) / "plans"
            self.write_report(report_root, preflight(execution_mode="save_draft"))
            with (
                patch.object(panel_server, "PREFLIGHT_REPORT_DIR", str(report_root)),
                patch.object(panel_server, "EXECUTION_PLAN_DIR", str(plan_root)),
            ):
                created = self.client.post(
                    "/api/preflight/execution-plan",
                    json={
                        "shop_id": "156450040", "report_id": "report.json",
                        "action": "save_draft",
                    },
                ).get_json()
                with patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start:
                    response = self.client.post(
                        "/api/run",
                        json={
                            "shop_id": "156450040",
                            "execution_plan_id": created["execution_plan_id"],
                            "save_only": True,
                        },
                    )
            self.assertEqual(response.status_code, 200)
            kwargs = start.call_args.kwargs
            self.assertTrue(kwargs["execution_plan"].endswith(created["execution_plan_id"]))
            self.assertTrue(kwargs["save_only"])

    def test_run_plan_rejects_publish_before_pilot_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            report_root = Path(directory) / "reports"
            plan_root = Path(directory) / "plans"
            self.write_report(report_root, preflight())
            with (
                patch.object(panel_server, "PREFLIGHT_REPORT_DIR", str(report_root)),
                patch.object(panel_server, "EXECUTION_PLAN_DIR", str(plan_root)),
                patch.dict(panel_server.os.environ, {"DP_API_E_PUBLISH_ENABLED": "0"}),
            ):
                created = self.client.post(
                    "/api/preflight/execution-plan",
                    json={
                        "shop_id": "156450040", "report_id": "report.json",
                        "action": "publish",
                    },
                ).get_json()
                with patch("panel_server.start_task") as start:
                    response = self.client.post(
                        "/api/run",
                        json={
                            "shop_id": "156450040",
                            "execution_plan_id": created["execution_plan_id"],
                            "save_only": False,
                        },
                    )
            self.assertEqual(response.status_code, 400)
            self.assertIn("发布尚未开启", response.get_json()["message"])
            start.assert_not_called()

    def test_run_rejects_action_override(self):
        with tempfile.TemporaryDirectory() as directory:
            report_root = Path(directory) / "reports"
            plan_root = Path(directory) / "plans"
            self.write_report(report_root, preflight(execution_mode="save_draft"))
            with (
                patch.object(panel_server, "PREFLIGHT_REPORT_DIR", str(report_root)),
                patch.object(panel_server, "EXECUTION_PLAN_DIR", str(plan_root)),
            ):
                created = self.client.post(
                    "/api/preflight/execution-plan",
                    json={
                        "shop_id": "156450040", "report_id": "report.json",
                        "action": "save_draft",
                    },
                ).get_json()
                with patch("panel_server.start_task") as start:
                    response = self.client.post(
                        "/api/run",
                        json={
                            "shop_id": "156450040",
                            "execution_plan_id": created["execution_plan_id"],
                            "save_only": False,
                        },
                    )
            self.assertEqual(response.status_code, 400)
            self.assertIn("禁止覆盖计划", response.get_json()["message"])
            start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
