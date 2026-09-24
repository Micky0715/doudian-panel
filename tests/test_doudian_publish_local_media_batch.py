import unittest

from doudian_api import DoudianAPIError
from scripts.doudian_publish_local_media_batch import (
    _can_start_with_safe_candidates,
    _save_draft_with_rate_limit_retry,
)


class PartialSafeBatchPolicyTests(unittest.TestCase):
    def test_bad_candidates_do_not_block_available_safe_candidates(self):
        self.assertTrue(_can_start_with_safe_candidates(1))
        self.assertTrue(_can_start_with_safe_candidates(8))

    def test_empty_safe_batch_is_blocked(self):
        self.assertFalse(_can_start_with_safe_candidates(0))


class EditRateLimitRetryTests(unittest.TestCase):
    def test_retries_only_explicit_request_limited_response(self):
        class Writer:
            def __init__(self):
                self.calls = 0

            def save_images_to_draft(self, _payload):
                self.calls += 1
                if self.calls == 1:
                    raise DoudianAPIError("product.editV2", {
                        "code": 60000, "sub_code": "isv.request-limited", "msg": "too fast",
                    })
                return {"code": 10000}

        waits = []
        notices = []
        response, attempt_count = _save_draft_with_rate_limit_retry(
            Writer(), {"commit": False}, attempts=3, backoff_seconds=2,
            sleep=waits.append, on_rate_limited=lambda *values: notices.append(values),
        )
        self.assertEqual(response["code"], 10000)
        self.assertEqual(attempt_count, 2)
        self.assertEqual(waits, [2])
        self.assertEqual(notices[0][0:2], (1, 2))

    def test_does_not_retry_unknown_api_error(self):
        class Writer:
            def save_images_to_draft(self, _payload):
                raise DoudianAPIError("product.editV2", {
                    "code": 50000, "sub_code": "isv.unknown", "msg": "unknown",
                })

        with self.assertRaisesRegex(DoudianAPIError, "isv.unknown"):
            _save_draft_with_rate_limit_retry(
                Writer(), {"commit": False}, attempts=3, backoff_seconds=1, sleep=lambda _: None,
            )


if __name__ == "__main__":
    unittest.main()
