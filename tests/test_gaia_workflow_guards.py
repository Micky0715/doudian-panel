import tempfile
import unittest
import hashlib
import json

from flask import Flask

from app.gaia_listing.api.routes import create_gaia_blueprint
from app.gaia_listing.application.batch_lifecycle import cancel_batch_record
from app.gaia_listing.application.draft_matching import match_new_drafts
from app.gaia_listing.application.submission_guard import can_submit, submission_idempotency_key
from app.gaia_listing.domain.models import GaiaBatch
from app.gaia_listing.infrastructure.batch_repository import GaiaBatchRepository


class GaiaWorkflowGuardTests(unittest.TestCase):
    @staticmethod
    def _snap(captured_at, items, shop_id='25595926'):
        snapshot = {'schema_version': 2, 'shop_id': shop_id, 'captured_at': captured_at,
                    'items': items, 'reported_total': len(items),
                    'status_filter': 0, 'check_status_filter': 1}
        source = {key: snapshot[key] for key in
                  ('shop_id', 'status_filter', 'check_status_filter', 'reported_total', 'items')}
        snapshot['checksum'] = hashlib.sha256(json.dumps(
            source, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        return snapshot

    def test_submission_is_disabled_by_default(self):
        ok, reason = can_submit({'state': 'confirmed'}, feature_enabled=False, confirmed=True)
        self.assertFalse(ok)
        self.assertIn('未启用', reason)

    def test_submission_requires_output_identity(self):
        ok, _ = can_submit({'state': 'confirmed'}, feature_enabled=True, confirmed=True)
        self.assertFalse(ok)

    def test_idempotency_key_is_stable(self):
        self.assertEqual(submission_idempotency_key('a', 'b'), submission_idempotency_key('a', 'b'))

    def test_draft_match_requires_unique_new_draft(self):
        result = match_new_drafts(
            [{'row_id': '1', 'brand': '荣耀', 'model': '400 Pro', 'condition': '95新'}],
            self._snap(100, [{'product_id': 'old'}]),
            self._snap(200, [
                {'product_id': 'old'},
                {'product_id': 'new', 'create_time': '120', 'status': 0, 'check_status': '1'},
            ]), shop_id='25595926', submitted_at=110,
            details_by_id={'new': {'brand': '荣耀', 'model': '400 Pro', 'condition': '95新'}},
            receipt_links_by_row={'1': 'new'},
        )
        self.assertEqual({'1': 'new'}, result['matched'])

    def test_ambiguous_draft_goes_unresolved(self):
        expected = [{'row_id': '1', 'brand': '红米', 'model': 'K80', 'condition': '99新'}]
        drafts = [{'product_id': value, 'create_time': '120', 'status': 0,
                   'check_status': '1'} for value in ('a', 'b')]
        details = {value: {'brand': '红米', 'model': 'K80', 'condition': '99新'} for value in ('a', 'b')}
        result = match_new_drafts(expected, self._snap(100, []), self._snap(200, drafts),
                                  shop_id='25595926', submitted_at=110, details_by_id=details)
        self.assertEqual({}, result['matched'])
        self.assertEqual(2, result['unresolved'][0]['candidate_count'])

    def test_two_rows_cannot_claim_one_draft(self):
        expected = [{'row_id': row_id, 'brand': '红米', 'model': 'K80', 'condition': '99新'}
                    for row_id in ('1', '2')]
        draft = {'product_id': 'new', 'create_time': '120', 'status': 0, 'check_status': '1'}
        result = match_new_drafts(expected, self._snap(100, []), self._snap(200, [draft]),
                                  shop_id='25595926', submitted_at=110,
                                  details_by_id={'new': {'brand': '红米', 'model': 'K80', 'condition': '99新'}},
                                  receipt_links_by_row={'1': 'new', '2': 'new'})
        self.assertEqual({}, result['matched'])
        self.assertEqual(2, len(result['unresolved']))
        self.assertTrue(all('竞争' in row['reason'] for row in result['unresolved']))

    def test_unique_candidate_without_gaia_receipt_is_manual_only(self):
        expected = [{'row_id': '1', 'brand': '红米', 'model': 'K80', 'condition': '99新'}]
        draft = {'product_id': 'new', 'create_time': '120', 'status': 0, 'check_status': '1'}
        result = match_new_drafts(expected, self._snap(100, []), self._snap(200, [draft]),
                                  shop_id='25595926', submitted_at=110,
                                  details_by_id={'new': {'brand': '红米', 'model': 'K80', 'condition': '99新'}})
        self.assertEqual({}, result['matched'])
        self.assertIn('回执', result['unresolved'][0]['reason'])

    def test_title_only_and_cross_shop_snapshots_never_match(self):
        expected = [{'row_id': '1', 'brand': '红米', 'model': 'K80', 'condition': '99新'}]
        title_only = {'product_id': 'new', 'name': '红米 K80 99新 二手手机',
                      'create_time': '120', 'status': 0, 'check_status': '1'}
        result = match_new_drafts(expected, self._snap(100, []),
                                  self._snap(200, [title_only]),
                                  shop_id='25595926', submitted_at=110, details_by_id={})
        self.assertEqual({}, result['matched'])
        self.assertIn('缺少', result['errors'][0])
        cross_shop = match_new_drafts(expected, self._snap(100, []),
                                      self._snap(200, [title_only], shop_id='208501840'),
                                      shop_id='25595926', submitted_at=110, details_by_id={})
        self.assertEqual({}, cross_shop['matched'])
        self.assertIn('店铺', cross_shop['errors'][0])

    def test_partial_or_tampered_snapshot_never_matches(self):
        expected = [{'row_id': '1', 'brand': '红米', 'model': 'K80', 'condition': '99新'}]
        draft = {'product_id': 'new', 'create_time': '120', 'status': 0, 'check_status': '1'}
        before = self._snap(100, [])
        after = self._snap(200, [draft])
        after['reported_total'] = 2
        result = match_new_drafts(expected, before, after, shop_id='25595926', submitted_at=110,
                                  details_by_id={'new': {'brand': '红米', 'model': 'K80', 'condition': '99新'}})
        self.assertEqual({}, result['matched'])
        self.assertIn('分页', result['errors'][0])
        after = self._snap(200, [draft])
        after['items'][0]['create_time'] = '121'
        result = match_new_drafts(expected, before, after, shop_id='25595926', submitted_at=110,
                                  details_by_id={'new': {'brand': '红米', 'model': 'K80', 'condition': '99新'}})
        self.assertEqual({}, result['matched'])
        self.assertIn('校验和', result['errors'][0])

    def test_repository_isolated_and_round_trips(self):
        with tempfile.TemporaryDirectory() as root:
            repo = GaiaBatchRepository(root)
            batch = GaiaBatch('abc123', '25595926', 2, 1, '/tmp/input.xlsx', 'hash')
            repo.save(batch)
            self.assertEqual('25595926', repo.get('abc123')['shop_id'])

    def test_cancel_records_reason_once_and_is_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            repo = GaiaBatchRepository(root)
            repo.save(GaiaBatch('abc123', '25595926', 2, 1, '/tmp/input.xlsx', 'hash',
                                state='validation_passed'))
            def cancel(data):
                return cancel_batch_record(data, shop_id='25595926', reason='运营终止本批')
            first = repo.mutate('abc123', cancel)
            second = repo.mutate('abc123', cancel)
            self.assertEqual('cancelled', second['state'])
            self.assertEqual(1, len(first['events']))
            self.assertEqual(1, len(second['events']))
            self.assertEqual('validation_passed', second['events'][0]['from'])

    def test_cancel_rejects_wrong_shop_and_submitted_batch(self):
        batch = GaiaBatch('abc123', '25595926', 2, 1, '/tmp/input.xlsx', 'hash',
                          state='gaia_submitted').to_dict()
        with self.assertRaisesRegex(ValueError, '店铺'):
            cancel_batch_record(batch, shop_id='208501840', reason='运营终止本批')
        with self.assertRaisesRegex(RuntimeError, '不能标记为取消'):
            cancel_batch_record(batch, shop_id='25595926', reason='运营终止本批')

    def test_cancel_api_never_submits_or_deletes_output(self):
        with tempfile.TemporaryDirectory() as root:
            app = Flask(__name__)
            app.register_blueprint(create_gaia_blueprint(root, known_shop_ids={'25595926'}))
            repo = GaiaBatchRepository(root + '/panel_data/gaia_listing/batches')
            repo.save(GaiaBatch('abc123', '25595926', 2, 1, '/tmp/input.xlsx', 'hash',
                                state='validation_passed', output_path='/tmp/gaia_upload.xlsx',
                                output_hash='outputhash'))
            client = app.test_client()
            response = client.post('/api/gaia/batches/abc123/cancel',
                                   json={'shop_id': '25595926', 'reason': '运营终止本批'})
            self.assertEqual(200, response.status_code)
            self.assertEqual('cancelled', response.json['batch']['state'])
            self.assertEqual('/tmp/gaia_upload.xlsx', response.json['batch']['output_path'])
            self.assertEqual('outputhash', response.json['batch']['output_hash'])
            wrong_shop = client.post('/api/gaia/batches/abc123/cancel',
                                     json={'shop_id': '208501840', 'reason': '运营终止本批'})
            self.assertEqual(400, wrong_shop.status_code)
            repeated = client.post('/api/gaia/batches/abc123/cancel',
                                   json={'shop_id': '25595926', 'reason': '重复点击'})
            self.assertEqual(200, repeated.status_code)
            self.assertEqual(1, len(repeated.json['batch']['events']))
            repo.save(GaiaBatch('def456', '25595926', 2, 1, '/tmp/input.xlsx', 'hash',
                                state='gaia_submitted'))
            submitted = client.post('/api/gaia/batches/def456/cancel',
                                    json={'shop_id': '25595926', 'reason': '运营终止本批'})
            self.assertEqual(409, submitted.status_code)
            self.assertEqual('gaia_submitted', repo.get('def456')['state'])


if __name__ == '__main__':
    unittest.main()
