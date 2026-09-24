#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Panel worker for the verified DouDian API media-publish path.

This worker intentionally has no Playwright dependency.  It creates a fresh,
complete draft snapshot, delegates the write/readback transaction to the
strict media batch command, and converts its persisted report into the panel's
``##EVT`` protocol.  Browser automation remains a separate fallback engine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.drafts import DraftScanner, DraftSnapshotStore


WORK_DIR = Path(__file__).resolve().parent
REPORT_ROOT = WORK_DIR / "panel_data" / "doudian_api_media_publish_reports"
SNAPSHOT_ROOT = WORK_DIR / "panel_data" / "doudian_draft_snapshots"
ENV_FILE = Path(os.environ.get("DP_ENV_FILE") or (
    WORK_DIR / "env.local" if (WORK_DIR / "env.local").is_file() else WORK_DIR / ".env.local"
)).resolve()


def emit(event_type: str, **data: Any) -> None:
    """Write one panel event, without leaking environment values."""
    print("##EVT" + json.dumps({"t": event_type, **data}, ensure_ascii=False), flush=True)


def log(tag: str, message: str, level: str = "info") -> None:
    emit("log", time=time.strftime("%H:%M:%S"), tag=tag, msg=message, level=level)


def _normalise_omissions(values: Any) -> str:
    rows = values if isinstance(values, list) else []
    return "；".join(
        "%s（%s）" % (str(row.get("name") or "未命名图片"), str(row.get("reason") or "未知原因"))
        for row in rows if isinstance(row, Mapping)
    )


def summarise_media_report(
    report: Mapping[str, Any],
    *,
    save_draft_only: bool,
) -> tuple[dict[str, Any], list[dict[str, str]], list[dict[str, str]]]:
    """Return panel progress, non-blocking image warnings, and manual items.

    A skipped oversized auxiliary image is operationally important, but is not
    a failed product when the remaining media still satisfy the 1+2 minimum.
    It is therefore a warning in logs, and a clearly labelled result entry,
    rather than a task failure.
    """
    summary = report.get("summary") if isinstance(report.get("summary"), Mapping) else {}
    items = report.get("items") if isinstance(report.get("items"), list) else []
    warnings = report.get("image_warnings") if isinstance(report.get("image_warnings"), list) else []
    skipped = report.get("skipped_candidates") if isinstance(report.get("skipped_candidates"), list) else []
    manual = report.get("manual_queue") if isinstance(report.get("manual_queue"), list) else []
    expected_results = (
        {"verified_draft", "draft_verified"}
        if save_draft_only else {"published_readback"}
    )
    success = sum(
        1 for item in items
        if isinstance(item, Mapping) and item.get("result") in expected_results
    )
    failed = len(manual)
    progress = {
        "total": int(summary.get("prepared") or len(items)),
        "processed": len([item for item in items if isinstance(item, Mapping) and item.get("result")]),
        "success": success,
        "failed": failed,
        "skipped": len(skipped),
        "current": "",
    }
    warning_entries: list[dict[str, str]] = []
    for warning in warnings:
        if not isinstance(warning, Mapping):
            continue
        omission = _normalise_omissions(warning.get("omitted_auxiliary"))
        warning_entries.append({
            "product_id": str(warning.get("product_id") or ""),
            "name": str(warning.get("name") or ""),
            "reason": "已省略辅助白底图：%s；主图区实际上传 %s 张" % (
                omission or "未提供明细", warning.get("uploaded_main_area_count") or "",
            ),
        })
    errors: list[dict[str, str]] = []
    for row in skipped:
        if isinstance(row, Mapping):
            errors.append({
                "product_id": str(row.get("product_id") or ""),
                "name": str(row.get("name") or ""),
                "error_type": "素材预检未通过",
                "reason": str(row.get("reason") or ""),
            })
    for row in manual:
        if isinstance(row, Mapping):
            errors.append({
                "product_id": str(row.get("product_id") or ""),
                "name": str(row.get("name") or ""),
                "error_type": "API 写入或回读失败",
                "reason": str(row.get("reason") or ""),
            })
    return progress, warning_entries, errors


