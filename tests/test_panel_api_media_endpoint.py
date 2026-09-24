import unittest
from unittest.mock import patch

import panel_server


class FakeTask:
    def brief(self):
        return {"id": "task", "state": "starting", "engine": "api_media"}


class PanelApiMediaEndpointTests(unittest.TestCase):
    def setUp(self):
        panel_server.app.config.update(TESTING=True)
        self.client = panel_server.app.test_client()

    def test_api_media_starts_only_with_explicit_shop_folder(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"208501840":"123"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "208501840", "engine": "api_media", "mode": "test",
                "test_n": 99, "release_stage": "", "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        kwargs = start.call_args.kwargs
        args = start.call_args.args
        self.assertEqual(kwargs["engine"], "api_media")
        self.assertEqual(kwargs["media_folder_id"], "123")
        self.assertEqual(args[2], 99)
        self.assertTrue(kwargs["save_only"])

    def test_shop_root_folder_zero_is_explicit_valid_binding(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"156450040":"0"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "156450040", "engine": "api_media", "mode": "test",
                "test_n": 1, "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.kwargs["media_folder_id"], "0")
        self.assertEqual(start.call_args.args[2], 1)

    def test_blank_release_stage_preserves_one_item_test(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"156450040":"0"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "156450040", "engine": "api_media", "mode": "test",
                "test_n": 1, "release_stage": "", "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[2], 1)

    def test_api_media_rejects_unknown_folder_without_starting_worker(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{}',
            }, clear=False),
            patch("panel_server.start_task") as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "208501840", "engine": "api_media", "mode": "test",
                "test_n": 1, "save_only": True,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("素材中心文件夹", response.get_json()["message"])
        start.assert_not_called()

    def test_api_media_batch_mode_starts_full_snapshot_worker(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"208501840":"123"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "208501840", "engine": "api_media", "mode": "batch",
                "test_n": 1, "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[1], "batch")
        self.assertEqual(start.call_args.args[2], 999)

    def test_api_media_accepts_twenty_item_release_stage(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"208501840":"123"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "208501840", "engine": "api_media", "mode": "test",
                "release_stage": "pilot", "release_stage_explicit": True,
                "test_n": 1, "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[2], 20)

    def test_cached_default_smoke_cannot_override_one_item(self):
        with (
            patch.dict(panel_server.os.environ, {
                "DP_DOUDIAN_MATERIAL_FOLDER_IDS_JSON": '{"156450040":"0"}',
            }, clear=False),
            patch("panel_server.start_task", return_value=(FakeTask(), "started")) as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "156450040", "engine": "api_media", "mode": "test",
                "test_n": 1, "release_stage": "smoke", "save_only": True,
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.args[2], 1)

    def test_api_media_publish_is_gated_until_full_flow_is_verified(self):
        with (
            patch.dict(panel_server.os.environ, {"DP_API_MEDIA_PUBLISH_ENABLED": "0"}),
            patch("panel_server.start_task") as start,
        ):
            response = self.client.post("/api/run", json={
                "shop_id": "156450040", "engine": "api_media", "mode": "test",
                "test_n": 1, "save_only": False,
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未放开", response.get_json()["message"])
        start.assert_not_called()

    def test_api_media_task_selects_non_browser_worker(self):
        with (
            patch.object(panel_server, "TASKS", {}),
            patch("panel_server.subprocess.Popen") as popen,
            patch("panel_server.threading.Thread") as thread,
            patch("panel_server.os.makedirs") as makedirs,
        ):
            task, message = panel_server.start_task(
                "208501840", "test", 1, save_only=True,
                engine="api_media", media_folder_id="123",
            )
        self.assertIsNotNone(task, message)
        command = popen.call_args.args[0]
        self.assertEqual(command[-1], panel_server.API_MEDIA_RUNNER)
        self.assertNotIn("panel_runner.py", command)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["DP_MEDIA_ACTION"], "save_draft")
        self.assertEqual(child_env["DP_MEDIA_FOLDER_ID"], "123")
        self.assertEqual(child_env["DP_MEDIA_SOURCE"], "feishu")
        self.assertEqual(child_env["DP_PREFLIGHT_WORKERS"], "2")
        makedirs.assert_not_called()
        thread.return_value.start.assert_called_once()

    def test_browser_task_receives_cloud_first_image_source(self):
        with (
            patch.object(panel_server, "TASKS", {}),
            patch("panel_server.subprocess.Popen") as popen,
            patch("panel_server.threading.Thread") as thread,
            patch("panel_server.seed_processed_log"),
            patch("panel_server.os.makedirs"),
            patch.dict(panel_server.os.environ, {}, clear=False),
        ):
            panel_server.os.environ.pop("DP_BROWSER_IMAGE_SOURCE", None)
            task, message = panel_server.start_task(
                "156450040", "test", 1, save_only=True, engine="browser",
            )
        self.assertIsNotNone(task, message)
        child_env = popen.call_args.kwargs["env"]
        self.assertEqual(child_env["DP_BROWSER_IMAGE_SOURCE"], "feishu_local_fallback")
        self.assertTrue(child_env["DP_BROWSER_CLOUD_CACHE_ROOT"].endswith(
            "panel_data/browser_cloud_material_cache"
        ))
        thread.return_value.start.assert_called_once()
