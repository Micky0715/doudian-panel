import unittest
from collections import deque
from unittest.mock import patch

import panel_server


class PanelTaskQueueTests(unittest.TestCase):
    SHOP_IDS = ["129675602", "636781", "7970406", "51165770", "25595926"]

    def _running_tasks(self):
        tasks = {}
        for slot, shop_id in enumerate(self.SHOP_IDS[:4]):
            task = panel_server.Task(shop_id, "test", 1, slot, engine="browser")
            task.state = "running"
            tasks[task.id] = task
        return tasks

    def test_fifth_shop_waits_and_is_dispatched_fifo_when_slot_frees(self):
        active = self._running_tasks()
        task_queue = deque()
        with (
            patch.object(panel_server, "TASKS", active),
            patch.object(panel_server, "TASK_QUEUE", task_queue),
            patch("panel_server.subprocess.Popen") as popen,
            patch("panel_server.threading.Thread") as thread,
            patch("panel_server.seed_processed_log"),
            patch("panel_server.os.makedirs"),
        ):
            queued, message = panel_server.start_task(
                self.SHOP_IDS[4], "test", 1, engine="browser"
            )
            self.assertIsNotNone(queued)
            self.assertEqual("queued", queued.state)
            self.assertEqual(1, queued.queue_position)
            self.assertIn("等待队列", message)
            popen.assert_not_called()

            first = next(iter(active.values()))
            first.state = "done"
            panel_server.dispatch_queued_tasks()

            self.assertEqual("starting", queued.state)
            self.assertEqual(0, queued.slot)
            self.assertIsNotNone(queued.started_at)
            popen.assert_called_once()
            thread.return_value.start.assert_called_once()
            self.assertEqual([], list(task_queue))

    def test_queued_task_can_be_cancelled_without_starting_worker(self):
        active = self._running_tasks()
        queued = panel_server.Task(self.SHOP_IDS[4], "batch", 999, None, engine="browser")
        queued.queue_position = 1
        active[queued.id] = queued
        task_queue = deque([queued.id])
        with (
            patch.object(panel_server, "TASKS", active),
            patch.object(panel_server, "TASK_QUEUE", task_queue),
        ):
            panel_server.app.config.update(TESTING=True)
            response = panel_server.app.test_client().post(
                "/api/stop", json={"task_id": queued.id}
            )
            self.assertEqual(200, response.status_code)
            self.assertEqual("stopped", queued.state)
            self.assertTrue(queued.stop_requested)
            self.assertEqual([], list(task_queue))
            self.assertIsNone(queued.proc)


if __name__ == "__main__":
    unittest.main()
