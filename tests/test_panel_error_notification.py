import unittest
from unittest.mock import patch

import panel_server


class ImmediateThread:
    def __init__(self, target, args=(), daemon=None):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


class PanelErrorNotificationTests(unittest.TestCase):
    def test_terminal_task_with_errors_is_scheduled_only_once(self):
        task = panel_server.Task("7970406", "test", 1, 0, engine="browser")
        task.state = "done"
        task.errors = [{"error_type": "型号未匹配", "reason": "test"}]
        with (
            patch("panel_server.threading.Thread", ImmediateThread),
            patch("panel_server.notify_task_errors", return_value={"sent": True}) as notify,
            patch("panel_server.push"),
        ):
            self.assertTrue(panel_server.schedule_task_error_notification(task))
            self.assertFalse(panel_server.schedule_task_error_notification(task))
        notify.assert_called_once()

    def test_success_task_without_errors_does_not_start_notification(self):
        task = panel_server.Task("7970406", "test", 1, 0, engine="browser")
        task.state = "done"
        with patch("panel_server.threading.Thread") as thread:
            self.assertFalse(panel_server.schedule_task_error_notification(task))
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
