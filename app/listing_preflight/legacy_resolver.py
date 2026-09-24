"""Narrow adapter to the current single brand/model resolver.

The browser automation module remains the current source of truth.  Only this
adapter may import it; the rest of preflight is independent and pure.
"""

from __future__ import annotations

from typing import Any, Mapping


def resolve_brand_model(title: str) -> Mapping[str, Any]:
    from test_doudian_batch import resolve_brand_model as legacy_resolve

    return legacy_resolve(title)
