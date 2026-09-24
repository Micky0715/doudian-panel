"""Atomic local persistence for immutable draft snapshots."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .scanner import DraftScanError, DraftSnapshot


class DraftSnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def save(self, snapshot: DraftSnapshot) -> Path:
        if not snapshot.shop_id.isdigit():
            raise DraftScanError("snapshot shop id must be numeric")
        directory = self.root / snapshot.shop_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (
            f"{snapshot.captured_at}_{snapshot.checksum[:12]}.json"
        )
        content = json.dumps(
            snapshot.to_dict(), ensure_ascii=False, sort_keys=True, indent=2,
        ) + "\n"
        if target.exists():
            if target.read_text(encoding="utf-8") != content:
                raise DraftScanError(f"snapshot path collision: {target.name}")
            return target

        fd, temp_name = tempfile.mkstemp(prefix=target.name + ".", dir=str(directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, target)
            os.chmod(target, 0o600)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return target
