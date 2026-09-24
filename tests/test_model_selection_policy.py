import unittest

from model_selection_policy import retry_same_keyword_after_modal


class ModelSelectionPolicyTest(unittest.TestCase):
    def test_closed_modal_retries_same_exact_keyword_once(self):
        self.assertTrue(retry_same_keyword_after_modal(
            modal_found=True, modal_closed=True, retry_count=0
        ))
        self.assertFalse(retry_same_keyword_after_modal(
            modal_found=True, modal_closed=True, retry_count=1
        ))

    def test_no_retry_without_confirmed_safe_close(self):
        self.assertFalse(retry_same_keyword_after_modal(
            modal_found=False, modal_closed=False, retry_count=0
        ))
        self.assertFalse(retry_same_keyword_after_modal(
            modal_found=True, modal_closed=False, retry_count=0
        ))
