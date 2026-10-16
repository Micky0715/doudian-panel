"""The immutable contract for Gaia's dual-condition listing workbook.

This module contains no file, browser or HTTP code.  Every future writer,
validator and UI preview must use these field names and row-building rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


GAIA_TEMPLATE_HEADERS: Final[tuple[str, ...]] = (
    "场域（1、直播间；2、直播货架）",
    "聚合标识（英文）",
    "是否质检链接（1-是，0-否）",
    "标品ID（standardSkuId）",
    "抖音成色ID（dyQualityId）",
    "名称前缀（namePrefix）",
    "选品策略，可以使用“且”、“或”、“非”\n三个逻辑词语组合货品标签",
    "商品规则前缀（例如A-B-C-）",
)

GAIA_FIXED_AGGREGATE_MARK: Final[str] = "A"
GAIA_FIXED_QUALITY_LINK: Final[str] = "1"
GAIA_DEFAULT_RULE_PREFIX: Final[str] = "a"
PHONE_SELECTION_STRATEGY: Final[str] = "D且E且G"


class GaiaTemplateContractError(ValueError):
    """Raised when a row cannot be proven valid for the Gaia template."""


def aggregate_mark_for_index(index: int) -> str:
    """Return A..Z, AA..AZ, etc. for a zero-based model position."""
    if index < 0:
        raise GaiaTemplateContractError("型号序号不能为负数")
    mark = ""
    while True:
        index, remainder = divmod(index, 26)
        mark = chr(ord("A") + remainder) + mark
        if index == 0:
            return mark
        index -= 1


def selection_strategy_for_category(category: str) -> str:
    """Only the exact phone category receives the confirmed strategy string."""
    return PHONE_SELECTION_STRATEGY if str(category or "").strip() == "手机" else ""


@dataclass(frozen=True)
class GaiaTemplateRow:
    """One A:H data row in the Gaia workbook, in its physical column order."""

    scene: str
    standard_sku_id: str
    dy_quality_id: str
    category: str
    aggregate_mark: str = GAIA_FIXED_AGGREGATE_MARK
    quality_link: str = GAIA_FIXED_QUALITY_LINK

    def __post_init__(self) -> None:
        if str(self.scene) not in {"1", "2"}:
            raise GaiaTemplateContractError("场域仅允许 1（直播间）或 2（直播货架）")
        if not str(self.standard_sku_id or "").strip():
            raise GaiaTemplateContractError("缺少标品ID（standardSkuId）")
        if not str(self.dy_quality_id or "").strip():
            raise GaiaTemplateContractError("缺少抖音成色ID（dyQualityId）")
        if not self.aggregate_mark or not self.aggregate_mark.isascii() or not self.aggregate_mark.isalpha() or not self.aggregate_mark.isupper():
            raise GaiaTemplateContractError("聚合标识必须是大写英文字母")
        if self.quality_link not in {"0", "1"}:
            raise GaiaTemplateContractError("是否质检链接只能填 1（是）或 0（否）")

    def values(self) -> tuple[str, ...]:
        """Return only writable A:H values. Template notes are never copied as data."""
        return (
            str(self.scene),
            self.aggregate_mark,
            self.quality_link,
            str(self.standard_sku_id).strip(),
            str(self.dy_quality_id).strip(),
            "",
            selection_strategy_for_category(self.category),
            GAIA_DEFAULT_RULE_PREFIX,
        )
