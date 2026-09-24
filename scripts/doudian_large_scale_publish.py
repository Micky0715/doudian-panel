"""Run a large verified publish as sequential batches of at most ten products."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.drafts import DraftScanner, DraftSnapshotStore
from scripts.doudian_sku_color_draft_once import _write_json


class LargeScalePublishError(RuntimeError):
    pass


def _report_path(lines: Iterable[str]) -> Path:
    for line in reversed(list(lines)):
        try:
            value = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, Mapping) and value.get("report"):
            return Path(str(value["report"])).resolve()
    raise LargeScalePublishError("子批次未返回报告路径")


def _scan_with_retry(reader: DoudianProductReader, shop_id: str, *, attempts: int = 3):
    """Retry only the read-only snapshot boundary; never retry a product write."""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return DraftScanner(reader, shop_id).scan(page_size=100, max_pages=200)
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            print(json.dumps({
                "event": "snapshot_retry", "attempt": attempt,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }, ensure_ascii=False), flush=True)
            time.sleep(5 * attempt)
    raise LargeScalePublishError("连续三次无法读取草稿快照") from last_error


def main() -> int:
    parser = argparse.ArgumentParser(description="Sequential verified large-scale DouDian publish")
    parser.add_argument("shop_id")
    parser.add_argument("--folder-id", required=True)
    parser.add_argument("--target", type=int, default=50)
    parser.add_argument("--until-empty", action="store_true",
                        help="continue until no safely publishable drafts remain")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--inter-product-delay", type=float, default=5.0)
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--image-root", default="/Users/atrenew/商品图片")
    parser.add_argument("--color-root", default="/Users/atrenew/商品图片/店铺SKU颜色图")
    parser.add_argument("--detail-root", default="/Users/atrenew/手机商详")
    parser.add_argument("--stop-file", default="")
    parser.add_argument(
        "--seed-skip-report", action="append", default=[],
        help="reuse skipped product ids from an earlier full-media batch report",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--report-root", default="panel_data/doudian_large_scale_publish")
    args = parser.parse_args()
    shop_id, folder_id = str(args.shop_id).strip(), str(args.folder_id).strip()
    if not shop_id.isdigit() or not folder_id.isdigit():
        parser.error("shop_id and folder_id must be numeric")
    if not 1 <= args.target <= 1000 or not 1 <= args.batch_size <= 10:
        parser.error("target safety cap must be 1..1000 and batch-size must be 1..10")
    if not 0 <= args.inter_product_delay <= 60:
        parser.error("inter-product-delay must be between 0 and 60 seconds")
    if not args.execute:
        parser.error("large-scale real publication requires explicit --execute")

    created_at = int(time.time())
    report_path = Path(args.report_root) / shop_id / f"{created_at}.json"
    aggregate: dict[str, Any] = {
        "shop_id": shop_id, "created_at": created_at, "target": args.target,
        "batch_size": args.batch_size, "mode": "publish", "batches": [],
        "until_empty": bool(args.until_empty),
        "published_product_ids": [], "result": "running",
    }
    skipped_product_ids: set[str] = set()
    for raw_path in args.seed_skip_report:
        seed_path = Path(raw_path).resolve()
        seed = json.loads(seed_path.read_text(encoding="utf-8"))
        if str(seed.get("shop_id") or "") != shop_id:
            parser.error("seed skip report shop does not match requested shop")
        for item in seed.get("skipped_candidates") or []:
            if isinstance(item, Mapping):
                product_id = str(item.get("product_id") or "").strip()
                if product_id.isdigit():
                    skipped_product_ids.add(product_id)
    aggregate["seed_skip_reports"] = [
        str(Path(value).resolve()) for value in args.seed_skip_report
    ]
    aggregate["known_skipped_product_ids"] = sorted(skipped_product_ids)
    _write_json(report_path, aggregate)
    gateway = DoudianReadOnlyGateway(args.env, shop_id)
    reader = DoudianProductReader(gateway)
    snapshots = DraftSnapshotStore(Path("panel_data/doudian_draft_snapshots"))

    while len(aggregate["published_product_ids"]) < args.target:
        if args.stop_file and Path(args.stop_file).is_file():
            aggregate.update({"result": "stopped", "stop_reason": "stop_file_requested"})
            break
        remaining = args.target - len(aggregate["published_product_ids"])
        limit = min(args.batch_size, remaining)
        batch_index = len(aggregate["batches"]) + 1
        print(json.dumps({"event": "batch_start", "batch": batch_index,
                          "published": len(aggregate["published_product_ids"]),
                          "target": args.target}, ensure_ascii=False), flush=True)
        try:
            snapshot = _scan_with_retry(reader, shop_id)
        except Exception as exc:
            aggregate["batches"].append({
                "batch": batch_index, "result": "snapshot_failed",
                "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            })
            aggregate.update({"result": "stopped_on_snapshot_failure"})
            _write_json(report_path, aggregate)
            break
        snapshot_path = snapshots.save(snapshot)
        command = [
            sys.executable, "-m", "scripts.doudian_full_media_batch", shop_id,
            "--snapshot", str(snapshot_path), "--folder-id", folder_id,
            "--limit", str(limit), "--env", args.env,
            "--image-root", args.image_root, "--color-root", args.color_root,
            "--detail-root", args.detail_root, "--execute",
            "--inter-product-delay", str(args.inter_product_delay),
        ]
        if args.stop_file:
            command.extend(["--stop-file", args.stop_file])
        for product_id in sorted(skipped_product_ids):
            command.extend(["--exclude-product-id", product_id])
        child = subprocess.run(
            command, cwd=str(Path(__file__).resolve().parents[1]), text=True,
            encoding="utf-8", errors="replace", stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env={
                **os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8",
            }, check=False,
        )
        lines = child.stdout.splitlines()
        try:
            child_report_path = _report_path(lines)
            child_report = json.loads(child_report_path.read_text(encoding="utf-8"))
        except Exception as exc:
            aggregate["batches"].append({
                "batch": batch_index, "returncode": child.returncode,
                "result": "report_unavailable", "error": f"{type(exc).__name__}: {exc}",
                "output_tail": lines[-10:],
            })
            aggregate.update({"result": "stopped_on_failure"})
            _write_json(report_path, aggregate)
            break

        summary = child_report.get("summary") or {}
        for item in child_report.get("skipped_candidates") or []:
            if isinstance(item, Mapping):
                product_id = str(item.get("product_id") or "").strip()
                if product_id.isdigit():
                    skipped_product_ids.add(product_id)
        published_ids = [
            str(item.get("product_id")) for item in child_report.get("items", [])
            if isinstance(item, Mapping) and item.get("result") == "published_readback"
        ]
        batch_record = {
            "batch": batch_index, "returncode": child.returncode,
            "snapshot": str(snapshot_path), "snapshot_total": snapshot.reported_total,
            "report": str(child_report_path), "summary": summary,
            "published_product_ids": published_ids,
        }
        aggregate["batches"].append(batch_record)
        aggregate["published_product_ids"].extend(published_ids)
        aggregate["known_skipped_product_ids"] = sorted(skipped_product_ids)
        _write_json(report_path, aggregate)
        print(json.dumps({"event": "batch_complete", **batch_record,
                          "total_published": len(aggregate["published_product_ids"])},
                         ensure_ascii=False), flush=True)

        if not published_ids:
            no_safe = (int(summary.get("prepared") or 0) == 0
                       and int(summary.get("manual_queue") or 0) == 0)
            aggregate.update({"result": (
                "completed_no_safe_candidates" if no_safe and args.until_empty
                else "stopped_no_safe_candidates" if no_safe
                else "stopped_on_failure"
            )})
            break
        if child.returncode != 0 or int(summary.get("manual_queue") or 0) > 0:
            aggregate.update({"result": "stopped_on_failure"})
            break

    if len(aggregate["published_product_ids"]) == args.target:
        aggregate["result"] = "stopped_at_safety_cap" if args.until_empty else "completed"
    aggregate["finished_at"] = int(time.time())
    aggregate["summary"] = {
        "target": args.target,
        "published_readback": len(aggregate["published_product_ids"]),
        "batches": len(aggregate["batches"]),
        "shortfall": (None if args.until_empty else
                      max(0, args.target - len(aggregate["published_product_ids"]))),
    }
    _write_json(report_path, aggregate)
    print(json.dumps({"result": aggregate["result"], "summary": aggregate["summary"],
                      "report": str(report_path)}, ensure_ascii=False), flush=True)
    return 0 if aggregate["result"] in {"completed", "completed_no_safe_candidates"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
