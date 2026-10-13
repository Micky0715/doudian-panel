"""Exact-path Feishu cloud image resolver; no filename fuzzy matching."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

from brand_model_rules import (
    image_model_leaf_allowed,
    model_exact_overrides,
    platform_brand_aliases,
    platform_brand_key,
)
from integrations.feishu_drive import FeishuDriveClient, RemoteDriveEntry, RemoteDriveFile
from model_catalogue import (
    model_key,
    spaced_model_key,
    versionless_model_candidates,
)

from .domain import (
    MAXIMUM_WHITE_IMAGES,
    CloudImage,
    MaterialBundle,
    MaterialOmission,
    MaterialPolicyError,
)
from .image_validation import ALLOWED_IMAGE_SUFFIXES, main_area_image_error
from .official_model_registry import OfficialModelRecord, OfficialModelRegistry


@dataclass(frozen=True)
class IndexedDriveFile:
    relative_path: tuple[str, ...]
    file: RemoteDriveFile


@dataclass(frozen=True)
class ModelFolderEvidence:
    strategy: str = "exact"
    folder_name: str = ""
    official_name: str = ""
    official_source_url: str = ""


def _segment_key(value: str) -> str:
    """Normalise one operational Drive path segment without fuzzy matching."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
    return re.sub(r"[\s._\-/·，,（）()]+", "", text)


def _image_stem(value: str) -> str:
    """Return an image filename without its final supported extension."""
    name = str(value or "").strip()
    folded = name.casefold()
    for suffix in sorted(ALLOWED_IMAGE_SUFFIXES, key=len, reverse=True):
        if folded.endswith(suffix):
            return name[:-len(suffix)].strip()
    return ""


def _literal_label(value: str) -> str:
    """Preserve meaningful word boundaries while normalising presentation."""
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).strip(),
    ).casefold()


def _literal_model(value: str, brand: str) -> str:
    """Strip only an explicitly separated reviewed brand prefix.

    ``OPPO Find X7`` and ``OPPOFind X7`` must remain distinguishable here;
    the latter is considered only by the later normalised fallback.
    """
    text = _literal_label(value)
    aliases = sorted(platform_brand_aliases(brand), key=len, reverse=True)
    for alias in aliases:
        prefix = _literal_label(alias)
        if prefix and text.startswith(prefix + " "):
            return text[len(prefix):].strip()
    return text


@dataclass(frozen=True)
class FeishuMaterialLayout:
    """The explicit cloud names; wrapper levels may separate path segments."""

    main_root: str = "抖音主图"
    white_root: str = "白底图库"
    white_category: str = "手机"
    main_path_layout: str = "shop_brand_model"
    brand_folder_overrides: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "FeishuMaterialLayout":
        layout = str(values.get("FEISHU_MAIN_PATH_LAYOUT") or "shop_brand_model").strip()
        if layout not in {"shop_brand_model", "shop_model"}:
            raise MaterialPolicyError("FEISHU_MAIN_PATH_LAYOUT must be shop_brand_model or shop_model")
        raw_overrides = str(values.get("FEISHU_BRAND_FOLDER_MAP_JSON") or "").strip()
        # Physical Drive folder spellings are operational configuration.  The
        # shared identity rules still decide whether a model is allowed below
        # that folder; this mapping only bridges names such as 苹果 -> Apple.
        overrides: dict[str, str] = {}
        if raw_overrides:
            try:
                supplied = json.loads(raw_overrides)
            except json.JSONDecodeError as exc:
                raise MaterialPolicyError("FEISHU_BRAND_FOLDER_MAP_JSON must be a JSON object") from exc
            if not isinstance(supplied, dict) or any(
                not str(key).strip() or not str(value).strip()
                for key, value in supplied.items()
            ):
                raise MaterialPolicyError("FEISHU_BRAND_FOLDER_MAP_JSON contains an invalid folder mapping")
            overrides.update({str(key).strip(): str(value).strip() for key, value in supplied.items()})
        return cls(
            main_root=str(values.get("FEISHU_MAIN_ROOT_NAME") or "抖音主图").strip(),
            white_root=str(values.get("FEISHU_WHITE_ROOT_NAME") or "白底图库").strip(),
            white_category=str(values.get("FEISHU_WHITE_CATEGORY") or "手机").strip(),
            main_path_layout=layout,
            brand_folder_overrides=tuple(sorted(overrides.items())),
        )

    def brand_folder(self, brand: str) -> str:
        clean_brand = str(brand).strip()
        return dict(self.brand_folder_overrides).get(clean_brand, clean_brand)


