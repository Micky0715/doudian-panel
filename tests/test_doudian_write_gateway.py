import json
import tempfile
import unittest
from pathlib import Path

from integrations.doudian.write_gateway import (
    DoudianSingleProductFullMediaDraftWriter, DoudianSingleProductGradeDetailDraftWriter,
    DoudianSingleProductImageDraftWriter, DoudianSingleProductLauncher,
    DoudianSingleProductPublisher,
    DoudianSingleProductSpecImageDraftWriter, DoudianSingleProductWriter,
    DoudianWriteBoundaryError, submission_payload_sha256,
)


class FakeClient:
    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        return {"code": 10000, "msg": "success"}


class WriteGatewayTests(unittest.TestCase):
    def make_env(self, root):
        path = Path(root) / "env.local"
        path.write_text(
            "DOUDIAN_APP_KEY=k\nDOUDIAN_APP_SECRET=s\nDOUDIAN_APP_TYPE=self_use\n"
            'DOUDIAN_SHOP_TOKENS_JSON={"10001":"token"}\n'
            "DOUDIAN_SHOP_REFRESH_TOKENS_JSON={}\n",
            encoding="utf-8",
        )
        return path

    def test_only_locked_product_and_model_payload_are_allowed(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductWriter(
                self.make_env(root), "10001", "90001",
                client_factory=lambda *_: client,
            )
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "commit": False,
            }
            writer.edit_model_properties(payload)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.edit_model_properties({**payload, "name": "unexpected"})
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.edit_model_properties({**payload, "product_id": "90002"})
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.edit_model_properties({**payload, "commit": True})

    def test_submission_requires_an_immutable_commit_true_plan(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "commit": True,
            }
            publisher = DoudianSingleProductPublisher(
                self.make_env(root), "10001", "90001", submission_payload_sha256(payload),
                client_factory=lambda *_: client,
            )
            publisher.submit_planned_draft(payload)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            with self.assertRaises(DoudianWriteBoundaryError):
                publisher.submit_planned_draft({**payload, "commit": False})
            with self.assertRaises(DoudianWriteBoundaryError):
                publisher.submit_planned_draft({**payload, "product_format_new": "{}"})

    def test_submission_requires_matching_full_name_and_exact_spaced_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "name": "99新Xiaomi/小米 测试手机",
                "name_prefix": "99新Xiaomi/小米 ", "commit": True,
            }
            publisher = DoudianSingleProductPublisher(
                self.make_env(root), "10001", "90001", submission_payload_sha256(payload),
                client_factory=lambda *_: client,
            )
            publisher.submit_planned_draft(payload)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            for changed in (
                {**payload, "name_prefix": ""},
                {**payload, "name_prefix": "99新Xiaomi/小米"},
                {**payload, "name": "测试手机"},
            ):
                with self.assertRaises(DoudianWriteBoundaryError):
                    publisher.submit_planned_draft(changed)

    def test_submission_can_preserve_one_material_centre_white_url(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "name": "99新Xiaomi/小米 测试手机",
                "name_prefix": "99新Xiaomi/小米 ",
                "white_back_ground_pic_url": (
                    "https://byte.example/obj/ecom-shop-material/white.jpg"
                ),
                "commit": True,
            }
            publisher = DoudianSingleProductPublisher(
                self.make_env(root), "10001", "90001", submission_payload_sha256(payload),
                client_factory=lambda *_: client,
            )
            publisher.submit_planned_draft(payload)
            self.assertEqual(client.calls, [("product.editV2", payload)])

    def test_launcher_only_allows_the_locked_product_id(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            launcher = DoudianSingleProductLauncher(
                self.make_env(root), "10001", "90001",
                client_factory=lambda *_: client,
            )
            launcher.launch({"product_id": "90001"})
            self.assertEqual(
                client.calls, [("product.launchProduct", {"product_id": "90001"})]
            )
            for changed in (
                {"product_id": "90002"},
                {"product_id": "90001", "commit": True},
                {},
            ):
                with self.assertRaises(DoudianWriteBoundaryError):
                    launcher.launch(changed)
            self.assertEqual(len(client.calls), 1)

    def test_image_writer_can_only_save_platform_urls_to_draft(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductImageDraftWriter(
                self.make_env(root), "10001", "90001", client_factory=lambda *_: client,
            )
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "pic": "https://byte.example/main.jpg|https://byte.example/white-1.jpg",
                "commit": False,
            }
            writer.save_images_to_draft(payload)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            media_payload = {
                **payload,
                "description": "https://byte.example/detail-1.jpg|https://byte.example/detail-2.jpg",
            }
            writer.save_images_to_draft(media_payload)
            self.assertEqual(client.calls[-1], ("product.editV2", media_payload))
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.save_images_to_draft({**payload, "commit": True})
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.save_images_to_draft({**payload, "pic": "/local/main.jpg"})
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.save_images_to_draft({**payload, "description": "not-a-platform-url"})

    def test_spec_image_writer_only_allows_exact_color_count_and_draft(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductSpecImageDraftWriter(
                self.make_env(root), "10001", "90001", client_factory=lambda *_: client,
            )
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "spec_pic": "https://byte.example/green.png,https://byte.example/black.png",
                "commit": False,
            }
            writer.save_spec_images_to_draft(payload, color_count=2)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            prefixed = {**payload, "name_prefix": "99新Xiaomi/小米"}
            writer.save_spec_images_to_draft(prefixed, color_count=2)
            self.assertEqual(client.calls[-1], ("product.editV2", prefixed))
            for changed, count in (
                ({**payload, "commit": True}, 2),
                ({**payload, "product_id": "90002"}, 2),
                ({**payload, "spec_pic": "/local/green.png,https://byte.example/black.png"}, 2),
                ({**payload, "spec_pic": payload["spec_pic"] + ",https://byte.example/white.png"}, 2),
                ({**payload, "spec_pics": []}, 2),
                ({**payload, "product_format_new": "{}"}, 2),
                ({**payload, "name_prefix": ""}, 2),
            ):
                with self.assertRaises(DoudianWriteBoundaryError):
                    writer.save_spec_images_to_draft(changed, color_count=count)
            self.assertEqual(len(client.calls), 2)

    def test_grade_detail_writer_preserves_prefix_and_sku_urls(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductGradeDetailDraftWriter(
                self.make_env(root), "10001", "90001", client_factory=lambda *_: client,
            )
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "description": "|".join(f"https://byte.example/detail-{n}.jpg" for n in range(15)),
                "spec_pic": "https://byte.example/green.jpg,https://byte.example/black.jpg",
                "name_prefix": "99新Xiaomi/小米", "commit": False,
            }
            writer.save_grade_details_to_draft(payload, detail_count=15, color_count=2)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            for changed in (
                {**payload, "commit": True}, {**payload, "name_prefix": ""},
                {**payload, "spec_pic": ""}, {**payload, "description": "https://byte.example/one.jpg"},
                {**payload, "pic": "https://byte.example/other.jpg"},
            ):
                with self.assertRaises(DoudianWriteBoundaryError):
                    writer.save_grade_details_to_draft(changed, detail_count=15, color_count=2)
            self.assertEqual(len(client.calls), 1)

    def test_full_media_writer_requires_all_media_fields_and_cannot_publish(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductFullMediaDraftWriter(
                self.make_env(root), "10001", "90001", client_factory=lambda *_: client,
            )
            payload = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "standard_brand_id": 663234468,
                "pic": "|".join(f"https://byte.example/main-{n}.jpg" for n in range(4)),
                "description": "|".join(f"https://byte.example/detail-{n}.jpg" for n in range(14)),
                "spec_pic": "https://byte.example/black.jpg,https://byte.example/white.jpg",
                "name_prefix": "9新OPPO", "commit": False,
            }
            writer.save_full_media_to_draft(payload, main_count=4, detail_count=14, color_count=2)
            self.assertEqual(client.calls, [("product.editV2", payload)])
            for changed in (
                {**payload, "commit": True},
                {key: value for key, value in payload.items() if key != "spec_pic"},
                {**payload, "spec_prices": []},
                {**payload, "name_prefix": ""},
                {**payload, "pic": "/tmp/main.jpg"},
            ):
                with self.assertRaises(DoudianWriteBoundaryError):
                    writer.save_full_media_to_draft(changed, main_count=4, detail_count=14, color_count=2)
            self.assertEqual(len(client.calls), 1)

    def test_full_media_writer_accepts_omitted_spec_field_but_rejects_partial_slots(self):
        with tempfile.TemporaryDirectory() as root:
            client = FakeClient()
            writer = DoudianSingleProductFullMediaDraftWriter(
                self.make_env(root), "10001", "90001", client_factory=lambda *_: client,
            )
            base = {
                "product_id": "90001", "product_format_new": json.dumps({"1493": []}),
                "standard_brand_id": 1,
                "pic": "https://byte.example/main.jpg|https://byte.example/w1.jpg|https://byte.example/w2.jpg",
                "description": "|".join(
                    f"https://byte.example/detail-{n}.jpg" for n in range(14)
                ),
                "name_prefix": "9新OPPO", "commit": False,
            }
            partial = {**base, "spec_pic": "https://byte.example/black.jpg,,https://byte.example/blue.jpg"}
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.save_full_media_to_draft(
                    partial, main_count=3, detail_count=14, color_count=3,
                )
            writer.save_full_media_to_draft(
                base, main_count=3, detail_count=14, color_count=0,
            )
            self.assertEqual(client.calls, [("product.editV2", base)])

            with_white = {
                **base,
                "white_back_ground_pic_url": (
                    "https://byte.example/obj/ecom-shop-material/white.jpg"
                ),
            }
            writer.save_full_media_to_draft(
                with_white, main_count=3, detail_count=14, color_count=0,
            )
            self.assertEqual(client.calls[-1], ("product.editV2", with_white))
            with self.assertRaises(DoudianWriteBoundaryError):
                writer.save_full_media_to_draft(
                    {**with_white, "white_back_ground_pic_url": "https://external.example/white.jpg"},
                    main_count=3, detail_count=14, color_count=0,
                )


if __name__ == "__main__":
    unittest.main()
