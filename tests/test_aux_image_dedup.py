import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_doudian_batch as dd


class AuxImageDedupTests(unittest.TestCase):
    def test_identical_files_with_different_names_count_once(self):
        with tempfile.TemporaryDirectory() as folder:
            aux = Path(folder) / "白底图"
            aux.mkdir()
            main = Path(folder) / "主图"
            main.mkdir()
            (main / "主图.jpg").write_bytes(b"real-main")
            (aux / "白底图_1.jpg").write_bytes(b"same-image")
            (aux / "白底图_2.jpg").write_bytes(b"same-image")
            (aux / "白底图_3.jpg").write_bytes(b"different-image")
            with patch.object(dd, "get_image_dir", return_value=folder):
                paths = dd.get_aux_image_paths({})
        self.assertEqual(["白底图_1.jpg", "白底图_3.jpg"],
                         [Path(path).name for path in paths])


if __name__ == "__main__":
    unittest.main()
