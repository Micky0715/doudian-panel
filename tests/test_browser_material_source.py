import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.browser_material_source import (
    BrowserCloudMaterialStager,
    BrowserMaterialSourceError,
)
from app.material_pipeline import CloudImage, MaterialBundle


def cloud_image(token, body, name):
    return CloudImage(token, "v1", name, hashlib.sha256(body).hexdigest(), len(body))


class FakeDrive:
    def __init__(self, bodies):
        self.bodies = dict(bodies)
        self.calls = []

    def download(self, token):
        self.calls.append(token)
        return self.bodies[token]


class FakeCatalog:
    def __init__(self, bundle):
        self.bundle = bundle
        self.calls = []

    def resolve_bundle(self, shop_id, brand, model):
        self.calls.append((shop_id, brand, model))
        return self.bundle


class BrowserCloudMaterialStagerTests(unittest.TestCase):
    def bundle(self, bodies):
        main = cloud_image("main", bodies["main"], "机型主图.jpeg")
        whites = tuple(
            cloud_image(token, bodies[token], "%s.png" % token)
            for token in ("white-1", "white-2", "white-3")
        )
        return MaterialBundle("156450040", "红米", "K90", main, whites)

    def test_cloud_plan_is_staged_in_browser_compatible_layout(self):
        bodies = {
            "main": b"main-image",
            "white-1": b"white-one",
            "white-2": b"white-two",
            "white-3": b"white-three",
        }
        with tempfile.TemporaryDirectory() as directory:
            drive = FakeDrive(bodies)
            catalog = FakeCatalog(self.bundle(bodies))
            result = BrowserCloudMaterialStager(catalog, drive, directory).stage(
                "156450040", "红米", "K90"
            )
            self.assertEqual((result.directory / "主图" / "主图.jpeg").read_bytes(), b"main-image")
            self.assertEqual(
                sorted(path.name for path in (result.directory / "白底图").iterdir()),
                ["白底图_1.png", "白底图_2.png", "白底图_3.png"],
            )
            self.assertEqual(catalog.calls, [("156450040", "红米", "K90")])

    def test_valid_staging_files_are_reused(self):
        bodies = {
            "main": b"main-image",
            "white-1": b"white-one",
            "white-2": b"white-two",
            "white-3": b"white-three",
        }
        with tempfile.TemporaryDirectory() as directory:
            drive = FakeDrive(bodies)
            stager = BrowserCloudMaterialStager(FakeCatalog(self.bundle(bodies)), drive, directory)
            first = stager.stage("156450040", "红米", "K90")
            calls_after_first = list(drive.calls)
            second = stager.stage("156450040", "红米", "K90")
            self.assertEqual(first.directory, second.directory)
            self.assertEqual(drive.calls, calls_after_first)

    def test_changed_download_is_rejected_before_staging(self):
        bodies = {
            "main": b"main-image",
            "white-1": b"white-one",
            "white-2": b"white-two",
            "white-3": b"white-three",
        }
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.bundle(bodies)
            drive = FakeDrive({**bodies, "main": b"tampered"})
            with self.assertRaisesRegex(BrowserMaterialSourceError, "字节数|摘要"):
                BrowserCloudMaterialStager(FakeCatalog(bundle), drive, directory).stage(
                    "156450040", "红米", "K90"
                )

    def test_webp_white_image_keeps_browser_visible_filename(self):
        bodies = {
            "main": b"main-image",
            "white-1": b"white-one",
            "white-2": b"white-two",
        }
        main = cloud_image("main", bodies["main"], "主图.jpg")
        whites = (
            cloud_image("white-1", bodies["white-1"], "银色.webp"),
            cloud_image("white-2", bodies["white-2"], "黑色.png"),
        )
        bundle = MaterialBundle("156450040", "红米", "K90", main, whites)
        with tempfile.TemporaryDirectory() as directory:
            result = BrowserCloudMaterialStager(
                FakeCatalog(bundle), FakeDrive(bodies), directory
            ).stage("156450040", "红米", "K90")
            self.assertTrue((result.directory / "白底图" / "白底图_1.webp").is_file())


class BrowserImageSourceRoutingTests(unittest.TestCase):
    def setUp(self):
        import test_doudian_batch as batch
        self.batch = batch
        batch._BROWSER_IMAGE_DIR_CACHE.clear()
        batch._BROWSER_CLOUD_MATERIAL_STAGER = None

    def test_cloud_is_primary_source(self):
        plan = type("Plan", (), {
            "resolved_model_folder": "K90", "model_folder_resolution": "exact",
            "main_substituted": False, "white_images": (1, 2),
        })()
        selection = type("Selection", (), {
            "directory": Path("/cloud/staged"), "plan": plan,
        })()
        stager = type("Stager", (), {"stage": lambda self, *args: selection})()
        with (
            patch.dict(self.batch.os.environ, {"DP_BROWSER_IMAGE_SOURCE": "feishu_local_fallback"}),
            patch.object(self.batch, "CURRENT_SHOP_ID", "156450040"),
            patch.object(self.batch, "_get_browser_cloud_stager", return_value=stager),
            patch.object(self.batch, "_get_local_image_dir") as local,
        ):
            result = self.batch.get_image_dir(
                {"brand_cn": "红米", "brand_normalized": "Redmi/红米", "model": "K90"}
            )
        self.assertEqual(result, "/cloud/staged")
        local.assert_not_called()

    def test_cloud_failure_uses_local_fallback_once(self):
        stager = type("Stager", (), {
            "stage": lambda self, *args: (_ for _ in ()).throw(RuntimeError("ambiguous")),
        })()
        parsed = {"brand_cn": "红米", "brand_normalized": "Redmi/红米", "model": "K90"}
        with (
            patch.dict(self.batch.os.environ, {"DP_BROWSER_IMAGE_SOURCE": "feishu_local_fallback"}),
            patch.object(self.batch, "CURRENT_SHOP_ID", "156450040"),
            patch.object(self.batch, "_get_browser_cloud_stager", return_value=stager),
            patch.object(self.batch, "_get_local_image_dir", return_value="/local/K90") as local,
        ):
            self.assertEqual(self.batch.get_image_dir(parsed), "/local/K90")
            self.assertEqual(self.batch.get_image_dir(parsed), "/local/K90")
        local.assert_called_once_with(parsed)

    def test_browser_aux_reader_accepts_staged_webp(self):
        with tempfile.TemporaryDirectory() as directory:
            main = Path(directory) / "主图"
            main.mkdir()
            (main / "主图.jpg").write_bytes(b"main")
            aux = Path(directory) / "白底图"
            aux.mkdir()
            expected = aux / "白底图_1.webp"
            expected.write_bytes(b"webp")
            with patch.object(self.batch, "get_image_dir", return_value=directory):
                self.assertEqual(self.batch.get_aux_image_paths({}), [str(expected)])


if __name__ == "__main__":
    unittest.main()
