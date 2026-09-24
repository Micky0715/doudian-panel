"""Browser editor must not save or publish with an unverified spec switch."""

import unittest
from unittest.mock import patch

import test_doudian_batch as dd


class SpecSwitchGuardTests(unittest.TestCase):
    def test_unknown_aria_is_not_clicked(self):
        class FakePage:
            def evaluate(self, *_):
                return {"found": True, "state": "unknown", "aria": None,
                        "className": "", "method": "test", "x": 0, "y": 0,
                        "w": 20, "h": 20}

            @property
            def mouse(self):
                raise AssertionError("未知开关不得点击")

        self.assertFalse(dd.close_spec_switch(FakePage()))

    def test_already_closed_never_clicks(self):
        with patch.object(dd, "_probe_spec_switch_state", return_value={
            "state": "关", "className": "", "method": "test",
        }), patch.object(dd, "close_spec_switch") as close, patch.object(dd.time, "sleep"):
            self.assertTrue(dd.ensure_spec_switch_closed(object(), "测试"))
        close.assert_not_called()

    def test_open_is_closed_and_rechecked(self):
        states = iter([
            {"state": "开", "className": "", "method": "test"},
            {"state": "关", "className": "", "method": "test"},
            {"state": "关", "className": "", "method": "test"},
        ])
        with patch.object(dd, "_probe_spec_switch_state", side_effect=lambda _: next(states)), \
             patch.object(dd, "close_spec_switch", return_value=True) as close, \
             patch.object(dd.time, "sleep"):
            self.assertTrue(dd.ensure_spec_switch_closed(object(), "测试"))
        close.assert_called_once()

    def test_missing_switch_fails_closed_without_click(self):
        with patch.object(dd, "_probe_spec_switch_state", return_value={
            "state": "unknown", "className": "", "method": "none",
        }), patch.object(dd, "close_spec_switch") as close, patch.object(dd.time, "sleep"):
            self.assertFalse(dd.ensure_spec_switch_closed(object(), "测试"))
        close.assert_not_called()

    def test_draft_save_is_blocked_before_button_when_switch_unknown(self):
        class FakePage:
            url = "https://fxg.jinritemai.com/ffa/g/create?product_id=1"

            def evaluate(self, *_):
                raise AssertionError("保存按钮不应被点击")

        with patch.object(dd, "_dismiss_blocking_overlay_before_save"), \
             patch.object(dd, "ensure_spec_switch_closed", return_value=False):
            self.assertFalse(dd.save_draft(FakePage()))
        self.assertIn("规格信息开关", dd.get_last_publish_error())


if __name__ == "__main__":
    unittest.main()
