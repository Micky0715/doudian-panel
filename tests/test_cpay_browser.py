import unittest
from unittest.mock import MagicMock

from integrations.doudian.cpay_browser import (
    CpayEnrollmentError, _wait_create_page, validate_request,
)
from integrations.doudian.cpay_readback import verify_cpay_membership
from integrations.doudian.cpay_terms import required_cpay_terms


class CpayBrowserGuardsTests(unittest.TestCase):
    def test_create_page_waits_for_async_tab(self):
        home = MagicMock(url='https://fxg.jinritemai.com/ffa/cpay/home')
        create = MagicMock(url='https://fxg.jinritemai.com/ffa/cpay/stages-interest/create')
        context = MagicMock()
        context.pages = [home]
        home.wait_for_timeout.side_effect = lambda _: context.pages.append(create)

        self.assertIs(_wait_create_page(context, home), create)
        home.wait_for_timeout.assert_called_once_with(250)
        create.get_by_text.assert_called_once_with('创建活动', exact=True)
        create.get_by_text.return_value.first.wait_for.assert_called_once_with(
            state='visible', timeout=10000)

    def test_12_selects_only_12_and_24_selects_12_plus_24(self):
        self.assertEqual((12,), required_cpay_terms(12))
        self.assertEqual((12, 24), required_cpay_terms(24))
        with self.assertRaises(ValueError):
            required_cpay_terms(6)

    def test_requires_known_shop_term_exact_activity_and_unique_batch_ids(self):
        name, ids = validate_request(
            '7970406', 12, ' 【专享】年框商家双11大回馈 ', ['3847298731712053572'])
        self.assertEqual('【专享】年框商家双11大回馈', name)
        self.assertEqual(['3847298731712053572'], ids)
        for shop, term, activity, products in [
            ('7970406', 6, '活动', ['3847298731712053572']),
            ('999999', 12, '活动', ['3847298731712053572']),
            ('7970406', 12, '【全店报名】活动', ['3847298731712053572']),
            ('7970406', 12, '活动', ['3847298731712053572'] * 2),
            ('7970406', 12, '活动', ['not-an-id']),
        ]:
            with self.subTest(shop=shop, term=term, activity=activity, products=products):
                with self.assertRaises(CpayEnrollmentError):
                    validate_request(shop, term, activity, products)

    def test_readback_requires_every_product_same_activity_and_term(self):
        class Gateway:
            def __init__(self, different=False, periods=(12,)):
                self.different = different
                self.periods = periods

            def call(self, method, params):
                product_id = params['product_id']
                return {'data': {'product_discount_and_promotion_activity_response': {
                    'discount_info': {'promotion_activity_id':
                        product_id if self.different else 'one-activity',
                        'period_shared_proportion_config': {
                            'period_shared_proportion_list': [
                                {'period': period} for period in self.periods]}}}}}

        ids = ['3847298731712053572', '3847298731712053573']
        self.assertTrue(verify_cpay_membership('', '7970406', 12, ids,
                        gateway=Gateway(), attempts=1)['confirmed'])
        self.assertFalse(verify_cpay_membership('', '7970406', 12, ids,
                         gateway=Gateway(different=True), attempts=1)['confirmed'])
        self.assertFalse(verify_cpay_membership('', '7970406', 24, ids,
                         gateway=Gateway(periods=(24,)), attempts=1)['confirmed'])
        self.assertTrue(verify_cpay_membership('', '7970406', 24, ids,
                        gateway=Gateway(periods=(12, 24)), attempts=1)['confirmed'])
        self.assertFalse(verify_cpay_membership('', '7970406', 12, ids,
                         gateway=Gateway(periods=(12, 24)), attempts=1)['confirmed'])


if __name__ == '__main__':
    unittest.main()
