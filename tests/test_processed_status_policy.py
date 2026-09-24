import unittest

from processed_status_policy import (
    DRAFT_TESTED_STATUS,
    PUBLISHED_STATUS,
    should_skip_in_normal_batch,
    success_status,
)


class ProcessedStatusPolicyTests(unittest.TestCase):
    def test_save_only_success_is_not_published(self):
        self.assertEqual(DRAFT_TESTED_STATUS, success_status(True))

    def test_publish_success_is_done(self):
        self.assertEqual(PUBLISHED_STATUS, success_status(False))

    def test_draft_tested_must_not_be_skipped(self):
        self.assertFalse(should_skip_in_normal_batch(DRAFT_TESTED_STATUS))

    def test_published_and_manual_are_skipped(self):
        self.assertTrue(should_skip_in_normal_batch("done"))
        self.assertTrue(should_skip_in_normal_batch("manual_required"))


if __name__ == "__main__":
    unittest.main()
