"""Fail-closed listing preflight decisions with no browser operations."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

from integrations.doudian.catalog import PhonePropertyCatalogue, PropertyCatalogueError
from model_catalogue import ModelCatalogue, canonical_brand

from .models import PreflightDecision
from .source_title import source_title


PHONE_CATEGORY_ID = "1000007628"
SCREEN_PROPERTY_ID = "6448"


def _decoded(value: Any) -> Any:
    current = value
    for _ in range(2):
        if not isinstance(current, str):
            return current
        try:
            current = json.loads(current)
        except (TypeError, ValueError):
            return value
    return current


def extract_property_selection(detail: Mapping[str, Any], property_id: str | int) -> Mapping[str, Any] | None:
    expected = str(property_id)
    for field in ("product_format_new", "product_format"):
        value = _decoded(detail.get(field))
        if not isinstance(value, Mapping):
            continue
        selected = value.get(expected)
        if isinstance(selected, list) and len(selected) == 1 and isinstance(selected[0], Mapping):
            return dict(selected[0])
    return None


class ListingPreflightInspector:
    def __init__(
        self,
        *,
        resolver: Callable[[str], Mapping[str, Any]],
        standard_catalogue: ModelCatalogue,
        platform_catalogue: ModelCatalogue,
        property_catalogue: PhonePropertyCatalogue,
        image_inspector,
        detail_image_inspector,
        reviewed_screen_shapes: list[Mapping[str, Any]] | None = None,
    ):
        self.resolver = resolver
        self.standard_catalogue = standard_catalogue
        self.platform_catalogue = platform_catalogue
        self.property_catalogue = property_catalogue
        self.image_inspector = image_inspector
        self.detail_image_inspector = detail_image_inspector
        self.reviewed_screen_shapes = [dict(row) for row in (reviewed_screen_shapes or [])]

    def inspect(self, item: Mapping[str, Any], detail: Mapping[str, Any]) -> PreflightDecision:
        product_id = str(item.get("product_id") or "")
        name = str(item.get("name") or "")
        category_id = str(item.get("category_leaf_id") or "")
        if category_id != PHONE_CATEGORY_ID:
            return PreflightDecision(
                product_id=product_id,
                name=name,
                decision="out_of_scope",
                category_id=category_id,
                reasons=({"code": "category_out_of_scope", "message": "非二手手机类目"},),
            )

        resolved = dict(self.resolver(source_title({**detail, "name": name})))
        brand = str(resolved.get("target_brand_cn") or "").strip()
        model = str(resolved.get("model") or "").strip()
        condition = str(resolved.get("condition") or "").strip()
        reasons: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        if not brand:
            reasons.append({"code": "brand_unresolved", "message": "标题无法唯一解析品牌"})
        if not model:
            reasons.append({"code": "model_unresolved", "message": "标题无法唯一解析型号"})

        standard_hit = self.standard_catalogue.resolve(brand, model) if brand and model else None
        if not standard_hit:
            reasons.append({"code": "standard_model_missing", "message": "盖亚标准型号库无唯一匹配"})

        platform_model = ""
        if brand and model:
            exact = str(resolved.get("target_model_exact") or "").strip()
            if exact and self._platform_has_exact(brand, exact):
                platform_model = exact
            else:
                platform_hit = self.platform_catalogue.resolve(brand, model)
                if platform_hit:
                    platform_model = str(platform_hit.get("model") or "")
        if not platform_model:
            reasons.append({"code": "platform_model_missing", "message": "抖店型号库无唯一精确匹配"})

        assets = self.image_inspector.inspect(brand, model) if brand and model else {
            "ok": False, "image_dir": "", "main_count": 0, "auxiliary_count": 0,
            "reasons": ["image_lookup_skipped"],
        }
        for code in assets.get("reasons", []):
            reasons.append({"code": str(code), "message": self._asset_message(str(code))})

        detail_assets = self.detail_image_inspector.inspect(condition)
        for code in detail_assets.get("reasons", []):
            reasons.append({"code": str(code), "message": self._asset_message(str(code))})

        screen_name = self._screen_shape(brand, model, detail)
        if not screen_name:
            warnings.append({
                "code": "screen_shape_unresolved",
                "message": "屏幕形态未填写；按运营规则本件只允许保存草稿",
            })

        execution_mode = "" if reasons else ("publish" if screen_name else "save_draft")

        return PreflightDecision(
            product_id=product_id,
            name=name,
            decision="ready" if not reasons else "manual_required",
            category_id=category_id,
            brand=brand,
            model=model,
            platform_model=platform_model,
            screen_shape=screen_name,
            image_dir=str(assets.get("image_dir") or ""),
            main_image_count=int(assets.get("main_count") or 0),
            auxiliary_image_count=int(assets.get("auxiliary_count") or 0),
            condition=condition,
            detail_image_count=int(detail_assets.get("count") or 0),
            execution_mode=execution_mode,
            warnings=tuple(warnings),
            reasons=tuple(reasons),
        )

    def _screen_shape(self, brand: str, model: str, detail: Mapping[str, Any]) -> str:
        existing = extract_property_selection(detail, SCREEN_PROPERTY_ID)
        if existing:
            name = str(existing.get("name") or "").strip()
            value = str(existing.get("value") or "").strip()
            prop = self.property_catalogue.get_property("screen_shape")
            options = [row for row in prop.get("options", []) if isinstance(row, Mapping)]
            matches = [
                row for row in options
                if (name and str(row.get("name") or "").strip().casefold() == name.casefold())
                or (value and str(row.get("value_id") or "") == value)
            ]
            if len(matches) == 1:
                return str(matches[0]["name"])

        matches = [
            row for row in self.reviewed_screen_shapes
            if canonical_brand(row.get("brand")) == canonical_brand(brand)
            and self._model_key(row.get("model")) == self._model_key(model)
        ]
        if len(matches) != 1:
            return ""
        try:
            option = self.property_catalogue.resolve_option(
                "screen_shape", str(matches[0].get("screen_shape") or "")
            )
        except PropertyCatalogueError:
            return ""
        return str(option["name"])

    def _platform_has_exact(self, brand: str, expected: str) -> bool:
        key = " ".join(expected.split()).casefold()
        return any(
            canonical_brand(row.get("brand")) == canonical_brand(brand)
            and " ".join(str(row.get("model") or "").split()).casefold() == key
            for row in self.platform_catalogue.rows
        )

    @staticmethod
    def _model_key(value: Any) -> str:
        return re.sub(r"[\s()（）·._/，,-]+", "", str(value or "")).casefold()

    @staticmethod
    def _asset_message(code: str) -> str:
        return {
            "shop_image_root_missing": "店铺图片根目录不存在",
            "model_image_dir_missing": "当前店铺没有该型号图片目录",
            "invalid_nested_image_layout": "型号图片被错误嵌套在主图/白底图目录内",
            "ambiguous_model_image_dirs": "当前店铺存在多个同型号图片目录",
            "ambiguous_main_images": "主图目录有多张非标准命名图片",
            "main_image_missing": "缺少可唯一确认的主图",
            "auxiliary_images_insufficient": "去重后白底图数量不足",
            "image_lookup_skipped": "品牌或型号未解析，未检查图片",
            "detail_image_root_missing": "共享商详图片目录不存在",
            "condition_unresolved": "标题和名称前缀中无法解析成色",
            "condition_image_missing": "共享商详图库缺少对应成色图",
            "condition_image_ambiguous": "共享商详图库存在多个同名成色图",
            "detail_image_count_invalid": "商详图数量不符合当前成色要求（准新品13张，其他成色14张）",
        }.get(code, code)


def load_reviewed_screen_shapes(path: str | Path) -> list[Mapping[str, Any]]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(value.get("schema_version") or 0) != 1 or not isinstance(value.get("models"), list):
        raise ValueError("invalid reviewed model property mapping")
    return value["models"]
