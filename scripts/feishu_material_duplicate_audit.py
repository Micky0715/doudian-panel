"""Read-only duplicate audit for the configured Feishu material libraries.

The audit deliberately has no write-capable Drive method.  It inventories the
configured ``抖音主图`` and ``白底图库`` trees, reports sibling-name conflicts,
and downloads only same-size image candidates to verify byte-identical files.
"""

from __future__ import annotations

import argparse
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import unicodedata

from doudian_api import read_env_file
from integrations.feishu_drive import FeishuDriveClient, FeishuDriveConfig


IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"})


def segment_key(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s._\-/·，,（）()]+", "", text)


def image_stem(value: str) -> str:
    path = Path(str(value or "").strip())
    return path.stem if path.suffix.casefold() in IMAGE_SUFFIXES else ""


@dataclass(frozen=True)
class Item:
    token: str
    name: str
    entry_type: str
    size_bytes: int
    revision: str
    parent_path: str
    path: str
    library: str


def duplicate_kind(names: list[str]) -> str:
    return "同名" if len(set(names)) == 1 else "规范化重名"


def group_payload(parent: str, key: str, items: list[Item]) -> dict[str, object]:
    names = sorted(item.name for item in items)
    return {
        "parent": parent,
        "normalized_key": key,
        "kind": duplicate_kind(names),
        "names": names,
        "paths": sorted(item.path for item in items),
        "items": [
            {
                "token": item.token,
                "name": item.name,
                "path": item.path,
                "entry_type": item.entry_type,
                "revision": item.revision,
                "size_bytes": item.size_bytes,
            }
            for item in sorted(items, key=lambda candidate: (candidate.name, candidate.token))
        ],
    }


