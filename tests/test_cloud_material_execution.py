import hashlib
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.material_pipeline import (
    CloudImage,
    CloudMaterialExecutor,
    MaterialBundle,
    MaterialPolicyError,
    build_material_plan,
)
from app.material_pipeline.cache import MaterialMetadataCache
from app.material_pipeline.feishu_catalog import (
    FeishuMaterialCatalog,
    FeishuMaterialLayout,
    IndexedDriveFile,
)
from integrations.doudian.materials.gateway import UploadedMaterial
from integrations.feishu_drive.client import RemoteDriveEntry, RemoteDriveFile


class FakeDrive:
    def __init__(self, blobs):
        self.blobs = blobs

    def download(self, token):
        return self.blobs[token]


def folder(token, name):
    return RemoteDriveEntry(token, name, "folder", "folder", "v1", 0)


def remote_file(token, name, size):
    return RemoteDriveEntry(token, name, "file", "image/png", "v1", size)


class FakeTreeDrive(FakeDrive):
    def __init__(self, blobs, tree):
        super().__init__(blobs)
        self.config = SimpleNamespace(material_root_folder_token="root")
        self.tree = tree
        self.listed_tokens = []

    def list_entries(self, token):
        self.listed_tokens.append(token)
        return iter(self.tree.get(token, ()))


class FakeMaterials:
    def __init__(self):
        self.calls = []
        self.urls = {}

    def upload_image(self, image_bytes, name, *, folder_id=None):
        self.calls.append((image_bytes, name, folder_id))
        material = UploadedMaterial(
            "m-" + name, "https://byte.example/" + name,
            hashlib.sha256(image_bytes).hexdigest(), len(image_bytes),
        )
        self.urls[material.material_id] = material.byte_url
        return material

    def get_material_byte_url(self, material_id):
        return self.urls.get(material_id, "")


class FakeWriter:
    def __init__(self):
        self.payloads = []

    def save_images_to_draft(self, payload):
        self.payloads.append(payload)
        return {"code": 10000}


def cloud(token, revision, data, name):
    return CloudImage(token, revision, name, hashlib.sha256(data).hexdigest(), len(data))


def square_png(seed: bytes) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 600, 600) + seed


