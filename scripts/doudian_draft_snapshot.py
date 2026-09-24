#!/usr/bin/env python3
"""Create a complete read-only DouDian draft snapshot for one shop."""

from __future__ import annotations

import argparse
from pathlib import Path

from integrations.doudian import DoudianProductReader, DoudianReadOnlyGateway
from integrations.doudian.drafts import DraftScanner, DraftSnapshotStore


def main() -> int:
    parser = argparse.ArgumentParser(description="DouDian full draft snapshot")
    parser.add_argument("shop_id", help="numeric DouDian shop id")
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output", default="panel_data/doudian_draft_snapshots")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--max-pages", type=int, default=200)
    args = parser.parse_args()

    gateway = DoudianReadOnlyGateway(Path(args.env), args.shop_id)
    reader = DoudianProductReader(gateway)
    snapshot = DraftScanner(reader, args.shop_id).scan(
        page_size=args.page_size, max_pages=args.max_pages,
    )
    path = DraftSnapshotStore(args.output).save(snapshot)
    print(
        f"DRAFT_SNAPSHOT_OK shop={snapshot.shop_id} total={snapshot.reported_total} "
        f"pages={snapshot.page_count} unique={len(snapshot.items)} "
        f"checksum={snapshot.checksum[:12]} file={path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