def retry_list(drive: FeishuDriveClient, token: str, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return tuple(drive.list_entries(token))
        except Exception as exc:  # diagnostic script records the exact blocked path
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    assert last_error is not None
    raise last_error


def retry_download(drive: FeishuDriveClient, token: str, attempts: int = 3) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return drive.download(token)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt)
    assert last_error is not None
    raise last_error


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default="env.local")
    parser.add_argument("--output", default="")
    parser.add_argument("--list-workers", type=int, default=8)
    parser.add_argument("--scope", choices=("main", "white", "both"), default="both")
    args = parser.parse_args()

    values = read_env_file(args.env)
    drive = FeishuDriveClient(FeishuDriveConfig.from_mapping(values), timeout=45)
    main_root = str(values.get("FEISHU_MAIN_ROOT_NAME") or "抖音主图").strip()
    white_root = str(values.get("FEISHU_WHITE_ROOT_NAME") or "白底图库").strip()
    wanted: dict[str, str] = {}
    if args.scope in {"main", "both"}:
        wanted[segment_key(main_root)] = "主图"
    if args.scope in {"white", "both"}:
        wanted[segment_key(white_root)] = "白底图"

    items: list[Item] = []
    folder_children: dict[str, list[Item]] = defaultdict(list)
    file_children: dict[str, list[Item]] = defaultdict(list)
    blockers: list[dict[str, str]] = []

    material_root = drive.config.material_root_folder_token
    root_entries = retry_list(drive, material_root)
    root_matches = [
        entry for entry in root_entries
        if entry.entry_type == "folder" and segment_key(entry.name) in wanted
    ]
    missing_roots = [
        label for key, label in wanted.items()
        if not any(segment_key(entry.name) == key for entry in root_matches)
    ]

    queue: deque[tuple[str, str, str]] = deque()
    for entry in root_matches:
        library = wanted[segment_key(entry.name)]
        root_item = Item(
            entry.token, entry.name, entry.entry_type, entry.size_bytes,
            entry.revision, "素材根目录", entry.name, library,
        )
        items.append(root_item)
        folder_children["素材根目录"].append(root_item)
        queue.append((entry.token, entry.name, library))

    scanned_folders = 0
    seen_folder_tokens: set[str] = set()
    workers = min(max(args.list_workers, 1), 12)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        while queue:
            batch: list[tuple[str, str, str]] = []
            while queue and len(batch) < workers:
                token, parent_path, library = queue.popleft()
                if token in seen_folder_tokens:
                    continue
                seen_folder_tokens.add(token)
                batch.append((token, parent_path, library))
            futures = [executor.submit(retry_list, drive, token) for token, _, _ in batch]
            for (token, parent_path, library), future in zip(batch, futures):
                try:
                    entries = future.result()
                except Exception as exc:
                    blockers.append({"path": parent_path, "error": str(exc)})
                    continue
                scanned_folders += 1
                if scanned_folders % 50 == 0:
                    print(
                        f"进度: 已扫描目录 {scanned_folders}，资源 {len(items)}，队列 {len(queue)}",
                        file=sys.stderr,
                        flush=True,
                    )
                for entry in entries:
                    current_path = parent_path + "/" + entry.name
                    item = Item(
                        entry.token, entry.name, entry.entry_type, entry.size_bytes,
                        entry.revision, parent_path, current_path, library,
                    )
                    items.append(item)
                    if entry.entry_type == "folder":
                        folder_children[parent_path].append(item)
                        queue.append((entry.token, current_path, library))
                    elif entry.entry_type == "file":
                        file_children[parent_path].append(item)

    folder_duplicate_groups: list[dict[str, object]] = []
    for parent, children in folder_children.items():
        groups: dict[str, list[Item]] = defaultdict(list)
        for item in children:
            groups[segment_key(item.name)].append(item)
        for key, group in groups.items():
            if key and len(group) > 1:
                payload = group_payload(parent, key, group)
                payload["library"] = group[0].library if len({i.library for i in group}) == 1 else "跨库"
                folder_duplicate_groups.append(payload)

    logical_image_duplicate_groups: list[dict[str, object]] = []
    image_files = [item for item in items if item.entry_type == "file" and image_stem(item.name)]
    for parent, children in file_children.items():
        groups: dict[str, list[Item]] = defaultdict(list)
        for item in children:
            stem = image_stem(item.name)
            if stem:
                groups[segment_key(stem)].append(item)
        for key, group in groups.items():
            if key and len(group) > 1:
                payload = group_payload(parent, key, group)
                payload["library"] = group[0].library
                payload["sizes"] = sorted({item.size_bytes for item in group})
                logical_image_duplicate_groups.append(payload)

    # Exact duplicates can only have the same byte size.  Hash only repeated
    # positive-size groups so a full-library audit does not needlessly download
    # thousands of entries whose Drive listing omitted ``size``.  Unknown-size
    # files are hashed only when they already form a same-parent logical-name
    # conflict.
    by_size: dict[int, list[Item]] = defaultdict(list)
    for item in image_files:
        if item.size_bytes > 0:
            by_size[item.size_bytes].append(item)
    hash_candidate_by_token: dict[str, Item] = {
        item.token: item
        for group in by_size.values() if len(group) > 1
        for item in group
    }
    for group in logical_image_duplicate_groups:
        wanted_paths = set(group["paths"])
        for item in image_files:
            if item.path in wanted_paths:
                hash_candidate_by_token[item.token] = item
    hash_candidates = list(hash_candidate_by_token.values())
    digest_groups: dict[str, list[Item]] = defaultdict(list)
    hash_failures: list[dict[str, str]] = []
    for index, item in enumerate(hash_candidates, 1):
        try:
            data = retry_download(drive, item.token)
            digest_groups[hashlib.sha256(data).hexdigest()].append(item)
        except Exception as exc:
            hash_failures.append({"path": item.path, "error": str(exc)})
        if index % 25 == 0:
            print(
                f"校验: 已下载哈希 {index}/{len(hash_candidates)}",
                file=sys.stderr,
                flush=True,
            )

    exact_content_groups: list[dict[str, object]] = []
    for digest, group in digest_groups.items():
        if len(group) < 2:
            continue
        libraries = sorted({item.library for item in group})
        parents = sorted({item.parent_path for item in group})
        exact_content_groups.append({
            "sha256": digest,
            "size_bytes": group[0].size_bytes,
            "libraries": libraries,
            "occurrences": len(group),
            "same_parent": len(parents) == 1,
            "paths": sorted(item.path for item in group),
            "items": [
                {
                    "token": item.token,
                    "name": item.name,
                    "path": item.path,
                    "parent": item.parent_path,
                    "revision": item.revision,
                    "size_bytes": item.size_bytes,
                }
                for item in sorted(group, key=lambda candidate: (candidate.path, candidate.token))
            ],
        })

    # A brand appearing under 手机 and 平板 is often intentional, but a search
    # that starts at the shop directory sees it as a same-depth conflict.  Keep
    # these separate from true sibling duplicates.
    category_scope_collisions: list[dict[str, object]] = []
    main_folders = [item for item in items if item.entry_type == "folder" and item.library == "主图"]
    brands_by_shop: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for item in main_folders:
        parts = item.path.split("/")
        if len(parts) != 4 or segment_key(parts[0]) != segment_key(main_root):
            continue
        shop, category, brand = parts[1], parts[2], parts[3]
        if segment_key(category) not in {segment_key("手机"), segment_key("平板")}:
            continue
        brands_by_shop[shop][segment_key(brand)].append(item.path)
    for shop, brands in brands_by_shop.items():
        for key, paths in brands.items():
            categories = {path.split("/")[2] for path in paths}
            if len(categories) > 1:
                category_scope_collisions.append({
                    "shop": shop,
                    "normalized_brand": key,
                    "categories": sorted(categories),
                    "paths": sorted(paths),
                })

    inventory_complete = not blockers and not missing_roots
    content_hash_complete = not hash_failures and all(
        item.size_bytes > 0 for item in image_files
    )
    report = {
        "generated_at": int(time.time()),
        "scope": [main_root, white_root],
        "coverage": {
            "complete": inventory_complete and content_hash_complete,
            "inventory_complete": inventory_complete,
            "content_hash_complete": content_hash_complete,
            "missing_roots": missing_roots,
            "listing_blockers": blockers,
            "hash_failures": hash_failures,
            "scanned_folders": scanned_folders,
            "folders": sum(1 for item in items if item.entry_type == "folder"),
            "files": sum(1 for item in items if item.entry_type == "file"),
            "images": len(image_files),
            "images_without_size_metadata": sum(1 for item in image_files if item.size_bytes <= 0),
            "hashed_candidates": len(hash_candidates),
        },
        "summary": {
            "sibling_folder_duplicate_groups": len(folder_duplicate_groups),
            "sibling_logical_image_duplicate_groups": len(logical_image_duplicate_groups),
            "exact_content_duplicate_groups": len(exact_content_groups),
            "main_exact_content_groups": sum(
                1 for group in exact_content_groups if group["libraries"] == ["主图"]
            ),
            "white_exact_content_groups": sum(
                1 for group in exact_content_groups if group["libraries"] == ["白底图"]
            ),
            "cross_library_exact_content_groups": sum(
                1 for group in exact_content_groups if len(group["libraries"]) > 1
            ),
            "category_scope_collision_groups": len(category_scope_collisions),
        },
        "folder_duplicates": sorted(folder_duplicate_groups, key=lambda row: (row["library"], row["parent"])),
        "logical_image_duplicates": sorted(
            logical_image_duplicate_groups, key=lambda row: (row["library"], row["parent"])
        ),
        "exact_content_duplicates": sorted(
            exact_content_groups, key=lambda row: (-int(row["occurrences"]), row["paths"][0])
        ),
        "category_scope_collisions": sorted(
            category_scope_collisions, key=lambda row: (row["shop"], row["normalized_brand"])
        ),
        "unsupported_checks": [
            "未做像素级近似/视觉相似检测；内容重复仅认 SHA-256 完全一致",
            "不同父目录下的同名文件不自动判定为重复，除非 SHA-256 一致",
            "Drive 清单未返回大小的跨目录图片，不做全量下载哈希；同目录逻辑重名仍会下载确认",
        ],
    }

    output = Path(args.output) if args.output else Path(
        "panel_data/feishu_duplicate_audit/%d.json" % report["generated_at"]
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report["coverage"], **report["summary"]}, ensure_ascii=False))
    # Missing size metadata makes cross-folder content hashing partial, but it
    # is not an execution failure: the complete name/path inventory and all
    # same-parent logical conflicts were still audited.
    return 0 if report["coverage"]["inventory_complete"] and not hash_failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
