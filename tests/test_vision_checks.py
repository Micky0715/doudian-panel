import os
import tempfile
import unittest
from unittest.mock import patch

from vision_checks import vision_check


class _Response:
    def read(self):
        return b'{"choices":[{"message":{"content":"{\\"verdict\\":\\"yes\\",\\"reason\\":\\"visible\\"}"}}]}'


class VisionChecksTest(unittest.TestCase):
    def test_unconfigured_is_unknown(self):
        with patch.dict(os.environ, {}, clear=True):
            result = vision_check("unused.png", "question")
        self.assertFalse(result.available)
        self.assertEqual("unknown", result.verdict)

    def test_structured_yes(self):
        with tempfile.NamedTemporaryFile(suffix=".png") as image:
            image.write(b"png")
            image.flush()
            env = {"DP_VISION_ENDPOINT": "https://vision.invalid", "DP_VISION_API_KEY": "x",
                   "DP_VISION_MODEL": "vision"}
            with patch.dict(os.environ, env, clear=True):
                result = vision_check(image.name, "question", opener=lambda *a, **k: _Response())
        self.assertTrue(result.available)
        self.assertEqual("yes", result.verdict)
