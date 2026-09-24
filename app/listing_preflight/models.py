from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PreflightDecision:
    product_id: str
    name: str
    decision: str
    category_id: str
    brand: str = ""
    model: str = ""
    platform_model: str = ""
    screen_shape: str = ""
    image_dir: str = ""
    main_image_count: int = 0
    auxiliary_image_count: int = 0
    condition: str = ""
    detail_image_count: int = 0
    execution_mode: str = ""
    warnings: tuple[dict[str, str], ...] = ()
    reasons: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["warnings"] = list(self.warnings)
        value["reasons"] = list(self.reasons)
        return value


@dataclass(frozen=True)
class PreflightReport:
    schema_version: int
    shop_id: str
    captured_at: int
    source_snapshot: str
    ready: int
    manual_required: int
    out_of_scope: int
    decisions: tuple[PreflightDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["decisions"] = [item.to_dict() for item in self.decisions]
        return value
