"""One-product model and media transaction with optional verified publish.

Main and white images must come from the target shop's exact model directory.
Colour images come from the shared SKU gallery and grades from 手机商详.
Existing detail URLs retain their order after the explicit legacy/grade-image
policy is applied.
No SKU structure or price/stock field is sent to editV2.  The default remains
dry-run; ``--execute`` saves and verifies a draft, while ``--publish`` adds one
immutable ``commit=true`` submission with bounded safe retries and an
online-state readback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Mapping

from app.grade_detail_images import build_grade_only_plan
from app.listing_preflight import ImageAssetInspector
from app.listing_preflight.legacy_resolver import resolve_brand_model
from app.material_pipeline import (
    CloudMaterialExecutor, FeishuMaterialCatalog, FeishuMaterialLayout,
    build_material_plan,
)
from app.material_pipeline.cache import MaterialMetadataCache
from app.sku_color_images import ColorGalleryError, resolve_cloud_color_images
from doudian_api import (
    DoudianAPIError, MODEL_PROPERTY_ID, find_property, read_env_file,
    resolve_model_option,
)
from doudian_product_service import (
    build_property_edit_payload, normalize_product_format_for_edit, parse_product_format,
)
from integrations.doudian import (
    DoudianProductReader, DoudianReadOnlyGateway, recommend_title_for_submission,
)
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.doudian.write_gateway import (
    DoudianSingleProductFullMediaDraftWriter,
    DoudianSingleProductPublisher,
    submission_payload_sha256,
)
from integrations.feishu_drive import FeishuDriveClient, FeishuDriveConfig
from scripts.doudian_apply_local_images import (
    _pic_urls, build_dynamic_main_area_plan, detail_urls,
)
from scripts.doudian_grade_detail_draft_once import (
    compose_detail_urls, select_existing_detail_urls, validate_existing_detail_urls,
)
from scripts.doudian_edit_phone_properties_batch import (
    SCREEN_PROPERTY_ID, _model_option, _screen_option, model_option_suggestions,
)
from scripts.doudian_sku_color_draft_once import (
    DEFAULT_GALLERY, GUARDED_FIELDS, _platform_path, _product_id,
    SkuColorDraftError, _source_fingerprint, _source_plan, _source_title,
    _stable, _write_json,
)


class FullMediaCanaryError(RuntimeError):
    pass


def _versionless_cloud_color_issue(main_plan: Any, color_plan_complete: bool) -> str | None:
    """A base-model folder is unsafe when its colours do not prove the variant."""
    if (getattr(main_plan, "model_folder_resolution", "") == "version_suffix_removed"
            and not color_plan_complete):
        return (
            "去除版本描述后虽命中云盘目录，但颜色规格不能完整对应；"
            "拒绝把基础版图片用于不同版本"
        )
    return None


VERIFIED_STANDARD_BRANDS = {
    # Verified through the previous locked Redmi canary and live readback.
    "红米": {"id": 663234468, "label": "Redmi/红米"},
}


_INVALID_MODEL_VALUES = {"", "/", "-", "--", "未知", "无"}


def _source_identity(detail: Mapping[str, Any], snapshot_name: str = "") -> tuple[
    str, dict[str, Any], str
]:
    """Resolve brand/model from detail, with a same-snapshot title fallback.

    Some draft-detail responses contain only the rendered brand prefix in
    ``name`` even though the draft-list row still contains the complete product
    name.  The fallback is intentionally used only when the detail-derived
    identity is unusable; a healthy detail title always remains authoritative.
    """
    title = _source_title(detail)
    parsed = dict(resolve_brand_model(title))
    brand = str(parsed.get("target_brand_cn") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if brand and model not in _INVALID_MODEL_VALUES:
        return title, parsed, "api_detail"

    fallback_name = str(snapshot_name or "").strip()
    if fallback_name:
        fallback_title = _source_title({
            "name_prefix": detail.get("name_prefix"),
            "name": fallback_name,
        })
        fallback_parsed = dict(resolve_brand_model(fallback_title))
        fallback_brand = str(fallback_parsed.get("target_brand_cn") or "").strip()
        fallback_model = str(fallback_parsed.get("model") or "").strip()
        if fallback_brand and fallback_model not in _INVALID_MODEL_VALUES:
            return fallback_title, fallback_parsed, "snapshot_fallback"
    return title, parsed, "api_detail_invalid"


def _is_retryable_publish_error(error: Exception) -> bool:
    """Retry only an explicit platform rejection whose outcome is known.

    Transport failures are intentionally excluded because the server may have
    accepted the submission before the connection failed.  They are resolved
    by readback only and never blindly submitted again.
    """
    if not isinstance(error, DoudianAPIError):
        return False
    code = str(error.code or "").strip()
    sub_code = str(error.sub_code or "").strip().casefold()
    if code == "60000" and sub_code == "isv.request-limited":
        return True
    return "请重试" in str(error.message or "")


def _publish_with_retry(
    publisher: DoudianSingleProductPublisher,
    submission: Mapping[str, Any],
    *,
    read_detail,
    expected_fingerprint: str,
    retries: int = 2,
    backoff_seconds: float = 3.0,
    sleep=time.sleep,
    on_failure=None,
) -> tuple[Mapping[str, Any], int, Mapping[str, Any] | None]:
    """Submit once plus bounded safe retries, with readback before every retry."""
    if not 0 <= retries <= 5 or backoff_seconds < 0:
        raise ValueError("invalid publish retry configuration")
    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            return publisher.submit_planned_draft(submission), attempt, None
        except Exception as exc:
            readback: Mapping[str, Any] | None = None
            try:
                candidate = read_detail()
                if isinstance(candidate, Mapping):
                    readback = candidate
            except Exception:
                # An unreadable post-submit state is uncertain. Never retry.
                if on_failure:
                    on_failure(attempt, None, exc, None, False)
                raise exc

            accepted = bool(
                readback is not None
                and readback.get("status") == 0
                and readback.get("check_status") == 3
            )
            if accepted:
                if on_failure:
                    on_failure(attempt, 0.0, exc, readback, True)
                return {"code": 10000, "readback_confirmed": True}, attempt, readback

            retryable = _is_retryable_publish_error(exc)
            delay = backoff_seconds * (2 ** (attempt - 1))
            will_retry = retryable and attempt < attempts
            if (will_retry and expected_fingerprint
                    and _source_fingerprint(readback or {}) != expected_fingerprint):
                if on_failure:
                    on_failure(attempt, None, exc, readback, False)
                raise FullMediaCanaryError("提交重试前草稿发生变化；拒绝重复提交") from exc
            if on_failure:
                on_failure(attempt, delay if will_retry else None, exc, readback, False)
            if not will_retry:
                raise
            sleep(delay)
    raise AssertionError("unreachable")


def _main_pic_3_4_issue(detail: Mapping[str, Any]) -> str | None:
    """Reject a legacy 3:4 field that visibly contains a non-3:4 asset."""
    values = detail.get("main_pic_3_4") or []
    if not values:
        return None
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return "现有主图3:4字段结构异常"
    match = re.search(r"_www(\d+)-(\d+)(?:$|[/?#])", values[0])
    if not match:
        return "现有主图3:4尺寸无法核验"
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0 or abs(width / height - 0.75) > 0.01:
        return f"现有主图3:4比例不合规（{width}×{height}）"
    return None


def _category_leaf_id(detail: Mapping[str, Any]) -> str:
    category = detail.get("category_detail") or {}
    if not isinstance(category, Mapping):
        raise FullMediaCanaryError("商品类目结构异常")
    for key in ("fourth_cid", "third_cid", "second_cid", "first_cid"):
        value = str(category.get(key) or "").strip()
        if value.isdigit() and int(value) > 0:
            return value
    raise FullMediaCanaryError("商品缺少末级类目 ID")


def resolve_official_model(gateway: DoudianReadOnlyGateway, detail: Mapping[str, Any],
                           model: str, brand: str, english_brand: str = "") -> dict[str, Any]:
    """Resolve a unique live category option; never infer a platform ID locally."""
    response = gateway.call("product.getCatePropertyV2", {
        "category_leaf_id": int(_category_leaf_id(detail)),
    })
    data = response.get("data") or {}
    properties = data.get("data") if isinstance(data, Mapping) else None
    if not isinstance(properties, list):
        raise FullMediaCanaryError("平台类目属性响应缺少属性列表")
    prop = find_property(properties, MODEL_PROPERTY_ID)
    if not prop:
        raise FullMediaCanaryError("平台类目没有型号属性 1493")
    option = resolve_model_option(prop, model, brand_names=(brand, english_brand))
    if not option or not str(option.get("value") or "").isdigit():
        raise FullMediaCanaryError("型号不能唯一匹配平台标准选项：" + model)
    return {"name": str(option["name"]).strip(), "value": int(option["value"])}


def _property_entry(properties: Mapping[str, Any], property_id: int | str) -> dict[str, Any]:
    entries = properties.get(str(property_id)) or []
    return dict(entries[0]) if len(entries) == 1 and isinstance(entries[0], Mapping) else {}


def _brand_identity_issue(detail: Mapping[str, Any], *, brand_cn: str,
                          brand_en: str, recommended_prefix: str) -> str | None:
    """Reject a missing brand or a platform prefix for a different brand."""
    standard_brand_id = str(detail.get("standard_brand_id") or "").strip()
    if not standard_brand_id.isdigit() or int(standard_brand_id) <= 0:
        return "标准品牌 ID 缺失"
    compact_prefix = re.sub(r"[\s/\-_.]+", "", recommended_prefix).casefold()
    expected = {
        re.sub(r"[\s/\-_.]+", "", value).casefold()
        for value in (brand_cn, brand_en) if str(value).strip()
    }
    if not expected or not any(value in compact_prefix for value in expected):
        return "标准品牌与标题解析品牌不一致"
    return None


def _planned_brand(detail: Mapping[str, Any], brand_cn: str) -> tuple[int, str, str]:
    """Return a verified brand id and a provisional prefix for title validation."""
    current = str(detail.get("standard_brand_id") or "").strip()
    verified = VERIFIED_STANDARD_BRANDS.get(brand_cn)
    if not verified:
        if not current.isdigit() or int(current) <= 0:
            raise FullMediaCanaryError("标准品牌 ID 缺失且没有已验证映射")
        return int(current), str(detail.get("name_prefix") or ""), "preserve_existing"

    label = str(verified["label"])
    prefix = str(detail.get("name_prefix") or "").strip()
    grade_match = re.match(r"^(99新|95新|90新|9新)\s*", prefix, flags=re.I)
    if not grade_match:
        raise FullMediaCanaryError("标题前缀无法提取成色，拒绝切换标准品牌")
    planned_prefix = f"{grade_match.group(1)} {label} "
    action = "already_matched" if current == str(verified["id"]) else "replace_with_verified"
    return int(verified["id"]), planned_prefix, action


def _read_checked(image: Any) -> bytes:
    data = image.path.read_bytes()
    if len(data) != image.size_bytes or hashlib.sha256(data).hexdigest() != image.sha256:
        raise FullMediaCanaryError("图库图片在预检后变化：" + str(image.path))
    return data


def verify_full_media(before: Mapping[str, Any], after: Mapping[str, Any], *,
                      main_urls: list[str], color_pairs: list[tuple[str, str]] | None,
                      detail_image_urls: list[str], model_option: Mapping[str, Any] | None = None,
                      screen_option: Mapping[str, Any] | None = None,
                      standard_brand_id: int | None = None,
                      name_prefix: str | None = None,
                      expected_white_background_url: str | None = None) -> list[str]:
    issues: list[str] = []
    if _product_id(before) != _product_id(after):
        issues.append("商品 ID 改变")
    for key in GUARDED_FIELDS:
        if key in {"pic", "description"}:
            continue
        if key == "standard_brand_id" and standard_brand_id is not None:
            if str(after.get(key) or "") != str(standard_brand_id):
                issues.append("平台标准品牌未正确保存")
            continue
        if key == "name_prefix" and name_prefix is not None:
            if after.get(key) != name_prefix:
                issues.append("平台推荐标题前缀未正确保存")
            continue
        if key == "product_format_new":
            left = normalize_product_format_for_edit(parse_product_format(before.get(key)))
            right = normalize_product_format_for_edit(parse_product_format(after.get(key)))
            if model_option is None:
                if left != right:
                    issues.append("商品属性改变")
            else:
                actual_model = _property_entry(right, MODEL_PROPERTY_ID)
                if (str(actual_model.get("value")) != str(model_option.get("value"))
                        or actual_model.get("name") != model_option.get("name")
                        or actual_model.get("diy_type") != 0):
                    issues.append("平台标准型号未正确保存")
                left.pop(str(MODEL_PROPERTY_ID), None)
                right.pop(str(MODEL_PROPERTY_ID), None)
                if screen_option is not None:
                    actual_screen = _property_entry(right, SCREEN_PROPERTY_ID)
                    if (str(actual_screen.get("value")) != str(screen_option.get("value"))
                            or actual_screen.get("name") != screen_option.get("name")
                            or actual_screen.get("diy_type") != 0):
                        issues.append("屏幕形态未正确保存")
                    left.pop(str(SCREEN_PROPERTY_ID), None)
                    right.pop(str(SCREEN_PROPERTY_ID), None)
                if left != right:
                    issues.append("型号和屏幕形态之外的商品属性改变" if screen_option is not None
                                  else "型号之外的商品属性改变")
        elif before.get(key) != after.get(key):
            issues.append(key + " 改变")
    for key in ("draft_status", "main_pic_3_4"):
        if before.get(key) != after.get(key):
            issues.append(key + " 改变")
    expected_white = expected_white_background_url or before.get("white_back_ground_pic_url")
    if after.get("white_back_ground_pic_url") != expected_white:
        issues.append("white_back_ground_pic_url 改变")
    if ([_platform_path(url) for url in _pic_urls(after.get("pic"))]
            != [_platform_path(url) for url in main_urls]):
        issues.append("主图/白底图数量、顺序或 URL 不一致")
    if ([_platform_path(url) for url in detail_urls(after.get("description"))]
            != [_platform_path(url) for url in detail_image_urls]):
        issues.append("详情图数量、顺序或 URL 不一致")
    rows = after.get("spec_pics") or []
    if color_pairs is None:
        if after.get("spec_pics") != before.get("spec_pics"):
            issues.append("未上传颜色规格图时既有规格图发生改变")
    elif not isinstance(rows, list) or len(rows) != len(color_pairs):
        issues.append("颜色规格图数量不一致")
    else:
        actual = {str(row.get("spec_detail_id")): _platform_path(str(row.get("pic") or ""))
                  for row in rows if isinstance(row, Mapping)}
        expected = {spec_id: _platform_path(url) for spec_id, url in color_pairs}
        if len(actual) != len(rows) or actual != expected:
            issues.append("颜色规格值 ID 与图片不对应")
    return issues


def _spec_pic_slots(detail: Mapping[str, Any],
                    urls_by_spec_id: Mapping[str, str]) -> list[str]:
    """Build API positional slots for the existing first specification group."""
    specs = detail.get("specs") or []
    if not isinstance(specs, list) or not specs or not isinstance(specs[0], Mapping):
        raise FullMediaCanaryError("无法建立颜色规格图位置映射")
    values = specs[0].get("values") or []
    if not isinstance(values, list) or not values:
        raise FullMediaCanaryError("颜色规格没有有效值")
    slots: list[str] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise FullMediaCanaryError("颜色规格值结构异常")
        spec_id = str(value.get("id") or "").strip()
        if not spec_id.isdigit():
            raise FullMediaCanaryError("颜色规格值 ID 无效")
        slots.append(str(urls_by_spec_id.get(spec_id) or ""))
    return slots


def _post_publish_issues(saved: Mapping[str, Any], live: Mapping[str, Any], *,
                         expected_name: str | None = None,
                         expected_prefix: str | None = None) -> tuple[list[str], list[dict[str, Any]]]:
    """Verify that publish changed state only, allowing platform 0→positive stock sync."""
    issues: list[str] = []
    if live.get("status") != 0 or live.get("check_status") != 3:
        issues.append("online_state_not_confirmed")
    for field in (
        "name_suffix", "pic", "description", "spec_pics",
        "specs", "standard_brand_id", "freight_id", "product_type", "spec_id",
        "white_back_ground_pic_url",
    ):
        if live.get(field) != saved.get(field):
            issues.append(field + "_changed_after_submit")
    if expected_name is None:
        if live.get("name") != saved.get("name"):
            issues.append("name_changed_after_submit")
        if live.get("name_prefix") != saved.get("name_prefix"):
            issues.append("name_prefix_changed_after_submit")
    else:
        if live.get("name") != expected_name:
            issues.append("recommended_title_not_applied")
        # The detail endpoint can either retain the separate field or fold it
        # into name after the product becomes live. Both representations are
        # accepted, but the full visible title above must match exactly.
        if expected_prefix is not None and live.get("name_prefix") not in (expected_prefix, "", None):
            issues.append("unexpected_name_prefix_after_submit")
    if normalize_product_format_for_edit(parse_product_format(live.get("product_format_new"))) != \
            normalize_product_format_for_edit(parse_product_format(saved.get("product_format_new"))):
        issues.append("properties_changed_after_submit")

    old_rows = saved.get("spec_prices") or []
    new_rows = live.get("spec_prices") or []
    old = {str(row.get("sku_id")): row for row in old_rows if isinstance(row, Mapping)}
    new = {str(row.get("sku_id")): row for row in new_rows if isinstance(row, Mapping)}
    stock_sync: list[dict[str, Any]] = []
    if list(old) != list(new):
        issues.append("sku_set_or_order_changed_after_submit")
    else:
        for sku_id in old:
            changed = sorted(key for key in set(old[sku_id]) | set(new[sku_id])
                             if old[sku_id].get(key) != new[sku_id].get(key))
            if not changed:
                continue
            before_stock = old[sku_id].get("stock_num")
            after_stock = new[sku_id].get("stock_num")
            if (changed == ["stock_num"]
                    and isinstance(before_stock, int) and not isinstance(before_stock, bool)
                    and isinstance(after_stock, int) and not isinstance(after_stock, bool)
                    and before_stock >= 0 and after_stock >= before_stock):
                stock_sync.append({"sku_id": sku_id, "before": before_stock,
                                   "after": after_stock})
            else:
                issues.append("sku_fields_changed_after_submit:" + sku_id)
    return issues, stock_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="One full-media API transaction")
    parser.add_argument("shop_id")
    parser.add_argument("product_id")
    parser.add_argument("--image-root", default="/Users/atrenew/商品图片")
    parser.add_argument("--color-root", default=DEFAULT_GALLERY)
    parser.add_argument("--detail-root", default="/Users/atrenew/手机商详")
    parser.add_argument("--main-area-source", choices=("local", "feishu"), default="local")
    parser.add_argument("--feishu-cache-root", default="panel_data/material_metadata_cache")
    parser.add_argument("--folder-id", default="0")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--report-root", default="panel_data/doudian_full_media_canary")
    parser.add_argument(
        "--snapshot-name", default="",
        help="same-snapshot list title; used only if detail API loses brand/model",
    )
    parser.add_argument(
        "--restore-white-background-url", default="",
        help="one-time recovery: restore a previously evidenced material-centre white URL",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--publish", action="store_true",
                        help="after verified draft, submit once and require online readback")
    parser.add_argument(
        "--publish-retries", type=int, default=2,
        help="safe retries after the initial publish submission (default: 2)",
    )
    args = parser.parse_args()
    shop_id, product_id = str(args.shop_id), str(args.product_id)
    if not shop_id.isdigit() or not product_id.isdigit() or not args.folder_id.isdigit():
        parser.error("shop_id, product_id and folder_id must be numeric")
    if args.publish and not args.execute:
        parser.error("--publish requires --execute")
    if not 0 <= args.publish_retries <= 5:
        parser.error("--publish-retries must be between 0 and 5")

    gateway = DoudianReadOnlyGateway(args.env, shop_id)
    reader = DoudianProductReader(gateway)
    before = dict(reader.get_product_detail(product_id, show_draft=True))
    if (_product_id(before) != product_id or before.get("status") != 0
            or before.get("check_status") != 1 or before.get("draft_status") != 1):
        raise FullMediaCanaryError("目标不是可编辑且未发布的草稿")
    if not str(before.get("name_prefix") or "").strip() or before.get("name_suffix"):
        raise FullMediaCanaryError("标题前/后缀不符合本次全图验收的保留条件")
    current_white_background_url = str(before.get("white_back_ground_pic_url") or "").strip()
    restore_white_background_url = str(args.restore_white_background_url or "").strip()
    if restore_white_background_url:
        if current_white_background_url and current_white_background_url != restore_white_background_url:
            raise FullMediaCanaryError("当前独立白底图与恢复证据不一致")
        if (not restore_white_background_url.startswith(("https://", "http://"))
                or "/obj/ecom-shop-material/" not in restore_white_background_url):
            raise FullMediaCanaryError("恢复值不是素材中心白底图 URL")
    expected_white_background_url = (
        restore_white_background_url or current_white_background_url or None
    )
    main_pic_issue = _main_pic_3_4_issue(before)
    if main_pic_issue:
        raise FullMediaCanaryError(main_pic_issue + "；本链路不修改该字段")
    title, parsed, source_title_source = _source_identity(before, args.snapshot_name)
    brand = str(parsed.get("target_brand_cn") or "").strip()
    english_brand = str(parsed.get("target_brand_en") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not brand or model in _INVALID_MODEL_VALUES:
        raise FullMediaCanaryError("商品品牌或型号无法唯一解析")
    # Keep all downstream title-dependent planners on the same recovered
    # source.  The live detail remains untouched until the explicit publish
    # transaction; this is only an in-memory planning view.
    planning_detail = dict(before)
    if source_title_source == "snapshot_fallback":
        planning_detail["name"] = str(args.snapshot_name).strip()
    raw_properties = gateway.call("product.getCatePropertyV2", {
        "category_leaf_id": int(_category_leaf_id(before)),
    })
    property_data = raw_properties.get("data") or {}
    properties = property_data.get("data", []) if isinstance(property_data, Mapping) else []
    model_prop = find_property(properties, MODEL_PROPERTY_ID)
    screen_prop = find_property(properties, SCREEN_PROPERTY_ID)
    if not model_prop or not screen_prop:
        raise FullMediaCanaryError("平台型号或屏幕形态属性不可用")
    model_option, model_source = _model_option(before, title, model_prop)
    screen_option, screen_source = _screen_option(before, screen_prop)
    if not model_option:
        aliases = tuple(value for value in (
            parsed.get("target_brand_en"), parsed.get("target_brand_cn"), parsed.get("brand"),
        ) if value)
        suggestions = model_option_suggestions(
            model_prop, model, brand_names=aliases,
        )
        suffix = ""
        if suggestions:
            suffix = "；平台近似候选=" + ", ".join(
                f"{item['name']}({item['value']})" for item in suggestions
            )
        raise FullMediaCanaryError("型号不能唯一匹配平台标准选项：" + model + suffix)
    if not screen_option:
        raise FullMediaCanaryError("屏幕形态不存在且平台没有直屏默认选项")
    current_properties = normalize_product_format_for_edit(
        parse_product_format(before.get("product_format_new")))
    current_model = _property_entry(current_properties, MODEL_PROPERTY_ID)
    current_screen = _property_entry(current_properties, SCREEN_PROPERTY_ID)
    model_option = dict(model_option)
    screen_option = dict(screen_option)
    model_action = ("already_matched" if
                    str(current_model.get("value")) == str(model_option["value"])
                    and current_model.get("name") == model_option["name"]
                    and current_model.get("diy_type") == 0 else "replace_with_official")
    planned_property_json = build_property_edit_payload(
        before, {MODEL_PROPERTY_ID: model_option, SCREEN_PROPERTY_ID: screen_option},
    )["product_format_new"]
    planned_standard_brand_id, planned_name_prefix, brand_action = _planned_brand(before, brand)
    projected_detail = {
        **planning_detail,
        "product_format_new": planned_property_json,
        "standard_brand_id": planned_standard_brand_id,
        "name_prefix": planned_name_prefix,
    }
    title_preflight = recommend_title_for_submission(
        gateway, projected_detail,
    )
    brand_issue = _brand_identity_issue(
        projected_detail, brand_cn=brand, brand_en=english_brand,
        recommended_prefix=title_preflight.name_prefix,
    )
    if brand_issue:
        raise FullMediaCanaryError(
            brand_issue + "：解析=" + str(parsed.get("target_brand_value") or brand)
            + "，平台前缀=" + title_preflight.name_prefix.strip()
        )
    cloud_drive = None
    if args.main_area_source == "local":
        inspected = ImageAssetInspector(args.image_root, shop_id).inspect(brand, model)
        if not inspected["ok"]:
            raise FullMediaCanaryError(
                "同店主图/白底图目录不完整或有歧义："
                + str(inspected["reasons"])
                + f"（解析品牌={brand!r}，型号={model!r}，标题={title!r}）"
            )
        main_plan = build_dynamic_main_area_plan(
            inspected["main_path"] or None, inspected["auxiliary_paths"],
        )
        main_auxiliary = tuple(main_plan.auxiliary)
        omitted_auxiliary = tuple(main_plan.skipped_auxiliary)
    else:
        env_values = read_env_file(args.env)
        cloud_drive = FeishuDriveClient(FeishuDriveConfig.from_mapping(env_values))
        cloud_catalog = FeishuMaterialCatalog(
            cloud_drive, layout=FeishuMaterialLayout.from_mapping(env_values),
        )
        main_plan = build_material_plan(
            cloud_catalog.resolve_bundle(shop_id, brand, model),
        )
        main_auxiliary = tuple(main_plan.white_images)
        omitted_auxiliary = tuple(main_plan.omitted_white_images)
    color_plan = None
    color_plan_issue = ""
    try:
        if args.main_area_source == "feishu":
            color_plan = resolve_cloud_color_images(
                main_plan.spec_image_candidates, planning_detail,
            )
        else:
            color_plan = _source_plan(
                planning_detail, args.color_root, allow_existing=True, require_complete=False,
            )
    except (ColorGalleryError, SkuColorDraftError, OSError) as exc:
        color_plan_issue = str(exc)
    matched_color_images = color_plan.images if color_plan is not None else ()
    color_plan_complete = bool(
        color_plan is not None
        and len(matched_color_images) == color_plan.color_count
        and not color_plan.missing
        and not color_plan.invalid
    )
    versionless_color_issue = _versionless_cloud_color_issue(
        main_plan, color_plan_complete,
    )
    if versionless_color_issue:
        raise FullMediaCanaryError(versionless_color_issue)
    # editV2's spec_pic is positional and rejects empty slots.  Therefore an
    # incomplete group must be omitted as a whole; sending fewer URLs would
    # silently attach later images to the wrong colour.
    color_images = matched_color_images if color_plan_complete else ()
    grade_plan = build_grade_only_plan(before, args.detail_root)
    use_combined_grade_image = grade_plan.strategy == "combined_existing"
    original_detail_urls = detail_urls(before.get("description"))
    validate_existing_detail_urls(original_detail_urls)
    retained_detail_urls, excluded_detail_urls = select_existing_detail_urls(
        original_detail_urls, use_combined_grade_image=use_combined_grade_image,
    )
    if len(retained_detail_urls) < 2:
        raise FullMediaCanaryError("过滤已知禁用图和旧成色图后少于 2 张原详情图")
    planned_detail_count = len(retained_detail_urls) + len(grade_plan.images)
    if planned_detail_count > 20:
        raise FullMediaCanaryError("合并成色图后的详情图超过 20 张")
    if (1 + len(main_auxiliary) + len(color_images)
            + planned_detail_count > 50):
        raise FullMediaCanaryError("主图、白底、颜色与详情图合计超过 50 张")
    summary = {
        "shop_id": shop_id, "product_id": product_id, "brand": brand, "model": model,
        "brand_en": english_brand,
        "source_title": title, "source_title_source": source_title_source,
        "standard_brand_id_before": before.get("standard_brand_id"),
        "standard_brand_id": planned_standard_brand_id,
        "brand_action": brand_action,
        "model_option": model_option, "model_before": current_model,
        "model_action": model_action, "model_source": model_source,
        "screen_shape": screen_option, "screen_before": current_screen,
        "screen_source": screen_source, "category_leaf_id": _category_leaf_id(before),
        "recommended_title": title_preflight.name,
        "recommended_name_prefix": title_preflight.name_prefix,
        "recommended_property_ids": list(title_preflight.property_ids),
        "main_area_source": args.main_area_source,
        "cloud_model_folder_resolution": getattr(
            main_plan, "model_folder_resolution", "local_exact",
        ),
        "cloud_model_folder": getattr(main_plan, "resolved_model_folder", ""),
        "official_model_name": getattr(main_plan, "official_model_name", ""),
        "official_model_source_url": getattr(
            main_plan, "official_model_source_url", "",
        ),
        "main": (str(main_plan.main.path) if args.main_area_source == "local"
                 else "feishu://%s/%s" % (main_plan.main.file_token, main_plan.main.name)),
        "white": ([str(image.path) for image in main_auxiliary]
                  if args.main_area_source == "local" else [
                      "feishu://%s/%s" % (image.file_token, image.name)
                      for image in main_auxiliary
                  ]),
        "main_substituted": main_plan.main_substituted,
        "white_background_url_preserved": expected_white_background_url,
        "white_background_recovery": bool(restore_white_background_url),
        "omitted_auxiliary": [
            {"name": image.name, "reason": image.reason}
            for image in omitted_auxiliary
        ],
        "colors": [{"name": image.color_name, "spec_detail_id": image.spec_detail_id,
                    "path": (str(image.path) if args.main_area_source == "local"
                             else "feishu://%s/%s" % (
                                 image.image.file_token, image.image.name,
                             ))} for image in color_images],
        "color_spec_count": color_plan.color_count if color_plan is not None else 0,
        "color_uploaded_count": len(color_images),
        "color_matched_count": len(matched_color_images),
        "color_missing": list(color_plan.missing) if color_plan is not None else [],
        "color_invalid": list(color_plan.invalid) if color_plan is not None else [],
        "color_plan_issue": color_plan_issue,
        "color_strategy": (
            "complete" if color_plan_complete
            else "incomplete_not_uploaded" if matched_color_images
            else "not_uploaded"
        ),
        "grades": [image.slot for image in grade_plan.images],
        "grade_values": list(grade_plan.grades),
        "grade_image_strategy": grade_plan.strategy,
        "existing_detail_count": len(original_detail_urls),
        "retained_existing_detail_count": len(retained_detail_urls),
        "excluded_detail_urls": excluded_detail_urls,
        "target_detail_count": planned_detail_count,
        "sku_count": len(before.get("spec_prices") or []),
        "execute": args.execute, "commit": False, "publish": args.publish,
    }
    print(_stable(summary))
    if not args.execute:
        return 0

    stamp = int(time.time())
    stem = Path(args.report_root) / shop_id / f"{stamp}_{product_id}"
    report_path = stem.with_suffix(".report.json")
    _write_json(stem.with_suffix(".before.json"), {"captured_at": stamp, "detail": before})
    report: dict[str, Any] = {**summary, "started_at": stamp, "result": "started", "uploaded": []}
    _write_json(report_path, report)
    materials = DoudianMaterialGateway(args.env, shop_id, cache_root="panel_data/doudian_material_cache")
    try:
        if args.main_area_source == "feishu":
            assert cloud_drive is not None
            cloud_executor = CloudMaterialExecutor(
                cloud_drive, materials, MaterialMetadataCache(args.feishu_cache_root),
                shop_id=shop_id, folder_id=args.folder_id,
            )
            resolved_main = cloud_executor.resolve(main_plan)
            main_materials = (resolved_main.main, *resolved_main.white_images)
            color_materials = cloud_executor.resolve_images(tuple(
                image.image for image in color_images
            ))
            for kind, source, material in (
                *(("main" if index == 0 else "white", source, material)
                  for index, (source, material) in enumerate(zip(
                      (main_plan.main, *main_auxiliary), main_materials,
                  ))),
                *(("color", image.image, material)
                  for image, material in zip(color_images, color_materials)),
            ):
                report["uploaded"].append({
                    "kind": kind,
                    "source": "feishu://%s/%s" % (source.file_token, source.name),
                    "sha256": source.sha256,
                    "material_id": material.material_id,
                })
                _write_json(report_path, report)
            grade_materials = []
            for image in grade_plan.images:
                material = materials.upload_image(
                    _read_checked(image), image.upload_name, folder_id=args.folder_id,
                )
                if material.sha256 != image.sha256:
                    raise FullMediaCanaryError(
                        "平台素材证据与源图哈希不符：" + str(image.path)
                    )
                grade_materials.append(material)
                report["uploaded"].append({
                    "kind": "grade", "source": str(image.path),
                    "sha256": image.sha256, "material_id": material.material_id,
                })
                _write_json(report_path, report)
            main_urls = [item.byte_url for item in main_materials]
            color_urls = [item.byte_url for item in color_materials]
            grade_urls = [item.byte_url for item in grade_materials]
        else:
            source_items = [
                ("main", main_plan.main, main_plan.main.name),
                *(("white", image, image.name) for image in main_auxiliary),
                *(("color", image, image.upload_name) for image in color_images),
                *(("grade", image, image.upload_name) for image in grade_plan.images),
            ]
            uploaded = []
            for kind, image, upload_name in source_items:
                material = materials.upload_image(
                    _read_checked(image), upload_name, folder_id=args.folder_id,
                )
                if material.sha256 != image.sha256:
                    raise FullMediaCanaryError(
                        "平台素材证据与源图哈希不符：" + str(image.path)
                    )
                uploaded.append(material)
                report["uploaded"].append({
                    "kind": kind, "source": str(image.path),
                    "sha256": image.sha256, "material_id": material.material_id,
                })
                _write_json(report_path, report)
            main_count = 1 + len(main_auxiliary)
            uploaded_color_count = len(color_images)
            main_urls = [item.byte_url for item in uploaded[:main_count]]
            color_urls = [
                item.byte_url for item in uploaded[main_count:main_count + uploaded_color_count]
            ]
            grade_urls = [item.byte_url for item in uploaded[main_count + uploaded_color_count:]]
        latest = dict(reader.get_product_detail(product_id, show_draft=True))
        if _source_fingerprint(before) != _source_fingerprint(latest):
            raise FullMediaCanaryError("素材上传期间商品变化；未写入商品")
        main_count = 1 + len(main_auxiliary)
        target_detail_urls = compose_detail_urls(
            original_detail_urls, grade_urls,
            use_combined_grade_image=use_combined_grade_image,
        )
        payload = {
            "product_id": product_id,
            "product_format_new": planned_property_json,
            "standard_brand_id": planned_standard_brand_id,
            "pic": "|".join(main_urls),
            "description": "|".join(target_detail_urls),
            "name_prefix": planned_name_prefix,
            "commit": False,
        }
        if expected_white_background_url:
            payload["white_back_ground_pic_url"] = expected_white_background_url
        color_pairs: list[tuple[str, str]] | None = None
        color_slot_count = 0
        if color_images:
            urls_by_spec_id = {
                image.spec_detail_id: url for image, url in zip(color_images, color_urls)
            }
            slots = _spec_pic_slots(before, urls_by_spec_id)
            payload["spec_pic"] = ",".join(slots)
            color_slot_count = len(slots)
            color_pairs = [(image.spec_detail_id, url)
                           for image, url in zip(color_images, color_urls)]
        report["payload_sha256"] = hashlib.sha256(_stable(payload).encode("utf-8")).hexdigest()
        report["result"] = "write_started"
        _write_json(report_path, report)
        writer = DoudianSingleProductFullMediaDraftWriter(args.env, shop_id, product_id)
        response = writer.save_full_media_to_draft(
            payload, main_count=main_count, detail_count=len(target_detail_urls),
            color_count=color_slot_count,
        )
        report["api_code"] = response.get("code")
        report["result"] = "write_accepted_readback_pending"
        _write_json(report_path, report)
        after = dict(reader.get_product_detail(product_id, show_draft=True))
        _write_json(stem.with_suffix(".after.json"), {"captured_at": int(time.time()), "detail": after})
        issues = verify_full_media(before, after, main_urls=main_urls,
                                   color_pairs=color_pairs, detail_image_urls=target_detail_urls,
                                   model_option=model_option, screen_option=screen_option,
                                   standard_brand_id=planned_standard_brand_id,
                                   name_prefix=planned_name_prefix,
                                   expected_white_background_url=expected_white_background_url)
        report["verification_errors"] = issues
        report["result"] = "verified_draft" if not issues else "unverified_needs_manual_review"
        _write_json(report_path, report)
        if not issues and args.publish:
            latest = dict(reader.get_product_detail(product_id, show_draft=True))
            if _source_fingerprint(latest) != _source_fingerprint(after):
                raise FullMediaCanaryError("草稿回读后发生变化；拒绝提交发布")
            publish_title_detail = dict(after)
            if source_title_source == "snapshot_fallback":
                publish_title_detail["name"] = str(args.snapshot_name).strip()
            title_plan = recommend_title_for_submission(gateway, publish_title_detail)
            submission = {
                "product_id": product_id,
                "product_format_new": payload["product_format_new"],
                "name": title_plan.name,
                "name_prefix": title_plan.name_prefix,
                "commit": True,
            }
            if expected_white_background_url:
                submission["white_back_ground_pic_url"] = expected_white_background_url
            digest = submission_payload_sha256(submission)
            report.update({
                "result": "publish_pending", "publish_payload_sha256": digest,
                "publish_retry_limit": args.publish_retries,
                "publish_attempts": [],
                "recommended_title": title_plan.name,
                "recommended_name_prefix": title_plan.name_prefix,
                "recommended_property_ids": list(title_plan.property_ids),
            })
            _write_json(report_path, report)
            publisher = DoudianSingleProductPublisher(args.env, shop_id, product_id, digest)
            def record_publish_failure(attempt, delay, error, readback, accepted):
                report["publish_attempts"].append({
                    "attempt": attempt,
                    "result": "accepted_by_readback" if accepted else "rejected",
                    "error": f"{type(error).__name__}: {error}",
                    "retry_in_seconds": delay,
                    "readback_status": readback.get("status") if readback else None,
                    "readback_check_status": readback.get("check_status") if readback else None,
                })
                _write_json(report_path, report)

            response, publish_attempt_count, accepted_readback = _publish_with_retry(
                publisher, submission,
                read_detail=lambda: dict(reader.get_product_detail(product_id, show_draft=True)),
                expected_fingerprint=_source_fingerprint(after),
                retries=args.publish_retries,
                on_failure=record_publish_failure,
            )
            if accepted_readback is None:
                report["publish_attempts"].append({
                    "attempt": publish_attempt_count,
                    "result": "api_accepted",
                    "api_code": response.get("code"),
                })
            report.update({"result": "api_accepted_readback_pending",
                           "publish_api_code": response.get("code"),
                           "publish_attempt_count": publish_attempt_count,
                           "publish_retry_count": max(0, publish_attempt_count - 1)})
            _write_json(report_path, report)
            published: dict[str, Any] = dict(accepted_readback or {})
            for attempt in range(1, 13):
                if not published:
                    published = dict(reader.get_product_detail(product_id, show_draft=True))
                if published.get("status") == 0 and published.get("check_status") == 3:
                    break
                published = {}
                if attempt < 12:
                    time.sleep(5)
            post_issues, stock_sync = _post_publish_issues(
                after, published, expected_name=title_plan.name,
                expected_prefix=title_plan.name_prefix,
            )
            report["post_publish_issues"] = post_issues
            report["stock_sync"] = stock_sync
            report["after_publish"] = {
                "status": published.get("status"),
                "check_status": published.get("check_status"),
                "draft_status": published.get("draft_status"),
                "name": published.get("name"),
                "name_prefix": published.get("name_prefix"),
                "main_count": len(_pic_urls(published.get("pic"))),
                "detail_count": len(detail_urls(published.get("description"))),
                "color_count": len(published.get("spec_pics") or []),
                "sku_count": len(published.get("spec_prices") or []),
            }
            report["result"] = "published_readback" if not post_issues else \
                "api_accepted_pending_or_needs_review"
            _write_json(report_path, report)
        print(_stable({"result": report["result"], "errors": issues,
                       "post_publish_issues": report.get("post_publish_issues", []),
                       "report": str(report_path), "publish": args.publish}))
        return 0 if report["result"] in {"verified_draft", "published_readback"} else 2
    except Exception as exc:
        previous_result = str(report.get("result") or "")
        if previous_result in {"publish_pending", "api_accepted_readback_pending"}:
            report["result"] = "publish_rejected_or_unknown_needs_manual_review"
        elif previous_result == "write_started":
            report["result"] = "write_rejected_no_product_change"
        elif previous_result in {
            "write_accepted_readback_pending", "verified_draft",
            "unverified_needs_manual_review",
        }:
            report["result"] = "failed_after_draft_write_needs_manual_review"
        else:
            report["result"] = "failed_before_product_write"
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        _write_json(report_path, report)
        print(_stable({"result": report["result"], "error": report["error"],
                       "report": str(report_path), "publish": args.publish}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
