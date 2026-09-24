"""Resolve Gaia catalogue models with the shared local-gallery rules.

One unambiguous phone model expands to all of its catalogue SKU variants.
Different brands/models remain ambiguous; no variant is picked at random.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from brand_model_rules import model_exact_overrides
from model_catalogue import BRAND_GROUPS, canonical_brand, model_key, versionless_model_candidates
import unicodedata

from ..domain.mapping_contract import ModelMappingRecord


@dataclass(frozen=True)
class ModelResolution:
    strategy: str
    records: tuple[ModelMappingRecord, ...]

    def as_dict(self) -> dict:
        identities = {(canonical_brand(item.brand), model_key(item.model), item.category)
                      for item in self.records}
        return {
            "strategy": self.strategy,
            "candidate_count": len(self.records),
            "unique_sku": len(self.records) == 1,
            "unique_model": len(identities) == 1,
            "model_count": len(identities),
            "candidates": [
                {
                    "category": item.category,
                    "brand": item.brand,
                    "model": item.model,
                    "standard_sku_id": item.standard_sku_id,
                }
                for item in self.records
            ],
        }


def _stable_unique(records: Iterable[ModelMappingRecord]) -> tuple[ModelMappingRecord, ...]:
    result: list[ModelMappingRecord] = []
    seen: set[str] = set()
    for record in records:
        if record.standard_sku_id not in seen:
            seen.add(record.standard_sku_id)
            result.append(record)
    return tuple(result)


def resolve_model_records(records: Iterable[ModelMappingRecord], *, brand: str,
                          model: str) -> ModelResolution:
    """Apply the same exact/alias/version-suffix order as local image lookup."""
    catalogue = tuple(records)
    brand_key = canonical_brand(brand)
    expected = model_key(model)
    if not brand_key or not expected:
        return ModelResolution("unresolved", ())
    brand_rows = tuple(
        item for item in catalogue if canonical_brand(item.brand) == brand_key
    )

    exact = _stable_unique(item for item in brand_rows if model_key(item.model) == expected)
    if exact:
        return ModelResolution("exact", exact)

    source_aliases = {
        model_key(source)
        for source, target in model_exact_overrides().items()
        if model_key(target) == expected and model_key(source) != expected
    }
    aliases = _stable_unique(
        item for item in brand_rows if model_key(item.model) in source_aliases
    )
    if aliases:
        return ModelResolution("rule_alias", aliases)

    base_keys = {model_key(value) for value in versionless_model_candidates(model)}
    versionless = _stable_unique(
        item for item in brand_rows if model_key(item.model) in base_keys
    )
    if versionless:
        return ModelResolution("version_suffix_removed", versionless)
    return ModelResolution("unresolved", ())


def resolve_model_records_any_brand(records: Iterable[ModelMappingRecord], *,
                                    model: str) -> ModelResolution:
    """Resolve an operator-entered model when the brand was omitted.

    The same conservative resolver is evaluated once per catalogue brand.  We
    keep only the strongest strategy found across all brands and still return
    every distinct SKU candidate, so a cross-brand collision is visible to the
    operator instead of being guessed.
    """
    catalogue = tuple(records)
    # A typed brand prefix is an explicit restriction, not decoration. Without
    # this, "小米 15" also matches "一加 15" after model_key strips the brand.
    normalized = unicodedata.normalize("NFKC", model).strip().casefold()
    for alias in sorted({name for names in BRAND_GROUPS.values() for name in names},
                        key=len, reverse=True):
        prefix = unicodedata.normalize("NFKC", alias).casefold()
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            remainder = normalized[len(prefix):]
            if prefix.isascii() and remainder[0].isalpha():
                continue
            return resolve_model_records(catalogue, brand=alias, model=model)
    brands = tuple(dict.fromkeys(item.brand for item in catalogue if item.brand.strip()))
    ranked = {"exact": 0, "rule_alias": 1, "version_suffix_removed": 2}
    matches: list[tuple[int, ModelResolution]] = []
    for brand in brands:
        result = resolve_model_records(catalogue, brand=brand, model=model)
        if result.records:
            matches.append((ranked.get(result.strategy, 99), result))
    if not matches:
        return ModelResolution("unresolved", ())
    best_rank = min(rank for rank, _ in matches)
    best = [result for rank, result in matches if rank == best_rank]
    records_at_best_rank = _stable_unique(
        record for result in best for record in result.records
    )
    strategy = best[0].strategy
    return ModelResolution("global_%s" % strategy, records_at_best_rank)


def resolve_standard_sku(records: Iterable[ModelMappingRecord], standard_sku_id: str) -> ModelResolution:
    expected = str(standard_sku_id or "").strip()
    matches = _stable_unique(
        item for item in records if item.standard_sku_id == expected
    )
    return ModelResolution("standard_sku_id" if matches else "unresolved", matches)


def resolve_operator_models(records: Iterable[ModelMappingRecord], rows: list) -> dict:
    """Expand only the explicitly requested, unambiguous phone models."""
    if not isinstance(rows, list) or not rows or len(rows) > 2000:
        raise ValueError("请填写 1～2000 个手机型号")
    phones = tuple(item for item in records if item.category.strip() == "手机")
    results, sku_ids = [], []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict) or not str(row.get("model") or "").strip():
            raise ValueError("第 %s 行缺少手机型号" % index)
        brand, model = (str(row.get(key) or "").strip() for key in ("brand", "model"))
        result = (resolve_model_records(phones, brand=brand, model=model) if brand else
                  resolve_model_records_any_brand(phones, model=model))
        payload = result.as_dict()
        payload.update(index=index, brand_input=brand, model_input=model)
        results.append(payload)
        if payload["unique_model"]:
            sku_ids.extend(item.standard_sku_id for item in result.records)
    deduplicated = list(dict.fromkeys(sku_ids))
    ready = all(item["unique_model"] for item in results) and len(deduplicated) <= 2000
    return {
        "success": ready,
        "all_unique": all(item["unique_sku"] for item in results),
        "all_models_resolved": all(item["unique_model"] for item in results),
        "duplicate_skus": len(sku_ids) != len(deduplicated),
        "deduplicated_count": len(sku_ids) - len(deduplicated),
        "selected_sku_ids": deduplicated if ready else [],
        "selected_sku_count": len(deduplicated) if ready else 0,
        "results": results,
        "message": ("已匹配全部型号，去重后共 %s 个国行标品 ID" % len(deduplicated) if ready else
                    "匹配超过 2000 个标品 ID，请分批填写型号" if len(deduplicated) > 2000 else
                    "存在未找到或跨品牌/型号歧义，请核对标出的型号"),
    }
