from enum import Enum


class GaiaBatchState(str, Enum):
    SOURCE_SELECTED = "source_selected"
    SOURCE_INSPECTED = "source_inspected"
    TEMPLATE_GENERATED = "template_generated"
    VALIDATION_PASSED = "validation_passed"
    CONFIRMED = "confirmed"
    GAIA_SUBMITTED = "gaia_submitted"
    GAIA_ACCEPTED = "gaia_accepted"
    WAITING_DOUDIAN_DRAFTS = "waiting_doudian_drafts"
    DOUDIAN_DRAFTS_MATCHED = "doudian_drafts_matched"
    HANDED_OFF = "handed_off"
    VALIDATION_FAILED = "validation_failed"
    GAIA_REJECTED = "gaia_rejected"
    SYNC_TIMEOUT = "sync_timeout"
    MANUAL_REQUIRED = "manual_required"
    CANCELLED = "cancelled"


FORWARD_TRANSITIONS = {
    GaiaBatchState.SOURCE_SELECTED: {GaiaBatchState.SOURCE_INSPECTED, GaiaBatchState.CANCELLED},
    GaiaBatchState.SOURCE_INSPECTED: {
        GaiaBatchState.TEMPLATE_GENERATED,
        GaiaBatchState.VALIDATION_FAILED,
        GaiaBatchState.CANCELLED,
    },
    GaiaBatchState.TEMPLATE_GENERATED: {
        GaiaBatchState.VALIDATION_PASSED,
        GaiaBatchState.VALIDATION_FAILED,
        GaiaBatchState.CANCELLED,
    },
    GaiaBatchState.VALIDATION_FAILED: {
        GaiaBatchState.SOURCE_INSPECTED,
        GaiaBatchState.CANCELLED,
    },
    GaiaBatchState.VALIDATION_PASSED: {GaiaBatchState.CONFIRMED, GaiaBatchState.CANCELLED},
    GaiaBatchState.CONFIRMED: {GaiaBatchState.GAIA_SUBMITTED, GaiaBatchState.CANCELLED},
    GaiaBatchState.GAIA_SUBMITTED: {
        GaiaBatchState.GAIA_ACCEPTED,
        GaiaBatchState.GAIA_REJECTED,
        GaiaBatchState.MANUAL_REQUIRED,
    },
    GaiaBatchState.GAIA_ACCEPTED: {
        GaiaBatchState.WAITING_DOUDIAN_DRAFTS,
        GaiaBatchState.MANUAL_REQUIRED,
    },
    GaiaBatchState.WAITING_DOUDIAN_DRAFTS: {
        GaiaBatchState.DOUDIAN_DRAFTS_MATCHED,
        GaiaBatchState.SYNC_TIMEOUT,
        GaiaBatchState.MANUAL_REQUIRED,
    },
    GaiaBatchState.DOUDIAN_DRAFTS_MATCHED: {
        GaiaBatchState.HANDED_OFF,
        GaiaBatchState.MANUAL_REQUIRED,
    },
}


def can_transition(current, target):
    current = GaiaBatchState(current)
    target = GaiaBatchState(target)
    return target in FORWARD_TRANSITIONS.get(current, set())
