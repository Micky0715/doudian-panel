"""Read-only construction of DouDian's mandatory recommended title prefix.

DouDian does not accept a prefix copied from ``product.detail`` as proof that
the current product properties still satisfy its title rule.  Immediately
before submission we query the category rule and then ask the platform to
recompute the prefix from the exact properties that will be submitted.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping, Protocol

from doudian_product_service import normalize_product_format_for_edit, parse_product_format


class _Gateway(Protocol):
    def call(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


class TitleRecommendationError(ValueError):
    pass


@dataclass(frozen=True)
class RecommendedTitlePlan:
    name: str
    name_prefix: str
    property_ids: tuple[str, ...]
    selected_properties: Mapping[str, list[dict[str, Any]]]


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes"}


def _data(response: Mapping[str, Any]) -> Mapping[str, Any]:
    value = response.get("data")
    if not isinstance(value, Mapping):
        raise TitleRecommendationError("平台标题规则响应缺少 data")
    return value


def _category_request(detail: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    category = detail.get("category_detail")
    if not isinstance(category, Mapping):
        raise TitleRecommendationError("商品缺少类目路径")
    levels = (
        ("first_cid", "first_cname"),
        ("second_cid", "second_cname"),
        ("third_cid", "third_cname"),
    )
    params: dict[str, Any] = {}
    leaf = ""
    for cid_key, name_key in levels:
        raw = str(category.get(cid_key) or "0").strip()
        cid = int(raw) if raw.isdigit() else 0
        params[cid_key] = cid
        params[cid_key.replace("cid", "cid_name")] = str(category.get(name_key) or "")
        if cid > 0:
            leaf = str(cid)
    fourth = str(category.get("fourth_cid") or "0").strip()
    if fourth.isdigit() and int(fourth) > 0:
        raise TitleRecommendationError("当前标题推荐适配器尚不支持四级类目")
    if not leaf:
        raise TitleRecommendationError("商品缺少有效末级类目 ID")
    return leaf, params


def _property_ids(rule: Mapping[str, Any]) -> tuple[str, ...]:
    value = rule.get("property_ids")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TitleRecommendationError("标题规则 property_ids 不是有效 JSON") from exc
    if not isinstance(value, (list, tuple)):
        raise TitleRecommendationError("标题规则缺少 property_ids")
    result = tuple(str(item).strip() for item in value if str(item).strip())
    if not result or any(not item.isdigit() for item in result):
        raise TitleRecommendationError("标题规则 property_ids 无效")
    return result


def _brand_name_from_existing_prefix(detail: Mapping[str, Any], properties: Mapping[str, Any],
                                     ids: tuple[str, ...]) -> str:
    """Recover the custom brand label used by DouDian's virtual property 1687.

    For this second-hand-phone category the detail API exposes the standard
    brand id separately and omits 1687 from ``product_format_new``. The title
    recommendation endpoint nevertheless requires a custom-brand entry with
    value 0; the currently stored platform prefix is the only authoritative
    source for its bilingual display label.
    """
    remaining = str(detail.get("name_prefix") or "").strip()
    for property_id in ids:
        if property_id == "1687":
            continue
        entries = properties.get(property_id)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            name = str(entry.get("name") or "").strip() if isinstance(entry, Mapping) else ""
            if name and remaining.startswith(name):
                remaining = remaining[len(name):].strip()
                break
    if not remaining:
        raise TitleRecommendationError("标题前缀缺少品牌属性 1687 的名称")
    return remaining


def _selected_properties(detail: Mapping[str, Any], ids: tuple[str, ...],
                         rule: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    properties = normalize_product_format_for_edit(parse_product_format(detail.get("product_format_new")))
    selected: dict[str, list[dict[str, Any]]] = {}
    for property_id in ids:
        entries = properties.get(property_id)
        if not isinstance(entries, list) or not entries:
            if property_id == "1687" and _truthy(rule.get("prefix_prop_has_brand")):
                selected[property_id] = [{
                    "value": 0,
                    "name": _brand_name_from_existing_prefix(detail, properties, ids),
                }]
                continue
            raise TitleRecommendationError("标题前缀所需属性缺失：" + property_id)
        values: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise TitleRecommendationError("标题属性结构异常：" + property_id)
            value = entry.get("value")
            name = str(entry.get("name") or "").strip()
            if value in (None, "") or not name:
                raise TitleRecommendationError("标题属性值不完整：" + property_id)
            if isinstance(value, str) and value.isdigit():
                value = int(value)
            values.append({"value": value, "name": name})
        selected[property_id] = values
    return selected


def _recommendations(value: Any) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        scene = value.get("recommend_scene")
        result = value.get("recommend_value")
        if isinstance(scene, str) and isinstance(result, str):
            found.append((scene, result))
        for key, child in value.items():
            if key in {"recommend_scene", "recommend_value"}:
                continue
            if key == "product_name_prefix" and isinstance(child, str):
                found.append((key, child))
            else:
                found.extend(_recommendations(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_recommendations(child))
    return found


def _base_name(detail: Mapping[str, Any], recommended_prefix: str) -> str:
    name = str(detail.get("name") or "").strip()
    if not name:
        raise TitleRecommendationError("商品标题为空")
    prefixes = (recommended_prefix.rstrip(), str(detail.get("name_prefix") or "").strip())
    for prefix in prefixes:
        if prefix and name.startswith(prefix):
            name = name[len(prefix):].lstrip()
            break
    if not name:
        raise TitleRecommendationError("去除旧前缀后商品标题为空")
    return name


def recommend_title_for_submission(gateway: _Gateway, detail: Mapping[str, Any]) -> RecommendedTitlePlan:
    """Return the exact platform prefix and full ``name`` for one submission."""
    leaf, category_params = _category_request(detail)
    rule_params: dict[str, Any] = {"category_id": int(leaf)}
    for source, target in (("standard_brand_id", "standard_brand_id"), ("spu_id", "spu_id")):
        value = str(detail.get(source) or "").strip()
        if value.isdigit() and int(value) > 0:
            rule_params[target] = int(value)
    rule_data = _data(gateway.call("product.getProductUpdateRule", rule_params))
    rule = rule_data.get("recommend_name_rule")
    if not isinstance(rule, Mapping) or not _truthy(rule.get("satisfy_prefix")):
        raise TitleRecommendationError("当前类目未返回可用的推荐标题前缀规则")
    ids = _property_ids(rule)
    selected = _selected_properties(detail, ids, rule)
    request = {
        "scene": ["product_name_prefix"],
        "category_leaf_id": int(leaf),
        **category_params,
        "select_property": selected,
        "use_brand_name": False,
    }
    recommend_data = _data(gateway.call("product.getRecommendName", request))
    candidates = [value for scene, value in _recommendations(recommend_data)
                  if scene == "product_name_prefix" and value]
    if len(set(candidates)) != 1:
        raise TitleRecommendationError("平台未返回唯一的商品标题前缀")
    prefix = candidates[0]
    if not prefix.strip() or not prefix.endswith(" "):
        raise TitleRecommendationError("平台推荐前缀未保留必需的末尾空格")
    name = prefix + _base_name(detail, prefix)
    return RecommendedTitlePlan(name, prefix, ids, selected)