def _read_report_path(lines: Iterable[str]) -> Path:
    """Read the batch command's final JSON envelope, tolerating SDK logging."""
    for raw in reversed(list(lines)):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        candidate = value.get("report") if isinstance(value, Mapping) else ""
        if candidate:
            return Path(str(candidate)).resolve()
    raise RuntimeError("API 素材批处理没有返回报告路径")


def _last_json_mapping(lines: Iterable[str]) -> Mapping[str, Any]:
    for raw in reversed(list(lines)):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping):
            return value
    raise RuntimeError("单商品全链路没有返回结果")


def run_batch_command(command: list[str]) -> tuple[int, list[str]]:
    """Run the batch worker while forwarding its structured item progress."""
    child = subprocess.Popen(
        command, cwd=str(WORK_DIR), text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        bufsize=1,
    )
    lines: list[str] = []
    if child.stdout is not None:
        for raw in iter(child.stdout.readline, ""):
            line = raw.rstrip("\n")
            lines.append(line)
            if line.startswith("##PANEL_PROGRESS"):
                try:
                    progress = json.loads(line[len("##PANEL_PROGRESS"):])
                except json.JSONDecodeError:
                    continue
                if isinstance(progress, Mapping):
                    emit("progress", **dict(progress))
            elif line.strip() and not line.lstrip().startswith("{"):
                log("API-OUT", line)
    return child.wait(), lines


