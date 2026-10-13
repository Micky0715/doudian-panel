import unittest
from datetime import datetime
from html import escape
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app.gaia_listing.application.price_uplift import PriceUpliftError
from integrations.gaia_browser.channel_reconcile import (
    _matching_search_response, _select_exact_shop_filter,
    _wait_for_fresh_page_rows, _wait_for_search_rows,
    reconcile_channel_products,
)
from integrations.gaia_browser.upload_form import GAIA_SHOP_NAMES


EXPECTED = [{'brand': 'Apple', 'model': '苹果 iPhone 17 Pro'}]
KEY = 'apple苹果iphone17pro'
PRODUCT = {
    'product_id': '3847298731712053572', 'shop_id': '168976032',
    'scene': '直播货架', 'title': '苹果 iPhone 17 Pro 二手手机',
    'strategy': 'D且E且G', 'created_at': '2026-10-09 17:41:47',
    'status': '待上架', 'quality_dimension': '二维成色',
}


class ChannelReconcileTests(unittest.TestCase):
    def check(self, before, after):
        return reconcile_channel_products(
            {KEY: {'items': before}}, {KEY: {'items': after}},
            shop_id='168976032', expected_products=EXPECTED,
            submitted_at=datetime(2026, 10, 9, 17, 41, 43),
            observed_at=datetime(2026, 10, 9, 17, 41, 50),
        )

    def test_nine_sku_rows_can_link_one_new_product(self):
        self.assertEqual([PRODUCT['product_id']], self.check([], [PRODUCT]))

    def test_old_product_is_not_reused(self):
        with self.assertRaisesRegex(PriceUpliftError, '不能唯一确认'):
            self.check([PRODUCT], [PRODUCT])

    def test_similar_model_does_not_match(self):
        with self.assertRaisesRegex(PriceUpliftError, '不能唯一确认'):
            self.check([], [dict(PRODUCT, title='苹果 iPhone 17 Pro Max 二手手机')])

    def test_ambiguous_concurrent_creation_stops(self):
        with self.assertRaisesRegex(PriceUpliftError, '2 个'):
            self.check([], [PRODUCT, dict(PRODUCT, product_id='3847298731712053573')])

    def test_cross_shop_or_scene_stops(self):
        for change in ({'shop_id': '7970406'}, {'scene': '直播间'}):
            with self.subTest(change=change), self.assertRaisesRegex(PriceUpliftError, '不能唯一确认'):
                self.check([], [dict(PRODUCT, **change)])


