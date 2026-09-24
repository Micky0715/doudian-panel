"""Remove only explicitly blocked legacy detail assets from selected products.

The command is dry-run by default. In execute mode it creates and verifies an
edit draft first, then submits that exact product update and verifies the live
readback. It never removes an image by position and stops a product before
submission if any non-target product content changes.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Mapping

from doudian_product_service import normalize_product_format_for_edit, parse_product_format
from integrations.doudian import (
    DoudianProductReader, DoudianReadOnlyGateway, recommend_title_for_submission,
)
from integrations.doudian.write_gateway import (
    DoudianSingleProductImageDraftWriter, DoudianSingleProductPublisher,
    submission_payload_sha256,
)
from scripts.doudian_apply_local_images import _pic_urls, detail_urls
from scripts.doudian_full_media_draft_once import _post_publish_issues
from scripts.doudian_grade_detail_draft_once import filter_blocked_detail_urls
from scripts.doudian_sku_color_draft_once import _platform_path, _product_id, _stable, _write_json


class BlockedDetailCleanupError(RuntimeError):
    pass


CONTENT_FIELDS = (
    "name", "name_prefix", "name_suffix", "brand_id", "standard_brand_id",
    "freight_id", "product_type", "spec_id", "specs", "spec_prices", "spec_pics",
    "white_back_ground_pic_url", "main_pic_3_4",
)


def _content_snapshot(detail: Mapping[str, Any], *, include_description: bool = True) -> dict[str, Any]:
    result = {key: detail.get(key) for key in CONTENT_FIELDS}
    result["product_format_new"] = normalize_product_format_for_edit(
        parse_product_format(detail.get("product_format_new"))
    )
    result["pic"] = [_platform_path(url) for url in _pic_urls(detail.get("pic"))]
    if include_description:
        result["description"] = [
            _platform_path(url) for url in detail_urls(detail.get("description"))
        ]
    return result


def _selected_ids(batch_report: str, explicit_ids: list[str]) -> list[str]:
    values = [str(value).strip() for value in explicit_ids if str(value).strip()]
    if batch_report:
        report = json.loads(Path(batch_report).read_text(encoding="utf-8"))
        values.extend(str(item.get("product_id") or "").strip()
                      for item in report.get("items", []) if isinstance(item, Mapping))
    values = [value for value in values if value]
    if not values or any(not value.isdigit() for value in values):
        raise BlockedDetailCleanupError("必须提供至少一个有效商品 ID")
    if len(values) != len(set(values)):
        raise BlockedDetailCleanupError("商品 ID 重复")
    if len(values) > 10:
        raise BlockedDetailCleanupError("单次最多清理 10 件商品")
    return values


def _wait_for_live(reader: DoudianProductReader, product_id: str,
                   expected_urls: list[str]) -> Mapping[str, Any]:
    expected = [_platform_path(url) for url in expected_urls]
    last: Mapping[str, Any] = {}
    for attempt in range(12):
        last = dict(reader.get_product_detail(product_id, show_draft=False))
        actual = [_platform_path(url) for url in detail_urls(last.get("description"))]
        if last.get("status") == 0 and last.get("check_status") == 3 and actual == expected:
            return last
        if attempt < 11:
            time.sleep(5)
    return last


def main() -> int:
    parser = argparse.ArgumentParser(description="Remove one blocked legacy detail image safely")
    parser.add_argument("shop_id")
    parser.add_argument("--product-id", action="append", default=[])
    parser.add_argument("--batch-report", default="")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report-root", default="panel_data/doudian_blocked_detail_cleanup")
    args = parser.parse_args()
    shop_id = str(args.shop_id).strip()
    if not shop_id.isdigit():
        parser.error("shop_id must be numeric")
    product_ids = _selected_ids(args.batch_report, args.product_id)

    gateway = DoudianReadOnlyGateway(args.env, shop_id)
    reader = DoudianProductReader(gateway)
    stamp = int(time.time())
    report_path = Path(args.report_root) / shop_id / f"{stamp}.json"
    report: dict[str, Any] = {
        "shop_id": shop_id, "created_at": stamp, "execute": args.execute,
        "product_ids": product_ids, "items": [], "result": "started",
    }
    _write_json(report_path, report)

    failed = False
    for product_id in product_ids:
        item: dict[str, Any] = {"product_id": product_id, "result": "checking"}
        report["items"].append(item)
        _write_json(report_path, report)
        try:
            live_before = dict(reader.get_product_detail(product_id, show_draft=False))
            draft_before = dict(reader.get_product_detail(product_id, show_draft=True))
            if _product_id(live_before) != product_id or _product_id(draft_before) != product_id:
                raise BlockedDetailCleanupError("商品详情 ID 与清理目标不一致")
            if live_before.get("status") != 0 or live_before.get("check_status") != 3:
                raise BlockedDetailCleanupError("商品当前不是审核通过的在售状态")
            if _content_snapshot(live_before) != _content_snapshot(draft_before):
                raise BlockedDetailCleanupError("商品存在未提交草稿，拒绝覆盖")

            original_urls = detail_urls(live_before.get("description"))
            retained_urls, blocked_urls = filter_blocked_detail_urls(original_urls)
            item.update({
                "before_detail_count": len(original_urls),
                "after_detail_count": len(retained_urls),
                "blocked_urls": blocked_urls,
            })
            if not blocked_urls:
                item["result"] = "already_clean"
                _write_json(report_path, report)
                continue
            if len(blocked_urls) != 1 or len(retained_urls) != len(original_urls) - 1:
                raise BlockedDetailCleanupError("禁用详情图命中数量不是 1，拒绝改动")
            if len(retained_urls) < 7:
                raise BlockedDetailCleanupError("删除后详情图少于安全下限 7 张")
            item["result"] = "ready" if not args.execute else "draft_write_pending"
            _write_json(report_path, report)
            if not args.execute:
                continue

            stem = report_path.with_name(f"{stamp}_{product_id}")
            _write_json(stem.with_suffix(".before.json"), {
                "captured_at": int(time.time()), "detail": live_before,
            })
            payload = {
                "product_id": product_id,
                "product_format_new": _stable(normalize_product_format_for_edit(
                    parse_product_format(live_before.get("product_format_new"))
                )),
                "pic": "|".join(_pic_urls(live_before.get("pic"))),
                "description": "|".join(retained_urls),
                "commit": False,
            }
            DoudianSingleProductImageDraftWriter(
                args.env, shop_id, product_id,
            ).save_images_to_draft(payload)
            draft_after = dict(reader.get_product_detail(product_id, show_draft=True))
            _write_json(stem.with_suffix(".draft.json"), {
                "captured_at": int(time.time()), "detail": draft_after,
            })
            if (_content_snapshot(live_before, include_description=False)
                    != _content_snapshot(draft_after, include_description=False)):
                raise BlockedDetailCleanupError("草稿回读出现非目标字段变化，未提交发布")
            if [_platform_path(url) for url in detail_urls(draft_after.get("description"))] != [
                _platform_path(url) for url in retained_urls
            ]:
                raise BlockedDetailCleanupError("草稿回读未精准删除目标详情图，未提交发布")

            title_plan = recommend_title_for_submission(gateway, draft_after)
            submission = {
                "product_id": product_id,
                "product_format_new": payload["product_format_new"],
                "name": title_plan.name,
                "name_prefix": title_plan.name_prefix,
                "commit": True,
            }
            digest = submission_payload_sha256(submission)
            item.update({"result": "publish_pending", "publish_payload_sha256": digest})
            _write_json(report_path, report)
            DoudianSingleProductPublisher(
                args.env, shop_id, product_id, digest,
            ).submit_planned_draft(submission)
            live_after = dict(_wait_for_live(reader, product_id, retained_urls))
            _write_json(stem.with_suffix(".after.json"), {
                "captured_at": int(time.time()), "detail": live_after,
            })
            issues, stock_sync = _post_publish_issues(
                draft_after, live_after,
                expected_name=title_plan.name, expected_prefix=title_plan.name_prefix,
            )
            if any(marker in _platform_path(url)
                   for url in detail_urls(live_after.get("description"))
                   for marker in ("1c7ee38c4ce1e75da8cd70eae3ed619f",)):
                issues.append("blocked_detail_still_present")
            item.update({"post_publish_issues": issues, "stock_sync": stock_sync})
            item["result"] = "published_readback" if not issues else "needs_manual_review"
            if issues:
                failed = True
            _write_json(report_path, report)
        except Exception as exc:
            failed = True
            item.update({"result": "failed", "error": f"{type(exc).__name__}: {str(exc)[:400]}"})
            _write_json(report_path, report)
            break

    report["result"] = "completed" if not failed else "stopped_on_failure"
    report["summary"] = {
        "requested": len(product_ids),
        "published_readback": sum(item.get("result") == "published_readback"
                                  for item in report["items"]),
        "already_clean": sum(item.get("result") == "already_clean" for item in report["items"]),
        "failed": sum(item.get("result") in {"failed", "needs_manual_review"}
                      for item in report["items"]),
    }
    _write_json(report_path, report)
    print(json.dumps({"result": report["result"], "summary": report["summary"],
                      "report": str(report_path)}, ensure_ascii=False))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
