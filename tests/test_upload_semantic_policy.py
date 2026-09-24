import json
import os
import unittest

from upload_semantic_policy import (authoritative_upload_verdict,
                                    causal_main_present, is_required_multiselect_error,
                                    exact_main_slot_binding, missing_aux_indices,
                                    needs_filechooser_fallback,
                                    semantic_aux_preview_present, session_uploads_present,
                                    strict_main_present, unique_main_candidate)


class UploadSemanticPolicyTest(unittest.TestCase):
    def test_permanent_hidden_duplicate_regressions(self):
        path = os.path.join(os.path.dirname(__file__), "upload_semantic_fixtures.json")
        with open(path, encoding="utf-8") as fixture_file:
            cases = json.load(fixture_file)
        for case in cases:
            with self.subTest(product_id=case["product_id"]):
                self.assertEqual(case["expect_upload"],
                                 unique_main_candidate(case["semantic_candidates"]))
                # Intentionally do not pass loose_wrapper_present: it is diagnostic only.
                self.assertEqual(case["expect_verify"], strict_main_present(
                    case["semantic_candidates"], case["strict_found"], case["strict_present"]
                ))

    def test_loose_wrapper_never_overrides_present_false(self):
        self.assertFalse(strict_main_present(1, True, False))

    def test_required_marker_is_not_part_of_authoritative_identity(self):
        # 2026-09-07: DouDian removed the star/required marker while retaining
        # one exact 商品正面图/上传主图 semantic input.  Candidate identity and
        # strict preview evidence stay authoritative; the marker is diagnostic.
        required_marker = False
        self.assertFalse(required_marker)
        self.assertTrue(strict_main_present(1, True, True))
        self.assertFalse(strict_main_present(2, True, True))

    def test_direct_input_reset_requires_real_filechooser(self):
        self.assertTrue(needs_filechooser_fallback(0))
        self.assertTrue(needs_filechooser_fallback(-1))
        self.assertFalse(needs_filechooser_fallback(1))

    def test_field_evidence_cannot_override_absent_required_main_slot(self):
        # Permanent negative regression for the 2026-09-01 false-positive:
        # field/global evidence was true and all four session URLs existed, while
        # the required main slot explicitly reported present=False.
        field_delta = 1
        causal_wrapper_present = True
        self.assertTrue(field_delta and causal_wrapper_present)  # diagnostic only
        self.assertFalse(authoritative_upload_verdict(
            1, True, False, 4, 4, 3
        ))

    def test_authoritative_verdict_requires_both_strict_main_and_session(self):
        self.assertTrue(authoritative_upload_verdict(1, True, True, 4, 4, 3))
        self.assertFalse(authoritative_upload_verdict(1, True, True, 4, 3, 3))
        self.assertFalse(authoritative_upload_verdict(2, True, True, 4, 4, 3))

    def test_exact_main_slot_binding_survives_react_input_replacement(self):
        self.assertTrue(exact_main_slot_binding(0, 0, 1, True))
        self.assertFalse(exact_main_slot_binding(0, 1, 1, True))
        self.assertFalse(exact_main_slot_binding(0, 0, 2, True))
        self.assertFalse(exact_main_slot_binding(0, 0, 1, False))

    def test_causal_binding_uses_semantic_slot_not_global_delta(self):
        self.assertTrue(causal_main_present(1, 1, True, True))
        self.assertTrue(causal_main_present(1, 2, True, True))
        self.assertTrue(causal_main_present(1, 0, True, True))
        self.assertFalse(causal_main_present(1, 1, True, False))
        self.assertFalse(causal_main_present(2, 2, True, True))

    def test_auxiliary_accepts_one_group_preview_despite_linked_global_preview(self):
        # Honor 400 Pro: one 1:1 auxiliary preview plus one linked page preview.
        global_delta = 2
        self.assertEqual(global_delta, 2)  # diagnostic only
        self.assertTrue(semantic_aux_preview_present(1))
        self.assertFalse(semantic_aux_preview_present(0))
        self.assertFalse(semantic_aux_preview_present(2))

    def test_old_images_cannot_satisfy_session_identity(self):
        self.assertTrue(session_uploads_present(4, 4, 3))
        self.assertFalse(session_uploads_present(3, 3, 3))
        self.assertFalse(session_uploads_present(4, 3, 3))

    def test_retry_only_returns_missing_auxiliary_images(self):
        # vivo S30 / Galaxy S23 Ultra: main + first two aux confirmed, only aux3 missing.
        self.assertEqual([2], missing_aux_indices(3, [1, 1, 1, 0]))
        self.assertEqual([], missing_aux_indices(3, [1, 1, 1, 1]))
        # A failed main must never make the auxiliary retry restart the main upload.
        self.assertEqual([0, 1, 2], missing_aux_indices(3, [0]))

    def test_retry_preserves_middle_failure_identity(self):
        # Find X7 Ultra: main, aux1, aux3 and aux4 succeeded; only aux2 failed.
        self.assertEqual([1], missing_aux_indices(4, [1, 1, 0, 1, 1]))

    def test_multiselect_error_is_probe_only(self):
        self.assertTrue(is_required_multiselect_error("请选择，最多选5项"))
        self.assertFalse(is_required_multiselect_error("可售库存不能小于0"))
