"""Pure safety policy for semantic image-target selection and verification."""


def unique_main_candidate(candidate_count: int) -> bool:
    """A main-image input is actionable only when semantic resolution is unique."""
    return int(candidate_count) == 1


def needs_filechooser_fallback(input_file_count: int) -> bool:
    """Use the visible semantic upload trigger when direct input injection is reset."""
    return int(input_file_count) != 1


def strict_main_present(candidate_count: int, found: bool, present: bool) -> bool:
    """Loose wrapper evidence can never override an absent semantic main slot."""
    return unique_main_candidate(candidate_count) and bool(found) and bool(present)


def exact_main_slot_binding(main_input_index: int, preview_index: int,
                            field_delta_count: int, preview_present: bool) -> bool:
    """Prove the preview replaced the exact semantic main-input slot."""
    return (
        int(main_input_index) == 0
        and int(preview_index) == int(main_input_index)
        and int(field_delta_count) == 1
        and bool(preview_present)
    )


def causal_main_present(candidate_count: int, upload_delta_count: int,
                        bound_found: bool, bound_present: bool) -> bool:
    """The semantic main slot is authoritative; global preview deltas are diagnostic.

    A single main upload may legitimately create linked 1:1 and 3:4 previews.  Once
    the upload target was uniquely resolved, success therefore depends on the
    semantic main slot containing a preview, not on the page-wide delta being 1.
    ``upload_delta_count`` remains in the signature for logging/regression data.
    """
    return (unique_main_candidate(candidate_count)
            and bool(bound_found) and bool(bound_present))


def semantic_aux_preview_present(group_delta_count: int) -> bool:
    """An auxiliary upload succeeds only with one new preview in its 1:1 group.

    The platform may create additional linked previews elsewhere on the page.
    Those page-wide deltas are diagnostic and must neither reject nor approve
    the upload; only the semantic auxiliary group is authoritative.
    """
    return int(group_delta_count) == 1


def session_uploads_present(expected_count: int, present_count: int,
                            valid_aux_count: int) -> bool:
    """Every main/aux preview accepted must be identified as created this session."""
    required = 1 + min(4, max(0, int(valid_aux_count)))
    return int(expected_count) == required and int(present_count) == required


def authoritative_upload_verdict(main_candidate_count: int,
                                  main_found: bool,
                                  main_present: bool,
                                  session_expected_count: int,
                                  session_present_count: int,
                                  valid_aux_count: int) -> bool:
    """Single authoritative upload verdict.

    Deliberately accepts no delta, wrapper, field-level or fallback evidence.
    Therefore a caller cannot make those diagnostics override an explicitly
    absent required main slot without changing this safety boundary and tests.
    """
    return (
        strict_main_present(main_candidate_count, main_found, main_present)
        and session_uploads_present(
            session_expected_count, session_present_count, valid_aux_count
        )
    )


def missing_aux_indices(valid_aux_count: int, upload_deltas) -> list[int]:
    """Return exact auxiliary indexes whose original upload produced no preview.

    ``upload_deltas`` is positional: main first, then aux1..aux4.  Counting
    confirmed previews loses the identity of a middle failure (for example
    ``[1, 1, 0, 1, 1]``); it would incorrectly retry aux4 instead of aux2.
    The main entry is deliberately ignored so retry can never restart it.
    """
    required_aux = min(4, max(0, int(valid_aux_count)))
    deltas = list(upload_deltas or [])
    aux_deltas = deltas[1:1 + required_aux]
    return [index for index in range(required_aux)
            if index >= len(aux_deltas) or int(aux_deltas[index] or 0) != 1]


def is_required_multiselect_error(reason: str) -> bool:
    text = str(reason or "")
    return "最多选" in text or "请选择" in text
