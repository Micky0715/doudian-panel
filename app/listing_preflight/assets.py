"""Strict, browser-free inspection of local product image assets."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from brand_model_rules import (
    image_directory_allowed, image_model_leaf_allowed, model_exact_overrides,
)
from image_catalogue import IMAGE_EXTENSIONS, find_model_image_dirs
from model_catalogue import model_key


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


class ImageAssetInspector:
    def __init__(self, image_root: str | Path, shop_id: str, *, allow_white_as_main: bool = True):
        self.image_root = Path(image_root).expanduser().resolve()
        self.shop_id = str(shop_id)
        # Scan from the store boundary instead of assuming one fixed category
        # layout.  Older stores use ``3C数码及配件/二手手机/品牌/型号``;
        # some operator-maintained stores are flat, while others contain one
        # additional category layer.  The exact brand parent and exact model
        # leaf checks in ``inspect`` keep this recursive lookup fail-closed.
        self.shop_root = self.image_root / self.shop_id
        self.allow_white_as_main = bool(allow_white_as_main)

    def inspect(self, brand: str, model: str) -> dict[str, Any]:
        if not self.shop_root.is_dir():
            return self._missing("shop_image_root_missing")
        candidates: list[Path] = []
        rejected_layout = []
        matched_lookup_model = str(model or "").strip()
        # First require the platform-normalized model.  Only when that exact
        # directory is absent may an explicit, data-backed source alias be
        # tried (for example Note 14 -> Note 14 5G).  This never performs fuzzy
        # prefix matching and therefore cannot collapse Pro/Max variants.
        lookup_models = [matched_lookup_model]
        expected_key = model_key(matched_lookup_model)
        for source_name, target_name in model_exact_overrides().items():
            if model_key(target_name) == expected_key and model_key(source_name) != expected_key:
                lookup_models.append(source_name)

        for lookup_model in lookup_models:
            current_candidates: list[Path] = []
            for raw in find_model_image_dirs(str(self.shop_root), lookup_model):
                path = Path(raw).resolve()
                relative = path.relative_to(self.shop_root)
                if not relative.parts:
                    continue
                # A model folder must still sit directly below its brand folder,
                # but that brand folder may be nested below any number of
                # category/import layers.
                if not image_directory_allowed(brand, path.parent.name):
                    continue
                if not image_model_leaf_allowed(brand, path.name):
                    continue
                if any(part in {"主图", "白底图"} for part in relative.parts[:-1]):
                    rejected_layout.append(str(path))
                    continue
                current_candidates.append(path)
            if current_candidates:
                candidates = current_candidates
                matched_lookup_model = lookup_model
                break
        candidates = sorted(set(candidates), key=lambda value: str(value).casefold())
        if not candidates:
            reason = "invalid_nested_image_layout" if rejected_layout else "model_image_dir_missing"
            result = self._missing(reason)
            result["rejected_layout"] = rejected_layout
            return result
        if len(candidates) != 1:
            result = self._missing("ambiguous_model_image_dirs")
            result["candidates"] = [str(path) for path in candidates]
            return result

        folder = candidates[0]
        main_dir = folder / "主图"
        aux_dir = folder / "白底图"
        main_files = self._images(main_dir)
        standard_main = [
            path for path in main_files if path.stem.casefold() == "主图"
        ]
        if len(standard_main) == 1:
            selected_main = standard_main
            main_reason = ""
        elif not standard_main and len(main_files) == 1:
            selected_main = main_files
            main_reason = ""
        elif len(main_files) > 1:
            selected_main = []
            main_reason = "ambiguous_main_images"
        else:
            selected_main = []
            main_reason = "main_image_missing"

        aux_files = [path for path in self._images(aux_dir) if path.stem.startswith("白底图_")]
        unique_aux = []
        seen_hashes = set()
        for path in aux_files:
            digest = _digest(path)
            if digest not in seen_hashes:
                seen_hashes.add(digest)
                unique_aux.append(path)

        required_aux = 2 if selected_main else (3 if self.allow_white_as_main else 2)
        reasons = []
        if main_reason and not (self.allow_white_as_main and len(unique_aux) >= 3):
            reasons.append(main_reason)
        if len(unique_aux) < required_aux:
            reasons.append("auxiliary_images_insufficient")
        return {
            "ok": not reasons,
            "image_dir": str(folder),
            "main_count": len(selected_main),
            "auxiliary_count": len(unique_aux),
            "main_substituted": not selected_main and self.allow_white_as_main and len(unique_aux) >= 3,
            # These paths are not a loose image search result: they are the
            # exact, brand-bound model directory selected above.  Keeping
            # them in the preflight evidence lets the API material writer use
            # precisely the same main/white decision as the browser runner.
            "main_path": str(selected_main[0]) if selected_main else "",
            "auxiliary_paths": [str(path) for path in unique_aux],
            "reasons": reasons,
            "candidates": [str(folder)],
            "matched_lookup_model": matched_lookup_model,
        }

    @staticmethod
    def _images(folder: Path) -> list[Path]:
        if not folder.is_dir():
            return []
        return sorted(
            [
                path for path in folder.iterdir()
                if path.is_file()
                and not path.name.startswith("._")
                and path.suffix.casefold() in IMAGE_EXTENSIONS
            ],
            key=lambda path: path.name.casefold(),
        )

    @staticmethod
    def _missing(reason: str) -> dict[str, Any]:
        return {
            "ok": False,
            "image_dir": "",
            "main_count": 0,
            "auxiliary_count": 0,
            "main_substituted": False,
            "main_path": "",
            "auxiliary_paths": [],
            "reasons": [reason],
            "candidates": [],
        }


class DetailImageInspector:
    """Validate the shared 14-image detail set without importing browser code."""

    GENERAL_SLOTS = (1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14)
    CONDITIONS = {"9新", "95新", "99新", "准新品"}

    def __init__(self, detail_root: str | Path):
        self.detail_root = Path(detail_root).expanduser().resolve()

    def inspect(self, condition: str) -> dict[str, Any]:
        normalized = str(condition or "").strip()
        reasons: list[str] = []
        files: list[Path] = []
        if not self.detail_root.is_dir():
            return {"ok": False, "count": 0, "condition": normalized,
                    "files": [], "reasons": ["detail_image_root_missing"]}
        if normalized not in self.CONDITIONS:
            reasons.append("condition_unresolved")

        for slot in self.GENERAL_SLOTS:
            matches = self._slot_files(str(slot))
            if len(matches) == 1:
                files.append(matches[0])
            elif not matches:
                reasons.append(f"detail_image_slot_missing:{slot}")
            else:
                reasons.append(f"detail_image_slot_ambiguous:{slot}")

        if normalized in self.CONDITIONS - {"准新品"}:
            matches = self._slot_files(normalized)
            if len(matches) == 1:
                files.insert(2, matches[0])
            elif not matches:
                reasons.append("condition_image_missing")
            else:
                reasons.append("condition_image_ambiguous")

        expected_count = 13 if normalized == "准新品" else 14
        if len(files) != expected_count:
            reasons.append("detail_image_count_invalid")
        return {
            "ok": not reasons,
            "count": len(files),
            "condition": normalized,
            "files": [str(path) for path in files],
            "reasons": reasons,
        }

    def _slot_files(self, stem: str) -> list[Path]:
        matches: list[Path] = []
        accepted_stems = {stem.casefold(), f" {stem}".casefold()}
        for path in self.detail_root.iterdir():
            if (
                path.is_file()
                and not path.name.startswith("._")
                and path.suffix.casefold() in IMAGE_EXTENSIONS
                and path.stem.casefold() in accepted_stems
            ):
                matches.append(path)
        return sorted(matches, key=lambda path: path.name.casefold())
