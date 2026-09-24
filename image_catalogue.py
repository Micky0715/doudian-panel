"""Recursive, exact and ambiguity-aware lookup for per-shop product images."""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

from model_catalogue import canonical_brand, model_key
from brand_model_rules import image_directory_allowed

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _has_product_images(folder: Path) -> bool:
    try:
        children = list(folder.iterdir())
    except OSError:
        return False
    for item in children:
        if item.is_dir() and item.name in {"主图", "白底图"}:
            try:
                if any(image.is_file() and image.suffix.casefold() in IMAGE_EXTENSIONS
                       and not image.name.startswith("._") for image in item.iterdir()):
                    return True
            except OSError:
                continue
    return any(item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS and
               (item.stem.startswith("主图") or item.stem.startswith("白底图")) for item in children)


@lru_cache(maxsize=32)
def _build_image_index(root: str) -> dict[str, tuple[str, ...]]:
    """Scan one image root once per worker process."""
    base = Path(root)
    grouped: dict[str, list[str]] = {}
    if not base.is_dir():
        return {}
    for current, dirs, _files in os.walk(base):
        folder = Path(current)
        if folder != base and _has_product_images(folder):
            key = model_key(folder.name)
            if key:
                grouped.setdefault(key, []).append(str(folder))
            dirs[:] = []
    return {
        key: tuple(sorted(set(paths), key=str.casefold))
        for key, paths in grouped.items()
    }


def find_model_image_dirs(root: str | os.PathLike[str], model: str) -> list[str]:
    """Find all exact model leaf directories at any hierarchy depth."""
    expected = model_key(model)
    if not expected:
        return []
    # Keep the caller's absolute path spelling.  On macOS ``Path.resolve()``
    # rewrites /var to /private/var, which makes returned paths unexpectedly
    # differ from the paths supplied by callers (and invalidates cache keys).
    absolute_root = os.path.abspath(os.fspath(root))
    return list(_build_image_index(absolute_root).get(expected, ()))


def clear_image_index_cache() -> None:
    """Refresh indexes after operators synchronize new image folders."""
    _build_image_index.cache_clear()


def product_image_fingerprint(folder: str | os.PathLike[str]) -> str:
    """Fingerprint usable main/white-background images, ignoring metadata files."""
    base = Path(folder)
    files = []
    for subdir in ("主图", "白底图"):
        target = base / subdir
        if target.is_dir():
            files.extend(item for item in target.iterdir()
                         if item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS
                         and not item.name.startswith("._"))
    if not files:
        files.extend(item for item in base.iterdir()
                     if item.is_file() and item.suffix.casefold() in IMAGE_EXTENSIONS
                     and (item.stem.startswith("主图") or item.stem.startswith("白底图"))
                     and not item.name.startswith("._"))
    if not files:
        return ""
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda path: path.name.casefold()):
        digest.update(item.name.casefold().encode("utf-8"))
        digest.update(b"\0")
        with item.open("rb") as image_file:
            for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()


def resolve_cross_store_image_dir(root: str | os.PathLike[str], current_store: str | os.PathLike[str],
                                  model: str) -> tuple[str | None, str, list[str]]:
    """Resolve an exact model in sibling stores without choosing conflicting images."""
    current = Path(current_store).resolve()
    candidates = []
    for match in find_model_image_dirs(root, model):
        path = Path(match).resolve()
        try:
            path.relative_to(current)
            continue
        except ValueError:
            pass
        candidates.append(str(path))
    candidates = sorted(set(candidates), key=str.casefold)
    if not candidates:
        return None, "not_found", []
    groups: dict[str, list[str]] = {}
    for candidate in candidates:
        fingerprint = product_image_fingerprint(candidate)
        if fingerprint:
            groups.setdefault(fingerprint, []).append(candidate)
    if not groups:
        return None, "not_found", candidates
    if len(groups) > 1:
        return None, "conflicting_sources", candidates
    selected = sorted(next(iter(groups.values())), key=str.casefold)[0]
    return selected, "unique_content", candidates


def resolve_white_library_image_dir(root: str | os.PathLike[str], brand: str,
                                    model: str) -> tuple[str | None, str, list[str]]:
    """Resolve one exact brand+model directory in the shared white-background library."""
    candidates = []
    absolute_root = Path(os.path.abspath(os.fspath(root)))
    for match in find_model_image_dirs(root, model):
        path = Path(match)
        relative = path.relative_to(absolute_root)
        top_level = relative.parts[0] if relative.parts else ""
        if brand and not image_directory_allowed(brand, top_level):
            continue
        candidates.append(str(path))
    candidates = sorted(set(candidates), key=str.casefold)
    if not candidates:
        return None, "not_found", []
    if len(candidates) > 1:
        fingerprints = {product_image_fingerprint(path) for path in candidates}
        fingerprints.discard("")
        if len(fingerprints) != 1:
            return None, "conflicting_sources", candidates
    return candidates[0], "exact", candidates
