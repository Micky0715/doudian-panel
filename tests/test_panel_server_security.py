import unittest
from unittest.mock import patch

try:
    import flask  # noqa: F401
    import panel_server
except ImportError:
    panel_server = None


@unittest.skipIf(panel_server is None, "Flask is not installed in this test interpreter")
class PanelServerInputValidationTests(unittest.TestCase):
    def setUp(self):
        panel_server.app.config.update(TESTING=True)
        self.client = panel_server.app.test_client()

    @patch("panel_server.start_task")
    def test_run_rejects_unknown_shop_before_starting_worker(self, start_task):
        response = self.client.post("/api/run", json={"shop_id": "../../tmp/owned"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])
        start_task.assert_not_called()

    def test_clear_logs_rejects_unknown_shop(self):
        response = self.client.post("/api/clear-logs", json={"shop_id": "../../tmp/owned"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_model_audit_rejects_unknown_shop(self):
        response = self.client.post(
            "/api/model-audit", json={"shop_id": "../../tmp/owned", "product_id": "123"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_autostart_handles_empty_and_invalid_json(self):
        response = self.client.post("/api/autostart", json={"action": "invalid"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_config_status_exposes_only_readiness_flags(self):
        response = self.client.get("/api/config-status")
        self.assertEqual(response.status_code, 200)
        value = response.get_json()
        self.assertIn("feishu_material_ready", value)
        self.assertEqual(value["browser_image_source"], "feishu_local_fallback")
        self.assertEqual(value["api_media_source"], "feishu")
        self.assertIn(value["api_image_migration_mode"], (
            "api_media_publish_with_readback", "api_media_draft_with_readback",
            "browser_fallback_only",
        ))
        self.assertIn("api_media_configured_shop_count", value)
        self.assertNotIn("FEISHU_APP_SECRET", value)


if __name__ == "__main__":
    unittest.main()
