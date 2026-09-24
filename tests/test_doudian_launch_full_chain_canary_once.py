import unittest

from scripts.doudian_launch_full_chain_canary_once import (
    _is_online,
    _launch_state_issue,
)


class FullChainLaunchStateTests(unittest.TestCase):
    def test_status_zero_and_check_three_is_online(self):
        self.assertTrue(_is_online({"status": 0, "check_status": 3}))

    def test_approved_offline_product_can_launch(self):
        self.assertIsNone(_launch_state_issue({"status": 1, "check_status": 3}))

    def test_draft_cannot_launch(self):
        self.assertEqual(
            _launch_state_issue({"status": 0, "check_status": 1}),
            "product_not_approved_and_offline",
        )


if __name__ == "__main__":
    unittest.main()
