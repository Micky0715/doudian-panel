"""Audited manufacturer-site model names used only as a safe tie-breaker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from model_catalogue import canonical_brand, model_key

from .domain import MaterialPolicyError


@dataclass(frozen=True)
class OfficialModelRecord:
    brand: str
    lookup_models: tuple[str, ...]
    official_name: str
    source_url: str
    verified_at: str


class OfficialModelRegistry:
    """Resolve only reviewed records carrying primary-source evidence.

    Critical listing jobs never scrape arbitrary search results at write time.
    An official-site lookup is reviewed once, stored with its URL/date, and
    then becomes deterministic evidence for later unattended batches.
    """

    def __init__(self, records: Iterable[OfficialModelRecord] = ()):
        self.records = tuple(records)

    @classmethod
    def bundled(cls) -> "OfficialModelRegistry":
        return cls.from_json(Path(__file__).with_name("official_models.json"))

    @classmethod
    def from_json(cls, path: str | Path) -> "OfficialModelRegistry":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = payload.get("models") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            raise MaterialPolicyError("official model registry must contain a models list")
        records: list[OfficialModelRecord] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise MaterialPolicyError("official model registry row must be an object")
            brand = str(row.get("brand") or "").strip()
            official_name = str(row.get("official_name") or "").strip()
            source_url = str(row.get("source_url") or "").strip()
            verified_at = str(row.get("verified_at") or "").strip()
            lookup = tuple(str(value).strip() for value in row.get("lookup_models", ())
                           if str(value).strip())
            if (not brand or not official_name or not lookup
                    or not source_url.startswith("https://") or not verified_at):
                raise MaterialPolicyError("official model registry row is incomplete")
            row_keys = {
                (canonical_brand(brand), model_key(value))
                for value in (*lookup, official_name)
            }
            for key in row_keys:
                if key in seen:
                    raise MaterialPolicyError("official model registry contains duplicate lookup keys")
                seen.add(key)
            records.append(OfficialModelRecord(
                brand, lookup, official_name, source_url, verified_at,
            ))
        return cls(records)

    def resolve(self, brand: Any, model: Any) -> OfficialModelRecord | None:
        expected = (canonical_brand(brand), model_key(model))
        matches = [
            record for record in self.records
            if any(
                (canonical_brand(record.brand), model_key(value)) == expected
                for value in (*record.lookup_models, record.official_name)
            )
        ]
        if len(matches) > 1:
            raise MaterialPolicyError("官网型号证据存在重复记录，拒绝猜测")
        return matches[0] if matches else None
