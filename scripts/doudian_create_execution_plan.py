#!/usr/bin/env python3
"""Convert one accepted API-D report into a short-lived API-E execution plan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.listing_execution import build_execution_plan, store_execution_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="Create DouDian API-E execution plan")
    parser.add_argument("shop_id")
    parser.add_argument("report")
    parser.add_argument("--action", required=True, choices=("save_draft", "publish"))
    parser.add_argument("--product-id", action="append", default=[])
    parser.add_argument("--output-root", default="panel_data/listing_execution")
    args = parser.parse_args()
    source = Path(args.report).expanduser().resolve()
    report = json.loads(source.read_text(encoding="utf-8"))
    if str(report.get("shop_id") or "") != str(args.shop_id):
        raise RuntimeError("report shop does not match requested shop")
    plan = build_execution_plan(
        report,
        source,
        requested_ids=args.product_id or None,
        action=args.action,
    )
    path = store_execution_plan(plan, args.output_root)
    print(
        f"EXECUTION_PLAN_OK shop={plan.shop_id} action={plan.action} "
        f"items={len(plan.items)} file={path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
