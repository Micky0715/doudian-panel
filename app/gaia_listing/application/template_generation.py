"""Pure orchestration from validated mapping records to Gaia template rows."""

from __future__ import annotations

from typing import Iterable, List

from ..domain.mapping_contract import ModelMappingRecord, QualityMappingRecord
from ..domain.template_contract import GaiaTemplateRow


def build_template_rows(*, scene: int | str, models: Iterable[ModelMappingRecord],
                        quality: QualityMappingRecord | Iterable[QualityMappingRecord]) -> List[GaiaTemplateRow]:
    """Build rows only; file I/O belongs to ``template_writer``."""
    rows = []
    seen_sku_ids = set()
    qualities = [quality] if isinstance(quality, QualityMappingRecord) else list(quality)
    if not qualities or len({item.label for item in qualities}) != len(qualities):
        raise ValueError("请选择不重复的成色")
    for model in models:
        sku_id = str(model.standard_sku_id).strip()
        if sku_id in seen_sku_ids:
            raise ValueError("型号映射表存在重复标品ID：%s" % sku_id)
        seen_sku_ids.add(sku_id)
        for selected_quality in qualities:
            rows.append(GaiaTemplateRow(
                scene=str(scene),
                standard_sku_id=sku_id,
                dy_quality_id=selected_quality.dy_quality_id,
                category=model.category,
            ))
    if not rows:
        raise ValueError("没有可生成的型号映射记录")
    return rows