class CloudMaterialExecutionTests(unittest.TestCase):
    def test_upload_order_is_main_then_white_and_second_run_is_cached(self):
        blobs = {"main": b"main", "w1": b"white1", "w2": b"white2", "w3": b"white3"}
        plan = build_material_plan(MaterialBundle(
            "156450040", "vivo", "X200", cloud("main", "v1", blobs["main"], "主图.jpg"),
            tuple(cloud(token, "v1", blobs[token], f"白底图_{index}.jpg") for index, token in enumerate(("w1", "w2", "w3"), 1)),
        ))
        with tempfile.TemporaryDirectory() as root:
            materials = FakeMaterials()
            executor = CloudMaterialExecutor(
                FakeDrive(blobs), materials, MaterialMetadataCache(Path(root) / "cache"),
                shop_id="156450040", folder_id="123",
            )
            resolved = executor.resolve(plan)
            self.assertEqual(resolved.pic.split("|")[0], "https://byte.example/主图.jpg")
            self.assertEqual(len(materials.calls), 4)
            self.assertTrue(all(call[2] == "123" for call in materials.calls))
            executor.resolve(plan)
            self.assertEqual(len(materials.calls), 4)
            resolved_again = executor.resolve_images(plan.spec_image_candidates)
            self.assertEqual(len(resolved_again), 3)
            self.assertEqual(len(materials.calls), 4)
            writer = FakeWriter()
            executor.save_draft_images(writer, "90001", '{"1493":[]}', resolved)
            self.assertFalse(writer.payloads[0]["commit"])
            self.assertEqual(writer.payloads[0]["pic"].split("|")[0], resolved.main.byte_url)

    def test_mutated_cloud_file_refuses_product_write(self):
        blobs = {"main": b"new"}
        item = cloud("main", "v1", b"old", "主图.jpg")
        with tempfile.TemporaryDirectory() as root:
            executor = CloudMaterialExecutor(
                FakeDrive(blobs), FakeMaterials(), MaterialMetadataCache(Path(root) / "cache"),
                shop_id="156450040", folder_id="123",
            )
            with self.assertRaisesRegex(ValueError, "changed after material plan"):
                executor._resolve_image(item)

    def test_catalog_uses_target_paths_and_records_omitted_extra_white_images(self):
        blobs = {"m": square_png(b"m")}
        blobs.update({f"w{index}": square_png(str(index).encode()) for index in range(1, 6)})
        entries = (
            IndexedDriveFile(("抖音主图", "156450040", "vivo", "X200", "主图.jpg"), RemoteDriveFile("m", "主图.jpg", "image/jpeg", "v1", len(blobs["m"]))),
            *(IndexedDriveFile(("白底图库", "手机", "vivo", "X200", f"白底图_{index}.jpg"), RemoteDriveFile(f"w{index}", f"白底图_{index}.jpg", "image/jpeg", "v1", len(blobs[f"w{index}"])) ) for index in range(1, 6)),
        )
        catalog = FeishuMaterialCatalog(FakeDrive(blobs))
        bundle = catalog.resolve_bundle("156450040", "vivo", "X200", entries)
        self.assertEqual(bundle.main.file_token, "m")
        self.assertEqual(len(bundle.white_images), 4)
        self.assertEqual(len(bundle.spec_image_candidates), 5)
        self.assertEqual(bundle.omitted_white_images[0].name, "白底图_5.jpg")

    def test_catalog_accepts_operator_colour_names_without_legacy_prefix(self):
        blobs = {
            "m": square_png(b"m"),
            "sky": square_png(b"sky"),
            "moon": square_png(b"moon"),
        }
        entries = (
            IndexedDriveFile(
                ("抖音主图", "156450040", "vivo", "X200", "主图.jpg"),
                RemoteDriveFile("m", "主图.jpg", "image/jpeg", "v1", len(blobs["m"])),
            ),
            IndexedDriveFile(
                ("白底图库", "手机", "vivo", "X200", "海阔天空.jpg"),
                RemoteDriveFile("sky", "海阔天空.jpg", "image/jpeg", "v1", len(blobs["sky"])),
            ),
            IndexedDriveFile(
                ("白底图库", "手机", "vivo", "X200", "大漠银月.jpeg"),
                RemoteDriveFile("moon", "大漠银月.jpeg", "image/jpeg", "v1", len(blobs["moon"])),
            ),
        )
        bundle = FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle(
            "156450040", "vivo", "X200", entries,
        )
        self.assertEqual(
            [item.name for item in bundle.white_images],
            ["大漠银月.jpeg", "海阔天空.jpg"],
        )

    def test_catalog_allows_white_fallback_only_when_four_valid_images_remain(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 5)}
        entries = tuple(
            IndexedDriveFile(("白底图库", "手机", "vivo", "X200", f"白底图_{index}.jpg"), RemoteDriveFile(f"w{index}", f"白底图_{index}.jpg", "image/jpeg", "v1", len(blobs[f"w{index}"])))
            for index in range(1, 5)
        )
        bundle = FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle("156450040", "vivo", "X200", entries)
        plan = build_material_plan(bundle)
        self.assertTrue(plan.main_substituted)
        self.assertEqual(plan.main.file_token, "w1")
        self.assertEqual(len(plan.white_images), 3)

    def test_catalog_reads_the_live_named_white_subfolder_without_scanning_neighbours(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 6)}
        tree = {
            "root": (folder("white-root", "白底图库"),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("apple", "苹果"),),
            "apple": (
                folder("iphone15", "苹果 iPhone 15"),
                folder("iphone15pro", "苹果 iPhone 15 Pro"),
            ),
            "iphone15": (folder("iphone15-whites", "白底图"),),
            "iphone15-whites": tuple(
                remote_file(f"w{index}", f"白底图_{index}.png", len(blobs[f"w{index}"]))
                for index in range(1, 6)
            ),
            "iphone15pro": (folder("should-not-read", "白底图"),),
        }
        drive = FakeTreeDrive(blobs, tree)
        bundle = FeishuMaterialCatalog(drive).resolve_bundle("156450040", "苹果", "iPhone 15")
        plan = build_material_plan(bundle)

        self.assertTrue(plan.main_substituted)
        self.assertEqual(plan.main.file_token, "w1")
        self.assertEqual([item.file_token for item in plan.white_images], ["w2", "w3", "w4"])
        self.assertEqual(bundle.omitted_white_images[0].name, "白底图_5.png")
        self.assertNotIn("iphone15pro", drive.listed_tokens)

    def test_live_main_lookup_descends_through_unmatched_wrapper_folders(self):
        blobs = {
            "main": square_png(b"main"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("operations", "运营维护"),
                folder("white-root", "白底图库"),
            ),
            "operations": (folder("main-root", "抖音主图"),),
            "main-root": (folder("shops", "店铺"),),
            "shops": (folder("shop", "156450040"),),
            "shop": (folder("phones", "二手手机"),),
            "phones": (folder("vivo", "vivo"),),
            "vivo": (folder("model", "X200"),),
            "model": (folder("main-images", "主图"),),
            "main-images": (remote_file("main", "X200.png", len(blobs["main"])),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-vivo", "vivo"),),
            "white-vivo": (folder("white-model", "X200"),),
            "white-model": (folder("white-images", "白底图"),),
            "white-images": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })
        drive = FakeTreeDrive(blobs, tree)

        bundle = FeishuMaterialCatalog(drive, layout=layout).resolve_bundle(
            "156450040", "vivo", "X200",
        )

        self.assertEqual(bundle.main.file_token, "main")
        self.assertEqual([item.file_token for item in bundle.white_images], ["w1", "w2"])

    def test_live_main_lookup_matches_direct_model_file_with_case_and_space_variants(self):
        blobs = {
            "main": square_png(b"main"),
            "other": square_png(b"other"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", " 抖音　主图 "),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", " 156450040 "),),
            "shop": (folder("phones", "手机"),),
            "phones": (folder("apple", "Apple"),),
            "apple": (
                remote_file("main", "IPHONE 16 E.JPG", len(blobs["main"])),
                remote_file("other", "iPhone 16 Pro.jpg", len(blobs["other"])),
            ),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-apple", "苹果"),),
            "white-apple": (folder("white-model", "iPhone 16e"),),
            "white-model": (
                remote_file("w1", "黑色.png", len(blobs["w1"])),
                remote_file("w2", "白色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("156450040", "苹果", "iPhone 16e")

        self.assertEqual(bundle.main.file_token, "main")
        self.assertEqual(bundle.main.name, "IPHONE 16 E.JPG")
        self.assertEqual([item.file_token for item in bundle.white_images], ["w2", "w1"])

    def test_live_direct_main_lookup_stays_inside_the_matching_brand(self):
        blobs = {
            "apple": square_png(b"apple"),
            "vivo": square_png(b"vivo"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "手机"),),
            "phones": (folder("apple-brand", "Apple"), folder("vivo-brand", "vivo")),
            "apple-brand": (remote_file("apple", "X200.jpg", len(blobs["apple"])),),
            "vivo-brand": (remote_file("vivo", "X200.jpg", len(blobs["vivo"])),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-vivo", "vivo"),),
            "white-vivo": (folder("white-model", "X200"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("156450040", "vivo", "X200")

        self.assertEqual(bundle.main.file_token, "vivo")

    def test_live_direct_main_lookup_rejects_other_explicit_brand_prefixes(self):
        blobs = {
            "oneplus": square_png(b"oneplus"),
            "iqoo": square_png(b"iqoo"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
            "w3": square_png(b"w3"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "7970406"),),
            # This reproduces the live flat shop_model branch: explicit files
            # from other brands must not become Xiaomi 15 candidates.
            "shop": (
                remote_file("oneplus", "一加 15.jpg", len(blobs["oneplus"])),
                remote_file("iqoo", "iQOO 15.jpg", len(blobs["iqoo"])),
            ),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("xiaomi", "小米"),),
            "xiaomi": (folder("white-model", "小米 15"),),
            "white-model": (
                remote_file("w1", "黑色.png", len(blobs["w1"])),
                remote_file("w2", "白色.png", len(blobs["w2"])),
                remote_file("w3", "蓝色.png", len(blobs["w3"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        plan = build_material_plan(FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("7970406", "小米", "15"))

        self.assertTrue(plan.main_substituted)
        self.assertIn(plan.main.file_token, {"w1", "w2", "w3"})
        self.assertNotIn(plan.main.file_token, {"oneplus", "iqoo"})

    def test_live_direct_main_lookup_selects_only_matching_explicit_brand(self):
        blobs = {
            "xiaomi": square_png(b"xiaomi"),
            "oneplus": square_png(b"oneplus"),
            "iqoo": square_png(b"iqoo"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "7970406"),),
            "shop": (
                remote_file("xiaomi", "小米 15.jpg", len(blobs["xiaomi"])),
                remote_file("oneplus", "一加 15.jpg", len(blobs["oneplus"])),
                remote_file("iqoo", "iQOO 15.jpg", len(blobs["iqoo"])),
            ),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-xiaomi", "小米"),),
            "white-xiaomi": (folder("white-model", "小米 15"),),
            "white-model": (
                remote_file("w1", "黑色.png", len(blobs["w1"])),
                remote_file("w2", "白色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("7970406", "小米", "15")

        self.assertEqual(bundle.main.file_token, "xiaomi")

    def test_live_main_lookup_searches_duplicate_brand_branches_for_one_model_hit(self):
        blobs = {
            "main": square_png(b"main"),
            "other": square_png(b"other"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "二手手机"),),
            "phones": (
                folder("vivo-a", "vivo"),
                folder("vivo-b", "VIVO"),
            ),
            "vivo-a": (remote_file("main", "X200.jpg", len(blobs["main"])),),
            "vivo-b": (remote_file("other", "X100.jpg", len(blobs["other"])),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-vivo", "vivo"),),
            "white-vivo": (folder("white-model", "X200"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("156450040", "vivo", "X200")

        self.assertEqual(bundle.main.file_token, "main")

    def test_live_main_lookup_rejects_same_model_in_duplicate_brand_branches(self):
        blobs = {
            "main-a": square_png(b"main-a"),
            "main-b": square_png(b"main-b"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "二手手机"),),
            "phones": (
                folder("vivo-a", "vivo"),
                folder("vivo-b", "VIVO"),
            ),
            "vivo-a": (remote_file("main-a", "X200.jpg", len(blobs["main-a"])),),
            "vivo-b": (remote_file("main-b", "vivo X200.jpg", len(blobs["main-b"])),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-vivo", "vivo"),),
            "white-vivo": (folder("white-model", "X200"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        with self.assertRaises(MaterialPolicyError):
            FeishuMaterialCatalog(
                FakeTreeDrive(blobs, tree), layout=layout,
            ).resolve_bundle("156450040", "vivo", "X200")

    def test_live_main_lookup_prefers_configured_redmi_folder_over_xiaomi_sibling(self):
        blobs = {
            "redmi": square_png(b"redmi"),
            "xiaomi": square_png(b"xiaomi"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "手机"),),
            "phones": (
                folder("redmi-brand", "Redmi"),
                folder("xiaomi-brand", "小米"),
            ),
            "redmi-brand": (
                remote_file("redmi", "REDMI Turbo 4.jpg", len(blobs["redmi"])),
            ),
            "xiaomi-brand": (
                remote_file("xiaomi", "REDMI Turbo 4.jpg", len(blobs["xiaomi"])),
            ),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-redmi", "Redmi"),),
            "white-redmi": (folder("white-model", "Redmi Turbo 4"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
            "FEISHU_BRAND_FOLDER_MAP_JSON": '{"红米":"Redmi"}',
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("156450040", "红米", "Turbo 4")

        self.assertEqual(bundle.main.file_token, "redmi")
        self.assertEqual([item.file_token for item in bundle.white_images], ["w1", "w2"])

    def test_live_direct_main_lookup_prefers_literal_spaced_model_name(self):
        blobs = {
            "spaced": square_png(b"spaced"),
            "compact": square_png(b"compact"),
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "手机"),),
            "phones": (folder("oppo", "OPPO"),),
            "oppo": (
                remote_file("spaced", "OPPO Find X7.jpg", len(blobs["spaced"])),
                remote_file("compact", "OPPOFind X7.jpg", len(blobs["compact"])),
            ),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-oppo", "OPPO"),),
            "white-oppo": (folder("white-model", "OPPO Find X7"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        bundle = FeishuMaterialCatalog(
            FakeTreeDrive(blobs, tree), layout=layout,
        ).resolve_bundle("156450040", "OPPO", "Find X7")

        self.assertEqual(bundle.main.file_token, "spaced")

    def test_live_direct_main_lookup_rejects_duplicate_literal_files(self):
        blobs = {
            "a": square_png(b"a"),
            "b": square_png(b"b"),
        }
        tree = {
            "root": (folder("main-root", "抖音主图"),),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("phones", "手机"),),
            "phones": (folder("apple", "Apple"),),
            "apple": (
                remote_file("a", "iPhone 16e.jpg", len(blobs["a"])),
                remote_file("b", "IPHONE 16e.png", len(blobs["b"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        with self.assertRaisesRegex(ValueError, "多个原始型号文字相同"):
            FeishuMaterialCatalog(
                FakeTreeDrive(blobs, tree), layout=layout,
            ).resolve_bundle("156450040", "苹果", "iPhone 16e")

    def test_recursive_main_lookup_rejects_two_nearest_model_matches(self):
        blobs = {
            "w1": square_png(b"w1"),
            "w2": square_png(b"w2"),
        }
        tree = {
            "root": (
                folder("main-root", "抖音主图"),
                folder("white-root", "白底图库"),
            ),
            "main-root": (folder("shop", "156450040"),),
            "shop": (folder("a", "A类"), folder("b", "B类")),
            "a": (folder("model-a", "X200"),),
            "b": (folder("model-b", "X200"),),
            "white-root": (folder("phone", "手机"),),
            "phone": (folder("white-vivo", "vivo"),),
            "white-vivo": (folder("white-model", "X200"),),
            "white-model": (
                remote_file("w1", "蓝色.png", len(blobs["w1"])),
                remote_file("w2", "黑色.png", len(blobs["w2"])),
            ),
        }
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
        })

        with self.assertRaisesRegex(ValueError, "多个原始型号文字相同"):
            FeishuMaterialCatalog(
                FakeTreeDrive(blobs, tree), layout=layout,
            ).resolve_bundle("156450040", "vivo", "X200")

    def test_explicit_shop_model_layout_does_not_guess_brand_path(self):
        layout = FeishuMaterialLayout.from_mapping({"FEISHU_MAIN_PATH_LAYOUT": "shop_model"})
        catalog = FeishuMaterialCatalog(FakeDrive({}), layout=layout)
        self.assertEqual(catalog._main_prefix("156450040", "vivo"), ("抖音主图", "156450040"))

    def test_live_apple_brand_folder_remains_chinese_by_default(self):
        layout = FeishuMaterialLayout.from_mapping({})
        self.assertEqual(layout.brand_folder("苹果"), "苹果")
        self.assertEqual(
            FeishuMaterialCatalog(FakeDrive({}), layout=layout)._white_prefix("苹果"),
            ("白底图库", "手机", "苹果"),
        )

    def test_catalog_matches_one_complete_normalised_model_folder_not_a_prefix(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 5)}
        entries = tuple(
            IndexedDriveFile(("白底图库", "手机", "苹果", "苹果 iPhone 15", f"白底图_{index}.jpg"), RemoteDriveFile(f"w{index}", f"白底图_{index}.jpg", "image/jpeg", "v1", len(blobs[f"w{index}"])))
            for index in range(1, 5)
        ) + (
            IndexedDriveFile(("白底图库", "手机", "苹果", "苹果 iPhone 15 Pro", "白底图_1.jpg"), RemoteDriveFile("other", "白底图_1.jpg", "image/jpeg", "v1", len(square_png(b"other")))),
        )
        blobs["other"] = square_png(b"other")
        bundle = FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle("156450040", "苹果", "iPhone 15", entries)
        self.assertIsNone(bundle.main)
        self.assertEqual([item.file_token for item in bundle.white_images], ["w1", "w2", "w3", "w4"])

    def test_cloud_folder_removes_trailing_edition_only_after_exact_miss(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 4)}
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "三星", "三星 Galaxy A56", f"颜色{index}.jpg"),
                RemoteDriveFile(f"w{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"w{index}"])),
            )
            for index in range(1, 4)
        )
        plan = build_material_plan(FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle(
            "156450040", "三星", "Galaxy A56 (5G版)", entries,
        ))
        self.assertEqual(plan.model_folder_resolution, "version_suffix_removed")
        self.assertEqual(plan.resolved_model_folder, "三星 Galaxy A56")

    def test_cloud_folder_keeps_supreme_edition_fallback_after_platform_normalisation(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 4)}
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "Redmi", "Redmi K70", f"颜色{index}.jpg"),
                RemoteDriveFile(f"w{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"w{index}"])),
            )
            for index in range(1, 4)
        )
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
            "FEISHU_BRAND_FOLDER_MAP_JSON": '{"红米":"Redmi"}',
        })
        plan = build_material_plan(FeishuMaterialCatalog(
            FakeDrive(blobs), layout=layout,
        ).resolve_bundle("156450040", "红米", "K70至尊", entries))
        self.assertEqual(plan.model_folder_resolution, "version_suffix_removed")
        self.assertEqual(plan.resolved_model_folder, "Redmi K70")

    def test_cloud_folder_reuses_reviewed_local_model_alias(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 4)}
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "Redmi", "Redmi Note 14", f"颜色{index}.jpg"),
                RemoteDriveFile(f"w{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"w{index}"])),
            )
            for index in range(1, 4)
        )
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
            "FEISHU_BRAND_FOLDER_MAP_JSON": '{"红米":"Redmi"}',
        })
        plan = build_material_plan(FeishuMaterialCatalog(
            FakeDrive(blobs), layout=layout,
        ).resolve_bundle("156450040", "红米", "Note 14 5G", entries))
        self.assertEqual(plan.model_folder_resolution, "rule_alias")
        self.assertEqual(plan.resolved_model_folder, "Redmi Note 14")

    def test_cloud_folder_alias_does_not_cross_xiaomi_redmi_identity(self):
        blobs = {f"w{index}": square_png(str(index).encode()) for index in range(1, 4)}
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "Redmi", "小米 Note 14", f"颜色{index}.jpg"),
                RemoteDriveFile(f"w{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"w{index}"])),
            )
            for index in range(1, 4)
        )
        layout = FeishuMaterialLayout.from_mapping({
            "FEISHU_MAIN_PATH_LAYOUT": "shop_model",
            "FEISHU_BRAND_FOLDER_MAP_JSON": '{"红米":"Redmi"}',
        })
        with self.assertRaisesRegex(ValueError, "at least two white-background images"):
            build_material_plan(FeishuMaterialCatalog(
                FakeDrive(blobs), layout=layout,
            ).resolve_bundle("156450040", "红米", "Note 14 5G", entries))

    def test_cloud_folder_keeps_exact_edition_ahead_of_base_fallback(self):
        blobs = {
            **{f"full{index}": square_png(f"full{index}".encode()) for index in range(1, 4)},
            **{f"base{index}": square_png(f"base{index}".encode()) for index in range(1, 4)},
        }
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "三星", folder_name, f"颜色{index}.jpg"),
                RemoteDriveFile(f"{prefix}{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"{prefix}{index}"])),
            )
            for folder_name, prefix in (
                ("三星 Galaxy A56 (5G版)", "full"), ("三星 Galaxy A56", "base"),
            )
            for index in range(1, 4)
        )
        plan = build_material_plan(FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle(
            "156450040", "三星", "Galaxy A56 (5G版)", entries,
        ))
        self.assertEqual(plan.model_folder_resolution, "exact")
        self.assertEqual(plan.resolved_model_folder, "三星 Galaxy A56 (5G版)")
        self.assertTrue(plan.main.file_token.startswith("full"))

    def test_literal_spelling_precedes_normalised_official_fallback(self):
        blobs = {
            **{f"good{index}": square_png(f"good{index}".encode()) for index in range(1, 4)},
            **{f"bad{index}": square_png(f"bad{index}".encode()) for index in range(1, 4)},
        }
        entries = tuple(
            IndexedDriveFile(
                ("白底图库", "手机", "荣耀", folder_name, f"颜色{index}.jpg"),
                RemoteDriveFile(f"{prefix}{index}", f"颜色{index}.jpg", "image/jpeg", "v1",
                                len(blobs[f"{prefix}{index}"])),
            )
            for folder_name, prefix in (
                ("荣耀 Magic V3", "good"), ("荣耀 Magicv3", "bad"),
            )
            for index in range(1, 4)
        )
        plan = build_material_plan(FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle(
            "156450040", "荣耀", "Magic V3", entries,
        ))
        self.assertEqual(plan.model_folder_resolution, "exact")
        self.assertEqual(plan.resolved_model_folder, "荣耀 Magic V3")
        self.assertEqual(plan.official_model_name, "")
        self.assertEqual(plan.official_model_source_url, "")
        self.assertTrue(plan.main.file_token.startswith("good"))

    def test_unreviewed_duplicate_normalised_folders_still_block(self):
        blobs = {"a": square_png(b"a"), "b": square_png(b"b")}
        entries = (
            IndexedDriveFile(
                ("白底图库", "手机", "荣耀", "荣耀 Magic V4", "黑色.jpg"),
                RemoteDriveFile("a", "黑色.jpg", "image/jpeg", "v1", len(blobs["a"])),
            ),
            IndexedDriveFile(
                ("白底图库", "手机", "荣耀", "Honor Magic V4", "白色.jpg"),
                RemoteDriveFile("b", "白色.jpg", "image/jpeg", "v1", len(blobs["b"])),
            ),
        )
        with self.assertRaisesRegex(ValueError, "多个原始型号文字相同"):
            FeishuMaterialCatalog(FakeDrive(blobs)).resolve_bundle(
                "156450040", "荣耀", "Magic V4", entries,
            )


if __name__ == "__main__":
    unittest.main()
