import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PanelTaskSyncUiTests(unittest.TestCase):
    def test_release_stage_defaults_to_manual_quantity(self):
        html = (ROOT / "panel.html").read_text(encoding="utf-8")
        self.assertIn('<option value="" selected>按上方填写数量</option>', html)
        self.assertIn("API 素材发布（逐件回读，支持全量批量）", html)

    def test_task_state_has_polling_fallback(self):
        script = (ROOT / "assets" / "panel.js").read_text(encoding="utf-8")
        self.assertIn("async function refreshTaskSnapshot()", script)
        self.assertIn("setInterval(refreshTaskSnapshot, 3000)", script)


if __name__ == "__main__":
    unittest.main()
