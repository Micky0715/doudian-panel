import json
import os
import tempfile
import unittest
from pathlib import Path

from doudian_api import DoudianAPIError, DoudianConfigError, read_env_file
from integrations.doudian import (
    DoudianProductReader,
    DoudianReadOnlyGateway,
    EnvTokenStore,
    ReadOnlyBoundaryError,
)


def write_env(path: Path, access="old-access", refresh="old-refresh", *,
              app_type="tool", metadata=None):
    meta = json.dumps(metadata or {}, separators=(",", ":"))
    path.write_text(
        "# keep this comment\n"
        "UNRELATED=value with spaces\n"
        "DOUDIAN_APP_KEY=app-key\n"
        "DOUDIAN_APP_SECRET=app-secret\n"
        f"DOUDIAN_APP_TYPE={app_type}\n"
        f'DOUDIAN_SHOP_TOKENS_JSON={{"10001":"{access}","20002":"other-access"}}\n'
        f'DOUDIAN_SHOP_REFRESH_TOKENS_JSON={{"10001":"{refresh}","20002":"other-refresh"}}\n'
        f"DOUDIAN_SHOP_TOKEN_META_JSON={meta}\n",
        encoding="utf-8",
    )


class TokenStoreTests(unittest.TestCase):
    def test_atomic_update_preserves_unrelated_content_and_other_shop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path)
            EnvTokenStore(path).update_shop_tokens("10001", "next-access", "next-refresh")
            text = path.read_text(encoding="utf-8")
            values = read_env_file(path)
            self.assertIn("# keep this comment", text)
            self.assertEqual(values["UNRELATED"], "value with spaces")
            self.assertEqual(json.loads(values["DOUDIAN_SHOP_TOKENS_JSON"]), {
                "10001": "next-access", "20002": "other-access",
            })
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_self_use_recreate_rejects_wrong_shop_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, app_type="self_use")
            before = path.read_bytes()
            with self.assertRaises(DoudianConfigError):
                EnvTokenStore(path).recreate_self_use_shop(
                    "10001", "old-access", lambda *_: {
                        "shop_id": "20002", "access_token": "wrong", "refresh_token": "wrong-r",
                    },
                )
            self.assertEqual(path.read_bytes(), before)

    def test_self_use_recreate_records_expiry_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, app_type="self_use")
            store = EnvTokenStore(path)
            config, recreated = store.recreate_self_use_shop(
                "10001", "old-access", lambda *_: {
                    "shop_id": "10001", "shop_name": "测试店铺",
                    "access_token": "new-access", "refresh_token": "new-refresh",
                    "expires_in": 604800,
                },
            )
            self.assertTrue(recreated)
            self.assertEqual(config.shop_tokens["10001"], "new-access")
            meta = config.shop_token_meta["10001"]
            self.assertEqual(meta["expires_in"], 604800)
            self.assertEqual(meta["access_expires_at"] - meta["issued_at"], 604800)
            self.assertEqual(meta["refresh_expires_in"], 14 * 24 * 60 * 60)

    def test_refresh_is_skipped_when_another_worker_already_rotated_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, access="already-new")
            called = []
            config, refreshed = EnvTokenStore(path).refresh_shop(
                "10001", "stale-access", lambda *_: called.append(True),
            )
            self.assertFalse(refreshed)
            self.assertFalse(called)
            self.assertEqual(config.shop_tokens["10001"], "already-new")


class FakeClient:
    def __init__(self, config, shop_id, calls, expire_old=True):
        self.token = config.shop_tokens[shop_id]
        self.calls = calls
        self.expire_old = expire_old

    def call(self, method, params):
        self.calls.append((self.token, method, params))
        if self.expire_old and self.token == "old-access":
            raise DoudianAPIError(method, {
                "code": 40003,
                "sub_code": "isv.access-token-expired",
                "sub_msg": "access token expired",
            })
        return {"code": 10000, "data": {"data": [], "total": 0}}


