"""Panel batch orchestration for the verified local full-media API path.

Read-only preflight runs through a small bounded worker pool.  As soon as the
next draft passes, the main thread performs its complete write/readback
transaction while the pool continues checking later drafts.  Writes therefore
remain strictly serial within a shop, but the user no longer waits for the
whole candidate set to finish preflight before the first item starts.

There is no browser fallback. Exact colour images are uploaded when present;
missing colour images are recorded and do not block the product.
"""

from __future__ import annotations

import argparse
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from scripts.doudian_edit_phone_properties_batch import PHONE_CATEGORY_ID


def _emit_panel_progress(**values: Any) -> None:
    """Stream progress to the panel without changing the final CLI envelope."""
    if not os.environ.get("DP_TASK_ID"):
        return
    print("##PANEL_PROGRESS" + json.dumps(values, ensure_ascii=False), flush=True)


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


def _json_rows(lines: Iterable[str]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            rows.append(value)
    return rows


def _failure_reason(lines: list[str]) -> str:
    nonempty = [line.strip() for line in lines if line.strip()]
    if not nonempty:
        return "子进程没有返回错误信息"
    return nonempty[-1][-500:]


def _run_single(args: argparse.Namespace, product_id: str, snapshot_name: str, *, execute: bool) -> tuple[
    int, Mapping[str, Any], Mapping[str, Any], list[str]
]:
    command = [
        sys.executable, "-m", "scripts.doudian_full_media_draft_once",
        args.shop_id, product_id,
        "--folder-id", args.folder_id,
        "--env", args.env,
        "--image-root", args.image_root,
        "--color-root", args.color_root,
        "--detail-root", args.detail_root,
        "--report-root", args.single_report_root,
        "--snapshot-name", str(snapshot_name or ""),
        "--publish-retries", str(args.publish_retries),
        "--main-area-source", args.main_area_source,
        "--feishu-cache-root", args.feishu_cache_root,
    ]
    if execute:
        command.append("--execute")
        if not args.save_draft_only:
            command.append("--publish")
    completed = subprocess.run(
        command,
        cwd=str(Path(__file__).resolve().parents[1]),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    lines = completed.stdout.splitlines()
    rows = _json_rows(lines)
    summary = rows[0] if rows else {}
    envelope = rows[-1] if rows else {}
    return completed.returncode, summary, envelope, lines


def _candidate_rows(snapshot: Mapping[str, Any], selected_ids: tuple[str, ...],
                    excluded_ids: frozenset[str] = frozenset()) -> list[Mapping[str, Any]]:
    rows = [
        row for row in snapshot.get("items", [])
        if isinstance(row, Mapping)
        and str(row.get("category_leaf_id") or "") == PHONE_CATEGORY_ID
        and str(row.get("product_id") or "") not in excluded_ids
    ]
    if not selected_ids:
        return rows
    by_id = {str(row.get("product_id") or ""): row for row in rows}
    return [by_id[value] for value in selected_ids if value in by_id]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verified full-media local API batch")
    parser.add_argument("shop_id")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--folder-id", required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--product-id", action="append", default=[])
    parser.add_argument("--exclude-product-id", action="append", default=[])
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--image-root", default="/Users/atrenew/商品图片")
    parser.add_argument("--color-root", default="/Users/atrenew/商品图片/店铺SKU颜色图")
    parser.add_argument("--detail-root", default="/Users/atrenew/手机商详")
    parser.add_argument("--main-area-source", choices=("local", "feishu"), default="local",
                        help="main/white source; Feishu also supplies exact colour-name files")
    parser.add_argument("--feishu-cache-root", default="panel_data/material_metadata_cache")
    parser.add_argument("--stop-file", default="")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--save-draft-only", action="store_true")
    parser.add_argument("--inter-product-delay", type=float, default=0.0)
    parser.add_argument(
        "--preflight-workers", type=int, default=2,
        help="parallel read-only preflight workers; writes remain serial (default: 2)",
    )
    parser.add_argument(
        "--publish-retries", type=int, default=2,
        help="safe retries after each initial final submission (default: 2)",
    )
    parser.add_argument("--report-root", default="panel_data/doudian_full_media_batch")
    parser.add_argument("--single-report-root", default="panel_data/doudian_full_media_canary")
    args = parser.parse_args()
    args.shop_id = str(args.shop_id).strip()
    args.folder_id = str(args.folder_id).strip()
    selected_ids = tuple(str(value).strip() for value in args.product_id if str(value).strip())
    excluded_ids = frozenset(
        str(value).strip() for value in args.exclude_product_id if str(value).strip()
    )
    if not args.shop_id.isdigit() or not args.folder_id.isdigit():
        parser.error("shop_id and folder_id must be numeric")
    if args.limit < 1:
        parser.error("limit must be positive")
    if not 0 <= args.inter_product_delay <= 60:
        parser.error("inter-product-delay must be between 0 and 60 seconds")
    if not 1 <= args.preflight_workers <= 4:
        parser.error("preflight-workers must be between 1 and 4")
    if not 0 <= args.publish_retries <= 5:
        parser.error("publish-retries must be between 0 and 5")
    if any(not value.isdigit() for value in selected_ids):
        parser.error("product ids must be numeric")
    if any(not value.isdigit() for value in excluded_ids):
        parser.error("excluded product ids must be numeric")
    if len(selected_ids) != len(set(selected_ids)) or len(selected_ids) > args.limit:
        parser.error("product ids must be unique and no more than limit")
    if args.save_draft_only and not args.execute:
        parser.error("--save-draft-only requires --execute")

    snapshot_path = Path(args.snapshot).resolve()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if str(snapshot.get("shop_id") or "") != args.shop_id:
        parser.error("snapshot shop does not match requested shop")
    candidates = _candidate_rows(snapshot, selected_ids, excluded_ids)
    report: dict[str, Any] = {
        "schema_version": 2,
        "pipeline": args.main_area_source + "_full_media_v2",
        "execution_strategy": "streaming_preflight_serial_write",
        "preflight_workers": args.preflight_workers,
        "created_at": int(time.time()),
        "shop_id": args.shop_id,
        "source_snapshot": str(snapshot_path),
        "mode": "save_draft_only" if args.save_draft_only else (
            "publish" if args.execute else "dry_run"
        ),
        "publish_retry_limit": args.publish_retries,
        "operations": [
            "exact model + screen-shape resolution",
            ("exact Feishu main/white + white-name SKU colour + grade-detail resolution"
             if args.main_area_source == "feishu" else
             "exact local main/white + available SKU colour + grade-detail resolution"),
            "material upload with digest evidence",
            "product.editV2(commit=false,pic,description,optional spec_pic,product_format_new,preserved white background)",
            "complete draft readback",
            *([] if args.save_draft_only or not args.execute else [
                "product.editV2(commit=true; initial + up to %d safe retries)"
                % args.publish_retries,
                "online audit-passed readback",
            ]),
        ],
        "selected_product_ids": list(selected_ids),
        "excluded_product_ids": sorted(excluded_ids),
        "items": [],
        "skipped_candidates": [],
        "manual_queue": [],
        "image_warnings": [],
        "color_warnings": [],
    }
    report_path = Path(args.report_root) / args.shop_id / f"{report['created_at']}.json"
    _atomic_json(report_path, report)

    prepared: list[dict[str, Any]] = []
    report["items"] = prepared
    checked_count = 0
    peak_inflight = 0
    candidate_index = 0
    worker_count = min(args.preflight_workers, args.limit, max(1, len(candidates)))
    pending: deque[tuple[Mapping[str, Any], Future[tuple[
        int, Mapping[str, Any], Mapping[str, Any], list[str]
    ]]]] = deque()

    def stopped() -> bool:
        return bool(args.stop_file and Path(args.stop_file).is_file())

    def submit_until_full(executor: ThreadPoolExecutor) -> None:
        nonlocal candidate_index, peak_inflight
        # Do not speculatively preflight more rows than are still needed to
        # reach the requested number of safe products.  A failed result frees
        # one slot and causes the next candidate to be scheduled.
        while (candidate_index < len(candidates)
               and len(prepared) + len(pending) < args.limit
               and len(pending) < worker_count
               and not stopped()):
            row = candidates[candidate_index]
            candidate_index += 1
            product_id = str(row.get("product_id") or "")
            snapshot_name = str(row.get("name") or "")
            pending.append((row, executor.submit(
                _run_single, args, product_id, snapshot_name, execute=False,
            )))
            peak_inflight = max(peak_inflight, len(pending))

    _emit_panel_progress(
        total=args.limit, processed=0, success=0, failed=0, skipped=0,
        current="启动 %d 路只读预检；通过后立即进入串行写入" % worker_count,
    )
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        submit_until_full(executor)
        while pending and len(prepared) < args.limit:
            if stopped():
                report.update({"stopped": True, "stop_reason": "panel_stop_requested"})
                for _, future in pending:
                    future.cancel()
                break
            row, future = pending.popleft()
            product_id = str(row.get("product_id") or "")
            name = str(row.get("name") or "")
            try:
                code, summary, _, lines = future.result()
            except Exception as exc:
                code, summary, lines = 2, {}, ["并发预检异常: %s" % exc]
            checked_count += 1
            if code == 0 and str(summary.get("product_id") or "") == product_id:
                item = {"product_id": product_id, "name": name,
                        "preflight": dict(summary), "preflight_order": checked_count}
                prepared.append(item)
                omitted = summary.get("omitted_auxiliary") or []
                if omitted or summary.get("main_substituted"):
                    report["image_warnings"].append({
                        "product_id": product_id,
                        "name": name,
                        "uploaded_main_area_count": 1 + len(summary.get("white") or []),
                        "main_substituted": bool(summary.get("main_substituted")),
                        "omitted_auxiliary": omitted,
                    })
                if (summary.get("color_strategy") != "complete"
                        or summary.get("color_missing") or summary.get("color_invalid")
                        or summary.get("color_plan_issue")):
                    report["color_warnings"].append({
                        "product_id": product_id,
                        "name": name,
                        "strategy": summary.get("color_strategy"),
                        "uploaded": int(summary.get("color_uploaded_count") or 0),
                        "total": int(summary.get("color_spec_count") or 0),
                        "missing": list(summary.get("color_missing") or []),
                        "invalid": list(summary.get("color_invalid") or []),
                        "reason": str(summary.get("color_plan_issue") or ""),
                    })
            else:
                report["skipped_candidates"].append({
                    "product_id": product_id, "name": name,
                    "reason": _failure_reason(lines),
                })

            # Persist every preflight result before any write starts.  The
            # executor continues checking the next rows while this main thread
            # performs the one permitted serial write transaction.
            _atomic_json(report_path, report)
            if code == 0 and str(summary.get("product_id") or "") == product_id \
                    and args.execute and not stopped():
                item = prepared[-1]
                product_id = item["product_id"]
                _emit_panel_progress(
                    total=args.limit,
                    processed=sum(bool(value.get("result")) for value in prepared),
                    success=0, failed=len(report["manual_queue"]),
                    skipped=len(report["skipped_candidates"]),
                    current="预检通过，正在%s：%s" % (
                        "保存草稿" if args.save_draft_only else "发布", name or product_id,
                    ),
                )
                item["transaction_state"] = "full_media_write_pending"
                _atomic_json(report_path, report)
                write_code, _, envelope, write_lines = _run_single(
                    args, product_id, item["name"], execute=True,
                )
                item["result"] = str(envelope.get("result") or "api_error")
                item["transaction_state"] = item["result"]
                if envelope.get("report"):
                    single_report_path = Path(str(envelope["report"])).resolve()
                    item["single_report"] = str(single_report_path)
                    try:
                        single_report = json.loads(single_report_path.read_text(encoding="utf-8"))
                        item["publish_attempt_count"] = int(
                            single_report.get("publish_attempt_count") or 0
                        )
                        item["publish_retry_count"] = int(
                            single_report.get("publish_retry_count") or 0
                        )
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        pass
                if (write_code != 0
                        or item["result"] not in {"verified_draft", "published_readback"}):
                    reason_values = envelope.get("post_publish_issues") or envelope.get("errors") or []
                    reason = str(envelope.get("error") or ",".join(
                        str(value) for value in reason_values
                    ) or _failure_reason(write_lines))
                    item["error"] = reason
                    report["manual_queue"].append({
                        "product_id": product_id, "name": item["name"], "reason": reason,
                    })
                _atomic_json(report_path, report)
                expected_result = "verified_draft" if args.save_draft_only else "published_readback"
                _emit_panel_progress(
                    total=args.limit,
                    processed=sum(bool(value.get("result")) for value in prepared),
                    success=sum(value.get("result") == expected_result for value in prepared),
                    failed=len(report["manual_queue"]),
                    skipped=len(report["skipped_candidates"]),
                    current="已完成：%s；后台继续预检" % (item.get("name") or product_id),
                )
                if len(prepared) < args.limit and args.inter_product_delay:
                    time.sleep(args.inter_product_delay)

            submit_until_full(executor)
            if not args.execute:
                _emit_panel_progress(
                    total=args.limit, processed=0, success=0, failed=0,
                    skipped=len(report["skipped_candidates"]),
                    current="已完成素材预检 %d/%d" % (len(prepared), args.limit),
                )

    report["preflight_checked"] = checked_count
    report["preflight_peak_inflight"] = peak_inflight
    _atomic_json(report_path, report)
    if not prepared:
        report["execution_blocked"] = "no_safe_full_media_candidates"

    expected = "verified_draft" if args.save_draft_only else "published_readback"
    report["summary"] = {
        "requested": args.limit,
        "prepared": len(prepared),
        "target_shortfall": max(0, args.limit - len(prepared)),
        "published_readback": sum(item.get("result") == "published_readback" for item in prepared),
        "draft_verified": sum(item.get("result") == "verified_draft" for item in prepared),
        "skipped_candidates": len(report["skipped_candidates"]),
        "image_warning_products": len(report["image_warnings"]),
        "omitted_auxiliary_images": sum(
            len(item.get("omitted_auxiliary") or []) for item in report["image_warnings"]
        ),
        "color_warning_products": len(report["color_warnings"]),
        "manual_queue": len(report["manual_queue"]),
        "preflight_checked": checked_count,
        "preflight_peak_inflight": peak_inflight,
    }
    _atomic_json(report_path, report)
    _emit_panel_progress(
        total=len(prepared),
        processed=sum(bool(item.get("result")) for item in prepared),
        success=sum(item.get("result") == expected for item in prepared),
        failed=len(report["manual_queue"]),
        skipped=len(report["skipped_candidates"]), current="",
    )
    print(json.dumps({"summary": report["summary"], "report": str(report_path)}, ensure_ascii=False))
    if not args.execute:
        return 0 if prepared and not report.get("execution_blocked") else 2
    if report.get("stopped"):
        return 0
    success = sum(item.get("result") == expected for item in prepared)
    return 0 if success == len(prepared) and success > 0 and not report["manual_queue"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