def _latest_verified_full_media_report(shop_id: str, product_id: str) -> Path | None:
    directory = WORK_DIR / "panel_data" / "doudian_full_media_canary" / shop_id
    candidates = sorted(
        directory.glob("*_%s.report.json" % product_id),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if directory.is_dir() else []
    for path in candidates:
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (str(report.get("shop_id") or "") == shop_id
                and str(report.get("product_id") or "") == product_id
                and report.get("result") == "verified_draft"
                and report.get("verification_errors") == []
                and report.get("publish") is False):
            return path.resolve()
    return None


def run_single_full_media(shop_id: str, product_id: str, folder_id: str,
                          action: str) -> int:
    """Run the exact one-product path used by the panel canary button."""
    verified = _latest_verified_full_media_report(shop_id, product_id) \
        if action == "publish" else None
    if verified:
        command = [
            sys.executable, "-m", "scripts.doudian_submit_verified_full_media_once",
            shop_id, product_id, "--source-report", str(verified),
            "--env", str(ENV_FILE), "--execute",
        ]
    else:
        command = [
            sys.executable, "-m", "scripts.doudian_full_media_draft_once",
            shop_id, product_id, "--folder-id", folder_id, "--env", str(ENV_FILE),
        ]
        if action != "dry_run":
            command.append("--execute")
        if action == "publish":
            command.append("--publish")
    child = subprocess.run(
        command, cwd=str(WORK_DIR), text=True, encoding="utf-8", errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
        check=False,
    )
    lines = child.stdout.splitlines()
    for line in lines:
        if line.strip() and not line.lstrip().startswith("{"):
            log("API-OUT", line)
    envelope = _last_json_mapping(lines)
    report_path = Path(str(envelope.get("report") or "")).resolve() \
        if envelope.get("report") else None
    report: Mapping[str, Any] = {}
    if report_path and report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
    result = str(envelope.get("result") or report.get("result") or "")
    expected = "published_readback" if action == "publish" else (
        "verified_draft" if action == "save_draft" else ""
    )
    if action == "dry_run":
        ok = child.returncode == 0
        message = "全链路预检通过" if ok else "全链路预检未通过"
    else:
        ok = child.returncode == 0 and result == expected
        message = ("已发布并确认在线" if action == "publish" else "已保存草稿并完成全媒体回读") \
            if ok else "全链路未完成，已停止并转人工核对"
    for omitted in report.get("omitted_auxiliary") or []:
        if isinstance(omitted, Mapping):
            log("IMAGE", "%s：未上传（%s）" % (
                omitted.get("name") or "辅助白底图", omitted.get("reason") or "不合规",
            ), "warning")
    if ok:
        retry_count = int(report.get("publish_retry_count") or 0)
        if retry_count:
            log("RETRY", "最终提交在第 %d 次尝试成功（已安全重试 %d 次）" % (
                int(report.get("publish_attempt_count") or retry_count + 1), retry_count,
            ), "warning")
        log("API", "%s：%s" % (report.get("model") or product_id, message), "success")
        emit("progress", total=1, processed=1, success=1, failed=0, skipped=0, current="")
        emit("errors", errors=[])
        return 0
    reason = str(report.get("error") or ",".join(
        str(value) for value in (envelope.get("post_publish_issues") or envelope.get("errors") or [])
    ) or "执行结果未达到验收标准")
    log("API", "%s：%s；%s" % (product_id, message, reason), "error")
    emit("progress", total=1, processed=1, success=0, failed=1, skipped=0, current="")
    emit("errors", errors=[{
        "product_id": product_id, "name": str(report.get("model") or ""),
        "error_type": "单商品全链路失败", "reason": reason,
    }])
    return child.returncode or 2


def create_snapshot(shop_id: str) -> tuple[Path, int, int]:
    gateway = DoudianReadOnlyGateway(ENV_FILE, shop_id)
    reader = DoudianProductReader(gateway)
    snapshot = DraftScanner(reader, shop_id).scan(page_size=100, max_pages=200)
    path = DraftSnapshotStore(SNAPSHOT_ROOT).save(snapshot)
    return path, snapshot.reported_total, snapshot.page_count


def effective_limit(mode: str, configured_limit: int, snapshot_total: int) -> int:
    """Batch means the complete current snapshot; test keeps its chosen count."""
    if mode == "batch":
        return max(1, snapshot_total)
    return configured_limit


def effective_preflight_workers(raw_value: str | None) -> int:
    """Return the bounded per-shop read-only preflight concurrency.

    Four shops may already be active at panel level, so the conservative
    default is two workers per shop.  Invalid deployment values fall back to
    that default; explicit values are clamped to the batch worker's 1..4
    safety boundary.
    """
    try:
        configured = int(str(raw_value or "2").strip())
    except ValueError:
        configured = 2
    return min(4, max(1, configured))


def main() -> int:
    shop_id = str(os.environ.get("DP_SHOP_ID") or "").strip()
    folder_id = str(os.environ.get("DP_MEDIA_FOLDER_ID") or "").strip()
    action = str(os.environ.get("DP_MEDIA_ACTION") or "save_draft").strip()
    mode = str(os.environ.get("DP_MODE") or "test").strip().casefold()
    # The panel API path now treats the maintained Feishu library as the
    # authoritative main/white source.  Local remains an explicit emergency
    # override, rather than silently shadowing cloud during acceptance tests.
    main_area_source = str(os.environ.get("DP_MEDIA_SOURCE") or "feishu").strip().casefold()
    selected_product_id = str(os.environ.get("DP_MEDIA_PRODUCT_ID") or "").strip()
    stop_file = str(os.environ.get("DP_STOP_FILE") or "").strip()
    preflight_workers = effective_preflight_workers(
        os.environ.get("DP_PREFLIGHT_WORKERS")
    )
    try:
        limit = int(os.environ.get("DP_MEDIA_LIMIT") or "1")
    except ValueError:
        limit = 1
    if not shop_id.isdigit() or not folder_id.isdigit() or not 1 <= limit <= 999:
        log("API", "API 素材任务配置无效", "error")
        return 2
    if mode not in ("test", "batch"):
        log("API", "API 素材任务模式无效", "error")
        return 2
    if action not in ("save_draft", "publish", "dry_run"):
        log("API", "未知 API 素材动作: %s" % action, "error")
        return 2
    if main_area_source not in ("feishu", "local"):
        log("API", "未知主图区素材来源: %s" % main_area_source, "error")
        return 2
    if selected_product_id and not selected_product_id.isdigit():
        log("API", "API 素材指定商品 ID 无效", "error")
        return 2

    emit("state", state="starting", message="API 素材任务准备中")
    try:
        if stop_file and Path(stop_file).is_file():
            emit("state", state="stopping", message="启动前已收到停止请求")
            return 0
        log("SNAPSHOT", "读取抖店最新草稿快照（完整分页）")
        snapshot_path, total, pages = create_snapshot(shop_id)
        log("SNAPSHOT", "草稿快照完成：%d 件，%d 页" % (total, pages), "success")
        limit = effective_limit(mode, limit, total)
        if mode == "batch":
            log("BATCH", "全量批量模式：扫描当前完整草稿箱，安全商品全部处理", "success")
        active_preflight_workers = min(preflight_workers, limit)
        log("SOURCE", "主图区素材来源：%s；商详基础图、实际成色图和颜色规格图使用本地已验收图库" % (
            "飞书云盘" if main_area_source == "feishu" else "本地备用图库",
        ))
        log(
            "PIPELINE",
            "%d 路只读预检与 1 路串行写入流水执行；预检通过一件即开始%s" % (
                active_preflight_workers,
                "保存草稿" if action == "save_draft" else (
                    "真实发布" if action == "publish" else "生成预检结果"
                ),
            ),
        )
        emit("progress", total=min(total, limit), processed=0, success=0, failed=0,
             skipped=0, current="正在准备 API 素材")
        emit("state", state="running", message="API 素材核验和写入中")

        if selected_product_id and limit == 1 and main_area_source == "local":
            log("API", "进入单商品全链路：标准型号、屏幕形态、主图/白底图、颜色规格图、成色详情图")
            return run_single_full_media(
                shop_id, selected_product_id, folder_id, action,
            )

        # Both local and Feishu sources now use the same verified full-media
        # transaction.  The source adapter differs, while model/brand, colour
        # slots, grade details, draft readback and optional publish stay shared.
        batch_module = "scripts.doudian_full_media_batch"
        command = [
            sys.executable, "-m", batch_module, shop_id,
            "--snapshot", str(snapshot_path), "--folder-id", folder_id,
            "--limit", str(limit), "--stop-file", stop_file, "--env", str(ENV_FILE),
            "--main-area-source", main_area_source,
            "--preflight-workers", str(preflight_workers),
        ]
        if selected_product_id:
            command.extend(["--product-id", selected_product_id])
        if action != "dry_run":
            command.append("--execute")
        if action == "save_draft":
            command.append("--save-draft-only")
        returncode, lines = run_batch_command(command)
        report_path = _read_report_path(lines)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        progress, warnings, errors = summarise_media_report(
            report, save_draft_only=action == "save_draft",
        )
        for warning in warnings:
            log("IMAGE", "%s：%s" % (warning["name"] or warning["product_id"], warning["reason"]), "warning")
        for row in errors:
            log("SKIP", "%s：%s" % (row["name"] or row["product_id"], row["reason"]), "warning")
        for item in report.get("items", []):
            if not isinstance(item, Mapping):
                continue
            result = str(item.get("result") or "prepared")
            if result in ("published_readback", "verified_draft", "draft_verified"):
                text = "已%s并完成全链路回读" % (
                    "发布且确认在线" if result == "published_readback" else "保存草稿",
                )
                retry_count = int(item.get("publish_retry_count") or 0)
                if retry_count:
                    log("RETRY", "%s：最终提交在第 %d 次尝试成功（已安全重试 %d 次）" % (
                        item.get("name") or item.get("product_id"),
                        int(item.get("publish_attempt_count") or retry_count + 1),
                        retry_count,
                    ), "warning")
                log("API", "%s：%s" % (item.get("name") or item.get("product_id"), text), "success")
        emit("progress", **progress)
        emit("errors", errors=errors)
        if report.get("stopped"):
            emit("state", state="stopping", message="已在商品间停止 API 素材任务")
            return 0
        if report.get("fatal_error"):
            log("API", "API 素材任务失败：%s" % report["fatal_error"], "error")
            return 2
        if returncode != 0:
            log("API", "API 素材批处理退出码 %s；详见本次报告" % returncode, "error")
            return returncode
        summary = report.get("summary") or {}
        if progress["success"] != int(summary.get("prepared") or 0):
            log("API", "批次未达到逐件全链路回读标准，拒绝标记成功", "error")
            return 2
        log("API", "完成：成功 %d，转人工 %d，预检跳过 %d，图片提醒 %d" % (
            progress["success"], progress["failed"], progress["skipped"],
            int(summary.get("image_warning_products") or 0),
        ), "success")
        return 0
    except Exception as exc:
        log("API", "API 素材任务异常：%s" % exc, "error")
        emit("errors", errors=[{
            "product_id": "", "name": "", "error_type": "API 素材任务异常", "reason": str(exc),
        }])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
