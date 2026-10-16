"""Stable data contracts for operator-maintained Gaia mapping files.

Parsing Excel/CSV and persisting versions belong to infrastructure.  This file
only defines the canonical records that the rest of the Gaia workflow accepts.
"""

from __future__ import annotations

from dataclasses import dataclass


class GaiaMappingContractError(ValueError):
    pass


@dataclass(frozen=True)
class ModelMappingRecord:
    brand: str
    model: str
    standard_sku_id: str
    category: str = "手机"
    # None means a legacy/operator mapping has no goods-level quality evidence.
    # An empty dict means the export was checked but has no supported goods.
    goods_by_quality: dict[str, list[str]] | None = None

    def goods_for_quality(self, quality_id: str) -> list[str] | None:
        if self.goods_by_quality is None:
            return None
        return list(self.goods_by_quality.get(str(quality_id), []))

    def __post_init__(self) -> None:
        if not str(self.brand or "").strip():
            raise GaiaMappingContractError("型号映射行缺少品牌")
        if not str(self.model or "").strip():
            raise GaiaMappingContractError("型号映射行缺少型号")
        if not str(self.standard_sku_id or "").strip():
            raise GaiaMappingContractError("型号映射行缺少 standardSkuId")


@dataclass(frozen=True)
class QualityMappingRecord:
    label: str
    dy_quality_id: str

    def __post_init__(self) -> None:
        if not str(self.label or "").strip():
            raise GaiaMappingContractError("成色映射缺少成色名称")
        if not str(self.dy_quality_id or "").strip():
            raise GaiaMappingContractError("成色映射缺少 dyQualityId")
