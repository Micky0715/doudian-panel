"""Pure retry rules for semantic model selection."""


def retry_same_keyword_after_modal(*, modal_found: bool, modal_closed: bool,
                                   retry_count: int, max_retries: int = 1) -> bool:
    """Retry the current exact keyword when a safe modal consumed its click.

    A modal interruption is an interaction failure, not evidence that the
    requested model is absent.  The caller must not advance to a broader or
    brand-prefixed keyword until the same keyword has received a real search.
    """
    return (
        bool(modal_found)
        and bool(modal_closed)
        and int(retry_count) < int(max_retries)
    )
