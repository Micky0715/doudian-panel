import unittest
from datetime import datetime
from html import escape
from unittest.mock import Mock, patch

from playwright.sync_api import sync_playwright

from app.gaia_listing.application.price_uplift import PriceUpliftError
from integrations.gaia_browser.channel_reconcile import (
    ChannelProductNotConfirmedError,
    _channel_search_request, _matching_search_response, _search_failure_detail,
    _search_response_total, _retry_stale_search,
    _model_search_queries, _title_matches, snapshot_channel_products,
    _select_exact_shop_filter,
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
        with self.assertRaises(ChannelProductNotConfirmedError) as caught:
            self.check([PRODUCT], [PRODUCT])
        self.assertEqual(0, caught.exception.diagnostics['new_id_count'])
        self.assertEqual({}, caught.exception.diagnostics['excluded'])
        self.assertIn('未查到本次新商品', str(caught.exception))

    def test_similar_model_does_not_match(self):
        with self.assertRaises(ChannelProductNotConfirmedError) as caught:
            self.check([], [dict(PRODUCT, title='苹果 iPhone 17 Pro Max 二手手机')])
        self.assertEqual({'title': 1}, caught.exception.diagnostics['excluded'])

    def test_other_samsung_variant_does_not_auto_associate(self):
        expected = [{'brand': '三星', 'model': '三星 Galaxy S21 (5G版)'}]
        key = '三星三星galaxys215g版'
        item = dict(PRODUCT, shop_id='51165770', title='三星Galaxy S21 FE 5G 二手手机')
        with self.assertRaises(ChannelProductNotConfirmedError) as caught:
            reconcile_channel_products(
                {key: {'items': []}}, {key: {'items': [item]}},
                shop_id='51165770', expected_products=expected,
                submitted_at=datetime(2026, 10, 9, 17, 41, 43),
                observed_at=datetime(2026, 10, 9, 17, 41, 50))
        self.assertEqual({'title': 1}, caught.exception.diagnostics['excluded'])

    def test_verified_5g_version_spelling_matches_only_same_variant(self):
        model = '三星 Galaxy S21 (5G版)'
        self.assertIn('三星Galaxy S21 5G', _model_search_queries(model))
        self.assertTrue(_title_matches('三星Galaxy S21 5G 二手手机', '三星', model))
        for title in ('三星Galaxy S21 二手手机',
                      '三星Galaxy S21 4G 二手手机',
                      '三星Galaxy S21+ 5G 二手手机',
                      '三星Galaxy S21 Ultra 5G 二手手机'):
            with self.subTest(title=title):
                self.assertFalse(_title_matches(title, '三星', model))

        expected = [{'brand': '三星', 'model': model}]
        key = '三星三星galaxys215g版'
        item = dict(PRODUCT, shop_id='51165770',
                    title='三星Galaxy S21 5G 二手手机',
                    created_at='2026-10-15 15:50:00')
        self.assertEqual([item['product_id']], reconcile_channel_products(
            {key: {'items': []}}, {key: {'items': [item]}},
            shop_id='51165770', expected_products=expected,
            submitted_at=datetime(2026, 10, 15, 15, 49, 43),
            observed_at=datetime(2026, 10, 15, 15, 50, 5)))

    def test_zero_candidates_identifies_status_filter_without_accepting_product(self):
        with self.assertRaises(ChannelProductNotConfirmedError) as caught:
            self.check([], [dict(PRODUCT, status='审核中')])
        self.assertEqual({'status': 1}, caught.exception.diagnostics['excluded'])
        self.assertEqual(1, caught.exception.diagnostics['new_id_count'])
        self.assertIn('状态不符 1 个', str(caught.exception))

    def test_ambiguous_concurrent_creation_stops(self):
        with self.assertRaisesRegex(PriceUpliftError, '2 个'):
            self.check([], [PRODUCT, dict(PRODUCT, product_id='3847298731712053573')])

    def test_cross_shop_or_scene_stops(self):
        for change in ({'shop_id': '7970406'}, {'scene': '直播间'}):
            with self.subTest(change=change), self.assertRaisesRegex(PriceUpliftError, '符合条件 0 个'):
                self.check([], [dict(PRODUCT, **change)])

    def test_livestream_room_matches_only_room_products(self):
        kwargs = dict(shop_id='168976032', expected_products=EXPECTED,
                      submitted_at=datetime(2026, 10, 9, 17, 41, 43),
                      observed_at=datetime(2026, 10, 9, 17, 41, 50), scene=1)
        room = dict(PRODUCT, scene='直播间')
        before = {KEY: {'items': []}}
        self.assertEqual([PRODUCT['product_id']], reconcile_channel_products(
            before, {KEY: {'items': [room]}}, **kwargs))
        with self.assertRaisesRegex(PriceUpliftError, '符合条件 0 个'):
            reconcile_channel_products(before, {KEY: {'items': [PRODUCT]}}, **kwargs)

    def test_gaia_whitespace_variant_is_found_but_still_requires_exact_product(self):
        self.assertEqual(['一加 9 Pro', '一加9 Pro', '一加 9Pro', '一加9Pro'],
                         _model_search_queries('一加 9 Pro'))
        self.assertEqual(
            ['苹果 iPhone 14', '苹果iPhone 14', '苹果 iPhone14', '苹果iPhone14'],
            _model_search_queries('苹果 iPhone 14'))
        self.assertEqual(
            ['三星 Galaxy S21 (5G版)', '三星Galaxy S21 (5G版)',
             '三星GalaxyS21(5G版)', '三星 Galaxy S21 5G',
             '三星Galaxy S21 5G', '三星GalaxyS215G'],
            _model_search_queries('三星 Galaxy S21 (5G版)'))
        self.assertEqual(
            ['iQOO 12 Pro', 'iQOO12 Pro', 'iQOO 12Pro', 'iQOO12Pro'],
            _model_search_queries('iQOO 12 Pro'))
        self.assertIn('一加 Ace5 Pro', _model_search_queries('一加 Ace 5 Pro'))
        iqoo = dict(PRODUCT, product_id='3848411662923202853',
                    shop_id='129675602', title='iQOO 12Pro 二手手机',
                    created_at='2026-10-15 18:03:13')
        self.assertEqual(['3848411662923202853'], reconcile_channel_products(
            {'iqooiqoo12pro': {'items': []}},
            {'iqooiqoo12pro': {'items': [iqoo]}},
            shop_id='129675602',
            expected_products=[{'brand': 'iQOO', 'model': 'iQOO 12 Pro'}],
            submitted_at=datetime(2026, 10, 15, 18, 3, 4),
            observed_at=datetime(2026, 10, 15, 18, 3, 20)))
        item = dict(PRODUCT, product_id='3848413505506115683', shop_id='51165770',
                    title='一加9 Pro 二手手机', created_at='2026-10-15 17:59:38')
        page = Mock()
        page.locator.return_value.count.return_value = 1
        page.get_by_role.return_value.filter.return_value.count.return_value = 1
        with patch('integrations.gaia_browser.channel_reconcile._select_exact_shop_filter'), \
             patch('integrations.gaia_browser.channel_reconcile._search_channel_model',
                   side_effect=[[], [item], [], [item]]) as search:
            after = snapshot_channel_products(
                page, '51165770', [{'brand': '一加', 'model': '一加 9 Pro'}])
        self.assertEqual(['一加 9 Pro', '一加9 Pro', '一加 9Pro', '一加9Pro'],
                         [call.args[-1] for call in search.call_args_list])
        before = {'一加一加9pro': {'items': []}}
        self.assertEqual([item['product_id']], reconcile_channel_products(
            before, after, shop_id='51165770',
            expected_products=[{'brand': '一加', 'model': '一加 9 Pro'}],
            submitted_at=datetime(2026, 10, 15, 17, 59, 30),
            observed_at=datetime(2026, 10, 15, 17, 59, 45)))


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
    def test_exact_api_response_count_is_validated(self):
        class Response:
            status = 200
            def json(self):
                return {'code': 200, 'data': {'totalCount': 2}}
        self.assertEqual(2, _search_response_total(Response()))
        with patch.object(Response, 'json', return_value={'code': 200, 'data': {'totalCount': 401}}):
            with self.assertRaisesRegex(PriceUpliftError, '超过 400'):
                _search_response_total(Response())

    def test_stale_ui_retries_exact_search_once_and_still_fails_closed(self):
        class Response:
            status = 200
            def json(self):
                return {'code': 200, 'data': {'totalCount': 2}}
        class Expected:
            value = Response()
            def __enter__(self):
                return self
            def __exit__(self, *_):
                return False
        class Page:
            def __init__(self):
                self.delays = []
            def wait_for_timeout(self, ms):
                self.delays.append(ms)
            def expect_response(self, predicate, timeout):
                self.predicate = predicate
                return Expected()
        class Search:
            clicks = 0
            def click(self, timeout):
                self.clicks += 1
        page, search = Page(), Search()
        rows = [{'product_id': '1234567890'}]
        with patch('integrations.gaia_browser.channel_reconcile._wait_for_search_rows',
                   return_value=(rows, 2)) as wait:
            self.assertEqual((rows, 2), _retry_stale_search(page, search, '51165770', '一加 10 Pro'))
        self.assertEqual(1, search.clicks)
        wait.assert_called_once_with(page, '51165770', '一加 10 Pro', 2)
        with patch('integrations.gaia_browser.channel_reconcile._wait_for_search_rows',
                   side_effect=PriceUpliftError('页面总数 26322 与本次接口结果 2 不一致')):
            with self.assertRaisesRegex(PriceUpliftError, '重查后仍未刷新'):
                _retry_stale_search(page, search, '51165770', '一加 10 Pro')
        self.assertEqual(2, search.clicks)

    def test_search_failure_distinguishes_no_request_timeout_and_wrong_filter(self):
        expected = {'shopIds': ['208501840'],
                    'channelSpuName': '苹果 iPhone 17 Pro', 'pageNo': 1}
        detail = lambda requests, responses=None, failures=None: _search_failure_detail(
            requests, responses or [], failures or [], '208501840', '苹果 iPhone 17 Pro')
        self.assertIn('未发出', detail([]))
        self.assertIn('25 秒内未返回', detail([expected]))
        self.assertIn('请求失败', detail([expected], failures=[(expected, 'net::ERR_TIMED_OUT')]))
        self.assertIn('HTTP 200', detail([expected], responses=[(expected, 200)]))
        self.assertIn('shopIds', detail([dict(expected, shopIds=['7970406'])]))
        self.assertIn('请求体不是有效 JSON', detail([{'invalid_body': True}]))

    def test_search_request_diagnostics_do_not_include_full_url(self):
        from urllib.parse import quote

        class Request:
            method = 'GET'
            url = ('https://gaiya.jd.com/api?apiName='
                   'pop.erp.goods.product.spu.query.product.list&body='
                   + quote('{"shopIds":["208501840"],"channelSpuName":"苹果 iPhone 17 Pro","pageNo":1}')
                   + '&token=secret')

        self.assertEqual({'shopIds': ['208501840'], 'channelSpuName': '苹果 iPhone 17 Pro',
                          'pageNo': 1}, _channel_search_request(Request()))

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
