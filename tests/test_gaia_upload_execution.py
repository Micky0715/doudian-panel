import tempfile
import threading
import unittest
from unittest.mock import patch

from app.gaia_listing.application.upload_execution import GaiaUploadExecution
from app.gaia_listing.infrastructure.batch_repository import GaiaBatchRepository
from app.gaia_listing.domain.models import GaiaBatch


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


if __name__ == '__main__':
    unittest.main()
