"""Export a redacted, reproducible diagnostics snapshot for the public repo.

Only text logs and selected execution receipts are included. Never copy browser
profiles, credentials, spreadsheets, images, or raw platform snapshots.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "diagnostics"
ARCHIVE = DEST / "runtime_logs_and_incidents.tar.gz"
LOG_SOURCES = [
    ROOT / "panel_data/logs",
    ROOT / "logs",
    ROOT / "outputs",
    ROOT / "tools",
]
RECEIPT_DIRS = [
    "panel_data/gaia_listing/batches",
    "panel_data/listing_execution",
    "panel_data/listing_preflight",
    "panel_data/doudian_api_write_reports",
    "panel_data/doudian_api_media_publish_reports",
    "panel_data/doudian_large_scale_publish",
    "panel_data/doudian_full_media_batch",
    "panel_data/doudian_full_media_launch",
    "panel_data/doudian_full_media_publish",
    "panel_data/feishu_concurrent_full_scan",
    "panel_data/feishu_concurrent_retry",
    "panel_data/feishu_full_media_batch_preflight",
    "panel_data/feishu_large_scale_draft",
]
ERROR = re.compile(r"(?i)\b(?:error|fail(?:ed|ure)?|exception|traceback)\b|失败|异常|跳过|未找到|无法|报错")
URL = re.compile(r"https?://[^\s\"'<>]+", re.I)
HOME = re.compile(r"/Users/[^/\s\"']+")
TMP = re.compile(r"/var/folders/[^\s\"']+")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[a-z]{2,}")
KEYS = r"access[_-]?token|refresh[_-]?token|app[_-]?secret|api[_-]?key|client[_-]?secret|authorization|cookie|password|passwd|session[_-]?key|sign"
QUOTED_SECRET = re.compile(rf'(?i)([\"\'](?:{KEYS})[\"\']\s*:\s*[\"\'])([^\"\']+)([\"\'])')
PLAIN_SECRET = re.compile(rf"(?i)(\b(?:{KEYS})\b\s*[=:]\s*)([^\s,;&}}]+)")
BEARER = re.compile(r"(?i)\bBearer\s+\S+")


def _url_label(match: re.Match[str]) -> str:
    host = urlsplit(match.group(0).rstrip(".,)]}")).hostname or "unknown"
    return f"[URL:{host}]"


def redact(text: str) -> str:
    text = URL.sub(_url_label, text)
    text = HOME.sub("[HOME]", text)
    text = TMP.sub("[TMP]", text)
    text = QUOTED_SECRET.sub(r"\1[REDACTED]\3", text)
    text = PLAIN_SECRET.sub(r"\1[REDACTED]", text)
    text = BEARER.sub("Bearer [REDACTED]", text)
    text = PHONE.sub("[PHONE]", text)
    return EMAIL.sub("[EMAIL]", text)


def sources() -> list[Path]:
    files: list[Path] = []
    for directory in LOG_SOURCES:
        if directory.exists():
            files.extend(p for p in directory.rglob("*") if p.is_file() and p.suffix in {".log", ".out", ".md"})
    files.extend(p for p in (ROOT / "debug_run.log", ROOT / "panel_server.out") if p.is_file())
    for directory in RECEIPT_DIRS:
        path = ROOT / directory
        if path.exists():
            files.extend(p for p in path.rglob("*") if p.is_file() and p.suffix in {".json", ".md"})
    return sorted(set(files))


def main() -> None:
    DEST.mkdir(exist_ok=True)
    manifest = []
    issues = []
    now = datetime.now(timezone.utc).isoformat()
    with tarfile.open(ARCHIVE, "w:gz", compresslevel=9) as archive:
        for path in sources():
            raw = path.read_bytes()
            # A legacy monitor output contains NUL padding; retain its content.
            raw = raw.replace(b"\0", b"[NUL]")
            relative = path.relative_to(ROOT).as_posix()
            text = redact(raw.decode("utf-8", errors="replace"))
            data = text.encode("utf-8")
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = 0o644
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))
            lines = text.splitlines()
            matches = sum(bool(ERROR.search(line)) for line in lines)
            manifest.append({"path": relative, "lines": len(lines), "error_lines": matches,
                             "sha256": hashlib.sha256(data).hexdigest()})
            if matches:
                issues.append({"path": relative, "error_lines": matches})
    (DEST / "manifest.json").write_text(
        json.dumps({"created_utc": now, "archive": ARCHIVE.name, "files": manifest},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    issue_lines = ["# 失败记录索引", "", "完整原文（经脱敏）位于 `runtime_logs_and_incidents.tar.gz`。",
                   "这里按文件列出错误关键词命中的行数；命中数不是失败商品件数。", "",
                   "| 来源文件 | 命中行数 |", "| --- | ---: |"]
    issue_lines += [f"| `{item['path']}` | {item['error_lines']} |" for item in issues]
    (DEST / "failure_index.md").write_text("\n".join(issue_lines) + "\n", encoding="utf-8")
    print(json.dumps({"files": len(manifest), "issue_files": len(issues),
                      "archive_bytes": ARCHIVE.stat().st_size}, ensure_ascii=False))


if __name__ == "__main__":
    main()
