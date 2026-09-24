from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ExecutionItem:
    product_id: str
    name: str
    brand: str
    model: str
    platform_model: str
    screen_shape: str
    condition: str
    image_dir: str
    main_image_count: int
    auxiliary_image_count: int
    detail_image_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionPlan:
    schema_version: int
    shop_id: str
    action: str
    created_at: int
    expires_at: int
    source_report: str
    source_report_sha256: str
    items: tuple[ExecutionItem, ...]
    checksum: str = ""

    def to_dict(self, *, include_checksum: bool = True) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [item.to_dict() for item in self.items]
        if not include_checksum:
            value.pop("checksum", None)
        return value
