"""Offline regression for Gaia upload widgets that clear the native file input."""

import copy
import unittest
from unittest.mock import Mock, patch

from integrations.gaia_browser.file_attachment import (
    FileAttachmentError, assess_attachment, wait_for_attachment,
)
from integrations.gaia_browser.submit import submit_gaia_batch
from integrations.gaia_browser.upload_preflight import preflight_gaia_upload


NAME, SIZE = "gaia_upload.xlsx", 1234


def snapshot(name=NAME, state="done", *, inputs=None, title=None):
    return {"entries": [{"names": [{"text": name, "title": name if title is None else title}],
                         "state": state}], "inputs": inputs or []}


class FakeDialog:
    def __init__(self, snapshots):
        self.snapshots = copy.deepcopy(snapshots)
        self.reads = 0
        self.visible = True
        self.shop_matches = 1
        self.quantity = "1"

    def evaluate(self, script, **kwargs):
        self.reads += 1
        if len(self.snapshots) > 1:
            return self.snapshots.pop(0)
        return self.snapshots[0]

    def get_by_text(self, text, **kwargs):
        return Mock(filter=Mock(return_value=Mock(count=Mock(return_value=self.shop_matches))))

    def get_by_placeholder(self, text, **kwargs):
        return Mock(input_value=Mock(return_value=self.quantity))

    def is_visible(self):
        return self.visible


class GaiaFileAttachmentTests(unittest.TestCase):
    def check(self, data, **kwargs):
        return assess_attachment(data, expected_name=NAME, expected_size=SIZE, **kwargs)

    def test_reset_input_is_not_missing_file_when_upload_list_is_ready(self):
        for state in ("done", "selected"):
            with self.subTest(state=state):
                result = self.check(snapshot(state=state))
                self.assertEqual("ready", result["state"])
                self.assertEqual("upload_list", result["source"])

    def test_plain_filename_elsewhere_in_dialog_is_not_attachment_evidence(self):
        result = self.check({"entries": [], "inputs": [], "dialog_text": NAME})
        self.assertEqual("pending", result["state"])

    def test_requires_exact_name_not_substring_or_same_extension(self):
        for name in ("old_" + NAME, NAME + ".bak", "another.xlsx", "", "盖亚模板.xlsx"):
            with self.subTest(name=name):
                self.assertEqual("invalid", self.check(snapshot(name))["state"])

    def test_file_title_cannot_conflict_with_visible_filename(self):
        self.assertEqual("invalid", self.check(snapshot(title="wrong.xlsx"))["state"])
        self.assertEqual("ready", self.check(snapshot(title=""))["state"])

    def test_duplicate_list_entries_are_rejected_even_when_names_match(self):
        data = snapshot()
        data["entries"] *= 2
        self.assertIn("多个文件", self.check(data)["message"])
        self.assertEqual("invalid", self.check(data)["state"])

    def test_error_and_unknown_state_never_become_ready(self):
        for state in ("error", "unknown", None):
            self.assertEqual("invalid", self.check(snapshot(state=state))["state"])

    def test_uploading_is_pending_not_success(self):
        self.assertEqual("pending", self.check(snapshot(state="uploading"))["state"])

    def test_live_input_must_agree_with_list_including_size(self):
        for files in ([{"name": "wrong.xlsx", "size": SIZE}],
                      [{"name": NAME, "size": SIZE + 1}],
                      [{"name": NAME, "size": SIZE}] * 2):
            with self.subTest(files=files):
                self.assertEqual("invalid", self.check(snapshot(inputs=files))["state"])
        self.assertEqual("ready", self.check(snapshot(inputs=[{"name": NAME, "size": SIZE}]))["state"])

    def test_native_only_fallback_is_not_allowed_after_list_was_seen(self):
        data = {"entries": [], "inputs": [{"name": NAME, "size": SIZE}]}
        self.assertEqual("ready", self.check(data)["state"])
        self.assertEqual("pending", self.check(data, require_list=True)["state"])

    def test_missing_expected_file_identity_is_rejected(self):
        result = assess_attachment(snapshot(), expected_name=NAME, expected_size=0)
        self.assertEqual("invalid", result["state"])

    def test_polling_waits_for_upload_complete_and_stable_evidence(self):
        dialog = FakeDialog([snapshot(state="uploading"), snapshot(), snapshot()])
        now = [0.0]
        page = Mock(wait_for_timeout=lambda ms: now.__setitem__(0, now[0] + ms / 1000))
        with patch("integrations.gaia_browser.file_attachment.time.monotonic", side_effect=lambda: now[0]), \
                patch("integrations.gaia_browser.file_attachment._emit") as emit:
            result = wait_for_attachment(page, dialog, expected_name=NAME, expected_size=SIZE,
                                         batch_id="abc", phase="after_attach")
        self.assertEqual("ready", result["state"])
        self.assertEqual(3, dialog.reads)
        self.assertEqual("gaia_file_readback", emit.call_args.args[0])

    def test_upload_timeout_reports_evidence_without_retrying_upload(self):
        dialog = FakeDialog([snapshot(state="uploading")])
        now = [0.0]
        page = Mock(wait_for_timeout=lambda ms: now.__setitem__(0, now[0] + ms / 1000))
        with patch("integrations.gaia_browser.file_attachment.time.monotonic", side_effect=lambda: now[0]), \
                patch("integrations.gaia_browser.file_attachment._emit") as emit:
            with self.assertRaises(FileAttachmentError) as error:
                wait_for_attachment(page, dialog, expected_name=NAME, expected_size=SIZE,
                                    batch_id="abc", phase="before_confirm", timeout_ms=300)
        self.assertIn("仍在上传", str(error.exception))
        self.assertEqual("uploading", error.exception.readback["snapshot"]["entries"][0]["state"])
        self.assertEqual(3, dialog.reads)
        self.assertEqual("gaia_file_readback_blocked", emit.call_args.args[0])

    def test_dom_read_error_is_logged_as_file_evidence_failure(self):
        dialog = Mock(evaluate=Mock(side_effect=TimeoutError("DOM detached")))
        with patch("integrations.gaia_browser.file_attachment._emit") as emit:
            with self.assertRaises(FileAttachmentError) as error:
                wait_for_attachment(Mock(), dialog, expected_name=NAME, expected_size=SIZE,
                                    batch_id="abc", phase="before_confirm")
        self.assertEqual("TimeoutError", error.exception.readback["error_type"])
        self.assertEqual("gaia_file_readback_blocked", emit.call_args.args[0])


