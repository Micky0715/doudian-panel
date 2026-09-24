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
