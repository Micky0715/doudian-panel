"""Build one stable source title from DouDian's split title fields."""

from __future__ import annotations

import re
from typing import Any, Mapping

from brand_model_rules import platform_brand_for_title


_CONDITION_RE = re.compile(r"(99新|95新|90新|9新|8成新|9成新)", re.I)


def source_title(detail: Mapping[str, Any]) -> str:
    """Combine ``name_prefix`` and ``name`` without duplicating brand text.

    DouDian drafts are inconsistent: some ``name`` values already contain the
    rendered prefix while others contain only the editable title.  When the
    name already identifies a platform brand, repeating the full prefix can
    turn a valid model into text such as ``99新 / Magic8 Pro``.  In that case
    only a missing condition is carried over from the prefix.
    """
    prefix = str(detail.get("name_prefix") or "").strip()
    name = str(detail.get("name") or "").strip()
    if not prefix:
        return name
    if not name:
        return prefix
    if name.casefold().startswith(prefix.casefold()):
        return name
    if platform_brand_for_title(name):
        if _CONDITION_RE.search(name):
            return name
        condition = _CONDITION_RE.search(prefix)
        return " ".join(part for part in (
            condition.group(1) if condition else "", name,
        ) if part)
    return f"{prefix} {name}"