class GaiaFinalConfirmTests(unittest.TestCase):
    def run_submission(self, *, data=None, quantity="1", shop_count=1, enabled=True, verify=None,
                       feedback="提交成功", check_cancelled=None):
        dialog = FakeDialog([data or snapshot()])
        dialog.quantity, dialog.shop_matches = quantity, shop_count
        confirm = Mock(is_enabled=Mock(return_value=enabled))
        confirm.click.side_effect = lambda **kwargs: setattr(dialog, "visible", False)
        self.confirm = confirm
        self.before = Mock()
        messages = Mock(all_inner_texts=Mock(side_effect=[[], [feedback]]))
        page = Mock()
        page.locator.return_value.filter.return_value = messages
        result = {"shop_name": "拍拍严选二手手机", "output_name": NAME, "output_size": SIZE,
                  "attachment_readback": {"source": "upload_list"}, "submitted": False}
        self.result = result
        def form(**kwargs):
            return kwargs["on_ready"](page, dialog, confirm, result)
        with patch("integrations.gaia_browser.submit.with_gaia_upload_form", side_effect=form), \
                patch("integrations.gaia_browser.submit._screenshot", return_value="test.png"), \
                patch("integrations.gaia_browser.file_attachment._emit"), \
                patch("integrations.gaia_browser.submit._emit"):
            return submit_gaia_batch(work_dir="unused", batch_id="abc", shop_id="5576543",
                                     profile_dir="unused", artifact_dir="unused",
                                     verify_handoff=verify or Mock(), before_submit=self.before,
                                     check_cancelled=check_cancelled)

    def test_goods_id_toast_is_retained_and_classified_as_rejected_without_retry(self):
        for text in ['没有对应的goodsId', '未找到对应 GoodsID', 'goodsId不存在']:
            result = self.run_submission(feedback=text)
            self.assertEqual('rejected', result['outcome'])
            self.assertEqual(text, result['feedback'])
            self.assertEqual([text], result['observed_feedback'])
            self.confirm.click.assert_called_once()

    def test_cancel_during_attachment_poll_never_records_intent_or_clicks(self):
        with self.assertRaisesRegex(RuntimeError, '运营停止'):
            self.run_submission(check_cancelled=Mock(side_effect=RuntimeError('运营停止')))
        self.before.assert_not_called()
        self.confirm.click.assert_not_called()

    def test_final_check_accepts_list_even_when_input_is_empty_then_clicks_once(self):
        result = self.run_submission()
        self.assertEqual("accepted", result["outcome"])
        self.assertEqual("upload_list", result["final_attachment_readback"]["source"])
        self.before.assert_called_once()
        self.confirm.click.assert_called_once()

    def test_failure_list_blocks_intent_and_click(self):
        with self.assertRaisesRegex(FileAttachmentError, "失败"):
            self.run_submission(data=snapshot(state="error"))
        self.before.assert_not_called()
        self.confirm.click.assert_not_called()
        self.assertFalse(self.result["submitted"])

    def test_replaced_file_blocks_intent_and_click(self):
        with self.assertRaisesRegex(FileAttachmentError, "文件名"):
            self.run_submission(data=snapshot(name="another.xlsx"))
        self.before.assert_not_called()
        self.confirm.click.assert_not_called()

    def test_store_quantity_and_button_guards_are_preserved(self):
        for kwargs in ({"quantity": "2"}, {"shop_count": 0}, {"shop_count": 2}, {"enabled": False}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(RuntimeError):
                    self.run_submission(**kwargs)
                self.before.assert_not_called()
                self.confirm.click.assert_not_called()

    def test_integrity_failure_stops_before_browser_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "哈希"):
            self.run_submission(verify=Mock(side_effect=RuntimeError("输出文件哈希变化")))
        self.before.assert_not_called()
        self.confirm.click.assert_not_called()

    def test_preflight_wrapper_cannot_supply_final_submit_callback(self):
        with patch("integrations.gaia_browser.upload_preflight.with_gaia_upload_form", return_value={"submitted": False}) as form:
            preflight_gaia_upload(work_dir="unused", batch_id="abc", shop_id="5576543",
                                  profile_dir="unused", artifact_dir="unused")
        self.assertNotIn("on_ready", form.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
