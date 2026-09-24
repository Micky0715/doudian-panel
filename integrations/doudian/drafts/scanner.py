"""Complete, fail-closed draft pagination for the DouDian product API."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol


# Verified on 2026-09-03 by exact ID reconciliation against the current UI.
# status=0 alone also includes checking/other records; check_status=1 is required.
DOUDIAN_DRAFT_STATUS = 0
DOUDIAN_DRAFT_CHECK_STATUS = 1


class DraftScanError(RuntimeError):
    pass


class ProductPageReader(Protocol):
    def list_products(self, *, page: int, size: int, **filters) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DraftItem:
    product_id: str
    name: str
    status: int
    check_status: str
    create_time: str
    update_time: str
    category_leaf_id: str
    category_path: str


@dataclass(frozen=True)
class DraftSnapshot:
    schema_version: int
    shop_id: str
    captured_at: int
    status_filter: int
    check_status_filter: int
    reported_total: int
    page_count: int
    page_size: int
    checksum: str
    items: tuple[DraftItem, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["items"] = [asdict(item) for item in self.items]
        return value


class DraftScanner:
    def __init__(
        self,
        reader: ProductPageReader,
        shop_id: str | int,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.reader = reader
        self.shop_id = str(shop_id).strip()
        if not self.shop_id.isdigit():
            raise DraftScanError("shop id must be numeric")
        self.clock = clock

    def scan(self, *, page_size: int = 100, max_pages: int = 200) -> DraftSnapshot:
        if not 1 <= int(page_size) <= 100:
            raise DraftScanError("page_size must be between 1 and 100")
        if int(max_pages) < 1:
            raise DraftScanError("max_pages must be positive")

        items: list[DraftItem] = []
        seen_ids: set[str] = set()
        page_signatures: set[tuple[str, ...]] = set()
        reported_total: int | None = None
        page = 1

        while page <= int(max_pages):
            payload = self.reader.list_products(
                page=page,
                size=int(page_size),
                status=DOUDIAN_DRAFT_STATUS,
                check_status=DOUDIAN_DRAFT_CHECK_STATUS,
            )
            rows = self._rows(payload)
            current_total = self._total(payload)
            if reported_total is None:
                reported_total = current_total
            elif current_total != reported_total:
                raise DraftScanError(
                    f"draft total changed during scan: {reported_total} -> {current_total}"
                )

            parsed = tuple(self._parse_item(row) for row in rows)
            signature = tuple(item.product_id for item in parsed)
            if signature and signature in page_signatures:
                raise DraftScanError(f"draft page repeated without progress at page {page}")
            if signature:
                page_signatures.add(signature)

            for item in parsed:
                if item.product_id in seen_ids:
                    raise DraftScanError(f"duplicate draft id across pages: {item.product_id}")
                seen_ids.add(item.product_id)
                items.append(item)

            if len(items) > current_total:
                raise DraftScanError(
                    f"collected more drafts than API total: {len(items)} > {current_total}"
                )
            if len(items) == current_total:
                break
            if not rows:
                raise DraftScanError(
                    f"empty draft page {page} before reported total {current_total}"
                )
            if len(rows) < int(page_size):
                raise DraftScanError(
                    f"short draft page {page} before reported total {current_total}"
                )
            page += 1
        else:
            raise DraftScanError(
                f"draft scan exceeded max_pages={max_pages} before reaching total"
            )

        total = int(reported_total or 0)
        checksum_source = {
            "shop_id": self.shop_id,
            "status_filter": DOUDIAN_DRAFT_STATUS,
            "check_status_filter": DOUDIAN_DRAFT_CHECK_STATUS,
            "reported_total": total,
            "items": [asdict(item) for item in items],
        }
        checksum = hashlib.sha256(json.dumps(
            checksum_source, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        return DraftSnapshot(
            schema_version=2,
            shop_id=self.shop_id,
            captured_at=int(self.clock()),
            status_filter=DOUDIAN_DRAFT_STATUS,
            check_status_filter=DOUDIAN_DRAFT_CHECK_STATUS,
            reported_total=total,
            page_count=page,
            page_size=int(page_size),
            checksum=checksum,
            items=tuple(items),
        )

    @staticmethod
    def _rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        for key in ("data", "list", "products", "product_list"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, Mapping)]
        raise DraftScanError("product.listV2 response has no product list")

    @staticmethod
    def _total(payload: Mapping[str, Any]) -> int:
        try:
            total = int(payload.get("total"))
        except (TypeError, ValueError) as exc:
            raise DraftScanError("product.listV2 response has invalid total") from exc
        if total < 0:
            raise DraftScanError("product.listV2 total must not be negative")
        return total

    @staticmethod
    def _parse_item(row: Mapping[str, Any]) -> DraftItem:
        product_id = str(row.get("product_id") or row.get("product_id_str") or "").strip()
        if not product_id.isdigit():
            raise DraftScanError("draft row has no numeric product id")
        try:
            status = int(row.get("status"))
        except (TypeError, ValueError) as exc:
            raise DraftScanError(f"draft {product_id} has invalid status") from exc
        if status != DOUDIAN_DRAFT_STATUS:
            raise DraftScanError(
                f"draft filter returned unexpected status for {product_id}: {status}"
            )
        check_status = str(row.get("check_status") or "").strip()
        if check_status != str(DOUDIAN_DRAFT_CHECK_STATUS):
            raise DraftScanError(
                f"draft filter returned unexpected check_status for {product_id}: {check_status}"
            )
        category = row.get("category_detail")
        category = category if isinstance(category, Mapping) else {}
        names = [
            str(category.get(key) or "").strip()
            for key in ("first_cname", "second_cname", "third_cname", "fourth_cname")
        ]
        leaf_id = ""
        for key in ("fourth_cid", "third_cid", "second_cid", "first_cid"):
            candidate = str(category.get(key) or "").strip()
            if candidate:
                leaf_id = candidate
                break
        return DraftItem(
            product_id=product_id,
            name=str(row.get("name") or "").strip(),
            status=status,
            check_status=check_status,
            create_time=str(row.get("create_time") or "").strip(),
            update_time=str(row.get("update_time") or "").strip(),
            category_leaf_id=leaf_id,
            category_path=" > ".join(name for name in names if name),
        )
