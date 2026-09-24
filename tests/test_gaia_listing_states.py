import unittest

from app.gaia_listing.domain.states import GaiaBatchState, can_transition


class GaiaListingStateTests(unittest.TestCase):
    def test_happy_path_is_explicit(self):
        path = [
            GaiaBatchState.SOURCE_SELECTED,
            GaiaBatchState.SOURCE_INSPECTED,
            GaiaBatchState.TEMPLATE_GENERATED,
            GaiaBatchState.VALIDATION_PASSED,
            GaiaBatchState.CONFIRMED,
            GaiaBatchState.GAIA_SUBMITTED,
            GaiaBatchState.GAIA_ACCEPTED,
            GaiaBatchState.WAITING_DOUDIAN_DRAFTS,
            GaiaBatchState.DOUDIAN_DRAFTS_MATCHED,
            GaiaBatchState.HANDED_OFF,
        ]
        for current, target in zip(path, path[1:]):
            self.assertTrue(can_transition(current, target))

    def test_generated_does_not_mean_submitted(self):
        self.assertFalse(can_transition("template_generated", "gaia_submitted"))

    def test_gaia_accepted_does_not_mean_doudian_matched(self):
        self.assertFalse(can_transition("gaia_accepted", "doudian_drafts_matched"))

    def test_doudian_matched_does_not_mean_published(self):
        with self.assertRaises(ValueError):
            can_transition("doudian_drafts_matched", "published")


if __name__ == "__main__":
    unittest.main()