class ChannelShopFilterTests(unittest.TestCase):
    def test_live_verified_channel_aliases(self):
        self.assertEqual('拍拍二手手机旗舰店', GAIA_SHOP_NAMES['25595926'])
        self.assertEqual('芯动严选二手手机', GAIA_SHOP_NAMES['221724193'])
        self.assertEqual('拍机堂Mall线上店', GAIA_SHOP_NAMES['221729127'])

    @staticmethod
    def fixture(shop_id, shop_name, *, option_id=None):
        actual_id = option_id or shop_id
        label = escape(shop_name, quote=True)
        return f'''<div class="ant-select ant-select-multiple">
          <div class="ant-select-selector"><input id="advanced_search_shopIds"></div>
        </div>
        <div class="ant-select-dropdown" style="display:none">
          <div role="option" aria-label="{label}">{actual_id}</div>
          <div class="ant-select-item-option" title="{label}">{label}</div>
        </div>
        <script>(() => {{
          const select = document.querySelector('.ant-select');
          const dropdown = document.querySelector('.ant-select-dropdown');
          const input = document.querySelector('input');
          select.querySelector('.ant-select-selector').onclick = () => dropdown.style.display = 'block';
          input.oninput = () => dropdown.style.display = input.value === {shop_name!r} ? 'block' : 'none';
          dropdown.querySelector('.ant-select-item-option').onclick = () => {{
            const tag = document.createElement('span');
            tag.className = 'ant-select-selection-item';
            tag.title = {shop_name!r};
            select.querySelector('.ant-select-selector').prepend(tag);
            dropdown.style.display = 'none';
          }};
        }})();</script>'''

    def test_all_mapped_shops_require_exact_name_and_numeric_id(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                for shop_id, shop_name in GAIA_SHOP_NAMES.items():
                    with self.subTest(shop_id=shop_id):
                        page.set_content(self.fixture(shop_id, shop_name))
                        _select_exact_shop_filter(page, shop_id, shop_name)
                        self.assertEqual(shop_name, page.locator('.ant-select-selection-item').get_attribute('title'))
                page.set_content(self.fixture('208501840', GAIA_SHOP_NAMES['208501840'], option_id='7970406'))
                with self.assertRaisesRegex(PriceUpliftError, 'ID 208501840'):
                    _select_exact_shop_filter(page, '208501840', GAIA_SHOP_NAMES['208501840'])
            finally:
                browser.close()


class ChannelPaginationTests(unittest.TestCase):
    def test_only_exact_shop_model_response_is_accepted(self):
        from urllib.parse import quote

        class Request:
            method = 'GET'

        class Response:
            request = Request()

            def __init__(self, body):
                self.url = ('https://gaiya.jd.com/api?apiName='
                            'pop.erp.goods.product.spu.query.product.list&body='
                            + quote(body))

        self.assertFalse(_matching_search_response(
            Response('{"pageNo":1,"pageSize":20}'), '208501840', '苹果 iPhone 17 Pro'))
        self.assertTrue(_matching_search_response(
            Response('{"pageNo":1,"shopIds":["208501840"],'
                     '"channelSpuName":"苹果 iPhone 17 Pro"}'),
            '208501840', '苹果 iPhone 17 Pro'))
        self.assertFalse(_matching_search_response(
            Response('{"pageNo":1,"shopIds":["7970406"],'
                     '"channelSpuName":"苹果 iPhone 17 Pro"}'),
            '208501840', '苹果 iPhone 17 Pro'))

    def test_search_waits_past_stale_other_shop_rows(self):
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('''<div class="ant-pagination-total-text">共26529条记录</div>
                    <div class="ant-pagination">共26529条记录 20 条/页</div>''')
                page.evaluate('''() => setTimeout(() => {
                    document.querySelector('.ant-pagination-total-text').textContent = '共1条记录';
                    document.querySelector('.ant-pagination').textContent = '共1条记录 20 条/页';
                }, 150)''')
                stale = PriceUpliftError('盖亚渠道商品搜索返回了其他店铺，停止关联')
                current = [{'product_id': '3847486726675235295',
                            'shop_id': '208501840', 'title': '苹果 iPhone 17 Pro 二手手机'}]
                with patch('integrations.gaia_browser.channel_reconcile._read_visible_rows',
                           side_effect=[stale, stale, current]) as reader:
                    rows, total = _wait_for_search_rows(page, '208501840',
                                                        '苹果 iPhone 17 Pro', 1,
                                                        timeout_seconds=2)
                self.assertEqual((current, 1), (rows, total))
                self.assertEqual(3, reader.call_count)
            finally:
                browser.close()

    def test_waits_for_new_rows_after_page_number_changes(self):
        old = [{'product_id': '111'}]
        partial = [{'product_id': '222'}]
        complete = [{'product_id': '222'}, {'product_id': '333'}]

        class DelayedPage:
            index = 0
            snapshots = [old, old, partial, complete]

            def wait_for_timeout(self, milliseconds):
                self.index = min(self.index + 1, len(self.snapshots) - 1)

        page = DelayedPage()
        with patch('integrations.gaia_browser.channel_reconcile._read_visible_rows',
                   side_effect=lambda _page, _shop: page.snapshots[page.index]):
            rows = _wait_for_fresh_page_rows(page, '208501840', {'111'}, 2)
        self.assertEqual(complete, rows)
        self.assertEqual(3, page.index)

    def test_never_accepts_repeated_previous_page(self):
        class StalePage:
            def wait_for_timeout(self, milliseconds):
                pass

        with patch('integrations.gaia_browser.channel_reconcile._read_visible_rows',
                   return_value=[{'product_id': '111'}]):
            with self.assertRaisesRegex(PriceUpliftError, '新结果未完整加载'):
                _wait_for_fresh_page_rows(StalePage(), '208501840', {'111'}, 1,
                                          timeout_seconds=0.01)


if __name__ == '__main__':
    unittest.main()
