import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operator_error_notifier import (
    LarkCliSender,
    build_task_error_message,
    load_notification_config,
    notify_task_errors,
    should_notify,
)


class FakeSender:
    def __init__(self):
        self.calls = []

    def send_text(self, text, *, idempotency_key):
        self.calls.append((text, idempotency_key))
        return {"ok": True, "data": {"message_id": "om_test"}}


class OperatorErrorNotifierTests(unittest.TestCase):
    def setUp(self):
        self.config = load_notification_config()
        self.task = {
            "id": "abcd1234", "shop_id": "7970406",
            "shop_name": "爱回收严选二手数码", "engine": "api_media",
            "save_only": False, "state": "done",
            "progress": {"success": 3, "failed": 1, "skipped": 0},
        }
        self.errors = [{
            "product_id": "384", "name": "小米 15",
            "error_type": "型号未匹配", "reason": "平台型号未唯一命中",
        }]

    def test_builds_shop_specific_mention_and_error_summary(self):
        text = build_task_error_message(self.task, self.errors, self.config)
        self.assertIn("爱回收严选二手数码（7970406）", text)
        self.assertIn('<at user_id="ou_badc799d878fc1e3a55a8b0b197a91cc">金仲伟</at>', text)
        self.assertIn("小米 15：平台型号未唯一命中", text)
        self.assertNotIn("API｜发布", text)

    def test_material_error_is_short_actionable_chinese(self):
        errors = [{
            "name": "三星 W24 Flip 二手手机", "product_id": "384",
            "error_type": "素材预检未通过",
            "reason": "app.material_pipeline.domain.MaterialPolicyError: at least two white-background images are required",
        }]
        text = build_task_error_message(self.task, errors, self.config)
        self.assertIn("三星 W24 Flip 二手手机：白底图不足 2 张，请补图后重试", text)
        self.assertNotIn("MaterialPolicyError", text)
        self.assertNotIn("white-background", text)
        self.assertNotIn("任务：", text)

    def test_no_error_success_task_is_not_notified(self):
        self.assertFalse(should_notify(self.task, []))
        sender = FakeSender()
        result = notify_task_errors(self.task, [], sender=sender)
        self.assertEqual({"sent": False, "reason": "no_operational_error"}, result)
        self.assertEqual([], sender.calls)

    def test_runtime_error_without_structured_items_is_not_silent(self):
        task = dict(self.task, state="error", message="进程退出码 2")
        sender = FakeSender()
        result = notify_task_errors(task, [], sender=sender)
        self.assertTrue(result["sent"])
        self.assertIn("进程退出码 2", sender.calls[0][0])

    def test_untrusted_error_cannot_inject_an_extra_mention(self):
        errors = [dict(self.errors[0], reason='<at user_id="all"></at>')]
        text = build_task_error_message(self.task, errors, self.config)
        self.assertNotIn('<at user_id="all"></at>', text)

    @patch("operator_error_notifier.subprocess.run")
    def test_cli_sender_uses_argv_without_shell_and_idempotency_key(self, run):
        run.return_value.returncode = 0
        run.return_value.stdout = json.dumps({
            "ok": True, "data": {"message_id": "om_test"},
        })
        run.return_value.stderr = ""
        sender = LarkCliSender(self.config)
        sender.send_text("hello", idempotency_key="ops-error-abcd1234")
        command = run.call_args.args[0]
        self.assertEqual("im", command[1])
        self.assertEqual("+messages-send", command[2])
        self.assertEqual("bot", command[command.index("--as") + 1])
        self.assertIn("oc_dc46a3d0b48fb403c7323de01f795c49", command)
        self.assertIn("ops-error-abcd1234", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_invalid_chat_id_is_rejected(self):
        bad = dict(self.config, chat_id="wrong")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_notification_config(path)


if __name__ == "__main__":
    unittest.main()
