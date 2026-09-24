"""Versioned, failure-closed repository for Gaia quality IDs.

The file is intentionally separate from the Excel template and model mapping
table: quality labels are task inputs, while Gaia import codes are controlled
configuration. These are NOT Doudian property 3320 option IDs. A missing ID is
an error, never a fallback value.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from ..domain.mapping_contract import GaiaMappingContractError, QualityMappingRecord


class QualityMappingRepository:
    """Read the approved label -> dyQualityId mapping from one JSON file."""

    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Dict[str, QualityMappingRecord]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GaiaMappingContractError("无法读取成色映射配置") from exc

        records: Dict[str, QualityMappingRecord] = {}
        seen_labels = set()
        for item in payload.get("qualities") or []:
            label = str(item.get("label") or "").strip()
            if not label:
                raise GaiaMappingContractError("成色映射存在空的成色名称")
            if label in seen_labels:
                raise GaiaMappingContractError("成色映射重复：%s" % label)
            seen_labels.add(label)
            dy_quality_id = str(item.get("dy_quality_id") or "").strip()
            if not dy_quality_id:
                continue
            record = QualityMappingRecord(label=label, dy_quality_id=dy_quality_id)
            records[label] = record
        return records

    def get_required(self, label: str) -> QualityMappingRecord:
        normalized = str(label or "").strip()
        if not normalized:
            raise GaiaMappingContractError("请选择成色")
        records = self.load()
        if normalized not in records:
            raise GaiaMappingContractError("成色“%s”未配置准确 dyQualityId" % normalized)
        return records[normalized]
