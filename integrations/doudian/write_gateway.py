"""Narrow, one-product DouDian write and submission boundaries.

This module deliberately does not share the read-only allowlist.  A writer is
constructed for one shop and one product ID, and the first migration step only
allows ``product.editV2`` with ``product_id`` + ``product_format_new``.

Saving a draft and submitting a product are deliberately different boundaries:
``DoudianSingleProductWriter`` only accepts ``commit=false`` while
``DoudianSingleProductPublisher`` only accepts a pre-hashed immutable
``commit=true`` payload.  A later feature must not be able to turn a save into
a publication merely by changing a boolean on the save path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping
import hashlib
import json

from doudian_api import DoudianClient, DoudianConfigError

from .token_store import EnvTokenStore


MODEL_ONLY_EDIT_KEYS = frozenset({"product_id", "product_format_new", "commit"})
RECOMMENDED_TITLE_SUBMISSION_KEYS = MODEL_ONLY_EDIT_KEYS | {"name", "name_prefix"}
RECOMMENDED_TITLE_SUBMISSION_KEYS_WITH_WHITE = (
    RECOMMENDED_TITLE_SUBMISSION_KEYS | {"white_back_ground_pic_url"}
)
IMAGE_DRAFT_EDIT_KEYS = frozenset({"product_id", "product_format_new", "pic", "commit"})
MEDIA_DRAFT_EDIT_KEYS = frozenset({"product_id", "product_format_new", "pic", "description", "commit"})
SPEC_IMAGE_DRAFT_EDIT_KEYS = frozenset({"product_id", "product_format_new", "spec_pic", "commit"})
SPEC_IMAGE_PREFIX_REPAIR_KEYS = SPEC_IMAGE_DRAFT_EDIT_KEYS | {"name_prefix"}
GRADE_DETAIL_DRAFT_KEYS = frozenset({
    "product_id", "product_format_new", "description", "spec_pic", "name_prefix", "commit",
})
FULL_MEDIA_DRAFT_KEYS = GRADE_DETAIL_DRAFT_KEYS | {"pic", "standard_brand_id"}
FULL_MEDIA_DRAFT_KEYS_WITHOUT_SPEC = FULL_MEDIA_DRAFT_KEYS - {"spec_pic"}
FULL_MEDIA_DRAFT_KEY_SETS = {
    FULL_MEDIA_DRAFT_KEYS,
    FULL_MEDIA_DRAFT_KEYS_WITHOUT_SPEC,
    FULL_MEDIA_DRAFT_KEYS | {"white_back_ground_pic_url"},
    FULL_MEDIA_DRAFT_KEYS_WITHOUT_SPEC | {"white_back_ground_pic_url"},
}


def submission_payload_sha256(payload: Mapping[str, Any]) -> str:
    """Return a stable digest for the exact API submission body."""
    return hashlib.sha256(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class DoudianWriteBoundaryError(PermissionError):
    pass


class DoudianSingleProductWriter:
    """Permit one model/property edit against one explicitly locked product."""

    def __init__(
        self,
        env_path: str | Path,
        shop_id: str | int,
        product_id: str | int,
        *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def edit_model_properties(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) != MODEL_ONLY_EDIT_KEYS:
            raise DoudianWriteBoundaryError(
                "first write phase only allows product_id, product_format_new and commit=false"
            )
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match write lock")
        value = body.get("product_format_new")
        if not isinstance(value, str) or not value.strip():
            raise DoudianWriteBoundaryError("product_format_new must be non-empty JSON text")
        if body.get("commit") is not False:
            raise DoudianWriteBoundaryError("first write phase requires commit=false (save draft only)")
        return self._client.call("product.editV2", body)


class DoudianSingleProductImageDraftWriter:
    """Save only one product's already-uploaded DouDian image URLs to draft.

    The separate class prevents a caller from slipping image changes into the
    property-only writer.  ``pic`` must be a non-empty, ordered ``|``-joined
    list of DouDian platform URLs and this boundary can never submit.
    """

    def __init__(
        self, env_path: str | Path, shop_id: str | int, product_id: str | int, *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def save_images_to_draft(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) not in {IMAGE_DRAFT_EDIT_KEYS, MEDIA_DRAFT_EDIT_KEYS}:
            raise DoudianWriteBoundaryError(
                "image draft edit only allows product_id, product_format_new, pic, optional description and commit=false"
            )
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match image write lock")
        if not isinstance(body.get("product_format_new"), str) or not body["product_format_new"].strip():
            raise DoudianWriteBoundaryError("product_format_new must be non-empty JSON text")
        pic = body.get("pic")
        if not isinstance(pic, str) or not pic.strip():
            raise DoudianWriteBoundaryError("pic must be a non-empty DouDian URL list")
        urls = [item.strip() for item in pic.split("|") if item.strip()]
        if not urls or any(not item.startswith(("http://", "https://")) for item in urls):
            raise DoudianWriteBoundaryError("pic contains an invalid platform URL")
        if body.get("commit") is not False:
            raise DoudianWriteBoundaryError("image migration requires commit=false (save draft only)")
        if "description" in body:
            description = body.get("description")
            if not isinstance(description, str) or not description.strip():
                raise DoudianWriteBoundaryError("description must be a non-empty DouDian URL list when supplied")
            detail_urls = [item.strip() for item in description.split("|") if item.strip()]
            if not detail_urls or any(not item.startswith(("http://", "https://")) for item in detail_urls):
                raise DoudianWriteBoundaryError("description contains an invalid platform URL")
        return self._client.call("product.editV2", body)


class DoudianSingleProductSpecImageDraftWriter:
    """Write only first-level SKU colour image URLs to one product draft.

    ``spec_pic`` is position-aligned to the existing first specification
    group's values. The caller must check that mapping and SKU invariants
    before and after this narrow write. This boundary cannot publish.
    """

    def __init__(
        self, env_path: str | Path, shop_id: str | int, product_id: str | int, *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def save_spec_images_to_draft(self, payload: Mapping[str, Any], *, color_count: int) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) not in {SPEC_IMAGE_DRAFT_EDIT_KEYS, SPEC_IMAGE_PREFIX_REPAIR_KEYS}:
            raise DoudianWriteBoundaryError(
                "SKU image draft edit only allows product_id, product_format_new, spec_pic, optional name_prefix and commit=false"
            )
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match SKU image write lock")
        if body.get("commit") is not False:
            raise DoudianWriteBoundaryError("SKU image edit requires commit=false (save draft only)")
        value = body.get("product_format_new")
        if not isinstance(value, str) or not value.strip():
            raise DoudianWriteBoundaryError("product_format_new must be non-empty JSON text")
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise DoudianWriteBoundaryError("product_format_new is not JSON") from exc
        if not isinstance(parsed, dict) or not parsed:
            raise DoudianWriteBoundaryError("product_format_new must be a non-empty JSON object")
        if "name_prefix" in body and (
            not isinstance(body["name_prefix"], str) or not body["name_prefix"].strip()
        ):
            raise DoudianWriteBoundaryError("name_prefix must be non-empty text when supplied")
        pic = body.get("spec_pic")
        if not isinstance(pic, str) or not pic.strip() or not 1 <= color_count <= 100:
            raise DoudianWriteBoundaryError("spec_pic and color_count must be non-empty")
        urls = pic.split(",")
        if len(urls) != color_count or any(
            not url or url != url.strip() or not url.startswith(("http://", "https://"))
            for url in urls
        ):
            raise DoudianWriteBoundaryError("spec_pic URLs must exactly match first-level colour count")
        return self._client.call("product.editV2", body)


class DoudianSingleProductGradeDetailDraftWriter:
    """Replace detail images on one draft while explicitly preserving SKU pics and prefix."""

    def __init__(self, env_path: str | Path, shop_id: str | int, product_id: str | int, *,
                 client_factory: Callable[..., Any] | None = None):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def save_grade_details_to_draft(self, payload: Mapping[str, Any], *,
                                    detail_count: int, color_count: int) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) != GRADE_DETAIL_DRAFT_KEYS or body.get("commit") is not False:
            raise DoudianWriteBoundaryError("grade detail edit requires exact fields and commit=false")
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match grade detail lock")
        try:
            props = json.loads(body.get("product_format_new"))
        except (TypeError, ValueError) as exc:
            raise DoudianWriteBoundaryError("product_format_new must be JSON") from exc
        if not isinstance(props, dict) or not props:
            raise DoudianWriteBoundaryError("product_format_new must be a non-empty object")
        if not isinstance(body.get("name_prefix"), str) or not body["name_prefix"].strip():
            raise DoudianWriteBoundaryError("name_prefix must preserve a non-empty title prefix")
        descriptions = body.get("description")
        colors = body.get("spec_pic")
        if not isinstance(descriptions, str) or not isinstance(colors, str):
            raise DoudianWriteBoundaryError("detail and SKU picture URL lists are required")
        detail_urls = descriptions.split("|")
        color_urls = colors.split(",")
        if (len(detail_urls) != detail_count or len(color_urls) != color_count
                or not 7 <= detail_count <= 20 or not 1 <= color_count <= 20
                or detail_count + color_count > 50):
            raise DoudianWriteBoundaryError("detail or colour URL count is invalid")
        if any(not url.startswith(("https://", "http://")) or url != url.strip()
               for url in detail_urls + color_urls):
            raise DoudianWriteBoundaryError("all image URLs must be platform HTTP URLs")
        return self._client.call("product.editV2", body)


class DoudianSingleProductFullMediaDraftWriter:
    """One locked draft write for main, white, colour and grade-detail images.

    The SKU structure itself is not sent or modified. This boundary cannot
    publish, and no partial subset of media fields can be submitted.
    """

    def __init__(self, env_path: str | Path, shop_id: str | int, product_id: str | int, *,
                 client_factory: Callable[..., Any] | None = None):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def save_full_media_to_draft(self, payload: Mapping[str, Any], *,
                                 main_count: int, detail_count: int, color_count: int) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) not in FULL_MEDIA_DRAFT_KEY_SETS \
                or body.get("commit") is not False:
            raise DoudianWriteBoundaryError("full media edit requires exact fields and commit=false")
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match full media lock")
        try:
            props = json.loads(body.get("product_format_new"))
        except (TypeError, ValueError) as exc:
            raise DoudianWriteBoundaryError("product_format_new must be JSON") from exc
        if not isinstance(props, dict) or not props:
            raise DoudianWriteBoundaryError("product_format_new must be a non-empty object")
        if not isinstance(body.get("name_prefix"), str) or not body["name_prefix"].strip():
            raise DoudianWriteBoundaryError("name_prefix must be preserved")
        if "white_back_ground_pic_url" in body:
            white_url = body.get("white_back_ground_pic_url")
            if (not isinstance(white_url, str) or white_url != white_url.strip()
                    or not white_url.startswith(("https://", "http://"))
                    or "/obj/ecom-shop-material/" not in white_url):
                raise DoudianWriteBoundaryError(
                    "white_back_ground_pic_url must be one non-empty material-centre URL"
                )
        standard_brand_id = body.get("standard_brand_id")
        if (isinstance(standard_brand_id, bool)
                or not str(standard_brand_id or "").isdigit()
                or int(standard_brand_id) <= 0):
            raise DoudianWriteBoundaryError("standard_brand_id must be a positive integer")
        has_spec_pic = "spec_pic" in body
        if not 3 <= main_count <= 5 or not 7 <= detail_count <= 20:
            raise DoudianWriteBoundaryError("full media image counts are outside the canary boundary")
        if has_spec_pic and not 1 <= color_count <= 20:
            raise DoudianWriteBoundaryError("colour slot count is outside the canary boundary")
        if not has_spec_pic and color_count != 0:
            raise DoudianWriteBoundaryError("color_count must be zero when spec_pic is omitted")
        nonempty_color_count = 0
        if main_count + detail_count > 50:
            raise DoudianWriteBoundaryError("full media image count exceeds 50")
        for field, separator, count in (
            ("pic", "|", main_count),
            ("description", "|", detail_count),
        ):
            value = body.get(field)
            if not isinstance(value, str):
                raise DoudianWriteBoundaryError(field + " must be URL text")
            urls = value.split(separator)
            if len(urls) != count or any(
                not url.startswith(("https://", "http://")) or url != url.strip()
                for url in urls
            ):
                raise DoudianWriteBoundaryError(field + " must exactly match valid URL count")
        if has_spec_pic:
            value = body.get("spec_pic")
            if not isinstance(value, str):
                raise DoudianWriteBoundaryError("spec_pic must be URL text")
            urls = value.split(",")
            if len(urls) != color_count or any(
                not url or url != url.strip()
                or not url.startswith(("https://", "http://"))
                for url in urls
            ):
                raise DoudianWriteBoundaryError(
                    "spec_pic must completely cover all slots with valid URLs"
                )
            nonempty_color_count = len(urls)
        if main_count + detail_count + nonempty_color_count > 50:
            raise DoudianWriteBoundaryError("full media image count exceeds 50")
        return self._client.call("product.editV2", body)


class DoudianSingleProductPublisher:
    """Submit one explicitly planned draft through ``product.editV2``.

    This boundary is intentionally separate from :class:`DoudianSingleProductWriter`.
    The caller must construct and persist an immutable plan first, then pass its
    digest here. Categories requiring a title prefix must carry the exact
    platform-recommended ``name_prefix`` (including its trailing space) and a
    matching full ``name``. No other product fields can be attached.
    """

    def __init__(
        self,
        env_path: str | Path,
        shop_id: str | int,
        product_id: str | int,
        planned_payload_sha256: str,
        *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        self.planned_payload_sha256 = str(planned_payload_sha256).strip().lower()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        if len(self.planned_payload_sha256) != 64 or any(
            value not in "0123456789abcdef" for value in self.planned_payload_sha256
        ):
            raise DoudianConfigError("planned payload digest must be a SHA-256 hex value")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def submit_planned_draft(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) not in {
            MODEL_ONLY_EDIT_KEYS,
            RECOMMENDED_TITLE_SUBMISSION_KEYS,
            RECOMMENDED_TITLE_SUBMISSION_KEYS_WITH_WHITE,
        }:
            raise DoudianWriteBoundaryError(
                "submission only allows product_id, product_format_new, optional recommended name/name_prefix and commit=true"
            )
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError("payload product id does not match publish lock")
        value = body.get("product_format_new")
        if not isinstance(value, str) or not value.strip():
            raise DoudianWriteBoundaryError("product_format_new must be non-empty JSON text")
        if body.get("commit") is not True:
            raise DoudianWriteBoundaryError("submission phase requires commit=true")
        if "name_prefix" in body:
            prefix = body["name_prefix"]
            name = body.get("name")
            if (not isinstance(prefix, str) or not prefix.strip() or not prefix.endswith(" ")
                    or not isinstance(name, str) or not name.startswith(prefix)
                    or not name[len(prefix):].strip()):
                raise DoudianWriteBoundaryError(
                    "submission name must contain the exact recommended prefix with trailing space"
                )
        if "white_back_ground_pic_url" in body:
            white_url = body.get("white_back_ground_pic_url")
            if (not isinstance(white_url, str) or white_url != white_url.strip()
                    or not white_url.startswith(("https://", "http://"))
                    or "/obj/ecom-shop-material/" not in white_url):
                raise DoudianWriteBoundaryError(
                    "white_back_ground_pic_url must be one non-empty material-centre URL"
                )
        if submission_payload_sha256(body) != self.planned_payload_sha256:
            raise DoudianWriteBoundaryError("payload differs from immutable publish plan")
        return self._client.call("product.editV2", body)


class DoudianSingleProductLauncher:
    """Launch one explicitly locked, already-approved DouDian product.

    ``product.editV2(commit=true)`` submits an edited draft for review.  Once
    that review has passed, the ordinary e-commerce launch operation is a
    separate ``product.launchProduct`` call.  Keeping it behind its own class
    prevents an edit/save caller from silently acquiring the ability to put a
    product on sale.
    """

    def __init__(
        self,
        env_path: str | Path,
        shop_id: str | int,
        product_id: str | int,
        *,
        client_factory: Callable[..., Any] | None = None,
    ):
        self.shop_id = str(shop_id).strip()
        self.product_id = str(product_id).strip()
        if not self.shop_id.isdigit() or not self.product_id.isdigit():
            raise DoudianConfigError("shop id and product id must be numeric")
        config = EnvTokenStore(env_path).load_config()
        factory = client_factory or (lambda cfg, shop: DoudianClient(cfg, shop))
        self._client = factory(config, self.shop_id)

    def launch(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        body = dict(payload)
        if set(body) != {"product_id"}:
            raise DoudianWriteBoundaryError(
                "launch only allows the locked product_id"
            )
        if str(body.get("product_id") or "") != self.product_id:
            raise DoudianWriteBoundaryError(
                "payload product id does not match launch lock"
            )
        return self._client.call("product.launchProduct", body)
