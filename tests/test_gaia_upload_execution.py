import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.gaia_listing.application.upload_execution import GaiaUploadExecution
from app.gaia_listing.infrastructure.batch_repository import GaiaBatchRepository
from app.gaia_listing.domain.models import GaiaBatch
from tests.test_gaia_price_uplift import make_template


class GaiaUploadExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = GaiaBatchRepository(self.tmp.name + '/batches')
        self.batch = GaiaBatch('abc123', '7970406', 2, 1, '/source', 'hash',
                              state='validation_passed', output_path='/output', output_hash='outputhash',
                              summary={'blocking_errors': 0, 'generated_rows': 2})
        self.repo.save(self.batch)

    def service(self, runner):
        return GaiaUploadExecution(self.tmp.name, self.repo,
                                   lambda batch, shop: {'ready': True}, lambda: True, runner)

    def start(self, service, **overrides):
        return service.start('abc123', **dict(dict(shop_id='7970406', expected_hash='outputhash', confirmed=True), **overrides))

    def test_requires_explicit_start_shop_and_immutable_file_identity(self):
        service = self.service(lambda **kwargs: self.fail('must not execute'))
        for values in ({'confirmed': False}, {'confirmed': 'true'}, {'shop_id': '123'}, {'expected_hash': 'changed'}):
            with self.assertRaises(ValueError):
                self.start(service, **values)
        self.assertEqual('validation_passed', self.repo.get('abc123')['state'])

    def test_repeated_start_is_single_execution_and_other_batch_cannot_share_browser(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def runner(**kwargs):
            calls.append(kwargs)
            kwargs['verify_handoff']()
            entered.set()
            release.wait(3)
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '提交成功'}
        service = self.service(runner)
        started_threads = []
        real_thread = threading.Thread
        def thread_factory(*args, **kwargs):
            thread = real_thread(*args, **kwargs)
            started_threads.append(thread)
            return thread
        with patch('app.gaia_listing.application.upload_execution.threading.Thread', side_effect=thread_factory):
            self.start(service)
            self.assertTrue(entered.wait(2))
            self.start(service)
            other = GaiaBatch(**self.batch.to_dict())
            other.id = 'def456'
            self.repo.save(other)
            with self.assertRaisesRegex(RuntimeError, '另一批'):
                service.start('def456', shop_id='7970406', expected_hash='outputhash', confirmed=True)
            release.set()
            started_threads[0].join(3)
        self.assertEqual(1, len(calls))
        result = self.repo.get('abc123')
        self.assertEqual('gaia_accepted', result['state'])
        self.assertTrue(result['gaia_receipt']['submit_intent'])
        self.assertFalse(result['gaia_receipt']['doudian_published'])
        self.start(service)
        self.assertEqual(1, len(calls))

    def test_timeout_after_submit_is_unknown_and_never_retried(self):
        def runner(**kwargs):
            kwargs['before_submit']()
            raise TimeoutError('network response lost')
        service = self.service(runner)
        # Run synchronously to assert final persistence deterministically.
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service)
        result = self.repo.get('abc123')
        self.assertEqual('manual_required', result['state'])
        self.assertEqual('unknown', result['gaia_receipt']['phase'])
        self.assertIn('禁止重试', result['gaia_receipt']['message'])

    def test_cancel_during_browser_preparation_prevents_submit(self):
        def runner(**kwargs):
            from app.gaia_listing.application.batch_lifecycle import cancel_batch_record
            self.repo.mutate('abc123', lambda data: cancel_batch_record(data, shop_id='7970406', reason='取消测试'))
            kwargs['before_submit']()
            self.fail('cancelled batch must not submit')
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(self.service(runner))
        self.assertEqual('cancelled', self.repo.get('abc123')['state'])

    def test_dead_worker_is_not_reported_running_forever(self):
        self.batch.state = 'gaia_submitted'
        self.batch.gaia_receipt = {'phase': 'submitting', 'worker_pid': 123456, 'submit_intent': True}
        self.repo.save(self.batch)
        with patch('os.kill', side_effect=ProcessLookupError):
            result = self.service(None).recover_interrupted(self.batch.to_dict())
        self.assertEqual('manual_required', result['state'])

    def test_stop_validates_identity_and_is_idempotent_before_start(self):
        service = self.service(None)
        for shop, digest in [('123', 'outputhash'), ('7970406', ''), ('7970406', 'changed')]:
            with self.assertRaises(ValueError):
                service.stop('abc123', shop_id=shop, expected_hash=digest)
        stopped = service.stop('abc123', shop_id='7970406', expected_hash='outputhash')
        repeated = service.stop('abc123', shop_id='7970406', expected_hash='outputhash')
        self.assertEqual(stopped, repeated)
        self.assertEqual('stopped', stopped['gaia_receipt']['phase'])
        with self.assertRaises(ValueError):
            self.start(service)

    def test_stop_between_verification_and_submit_intent_wins_atomic_race(self):
        service = self.service(None)
        def runner(**kwargs):
            original_verify = service.verified
            def verify_then_stop(*args):
                result = original_verify(*args)
                service.stop('abc123', shop_id='7970406', expected_hash='outputhash')
                return result
            service.verified = verify_then_stop
            kwargs['before_submit']()
            self.fail('must not click')
        service.runner = runner
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service)
        result = self.repo.get('abc123')
        self.assertEqual('cancelled', result['state'])
        self.assertEqual('stopped', result['gaia_receipt']['phase'])
        self.assertFalse(result['gaia_receipt']['submit_intent'])

    def test_stop_after_submit_intent_cannot_revoke_or_cancel_receipt(self):
        service = self.service(None)
        def runner(**kwargs):
            kwargs['before_submit']()
            with self.assertRaisesRegex(RuntimeError, '无法.*撤回'):
                service.stop('abc123', shop_id='7970406', expected_hash='outputhash')
            kwargs['check_cancelled']()
            return {'outcome': 'accepted', 'feedback': '提交成功'}
        service.runner = runner
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service)
        self.assertEqual('gaia_accepted', self.repo.get('abc123')['state'])

    def test_stopped_dead_worker_finishes_cleanup_without_submit(self):
        self.batch.state = 'cancelled'
        self.batch.gaia_receipt = {'phase': 'stopping', 'worker_pid': 123456, 'submit_intent': False}
        self.repo.save(self.batch)
        with patch('os.kill', side_effect=ProcessLookupError):
            result = self.service(None).recover_interrupted(self.batch.to_dict())
        self.assertEqual('cancelled', result['state'])
        self.assertEqual('stopped', result['gaia_receipt']['phase'])

    def test_start_automatically_imports_verified_product_to_selected_term(self):
        self.batch.summary.update(expected_product_count=1,
                                  expected_products=[{'brand': '苹果', 'model': 'iPhone 17 Pro'}])
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        uploads = []

        def gaia_runner(**kwargs):
            self.assertTrue(kwargs['auto_price'])
            self.assertEqual(1, len(kwargs['expected_products']))
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '导入成功',
                    'verified_product_ids': ['3847298731712053572']}

        def price_runner(**kwargs):
            uploads.append(kwargs)
            self.assertEqual(24, kwargs['installments'])
            self.assertTrue(Path(kwargs['workbook_path']).is_file())
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '导入成功'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            gaia_runner, price_template=lambda: template, price_runner=price_runner,
            activity_runner=lambda **kwargs: (kwargs['before_submit']() or {
                'outcome': 'submitted_unverified', 'product_ids': kwargs['product_ids']}),
            activity_probe=lambda env, shop, term, ids, **kwargs: {
                'confirmed': False, 'products': {ids[0]: {'periods': []}}},
            activity_session_probe=lambda profile, shop, activity: {'ready': True},
            price_probe=lambda profile, shop, term: {
                'rule_name': '爱回收严选二手数码【7970406】-24期',
                'rate_text': '浮动比例：9%', 'submitted': False},
        )
        with self.assertRaisesRegex(ValueError, '活动卡片'):
            self.start(service, installments=24)
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service, installments=24, activity_name='【专享】年框商家双11大回馈')
        result = self.repo.get('abc123')
        self.assertEqual('manual_required', result['state'])
        self.assertEqual(['3847298731712053572'], result['doudian_draft_ids'])
        self.assertEqual('accepted', result['gaia_receipt']['price_phase'])
        self.assertEqual('submitted_unverified', result['gaia_receipt']['activity_phase'])
        self.assertEqual(1, len(uploads))

    def test_price_probe_accepts_renamed_shop_but_rejects_wrong_id_or_term(self):
        def service_for(name):
            return GaiaUploadExecution(
                self.tmp.name, self.repo, lambda batch, shop: {'ready': True},
                lambda: True, price_probe=lambda profile, shop, term: {
                    'rule_name': name, 'rate_text': '浮动比例：4.3%', 'submitted': False},
            )
        service_for('运营新名称【7970406】 -12期')._verify_price_rule('7970406', 12)
        with self.assertRaisesRegex(RuntimeError, '店铺 ID'):
            service_for('运营新名称【7970407】 -12期')._verify_price_rule('7970406', 12)
        with self.assertRaisesRegex(RuntimeError, '期数'):
            service_for('运营新名称【7970406】 -24期')._verify_price_rule('7970406', 12)

    def test_activity_is_verified_only_after_readback_of_this_batch(self):
        product_id = '3847298731712053572'
        self.batch.state = 'handed_off'
        self.batch.doudian_draft_ids = [product_id]
        self.batch.gaia_receipt = {
            'price_phase': 'accepted', 'activity_phase': 'waiting_price',
            'activity_name': '【专享】年框商家双11大回馈',
        }
        self.repo.save(self.batch)
        probes = []

        def probe(env, shop, term, ids, **kwargs):
            probes.append(kwargs)
            if kwargs.get('attempts') == 1:
                return {'confirmed': False, 'products': {product_id: {'periods': []}}}
            return {'confirmed': True, 'products': {product_id: {
                'periods': [12], 'activity_id': 'activity-1'}}}

        def runner(**kwargs):
            self.assertEqual([product_id], kwargs['product_ids'])
            kwargs['before_submit']()
            return {'outcome': 'submitted_unverified'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            activity_runner=runner, activity_probe=probe,
        )
        service._run_activity_enrollment('abc123', '7970406', 12, [product_id])
        final = self.repo.get('abc123')
        self.assertEqual('handed_off', final['state'])
        self.assertEqual('verified', final['gaia_receipt']['activity_phase'])
        self.assertTrue(final['gaia_receipt']['activity_submit_intent'])
        self.assertEqual(2, len(probes))

    def test_missing_doudian_activity_session_blocks_before_gaia_submit(self):
        self.batch.summary['expected_products'] = [{'brand': '苹果', 'model': 'iPhone 17 Pro'}]
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            runner=lambda **kwargs: self.fail('must not create Gaia product'),
            price_template=lambda: template,
            activity_session_probe=lambda profile, shop, activity: (_ for _ in ()).throw(
                RuntimeError('分期浏览器未登录')),
        )
        with self.assertRaisesRegex(RuntimeError, '未登录'):
            self.start(service, installments=12, activity_name='【专享】年框商家双11大回馈')
        self.assertEqual('validation_passed', self.repo.get('abc123')['state'])

    def test_missing_product_id_never_uploads_price_rule(self):
        self.batch.summary.update(expected_product_count=1,
                                  expected_products=[{'brand': '苹果', 'model': 'iPhone 17 Pro'}])
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)

        def gaia_runner(**kwargs):
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '导入成功',
                    'price_reconcile_error': '本次新增商品不能唯一确认'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            gaia_runner, price_template=lambda: template,
            price_runner=lambda **kwargs: self.fail('must not import price'),
            activity_session_probe=lambda profile, shop, activity: {'ready': True},
            price_probe=lambda profile, shop, term: {
                'rule_name': '爱回收严选二手数码【7970406】-12期',
                'rate_text': '浮动比例：4.3%', 'submitted': False},
        )
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service, installments=12, activity_name='【专享】年框商家双11大回馈')
        result = self.repo.get('abc123')
        self.assertEqual('manual_required', result['state'])
        self.assertEqual('id_blocked', result['gaia_receipt']['price_phase'])

    def test_price_only_resume_never_recreates_gaia_product(self):
        product_id = '3847478441691709635'
        self.batch.state = 'manual_required'
        self.batch.summary['expected_product_count'] = 1
        self.batch.doudian_draft_ids = [product_id]
        self.batch.gaia_receipt = {
            'phase': 'accepted', 'submit_intent': True, 'price_phase': 'failed',
            'price_installments': 12,
            'result': {'outcome': 'accepted', 'verified_product_ids': [product_id]},
        }
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        uploads = []

        def price_runner(**kwargs):
            uploads.append(kwargs)
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '导入成功'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            runner=lambda **kwargs: self.fail('must not recreate Gaia product'),
            price_template=lambda: template, price_runner=price_runner,
            price_probe=lambda profile, shop, term: {
                'rule_name': '爱回收严选二手数码【7970406】-12期',
                'rate_text': '浮动比例：4.3%', 'submitted': False},
        )
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, **kw: type('Inline', (), {'start': lambda self: target()})()
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)
        result = self.repo.get('abc123')
        self.assertEqual('handed_off', result['state'])
        self.assertEqual('accepted', result['gaia_receipt']['price_phase'])
        self.assertTrue(result['gaia_receipt']['price_resume_started'])
        self.assertEqual(1, len(uploads))
        with self.assertRaisesRegex(RuntimeError, '仅允许'):
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)

    def test_price_resume_refuses_any_prior_price_submit_intent(self):
        self.batch.state = 'manual_required'
        self.batch.summary['expected_product_count'] = 1
        self.batch.doudian_draft_ids = ['3847478441691709635']
        self.batch.gaia_receipt = {
            'phase': 'accepted', 'submit_intent': True, 'price_phase': 'failed',
            'price_submit_intent': True, 'price_installments': 12,
            'result': {'outcome': 'accepted', 'verified_product_ids': self.batch.doudian_draft_ids},
        }
        self.repo.save(self.batch)
        service = self.service(lambda **kwargs: self.fail('must not submit'))
        with self.assertRaisesRegex(RuntimeError, '仅允许'):
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)

    def test_price_resume_allows_only_verified_rejection_with_live_absence(self):
        product_id = '3847478441691709635'
        self.batch.state = 'manual_required'
        self.batch.summary['expected_product_count'] = 1
        self.batch.doudian_draft_ids = [product_id]
        self.batch.gaia_receipt = {
            'phase': 'accepted', 'submit_intent': True, 'price_phase': 'rejected',
            'price_submit_intent': True, 'price_installments': 12,
            'result': {'outcome': 'accepted', 'verified_product_ids': [product_id]},
            'price_result': {
                'outcome': 'rejected',
                'submit_responses': [{
                    'api_name': 'pop.digital.dy.product.floating.adjust.temp.import',
                    'code': '400', 'msg': '导入数据不能为空！',
                }],
                'membership': {'found_ids': [], 'missing_ids': [product_id]},
            },
        }
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        calls = []

        def price_runner(**kwargs):
            calls.append(kwargs)
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '规则列表已核实'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            runner=lambda **kwargs: self.fail('must not recreate Gaia product'),
            price_template=lambda: template, price_runner=price_runner,
            price_probe=lambda profile, shop, term: {
                'rule_name': '爱回收严选二手数码【7970406】-12期',
                'rate_text': '浮动比例：4.3%', 'submitted': False},
        )
        with patch('integrations.gaia_browser.price_rule_probe.probe_rule_membership',
                   return_value={'found_ids': [], 'missing_ids': [product_id]}), \
                patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, **kw: type('Inline', (), {'start': lambda self: target()})()
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)
        self.assertEqual(1, len(calls))
        self.assertEqual('handed_off', self.repo.get('abc123')['state'])


    def test_price_only_retry_after_pre_submit_failure_is_bounded(self):
        product_id = '3847478441691709635'
        self.batch.state = 'manual_required'
        self.batch.summary['expected_product_count'] = 1
        self.batch.doudian_draft_ids = [product_id]
        self.batch.gaia_receipt = {
            'phase': 'accepted', 'submit_intent': True, 'price_phase': 'failed',
            'price_installments': 12,
            'result': {'outcome': 'accepted', 'verified_product_ids': [product_id]},
        }
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        calls = []

        def price_runner(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError('选择文件按钮结构变化')
            kwargs['before_submit']()
            return {'outcome': 'accepted', 'feedback': '导入成功'}

        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            runner=lambda **kwargs: self.fail('must not recreate Gaia product'),
            price_template=lambda: template, price_runner=price_runner,
            price_probe=lambda profile, shop, term: {
                'rule_name': '爱回收严选二手数码【7970406】-12期',
                'rate_text': '浮动比例：4.3%', 'submitted': False},
        )
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, **kw: type('Inline', (), {'start': lambda self: target()})()
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)
            first = self.repo.get('abc123')
            self.assertEqual('failed', first['gaia_receipt']['price_phase'])
            self.assertFalse(first['gaia_receipt'].get('price_submit_intent'))
            self.assertEqual(1, first['gaia_receipt']['price_resume_attempts'])
            service.resume_price_only('abc123', shop_id='7970406',
                                      expected_hash='outputhash', confirmed=True)
        final = self.repo.get('abc123')
        self.assertEqual('handed_off', final['state'])
        self.assertEqual(2, final['gaia_receipt']['price_resume_attempts'])
        self.assertEqual(2, len(calls))

    def test_missing_price_rule_blocks_before_gaia_product_creation(self):
        self.batch.summary['expected_products'] = [{'brand': '苹果', 'model': 'iPhone 17 Pro'}]
        self.repo.save(self.batch)
        template = Path(self.tmp.name) / 'price_template.xlsx'
        make_template(template)
        service = GaiaUploadExecution(
            self.tmp.name, self.repo, lambda batch, shop: {'ready': True}, lambda: True,
            runner=lambda **kwargs: self.fail('must not create Gaia product'),
            price_template=lambda: template,
            activity_session_probe=lambda profile, shop, activity: {'ready': True},
            price_probe=lambda profile, shop, term: (_ for _ in ()).throw(
                RuntimeError('未找到精确匹配的价格上浮规则')),
        )
        with patch('app.gaia_listing.application.upload_execution.threading.Thread') as thread:
            thread.side_effect = lambda target, args, **kw: type('Inline', (), {'start': lambda self: target(*args)})()
            self.start(service, installments=12, activity_name='【专享】年框商家双11大回馈')
        result = self.repo.get('abc123')
        self.assertEqual('manual_required', result['state'])
        self.assertFalse(result['gaia_receipt']['submit_intent'])
        self.assertIn('未找到精确匹配', result['gaia_receipt']['message'])


if __name__ == '__main__':
    unittest.main()