class GatewayTests(unittest.TestCase):
    def test_category_property_method_is_allowed_as_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path)
            calls = []
            gateway = DoudianReadOnlyGateway(
                path,
                "10001",
                client_factory=lambda config, shop: FakeClient(
                    config, shop, calls, expire_old=False,
                ),
            )
            for method, params in (
                ("product.getCatePropertyV2", {"category_leaf_id": 1000007628}),
                ("product.getProductUpdateRule", {"category_id": 1000007628}),
                ("product.getRecommendName", {"category_leaf_id": 1000007628}),
                ("cpaypromotion.queryProductCpayPromotionAndShopStatus", {}),
            ):
                gateway.call(method, params)
            self.assertEqual([row[1] for row in calls], [
                "product.getCatePropertyV2", "product.getProductUpdateRule",
                "product.getRecommendName",
                "cpaypromotion.queryProductCpayPromotionAndShopStatus",
            ])

    def test_expired_token_refreshes_current_shop_and_retries_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path)
            calls = []
            refresh_args = []

            def refresh(app_key, app_secret, refresh_token):
                refresh_args.append((app_key, app_secret, refresh_token))
                return {"access_token": "next-access", "refresh_token": "next-refresh"}

            gateway = DoudianReadOnlyGateway(
                path,
                "10001",
                client_factory=lambda config, shop: FakeClient(config, shop, calls),
                refresh_fn=refresh,
            )
            response = gateway.call("product.listV2", {"page": 1, "size": 1})
            self.assertEqual(response["code"], 10000)
            self.assertEqual([item[0] for item in calls], ["old-access", "next-access"])
            self.assertEqual(len(refresh_args), 1)
            values = read_env_file(path)
            self.assertEqual(json.loads(values["DOUDIAN_SHOP_TOKENS_JSON"])["10001"], "next-access")
            self.assertEqual(json.loads(values["DOUDIAN_SHOP_TOKENS_JSON"])["20002"], "other-access")

    def test_non_read_method_is_physically_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path)
            gateway = DoudianReadOnlyGateway(
                path,
                "10001",
                client_factory=lambda config, shop: FakeClient(config, shop, [], expire_old=False),
            )
            for method in ("product.addV2", "product.editV2", "product.launchProduct"):
                with self.subTest(method=method), self.assertRaises(ReadOnlyBoundaryError):
                    gateway.call(method, {})

    def test_non_expiration_api_error_is_not_refreshed(self):
        class InvalidClient:
            def call(self, method, params):
                raise DoudianAPIError(method, {
                    "code": 40003, "sub_code": "isv.param-error", "sub_msg": "bad param",
                })

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path)
            refreshed = []
            gateway = DoudianReadOnlyGateway(
                path, "10001", client_factory=lambda *_: InvalidClient(),
                refresh_fn=lambda *_: refreshed.append(True),
            )
            with self.assertRaises(DoudianAPIError):
                gateway.call("product.listV2", {"page": 1, "size": 1})
            self.assertFalse(refreshed)

    def test_self_use_expired_token_is_recreated_without_refresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, app_type="self_use")
            calls = []
            creates = []

            def create(app_key, app_secret, shop_id):
                creates.append((app_key, app_secret, shop_id))
                return {
                    "shop_id": shop_id, "shop_name": "测试店铺", "expires_in": 604800,
                    "access_token": "next-access", "refresh_token": "next-refresh",
                }

            gateway = DoudianReadOnlyGateway(
                path, "10001",
                client_factory=lambda config, shop: FakeClient(config, shop, calls),
                create_self_use_fn=create,
                refresh_fn=lambda *_: self.fail("self-use applications must not call token.refresh"),
            )
            response = gateway.call("product.listV2", {"page": 1, "size": 1})
            self.assertEqual(response["code"], 10000)
            self.assertEqual([item[0] for item in calls], ["old-access", "next-access"])
            self.assertEqual(len(creates), 1)

    def test_self_use_renews_inside_one_hour_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, app_type="self_use", metadata={
                "10001": {"access_expires_at": 10000},
            })
            calls = []
            gateway = DoudianReadOnlyGateway(
                path, "10001", clock=lambda: 7000,
                client_factory=lambda config, shop: FakeClient(
                    config, shop, calls, expire_old=False,
                ),
                create_self_use_fn=lambda app_key, app_secret, shop: {
                    "shop_id": shop, "access_token": "next-access",
                    "refresh_token": "next-refresh", "expires_in": 604800,
                },
            )
            gateway.call("product.listV2", {"page": 1, "size": 1})
            self.assertEqual(calls[0][0], "next-access")

    def test_self_use_does_not_renew_more_than_one_hour_early(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "env.local"
            write_env(path, app_type="self_use", metadata={
                "10001": {"access_expires_at": 10000},
            })
            calls = []
            creates = []
            gateway = DoudianReadOnlyGateway(
                path, "10001", clock=lambda: 6399,
                client_factory=lambda config, shop: FakeClient(
                    config, shop, calls, expire_old=False,
                ),
                create_self_use_fn=lambda *_: creates.append(True),
            )
            gateway.call("product.listV2", {"page": 1, "size": 1})
            self.assertEqual(calls[0][0], "old-access")
            self.assertFalse(creates)


class ProductReaderTests(unittest.TestCase):
    class Gateway:
        def __init__(self):
            self.calls = []
        def call(self, method, params):
            self.calls.append((method, params))
            return {"code": 10000, "data": {"ok": True}}

    def test_product_reads_use_documented_contracts(self):
        gateway = self.Gateway()
        reader = DoudianProductReader(gateway)
        self.assertTrue(reader.list_products(page=2, size=10, status=0)["ok"])
        self.assertTrue(reader.get_product_detail("3838000000000000000")["ok"])
        self.assertTrue(reader.get_sku_detail(sku_id="12345")["ok"])
        self.assertEqual(gateway.calls[1], (
            "product.detail",
            {"product_id": "3838000000000000000", "show_draft": "true"},
        ))

    def test_invalid_or_ambiguous_inputs_are_rejected(self):
        reader = DoudianProductReader(self.Gateway())
        with self.assertRaises(DoudianConfigError):
            reader.list_products(unknown=True)
        with self.assertRaises(DoudianConfigError):
            reader.get_product_detail("not-an-id")
        with self.assertRaises(DoudianConfigError):
            reader.get_sku_detail(sku_id="1", code="two")


if __name__ == "__main__":
    unittest.main()
