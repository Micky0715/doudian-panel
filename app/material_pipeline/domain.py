"""Pure data and safety rules for cloud-hosted listing images."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


ALLOWED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
MINIMUM_WHITE_IMAGES = 2
MAXIMUM_WHITE_IMAGES = 4


class MaterialPolicyError(ValueError):
    """Raised before any DouDian material or product write is attempted."""


@dataclass(frozen=True)
class CloudImage:
    """A single immutable source file in Feishu Drive.

    ``file_token`` and ``revision`` identify the remote source. ``sha256`` is
    calculated after download, so a cached DouDian URL can never be reused for
    a silently replaced cloud file.
    """

    file_token: str
    revision: str
    name: str
    sha256: str
    size_bytes: int

    def validate(self) -> None:
        if not self.file_token.strip():
            raise MaterialPolicyError("cloud image file token is required")
        if not self.revision.strip():
            raise MaterialPolicyError("cloud image revision is required")
        if not self.name.strip() or not any(
            self.name.casefold().endswith(value) for value in ALLOWED_IMAGE_SUFFIXES
        ):
            raise MaterialPolicyError("cloud image must use jpg/jpeg/png/webp")
        digest = self.sha256.strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise MaterialPolicyError("cloud image sha256 must be a SHA-256 hex value")
        if self.size_bytes <= 0:
            raise MaterialPolicyError("cloud image is empty")


@dataclass(frozen=True)
class MaterialOmission:
    """A white image deliberately not used, with an operator-facing reason."""

    name: str
    reason: str


@dataclass(frozen=True)
class MaterialBundle:
    """The one primary image and shared white images for a listing.

    A real store main is preferred. If it is truly absent (not merely invalid)
    the first of at least three safe white images becomes the main, matching the browser
    policy. The resulting plan records that substitution explicitly.
    """

    shop_id: str
    brand: str
    model: str
    main: CloudImage | None
    white_images: tuple[CloudImage, ...]
    omitted_white_images: tuple[MaterialOmission, ...] = ()
    # Every valid image in the exact white-library model directory.  The main
    # area is capped at four whites, but the same operator-maintained colour
    # images may also be needed for all first-level SKU colour slots.
    spec_image_candidates: tuple[CloudImage, ...] = ()
    model_folder_resolution: str = "exact"
    resolved_model_folder: str = ""
    official_model_name: str = ""
    official_model_source_url: str = ""

    def validate(self, *, minimum_white_images: int = MINIMUM_WHITE_IMAGES) -> None:
        if not self.shop_id.strip().isdigit():
            raise MaterialPolicyError("shop id must be numeric")
        if not self.brand.strip() or not self.model.strip():
            raise MaterialPolicyError("brand and model are required")
        if self.model_folder_resolution not in {
            "exact", "rule_alias", "version_suffix_removed", "official_registry",
        }:
            raise MaterialPolicyError("unknown model folder resolution strategy")
        if self.model_folder_resolution == "official_registry" and (
            not self.official_model_name.strip()
            or not self.official_model_source_url.startswith("https://")
        ):
            raise MaterialPolicyError("official model resolution requires audited evidence")
        if len(self.white_images) < minimum_white_images:
            raise MaterialPolicyError("at least two white-background images are required")
        if len(self.white_images) > MAXIMUM_WHITE_IMAGES:
            raise MaterialPolicyError("at most four white-background images may be uploaded")
        if self.main is None and len(self.white_images) < minimum_white_images + 1:
            raise MaterialPolicyError("missing main image requires three distinct uploadable white images")
        if self.main is not None:
            self.main.validate()
        seen = {self.main.sha256.casefold()} if self.main is not None else set()
        for item in self.white_images:
            item.validate()
            digest = item.sha256.casefold()
            if digest in seen:
                raise MaterialPolicyError("main and white images must have distinct content")
            seen.add(digest)
        candidate_seen = set()
        for item in self.spec_image_candidates:
            item.validate()
            digest = item.sha256.casefold()
            if digest in candidate_seen:
                raise MaterialPolicyError("spec image candidates must have distinct content")
            candidate_seen.add(digest)


@dataclass(frozen=True)
class MaterialPlan:
    """Immutable evidence to upload exactly one product image set."""

    shop_id: str
    brand: str
    model: str
    main: CloudImage
    white_images: tuple[CloudImage, ...]
    main_substituted: bool = False
    omitted_white_images: tuple[MaterialOmission, ...] = ()
    spec_image_candidates: tuple[CloudImage, ...] = ()
    model_folder_resolution: str = "exact"
    resolved_model_folder: str = ""
    official_model_name: str = ""
    official_model_source_url: str = ""
    source: str = "feishu_drive"

    def as_dict(self) -> dict[str, Any]:
        def encode(item: CloudImage) -> dict[str, Any]:
            return {
                "file_token": item.file_token,
                "revision": item.revision,
                "name": item.name,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }

        return {
            "source": self.source,
            "shop_id": self.shop_id,
            "brand": self.brand,
            "model": self.model,
            "main": encode(self.main),
            "white_images": [encode(item) for item in self.white_images],
            "main_substituted": self.main_substituted,
            "omitted_white_images": [
                {"name": item.name, "reason": item.reason}
                for item in self.omitted_white_images
            ],
            "spec_image_candidates": [
                encode(item) for item in self.spec_image_candidates
            ],
            "model_folder_resolution": self.model_folder_resolution,
            "resolved_model_folder": self.resolved_model_folder,
            "official_model_name": self.official_model_name,
            "official_model_source_url": self.official_model_source_url,
        }
