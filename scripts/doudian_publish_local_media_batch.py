"""Publish a small API-only batch after strict local-media replacement.

This is the production counterpart to the browser image policy.  It accepts
only drafts whose exact brand/model source directory provides either a real
main image plus at least three distinct white images, or at least four white
images for the explicit no-main substitution mode.  Every item also requires
the complete 14-image detail set.

For each selected draft the order is immutable and fail-closed:

1. write local bytes to DouDian materials (reusing revalidated cache entries);
2. save ``pic`` + 14-image platform URL detail list + reviewed properties with ``commit=false``;
3. read back the exact ordered media and properties;
4. only then submit the same verified draft with ``commit=true``.

There is no browser fallback and no guessed image/model directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Mapping

from app.material_pipeline import (
    CloudMaterialExecutor,
    FeishuMaterialCatalog,
    FeishuMaterialLayout,
    build_material_plan,
)
from app.material_pipeline.cache import MaterialMetadataCache
from app.listing_preflight.assets import DetailImageInspector, ImageAssetInspector
from app.listing_preflight.legacy_resolver import resolve_brand_model
from doudian_api import DoudianAPIError, MODEL_PROPERTY_ID, find_property, read_env_file
from doudian_product_service import build_property_edit_payload
from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.materials import DoudianMaterialGateway
from integrations.feishu_drive import FeishuDriveClient, FeishuDriveConfig
from integrations.doudian.write_gateway import (
    DoudianSingleProductImageDraftWriter, DoudianSingleProductPublisher,
    submission_payload_sha256,
)
from scripts.doudian_apply_local_images import (
    LocalImage, LocalImagePolicyError, _pic_urls, build_dynamic_main_area_plan,
    detail_html, read_detail_image_set, verify_media_readback,
    detail_urls as parse_detail_urls,
)
from scripts.doudian_edit_phone_properties_batch import (
    PHONE_CATEGORY_ID, SCREEN_PROPERTY_ID, _model_option, _screen_option, _verify,
)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _source_metadata(image: LocalImage) -> dict[str, Any]:
    return {"name": image.name, "sha256": image.sha256, "size_bytes": image.size_bytes}


def _resolve_sources(
    title: str, name_prefix: str, assets: ImageAssetInspector, details: DetailImageInspector,
) -> tuple[dict[str, Any], Any, tuple[LocalImage, ...], tuple[LocalImage, ...], dict[str, Any]]:
    """Resolve an exact model source and apply the dynamic no-main policy."""
    parsed = dict(resolve_brand_model(" ".join(part for part in (name_prefix, title) if part)))
    brand = str(parsed.get("target_brand_cn") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not brand or not model:
        raise LocalImagePolicyError("brand_or_model_unresolved")
    evidence = assets.inspect(brand, model)
    if not evidence.get("ok"):
        raise LocalImagePolicyError("image_source_invalid:" + ",".join(evidence.get("reasons") or []))
    area = build_dynamic_main_area_plan(
        str(evidence.get("main_path") or "") or None,
        tuple(evidence.get("auxiliary_paths") or ()),
    )
    detail = details.inspect(str(parsed.get("condition") or ""))
    if not detail.get("ok"):
        raise LocalImagePolicyError("detail_source_invalid:" + ",".join(detail.get("reasons") or []))
    detail_images = read_detail_image_set(tuple(detail.get("files") or ()))
    return parsed, area, detail_images, tuple(area.auxiliary), evidence


def _resolve_feishu_sources(
    shop_id: str, title: str, name_prefix: str, catalog: FeishuMaterialCatalog,
    details: DetailImageInspector,
) -> tuple[dict[str, Any], Any, tuple[LocalImage, ...], tuple[Any, ...], dict[str, Any]]:
    """Build one immutable cloud main-area plan plus the existing 14-detail set.

    The migration boundary is deliberately narrow: Feishu supplies only the
    main picture and white pictures.  Detail images remain in the separately
    maintained local ``手机商详`` library until that source has its own reviewed
    cloud contract.  No local main-area image is used as an automatic fallback.
    """
    parsed = dict(resolve_brand_model(" ".join(part for part in (name_prefix, title) if part)))
    brand = str(parsed.get("target_brand_cn") or "").strip()
    model = str(parsed.get("model") or "").strip()
    if not brand or not model:
        raise LocalImagePolicyError("brand_or_model_unresolved")
    plan = build_material_plan(catalog.resolve_bundle(shop_id, brand, model))
    detail = details.inspect(str(parsed.get("condition") or ""))
    if not detail.get("ok"):
        raise LocalImagePolicyError("detail_source_invalid:" + ",".join(detail.get("reasons") or []))
    detail_images = read_detail_image_set(tuple(detail.get("files") or ()))
    return parsed, plan, detail_images, tuple(plan.white_images), {
        "source": "feishu_drive", "main_substituted": plan.main_substituted,
    }


def _material_urls(gateway: DoudianMaterialGateway, folder_id: str,
                   images: tuple[LocalImage, ...]) -> tuple[list[str], list[str]]:
    uploaded = [gateway.upload_image(image.image_bytes, image.name, folder_id=folder_id) for image in images]
    if any(item.sha256 != source.sha256 for item, source in zip(uploaded, images)):
        raise LocalImagePolicyError("material_digest_mismatch")
    urls = [item.byte_url for item in uploaded]
    if len(urls) != len(set(urls)):
        raise LocalImagePolicyError("material_urls_are_not_distinct")
    return urls, [item.material_id for item in uploaded]


def _old_media(detail: Mapping[str, Any]) -> tuple[str, str]:
    old_pic = "|".join(_pic_urls(detail.get("pic")))
    # Browser-created drafts may still expose their detail images as HTML.
    # The API contract uses a pipe-delimited URL list, so normalize the
    # snapshot into that documented form before a possible rollback.
    old_description = "|".join(parse_detail_urls(detail.get("description")))
    if not old_pic or not old_description:
        raise LocalImagePolicyError("rollback_requires_existing_pic_and_description")
    return old_pic, old_description


def _verify_media_draft(before: Mapping[str, Any], after: Mapping[str, Any],
                        model: Mapping[str, Any], screen: Mapping[str, Any]) -> list[str]:
    """Verify properties without treating intentional media replacement as drift.

    ``_verify`` is shared with the property-only workflow and correctly treats
    every media mutation as unexpected there.  This workflow has one explicit
    exception: ``pic`` and ``description`` are replaced and then proven by the
    separate exact media readback.  No other critical field is exempted.
    """
    intentional = {"pic_changed", "description_changed"}
    return [issue for issue in _verify(before, after, model, screen) if issue not in intentional]


def _candidate_rows(snapshot: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        row for row in snapshot.get("items", [])
        if isinstance(row, Mapping) and str(row.get("category_leaf_id") or "") == PHONE_CATEGORY_ID
    ]


def _can_start_with_safe_candidates(count: int) -> bool:
    """A partial safe batch is useful; only an empty one is blocked.

    ``--limit`` is a ceiling, not an all-or-nothing contract.  A bad source
    image must be recorded for people to fix, while other drafts with complete
    evidence are still allowed to proceed.
    """
    return count > 0


def _is_edit_rate_limited(error: Exception) -> bool:
    """Retry only the platform's explicit no-write rate-limit response.

    Network timeouts and unknown server errors are deliberately not retried:
    their write outcome may be unknown and must instead be resolved through a
    fresh product readback.  ``isv.request-limited`` explicitly means DouDian
    refused the request before handling it, so the identical draft-save body
    is safe to try again after a cooldown.
    """
    return (
        isinstance(error, DoudianAPIError)
        and str(error.code) == "60000"
        and str(error.sub_code or "").strip().casefold() == "isv.request-limited"
    )


def _save_draft_with_rate_limit_retry(
    writer: DoudianSingleProductImageDraftWriter, payload: Mapping[str, Any], *,
    attempts: int, backoff_seconds: float, sleep=time.sleep, on_rate_limited=None,
) -> tuple[Mapping[str, Any], int]:
    """Save one known draft body with bounded backoff for code 60000 only."""
    if attempts < 1 or backoff_seconds < 0:
        raise ValueError("invalid editV2 retry configuration")
    for attempt in range(1, attempts + 1):
        try:
            return writer.save_images_to_draft(payload), attempt
        except Exception as exc:
            if not _is_edit_rate_limited(exc) or attempt == attempts:
                raise
            delay = backoff_seconds * (2 ** (attempt - 1))
            if on_rate_limited:
                on_rate_limited(attempt, delay, exc)
            sleep(delay)
    raise AssertionError("unreachable")


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict local-media API publish batch")
    parser.add_argument("shop_id")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--folder-id", required=True, help="explicit DouDian material folder ID")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--product-id", action="append", default=[],
                        help="optional exact draft ID selector for a one-item safe validation")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--image-root", default="/Users/atrenew/商品图片")
    parser.add_argument("--detail-image-root", default="/Users/atrenew/手机商详")
    parser.add_argument("--cache-root", default="panel_data/doudian_material_cache")
    parser.add_argument("--main-area-source", choices=("local", "feishu"), default="local",
                        help="local is the compatibility path; feishu reads only the configured cloud library")
    parser.add_argument("--feishu-cache-root", default="panel_data/material_metadata_cache",
                        help="immutable Feishu-to-DouDian material evidence cache")
    parser.add_argument("--edit-rate-limit-attempts", type=int, default=3,
                        help="bounded retries for explicit editV2 code 60000 only")
    parser.add_argument("--edit-rate-limit-backoff", type=float, default=20.0,
                        help="first editV2 rate-limit cooldown; later waits double")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--save-draft-only", action="store_true",
                        help="with --execute, verify the draft write but never submit it")
    parser.add_argument("--stop-file", default="",
                        help="optional panel stop sentinel; checked between products")
    parser.add_argument("--report-root", default="panel_data/doudian_api_media_publish_reports")
    parser.add_argument("--snapshot-root", default="panel_data/doudian_api_media_publish_snapshots")
    args = parser.parse_args()

    shop_id, folder_id = str(args.shop_id).strip(), str(args.folder_id).strip()
    if not shop_id.isdigit() or not folder_id.isdigit():
        raise SystemExit("shop_id and folder_id must be numeric")
    if not 1 <= args.limit <= 10:
        raise SystemExit("limit must be between 1 and 10")
    selected_product_ids = tuple(str(value).strip() for value in args.product_id if str(value).strip())
    if any(not value.isdigit() for value in selected_product_ids):
        raise SystemExit("--product-id must be numeric")
    if selected_product_ids and len(selected_product_ids) > args.limit:
        raise SystemExit("--product-id count cannot exceed --limit")
    if args.save_draft_only and not args.execute:
        raise SystemExit("--save-draft-only requires --execute")
    if not 1 <= args.edit_rate_limit_attempts <= 5 or args.edit_rate_limit_backoff < 0:
        raise SystemExit("invalid editV2 rate-limit retry configuration")
    source_snapshot = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    if str(source_snapshot.get("shop_id") or "") != shop_id:
        raise SystemExit("snapshot shop does not match requested shop")

    report: dict[str, Any] = {
        "schema_version": 1, "created_at": int(time.time()), "shop_id": shop_id,
        "source_snapshot": str(Path(args.snapshot).resolve()),
        "mode": "save_draft_only" if args.save_draft_only else ("execute" if args.execute else "dry_run"),
        "operations": [
            "open.materialToken+ImageX+material.uploadImageSync",
            "product.editV2(commit=false,pic,description,product_format_new)",
            "product.detail exact media readback",
            *([] if args.save_draft_only else ["product.editV2(commit=true)"]),
        ],
        "main_area_source": args.main_area_source,
        "selected_product_ids": list(selected_product_ids),
        "edit_rate_limit_policy": {
            "attempts": args.edit_rate_limit_attempts,
            "initial_backoff_seconds": args.edit_rate_limit_backoff,
            "retry_only": "code=60000,sub_code=isv.request-limited",
        },
        "items": [], "skipped_candidates": [], "image_warnings": [], "manual_queue": [],
    }
    report_path = Path(args.report_root) / shop_id / f"{report['created_at']}.json"
    # Persist the transaction envelope before any remote read or write.  A
    # worker can be interrupted after a material upload or a product API call;
    # the next operator must still have the selected draft IDs and a durable
    # recovery trail rather than an absent report.
    _atomic_json(report_path, report)

    def should_stop() -> bool:
        return bool(args.stop_file and Path(args.stop_file).is_file())

    try:
        reader = DoudianProductReader(DoudianReadOnlyGateway(args.env, shop_id))
        property_gateway = DoudianReadOnlyGateway(args.env, shop_id)
        raw = property_gateway.call("product.getCatePropertyV2", {"category_leaf_id": int(PHONE_CATEGORY_ID)})
        container = raw.get("data") or {}
        properties = container.get("data", []) if isinstance(container, Mapping) else []
        model_prop, screen_prop = find_property(properties, MODEL_PROPERTY_ID), find_property(properties, SCREEN_PROPERTY_ID)
        if not model_prop or not screen_prop:
            raise LocalImagePolicyError("platform_model_or_screen_property_missing")
        image_assets = ImageAssetInspector(args.image_root, shop_id, allow_white_as_main=True)
        detail_assets = DetailImageInspector(args.detail_image_root)
        cloud_drive = None
        cloud_catalog = None
        if args.main_area_source == "feishu":
            env_values = read_env_file(args.env)
            cloud_drive = FeishuDriveClient(FeishuDriveConfig.from_mapping(env_values))
            cloud_catalog = FeishuMaterialCatalog(
                cloud_drive, layout=FeishuMaterialLayout.from_mapping(env_values),
            )

        prepared: list[tuple[dict[str, Any], Mapping[str, Any], dict[str, Any], Any, tuple[LocalImage, ...], tuple[LocalImage, ...]]] = []
        for row in _candidate_rows(source_snapshot):
            if len(prepared) >= args.limit:
                break
            product_id, title = str(row.get("product_id") or ""), str(row.get("name") or "")
            if selected_product_ids and product_id not in selected_product_ids:
                continue
            try:
                if args.main_area_source == "local":
                    # Local resolution happens first, preventing API detail
                    # scans for models that clearly have no safe source images.
                    parsed_for_source = dict(resolve_brand_model(title))
                    tentative_brand = str(parsed_for_source.get("target_brand_cn") or "").strip()
                    tentative_model = str(parsed_for_source.get("model") or "").strip()
                    source_evidence = image_assets.inspect(tentative_brand, tentative_model) if tentative_brand and tentative_model else {"ok": False}
                    if not source_evidence.get("ok"):
                        report["skipped_candidates"].append({
                            "product_id": product_id, "name": title,
                            "reason": "image_source_invalid:" + ",".join(source_evidence.get("reasons") or ["brand_or_model_unresolved"]),
                        })
                        continue
                before = dict(reader.get_product_detail(product_id, show_draft=True))
                if before.get("status") != 0 or str(before.get("check_status")) != "1":
                    continue
                if args.main_area_source == "feishu":
                    assert cloud_catalog is not None
                    parsed, area, detail_images, auxiliary, evidence = _resolve_feishu_sources(
                        shop_id, title, str(before.get("name_prefix") or ""), cloud_catalog, detail_assets,
                    )
                    main_area = (area.main, *auxiliary)
                    omitted_auxiliary = area.omitted_white_images
                else:
                    parsed, area, detail_images, auxiliary, evidence = _resolve_sources(
                        title, str(before.get("name_prefix") or ""), image_assets, detail_assets,
                    )
                    main_area = (area.main, *auxiliary)
                    omitted_auxiliary = area.skipped_auxiliary
                model, model_source = _model_option(before, title, model_prop)
                screen, screen_source = _screen_option(before, screen_prop)
                if not model or not screen:
                    raise LocalImagePolicyError("model_or_screen_unresolved")
                old_pic, old_description = _old_media(before)
                format_payload = build_property_edit_payload(before, {
                    MODEL_PROPERTY_ID: model, SCREEN_PROPERTY_ID: screen,
                })
                item = {
                    "product_id": product_id, "name": title,
                    "brand": parsed.get("target_brand_cn"), "model_name": parsed.get("model"),
                    "condition": parsed.get("condition"), "image_dir": evidence.get("image_dir"),
                    "main_area_source": args.main_area_source,
                    "main_substituted": area.main_substituted,
                    "main": _source_metadata(area.main),
                    "auxiliary": [_source_metadata(image) for image in auxiliary],
                    "omitted_auxiliary": [
                        {"name": skipped.name, "reason": skipped.reason}
                        for skipped in omitted_auxiliary
                    ],
                    "details": [_source_metadata(image) for image in detail_images],
                    "model": model, "screen_shape": screen,
                    "model_source": model_source, "screen_source": screen_source,
                    "old_pic_count": len(_pic_urls(before.get("pic")),),
                    "old_detail_image_count": len(parse_detail_urls(before.get("description"))),
                }
                format_payload["commit"] = False
                prepared.append((item, before, format_payload, area, main_area, detail_images))
                report["items"].append(item)
                if item["omitted_auxiliary"]:
                    report["image_warnings"].append({
                        "product_id": product_id, "name": title,
                        "uploaded_main_area_count": 1 + len(auxiliary),
                        "omitted_auxiliary": item["omitted_auxiliary"],
                    })
                _atomic_json(report_path, report)
            except Exception as exc:
                # These items have not been touched.  They are informative
                # candidate skips, not failures of the chosen 10-product
                # transaction and must not prevent an otherwise safe batch.
                skipped_item = {
                    "product_id": product_id, "name": title, "reason": str(exc),
                }
                omitted = getattr(exc, "skipped_auxiliary", ())
                if omitted:
                    skipped_item["omitted_auxiliary"] = [
                        {"name": item.name, "reason": item.reason} for item in omitted
                    ]
                report["skipped_candidates"].append(skipped_item)
                _atomic_json(report_path, report)

        if not _can_start_with_safe_candidates(len(prepared)):
            report["execution_blocked"] = "no_safe_candidates"
        elif args.execute:
            materials = DoudianMaterialGateway(args.env, shop_id, cache_root=args.cache_root)
            cloud_executor = None
            if args.main_area_source == "feishu":
                assert cloud_drive is not None
                cloud_executor = CloudMaterialExecutor(
                    cloud_drive, materials, MaterialMetadataCache(args.feishu_cache_root),
                    shop_id=shop_id, folder_id=folder_id,
                )
            for item, before, format_payload, area, main_area, detail_images in prepared:
                if should_stop():
                    report["stopped"] = True
                    report["stop_reason"] = "panel_stop_requested"
                    break
                product_id = item["product_id"]
                snapshot_path = Path(args.snapshot_root) / shop_id / f"{int(time.time())}_{product_id}.json"
                _atomic_json(snapshot_path, {"shop_id": shop_id, "captured_at": int(time.time()), "detail": before})
                item.update({"before_snapshot": str(snapshot_path), "transaction_state": "snapshot_saved"})
                _atomic_json(report_path, report)
                try:
                    item["transaction_state"] = "uploading_materials"
                    _atomic_json(report_path, report)
                    if cloud_executor is not None:
                        resolved_main = cloud_executor.resolve(area)
                        pic_urls = _pic_urls(resolved_main.pic)
                        main_material_ids = [resolved_main.main.material_id, *(
                            image.material_id for image in resolved_main.white_images
                        )]
                    else:
                        pic_urls, main_material_ids = _material_urls(materials, folder_id, main_area)
                    detail_urls, detail_material_ids = _material_urls(materials, folder_id, detail_images)
                    item.update({
                        "main_material_ids": main_material_ids,
                        "detail_material_ids": detail_material_ids,
                        "expected_main_count": len(pic_urls),
                        "expected_detail_count": len(detail_urls),
                        "transaction_state": "materials_uploaded",
                    })
                    _atomic_json(report_path, report)
                    payload = {
                        **format_payload, "pic": "|".join(pic_urls),
                        "description": detail_html(detail_urls),
                    }
                    writer = DoudianSingleProductImageDraftWriter(args.env, shop_id, product_id)
                    item["transaction_state"] = "draft_write_pending"
                    _atomic_json(report_path, report)

                    def on_rate_limited(attempt: int, delay: float, error: Exception) -> None:
                        item.setdefault("rate_limit_retries", []).append({
                            "attempt": attempt,
                            "wait_seconds": delay,
                            "error": str(error),
                        })
                        item["transaction_state"] = "rate_limited_waiting"
                        _atomic_json(report_path, report)

                    draft_response, attempt_count = _save_draft_with_rate_limit_retry(
                        writer, payload, attempts=args.edit_rate_limit_attempts,
                        backoff_seconds=args.edit_rate_limit_backoff,
                        on_rate_limited=on_rate_limited,
                    )
                    item.update({
                        "draft_write_code": draft_response.get("code"),
                        "draft_write_attempt_count": attempt_count,
                        "transaction_state": "draft_write_returned",
                    })
                    _atomic_json(report_path, report)
                    after_draft = dict(reader.get_product_detail(product_id, show_draft=True))
                    issues = _verify_media_draft(before, after_draft, item["model"], item["screen_shape"])
                    issues.extend(verify_media_readback(after_draft, payload["pic"], detail_urls))
                    item.update({
                        "verification_issues": issues,
                    })
                    if issues:
                        try:
                            old_pic, old_description = _old_media(before)
                            rollback_payload = build_property_edit_payload(before, {})
                            rollback_payload.update({
                                "pic": old_pic, "description": old_description, "commit": False,
                            })
                            rollback = writer.save_images_to_draft(rollback_payload)
                            item.update({
                                "result": "rolled_back", "rollback_code": rollback.get("code"),
                                "transaction_state": "rolled_back",
                            })
                            reason = "media_readback_failed:" + ",".join(issues)
                        except Exception as rollback_exc:
                            item.update({
                                "result": "rollback_failed", "rollback_error": str(rollback_exc),
                                "transaction_state": "rollback_failed",
                            })
                            reason = "media_readback_failed:" + ",".join(issues) + ";rollback_failed:" + str(rollback_exc)
                        report["manual_queue"].append({"product_id": product_id, "name": item["name"], "reason": reason})
                    elif args.save_draft_only:
                        item.update({"result": "draft_verified", "transaction_state": "draft_verified"})
                    else:
                        submission = {
                            "product_id": product_id,
                            "product_format_new": payload["product_format_new"],
                            "commit": True,
                        }
                        name_prefix = str(after_draft.get("name_prefix") or "").strip()
                        if name_prefix:
                            submission["name_prefix"] = name_prefix
                        publisher = DoudianSingleProductPublisher(
                            args.env, shop_id, product_id, submission_payload_sha256(submission),
                        )
                        response = publisher.submit_planned_draft(submission)
                        after_submit = dict(reader.get_product_detail(product_id, show_draft=True))
                        item.update({
                            "submit_code": response.get("code"), "result": "accepted",
                            "transaction_state": "accepted",
                            "after_submit": {
                                "status": after_submit.get("status"),
                                "check_status": after_submit.get("check_status"),
                                "main_count": len(_pic_urls(after_submit.get("pic"))),
                                "detail_image_count": len(parse_detail_urls(after_submit.get("description"))),
                            },
                        })
                except Exception as exc:
                    item.update({"result": "api_error", "error": str(exc), "transaction_state": "failed"})
                    report["manual_queue"].append({"product_id": product_id, "name": item["name"], "reason": "api_error:" + str(exc)})
                _atomic_json(report_path, report)

        report["summary"] = {
            "requested": args.limit, "prepared": len(prepared),
            "target_shortfall": max(0, args.limit - len(prepared)),
            "accepted": sum(item.get("result") == "accepted" for item in report["items"]),
            "draft_verified": sum(item.get("result") == "draft_verified" for item in report["items"]),
            "rolled_back": sum(item.get("result") == "rolled_back" for item in report["items"]),
            "skipped_candidates": len(report["skipped_candidates"]),
            "image_warning_products": len(report["image_warnings"]),
            "omitted_auxiliary_images": sum(len(item["omitted_auxiliary"]) for item in report["items"]),
            "manual_queue": len(report["manual_queue"]),
        }
    except Exception as exc:
        report["fatal_error"] = str(exc)
        report["summary"] = {"requested": args.limit, "prepared": 0, "target_shortfall": args.limit, "accepted": 0, "draft_verified": 0, "rolled_back": 0, "skipped_candidates": len(report["skipped_candidates"]), "image_warning_products": len(report["image_warnings"]), "omitted_auxiliary_images": 0, "manual_queue": len(report["manual_queue"])}
    _atomic_json(report_path, report)
    print(json.dumps({"summary": report["summary"], "report": str(report_path)}, ensure_ascii=False))
    if not args.execute:
        return 0 if _can_start_with_safe_candidates(report["summary"]["prepared"]) and not report.get("execution_blocked") else 2
    if report.get("stopped"):
        return 0
    successful = report["summary"]["draft_verified"] if args.save_draft_only else report["summary"]["accepted"]
    return 0 if successful == report["summary"]["prepared"] and successful > 0 and not report["manual_queue"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