class FeishuMaterialCatalog:
    """Read a prescribed two-library layout below one Feishu folder.

    Brand folders use an explicit physical-name mapping. Model identity and
    reviewed aliases reuse the same fail-closed rules as the local gallery.
    """

    def __init__(self, drive: FeishuDriveClient, *, layout: FeishuMaterialLayout | None = None,
                 official_registry: OfficialModelRegistry | None = None,
                 max_depth: int = 7):
        self.drive = drive
        self.layout = layout or FeishuMaterialLayout()
        self.official_registry = official_registry or OfficialModelRegistry.bundled()
        self.max_depth = max_depth
        # A batch resolves many products against the same Drive tree.  Keep
        # read-only directory listings for the lifetime of this catalogue so
        # recursive lookup does not multiply Feishu API calls per product.
        self._folder_entries: dict[str, tuple[RemoteDriveEntry, ...]] = {}

    def _entries(self, folder_token: str) -> tuple[RemoteDriveEntry, ...]:
        token = str(folder_token).strip()
        if not token:
            raise MaterialPolicyError("Feishu folder token is required")
        if token not in self._folder_entries:
            self._folder_entries[token] = tuple(self.drive.list_entries(token))
        return self._folder_entries[token]

    def index(self) -> tuple[IndexedDriveFile, ...]:
        files: list[IndexedDriveFile] = []

        def visit(folder_token: str, path: tuple[str, ...]) -> None:
            if len(path) > self.max_depth:
                raise MaterialPolicyError("Feishu material folder nesting exceeds migration limit")
            for entry in self._entries(folder_token):
                current = path + (entry.name,)
                if entry.entry_type == "folder":
                    visit(entry.token, current)
                elif entry.entry_type == "file":
                    files.append(IndexedDriveFile(current, RemoteDriveFile.from_entry(entry)))

        visit(self.drive.config.material_root_folder_token, ())
        return tuple(sorted(files, key=lambda item: tuple(value.casefold() for value in item.relative_path)))

    def _main_prefix(self, shop_id: str, brand: str) -> tuple[str, ...]:
        if self.layout.main_path_layout == "shop_model":
            return (self.layout.main_root, shop_id)
        return (self.layout.main_root, shop_id, self.layout.brand_folder(brand))

    def _white_prefix(self, brand: str) -> tuple[str, ...]:
        return (
            self.layout.white_root,
            self.layout.white_category,
            self.layout.brand_folder(brand),
        )

    def _select_model_folder(
        self, names: tuple[str, ...], brand: str, model: str, *, library: str,
    ) -> tuple[int | None, ModelFolderEvidence]:
        """Apply the shared local/cloud conservative model rules.

        The local gallery resolver has long supported reviewed source-name
        aliases (for example ``Note 14`` -> ``Note 14 5G``).  Cloud material
        lookup must use the same data-backed rule set instead of requiring
        operators to rename an otherwise authoritative Drive directory.
        """
        expected = model_key(model)
        if not expected:
            raise MaterialPolicyError("cloud material model is empty after normalisation")
        official = self.official_registry.resolve(brand, model)

        def allowed(index: int) -> bool:
            return image_model_leaf_allowed(brand, names[index])

        def official_choice(indices: list[int], record: OfficialModelRecord | None):
            if not record:
                return None
            wanted = spaced_model_key(record.official_name)
            matches = [index for index in indices if spaced_model_key(names[index]) == wanted]
            return matches[0] if len(matches) == 1 else None

        # Prefer the operator's literal spelling before the broader key that
        # removes spaces.  This mirrors the established local colour-gallery
        # resolver and resolves e.g. ``OPPO Find X7`` ahead of ``OPPOFind X7``.
        expected_literal = _literal_model(model, brand)
        literal = [
            index for index, name in enumerate(names)
            if allowed(index) and _literal_model(name, brand) == expected_literal
        ]
        if len(literal) == 1:
            index = literal[0]
            return index, ModelFolderEvidence("exact", names[index])
        if len(literal) > 1:
            raise MaterialPolicyError(
                "%s存在多个原始型号文字相同的目录，拒绝猜测: %s" % (
                    library, ", ".join(names[index] for index in literal),
                )
            )

        exact = [
            index for index, name in enumerate(names)
            if allowed(index) and model_key(name) == expected
        ]
        if len(exact) == 1:
            index = exact[0]
            return index, ModelFolderEvidence("exact", names[index])
        if len(exact) > 1:
            chosen = official_choice(exact, official)
            if chosen is not None:
                return chosen, ModelFolderEvidence(
                    "official_registry", names[chosen], official.official_name,
                    official.source_url,
                )
            raise MaterialPolicyError(
                "%s存在多个规范化型号目录，官网证据仍不能唯一确认: %s" % (
                    library, ", ".join(names[index] for index in exact),
                )
            )

        # Reuse the same reviewed source aliases as the local main/white and
        # SKU-colour resolvers.  This is not fuzzy matching: aliases are
        # explicit rows in brand_model_rules.json and a hit must stay unique.
        source_alias_keys = {
            model_key(source_name)
            for source_name, target_name in model_exact_overrides().items()
            if model_key(target_name) == expected and model_key(source_name) != expected
        }
        source_aliases = [
            index for index, name in enumerate(names)
            if allowed(index) and model_key(name) in source_alias_keys
        ]
        if len(source_aliases) == 1:
            index = source_aliases[0]
            return index, ModelFolderEvidence("rule_alias", names[index])
        if len(source_aliases) > 1:
            raise MaterialPolicyError(
                "%s命中多个已复核的型号别名目录，拒绝猜测: %s" % (
                    library, ", ".join(names[index] for index in source_aliases),
                )
            )

        versionless: list[int] = []
        for base in versionless_model_candidates(model):
            base_key = model_key(base)
            versionless.extend(
                index for index, name in enumerate(names)
                if allowed(index) and model_key(name) == base_key and index not in versionless
            )
        if len(versionless) == 1:
            index = versionless[0]
            return index, ModelFolderEvidence("version_suffix_removed", names[index])
        if len(versionless) > 1:
            chosen = official_choice(versionless, official)
            if chosen is not None:
                return chosen, ModelFolderEvidence(
                    "official_registry", names[chosen], official.official_name,
                    official.source_url,
                )
            raise MaterialPolicyError(
                "%s去除版本描述后仍有多个型号目录，拒绝猜测: %s" % (
                    library, ", ".join(names[index] for index in versionless),
                )
            )

        if official:
            official_key = model_key(official.official_name)
            official_candidates = [
                index for index, name in enumerate(names)
                if allowed(index) and model_key(name) == official_key
            ]
            chosen = official_choice(official_candidates, official)
            if chosen is not None:
                return chosen, ModelFolderEvidence(
                    "official_registry", names[chosen], official.official_name,
                    official.source_url,
                )
            if official_candidates:
                raise MaterialPolicyError(
                    "%s官网标准名仍不能唯一定位目录: %s" % (
                        library, ", ".join(names[index] for index in official_candidates),
                    )
                )
        return None, ModelFolderEvidence()

    def _exact_model_path(
        self, entries: Iterable[IndexedDriveFile], prefix: tuple[str, ...], brand: str,
        model: str, *, library: str,
    ) -> tuple[tuple[str, ...] | None, ModelFolderEvidence]:
        """Return one model path through the guarded three-stage resolver."""
        # A material may be stored directly in the model directory (the
        # migration layout) or in its ``主图`` / ``白底图`` child directory
        # (the live operator layout).  In both cases the model directory is
        # the first directory beneath the configured prefix.
        folders = {
            item.relative_path[:len(prefix) + 1]
            for item in entries
            if item.relative_path[:len(prefix)] == prefix
            and len(item.relative_path) >= len(prefix) + 2
        }
        candidates = sorted(
            folders,
            key=lambda folder: tuple(value.casefold() for value in folder),
        )
        index, evidence = self._select_model_folder(
            tuple(path[-1] for path in candidates), brand, model, library=library,
        )
        return (candidates[index] if index is not None else None), evidence

    def _descendant_folder(
        self, parent_token: str, name: str, *, library: str,
        excluded_branch_names: frozenset[str] = frozenset(),
    ) -> RemoteDriveEntry | None:
        """Find one exact folder name, continuing downward after a miss.

        Search is breadth-first: a direct/nearer exact folder wins over a
        deeper archival copy.  Two matches at the same nearest depth are
        ambiguous and must never be resolved by Drive iteration order.
        """
        expected_key = _segment_key(name)
        excluded_keys = {_segment_key(value) for value in excluded_branch_names}
        frontier = [str(parent_token).strip()]
        visited = set(frontier)
        for _depth in range(1, self.max_depth + 1):
            next_frontier: list[str] = []
            matches: list[RemoteDriveEntry] = []
            for token in frontier:
                for entry in self._entries(token):
                    if entry.entry_type != "folder":
                        continue
                    entry_key = _segment_key(entry.name)
                    if entry_key == expected_key:
                        matches.append(entry)
                    if (entry_key not in excluded_keys
                            and entry.token not in visited):
                        visited.add(entry.token)
                        next_frontier.append(entry.token)
            if len(matches) > 1:
                raise MaterialPolicyError(
                    "%s向下查找时存在多个同层目录，拒绝猜测: %s" % (library, name)
                )
            if matches:
                return matches[0]
            if not next_frontier:
                return None
            frontier = next_frontier
        return None

    def _descendant_main_files(
        self, parent_token: str, brand: str, model: str, *, brand_scoped: bool,
    ) -> tuple[tuple[RemoteDriveFile, ...], ModelFolderEvidence]:
        """Resolve a main stored either as a model file or below a model folder.

        The live operator library currently uses both layouts.  In particular,
        some stores keep files such as ``Apple/iPhone 16 e.jpg`` directly below
        the brand directory.  File stems and folder names therefore share the
        same conservative model resolver; extensions are never part of model
        identity.
        """
        frontier = [str(parent_token).strip()]
        visited = set(frontier)
        wanted_brand = platform_brand_key(brand)
        configured_brand_key = _segment_key(self.layout.brand_folder(brand))

        for _depth in range(1, self.max_depth + 1):
            folders: list[RemoteDriveEntry] = []
            image_entries: list[RemoteDriveEntry] = []
            images_by_parent: dict[str, list[RemoteDriveEntry]] = {}
            for token in frontier:
                for entry in self._entries(token):
                    if entry.entry_type == "folder":
                        folders.append(entry)
                    elif entry.entry_type == "file" and _image_stem(entry.name):
                        image_entries.append(entry)
                        images_by_parent.setdefault(token, []).append(entry)

            # A visible brand directory is stronger identity evidence than a
            # model-like leaf below one of its siblings.  Scope the traversal
            # before comparing model folders/files; otherwise a request for
            # Xiaomi ``15`` can see ``OnePlus 15`` and ``iQOO 15`` together
            # when shop_model contains an operational category layer.
            if not brand_scoped:
                configured_folders = [
                    entry for entry in folders
                    if _segment_key(entry.name) == configured_brand_key
                ]
                brand_folders = configured_folders
                if not brand_folders:
                    brand_folders = [
                        entry for entry in folders
                        if platform_brand_key(entry.name) == wanted_brand
                    ]
                if brand_folders:
                    # Duplicate same-brand branches are a search boundary, not
                    # yet a model ambiguity. Traverse all of them and let the
                    # conservative model resolver accept only one downstream
                    # hit. If two branches both contain the target model, it
                    # still rejects instead of selecting by directory order.
                    frontier = [entry.token for entry in brand_folders]
                    visited.update(frontier)
                    brand_scoped = True
                    continue

            folder_index, folder_evidence = self._select_model_folder(
                tuple(entry.name for entry in folders), brand, model,
                library="飞书店铺主图目录",
            )
            if len(frontier) > 1:
                matching_branches = [
                    token for token in frontier
                    if self._select_main_file(images_by_parent.get(token, ()), brand, model)[0]
                ]
                if len(matching_branches) > 1:
                    raise MaterialPolicyError("飞书店铺主图在多个同品牌目录命中同一型号，拒绝跨目录随机选择")
            selected_file, file_evidence = self._select_main_file(
                image_entries, brand, model,
            )
            if folder_index is not None and selected_file is not None:
                raise MaterialPolicyError(
                    "飞书店铺主图同时命中型号文件与型号目录，拒绝猜测: %s" % model
                )
            if selected_file is not None:
                return (
                    (RemoteDriveFile.from_entry(selected_file),),
                    file_evidence,
                )
            if folder_index is not None:
                model_folder = folders[folder_index]
                return self._main_files_from_remote_folder(model_folder), folder_evidence

            next_frontier: list[str] = []
            for entry in folders:
                if entry.token not in visited:
                    visited.add(entry.token)
                    next_frontier.append(entry.token)
            if not next_frontier:
                return (), ModelFolderEvidence()
            frontier = next_frontier
        return (), ModelFolderEvidence()

    @staticmethod
    def _pick_main(files: Iterable[RemoteDriveFile]) -> RemoteDriveFile:
        """Arbitrarily pick one eligible cover, stable across retries and Drive ordering."""
        candidates = sorted(files, key=lambda item: (item.name.casefold(), item.file_token))
        if not candidates:
            raise MaterialPolicyError("没有可用的店铺主图候选")
        identity = "\n".join(item.file_token for item in candidates).encode("utf-8")
        index = int.from_bytes(hashlib.sha256(identity).digest()[:8], "big") % len(candidates)
        return candidates[index]

    def _select_main_file(
        self, entries: Iterable[RemoteDriveEntry], brand: str, model: str,
    ) -> tuple[RemoteDriveEntry | None, ModelFolderEvidence]:
        """Only treat an exact model or its numbered image copies as covers."""
        files = tuple(entries)
        expected_literal = _literal_model(model, brand)
        expected_key = model_key(model)
        eligible = [
            entry for entry in files
            if _image_stem(entry.name)
            and image_model_leaf_allowed(brand, _image_stem(entry.name))
        ]
        for strategy, matches in (
            ("exact", [entry for entry in eligible
                       if _literal_model(_image_stem(entry.name), brand) == expected_literal]),
            ("exact", [entry for entry in eligible
                       if model_key(_image_stem(entry.name)) == expected_key]),
            ("numbered_main", [entry for entry in eligible
                               if (match := re.fullmatch(r"(.+)_([1-9]\d*)", _image_stem(entry.name)))
                               and model_key(match.group(1)) == expected_key]),
        ):
            if matches:
                selected = self._pick_main(RemoteDriveFile.from_entry(entry) for entry in matches)
                entry = next(item for item in matches if item.token == selected.file_token)
                return entry, ModelFolderEvidence(strategy, entry.name)
        index, evidence = self._select_model_folder(
            tuple(_image_stem(entry.name) for entry in files),
            brand, model, library="飞书店铺主图文件",
        )
        return (files[index], evidence) if index is not None else (None, evidence)

    def _descendant_model_folder(
        self, parent_token: str, brand: str, model: str, *, library: str,
    ) -> tuple[RemoteDriveEntry | None, ModelFolderEvidence]:
        """Find the nearest unique model folder below a resolved library path."""
        frontier = [str(parent_token).strip()]
        visited = set(frontier)
        for _depth in range(1, self.max_depth + 1):
            folders: list[RemoteDriveEntry] = []
            for token in frontier:
                folders.extend(
                    entry for entry in self._entries(token)
                    if entry.entry_type == "folder"
                )
            index, evidence = self._select_model_folder(
                tuple(entry.name for entry in folders), brand, model, library=library,
            )
            if index is not None:
                return folders[index], evidence
            next_frontier = []
            for entry in folders:
                if entry.token not in visited:
                    visited.add(entry.token)
                    next_frontier.append(entry.token)
            if not next_frontier:
                return None, ModelFolderEvidence()
            frontier = next_frontier
        return None, ModelFolderEvidence()

    def _remote_model_folder(
        self, prefix: tuple[str, ...], brand: str, model: str, *, library: str,
    ) -> tuple[RemoteDriveEntry | None, ModelFolderEvidence]:
        """Return exactly one configured library/brand/model folder.

        A full recursive Drive index is useful for offline audits, but is both
        slow and overly broad for a listing task. Runtime resolution therefore
        walks the required exact folder names, continuing through unmatched
        wrapper levels, and selects the nearest unique full model-key match.
        """
        parent_token = self.drive.config.material_root_folder_token
        for index, part in enumerate(prefix):
            excluded = frozenset()
            if index == 0 and part == self.layout.main_root:
                excluded = frozenset({self.layout.white_root})
            elif index == 0 and part == self.layout.white_root:
                excluded = frozenset({self.layout.main_root})
            child = self._descendant_folder(
                parent_token, part, library=library,
                excluded_branch_names=excluded,
            )
            if child is None:
                return None, ModelFolderEvidence()
            parent_token = child.token
        return self._descendant_model_folder(
            parent_token, brand, model, library=library,
        )

    def _remote_main_files(
        self, prefix: tuple[str, ...], brand: str, model: str,
    ) -> tuple[tuple[RemoteDriveFile, ...], ModelFolderEvidence]:
        """Resolve the required main-library prefix, then one unique asset."""
        parent_token = self.drive.config.material_root_folder_token
        for index, part in enumerate(prefix):
            excluded = frozenset()
            if index == 0 and part == self.layout.main_root:
                excluded = frozenset({self.layout.white_root})
            child = self._descendant_folder(
                parent_token, part, library="飞书店铺主图",
                excluded_branch_names=excluded,
            )
            if child is None:
                return (), ModelFolderEvidence()
            parent_token = child.token
        return self._descendant_main_files(
            parent_token, brand, model,
            brand_scoped=self.layout.main_path_layout == "shop_brand_model",
        )

    def _files_in_optional_child(
        self, model_folder: RemoteDriveEntry | None, child_name: str, *, library: str,
    ) -> tuple[tuple[RemoteDriveFile, ...], bool]:
        """Read one image directory, with a temporary flat-layout fallback.

        The live library stores images below named ``主图`` / ``白底图`` folders.
        Older migrated batches used files immediately in the model folder.
        A named image folder may sit below operational wrapper levels, but it
        must still be the nearest unique exact match within this model only.
        """
        if model_folder is None:
            return (), False
        child = self._descendant_folder(model_folder.token, child_name, library=library)
        source_token = child.token if child else model_folder.token
        return (
            tuple(
                RemoteDriveFile.from_entry(entry)
                for entry in self._entries(source_token)
                if entry.entry_type == "file"
            ),
            child is not None,
        )

    @staticmethod
    def _image_files(files: Iterable[RemoteDriveFile]) -> tuple[RemoteDriveFile, ...]:
        return tuple(
            item for item in files
            if any(item.name.casefold().endswith(suffix) for suffix in ALLOWED_IMAGE_SUFFIXES)
        )

    @staticmethod
    def _named_main_files(files: Iterable[RemoteDriveFile]) -> tuple[RemoteDriveFile, ...]:
        return tuple(
            item for item in files
            if _segment_key(_image_stem(item.name)) == _segment_key("主图")
        )

    def _main_files_from_remote_folder(
        self, model_folder: RemoteDriveEntry | None,
    ) -> tuple[RemoteDriveFile, ...]:
        files, has_main_child = self._files_in_optional_child(
            model_folder, "主图", library="飞书店铺主图",
        )
        named = self._named_main_files(files)
        if named:
            return (self._pick_main(named),)
        if not has_main_child:
            # Legacy flat directory requires the stable 主图.* filename.  It
            # must not mistake a colocated white image for a store main image.
            return ()
        candidates = self._image_files(files)
        # The explicit 主图 folder is already bound to the selected model.
        return (self._pick_main(candidates),) if candidates else ()

    @staticmethod
    def _entry_files_in_optional_child(
        entries: Iterable[IndexedDriveFile], model_path: tuple[str, ...] | None, child_name: str,
    ) -> tuple[tuple[RemoteDriveFile, ...], bool]:
        if model_path is None:
            return (), False
        nested_path = model_path + (child_name,)
        nested = tuple(item.file for item in entries if item.relative_path[:-1] == nested_path)
        if nested:
            return nested, True
        return (
            tuple(item.file for item in entries if item.relative_path[:-1] == model_path),
            False,
        )

    @staticmethod
    def _natural_name_key(value: str) -> tuple[object, ...]:
        return tuple(
            int(item) if item.isdigit() else item.casefold()
            for item in re.split(r"(\d+)", value)
        )

    def resolve_bundle(self, shop_id: str, brand: str, model: str,
                       entries: Iterable[IndexedDriveFile] | None = None) -> MaterialBundle:
        shop = str(shop_id).strip()
        main_prefix = self._main_prefix(shop, brand.strip())
        white_prefix = self._white_prefix(brand.strip())
        if entries is None:
            remote_mains, _ = self._remote_main_files(main_prefix, brand, model)
            mains = list(remote_mains)
            white_folder, folder_evidence = self._remote_model_folder(
                white_prefix, brand, model, library="飞书白底图库",
            )
            white_source, _ = self._files_in_optional_child(
                white_folder, "白底图", library="飞书白底图库",
            )
            # This is already an exact, operator-maintained white-library model
            # directory.  Colour names such as 海阔天空.jpg are authoritative;
            # filenames no longer need the legacy 白底图_ prefix.
            white_files = list(self._image_files(white_source))
        else:
            all_files = tuple(entries)
            main_path, _ = self._exact_model_path(
                all_files, main_prefix, brand, model, library="飞书店铺主图",
            )
            white_path, folder_evidence = self._exact_model_path(
                all_files, white_prefix, brand, model, library="飞书白底图库",
            )
            main_source, main_has_child = self._entry_files_in_optional_child(all_files, main_path, "主图")
            mains = list(self._named_main_files(main_source))
            if mains:
                mains = [self._pick_main(mains)]
            if not mains and main_has_child:
                candidates = self._image_files(main_source)
                mains = [self._pick_main(candidates)] if candidates else []
            white_source, _ = self._entry_files_in_optional_child(all_files, white_path, "白底图")
            white_files = list(self._image_files(white_source))
        # An existing but invalid main must block. Treating it as absent would
        # silently replace an operator-maintained cover with a white image.
        main = self._download_verified(mains[0], role="主图") if mains else None
        omissions: list[MaterialOmission] = []
        white_images: list[CloudImage] = []
        spec_image_candidates: list[CloudImage] = []
        seen = {main.sha256.casefold()} if main else set()
        for item in sorted(white_files, key=lambda value: self._natural_name_key(value.name)):
            try:
                candidate = self._download_verified(item, role="白底图")
            except MaterialPolicyError as exc:
                omissions.append(MaterialOmission(item.name, str(exc)))
                continue
            digest = candidate.sha256.casefold()
            if digest in seen:
                omissions.append(MaterialOmission(item.name, "与主图或已选白底图内容重复"))
                continue
            seen.add(digest)
            spec_image_candidates.append(candidate)
            if len(white_images) < MAXIMUM_WHITE_IMAGES:
                white_images.append(candidate)
            else:
                omissions.append(MaterialOmission(
                    item.name, "主图区白底图最多 4 张；该图仍保留用于颜色规格匹配",
                ))
        return MaterialBundle(
            shop, brand.strip(), model.strip(), main, tuple(white_images), tuple(omissions),
            spec_image_candidates=tuple(spec_image_candidates),
            model_folder_resolution=folder_evidence.strategy,
            resolved_model_folder=folder_evidence.folder_name,
            official_model_name=folder_evidence.official_name,
            official_model_source_url=folder_evidence.official_source_url,
        )

    def _download_verified(self, item: RemoteDriveFile, *, role: str) -> CloudImage:
        data = self.drive.download(item.file_token)
        if item.size_bytes and len(data) != item.size_bytes:
            raise MaterialPolicyError("%s下载字节数与飞书文件元数据不一致" % role)
        reason = main_area_image_error(item.name, data)
        if reason:
            raise MaterialPolicyError("%s%s" % (role, reason))
        return CloudImage(
            file_token=item.file_token,
            revision=item.revision,
            name=item.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        )
