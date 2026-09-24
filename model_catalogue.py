"""Conservative lookup against the authoritative Excel-derived model catalogue."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Iterable, Mapping
from brand_model_rules import catalogue_groups


BRAND_GROUPS = catalogue_groups()

# Only trailing edition labels are removable. Core model differentiators such
# as Pro/Max/Ultra/+/Fold/Flip are deliberately absent from this list.
KNOWN_VERSION_SUFFIXES = tuple(sorted({
    "北斗卫星消息版", "卫星通信版", "保时捷设计版", "运营商定制版",
    "徕卡版", "活力版", "典藏版", "青春版", "竞速版", "极速版",
    "特别版", "限定版", "珍藏版", "纪念版", "高能版", "优享版",
    "乐臻版", "影像版", "电竞版", "标准版", "5G版", "4G版",
}, key=len, reverse=True))


def _text(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def canonical_brand(value: Any) -> str:
    normalized = re.sub(r"\s+", "", _text(value))
    for key, aliases in BRAND_GROUPS.items():
        if normalized in {re.sub(r"\s+", "", _text(x)) for x in aliases}:
            return key
    return normalized


def _strip_brand_prefix(value: Any) -> str:
    text = _text(value)
    aliases = sorted({alias for group in BRAND_GROUPS.values() for alias in group}, key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for alias in aliases:
            updated = re.sub(r"^\s*" + re.escape(_text(alias)) + r"(?:\s*[/·-]\s*)?", "", text)
            if updated != text:
                text, changed = updated.strip(), True
                break
    return text


def model_key(value: Any) -> str:
    """Remove brand prefix/formatting only; retain all variant-defining text."""
    key = re.sub(r"[\s()（）·._/，,-]+", "", _strip_brand_prefix(value))
    # 盖亚/运营表写作“K70（至尊版）”，抖店属性枚举写作“K70至尊”。
    # 两者是同一官方型号；只归一这一项明确的文字差异，不能把 Pro/Max/Ultra
    # 等版本词泛化掉，否则会再次发生型号坍缩。
    return key.replace("至尊版", "至尊")


def spaced_model_key(value: Any) -> str:
    """Brand-stripped text that keeps single-space token boundaries.

    Used to tell a real space-separated variant ("K80" + " Pro") apart from a
    different model that merely starts with the same digits ("K80" + "E"),
    which model_key()'s fully-stripped form can no longer distinguish.
    """
    text = re.sub(r"[()（）·._/，,-]+", " ", _strip_brand_prefix(value))
    return re.sub(r"\s+", " ", text).strip()


def versionless_model_candidates(value: Any) -> tuple[str, ...]:
    """Return conservative base names after removing one trailing ``...版``.

    Callers must always try the complete model first and may only accept this
    fallback when it resolves to one unique candidate.  This helper never
    removes core hardware variants such as Pro, Max, Ultra, Fold, Flip or ``+``.
    """
    source = " ".join(str(value or "").split()).strip()
    if not source:
        return ()
    values: list[str] = []
    parenthesised = re.sub(r"\s*[（(][^()（）]{1,32}版[）)]\s*$", "", source).strip()
    if parenthesised and parenthesised != source:
        values.append(parenthesised)
    else:
        for suffix in KNOWN_VERSION_SUFFIXES:
            if source.casefold().endswith(suffix.casefold()):
                base = source[:-len(suffix)].strip(" -_/／·()（）")
                if base and base != source:
                    values.append(base)
                break
    # 抖店平台枚举会把“至尊版”规范化成“至尊”（model_key 也明确把两者视为
    # 同一写法）。当完整平台型号随后用于图库检索时，仍需保留既有的
    # “移除版本描述后再唯一匹配”回退，否则 K70（至尊版）会在这一步失配。
    if re.search(r"至尊\s*$", source):
        supreme = re.sub(r"\s*至尊\s*$", "", source).strip(" -_/／·()（）")
        if supreme and supreme != source:
            values.append(supreme)
    if parenthesised == source:
        generic = re.sub(r"\s+[^\s()（）]{1,24}版\s*$", "", source).strip()
        if generic and generic != source:
            values.append(generic)
    return tuple(dict.fromkeys(values))


class ModelCatalogue:
    def __init__(self, rows: Iterable[Mapping[str, Any]]):
        self.rows = [dict(row) for row in rows if str(row.get("model") or "").strip()]

    @classmethod
    def from_json(cls, path: str | Path) -> "ModelCatalogue":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        rows = payload.get("catalogue", payload) if isinstance(payload, Mapping) else payload
        if not isinstance(rows, list):
            raise ValueError("model catalogue must contain a list")
        return cls(rows)

    def resolve(self, brand: Any, model: Any) -> Mapping[str, Any] | None:
        expected_model = model_key(model)
        if not expected_model:
            return None
        expected_brand = canonical_brand(brand)
        candidates = self.rows
        if expected_brand:
            candidates = [row for row in candidates if canonical_brand(row.get("brand")) == expected_brand]

        exact = [row for row in candidates if model_key(row.get("model")) == expected_model]
        if exact:
            return _unique_or_none(exact)

        # Conservative fallback: titles often carry a marketing tail after the
        # real model ("Mate 60 RS 非凡大师" + " 高端商务", "Magic8 Pro Air" +
        # " 轻薄智能"), so requiring exact equality misses catalogue entries
        # that are genuinely correct. That tail is always space-separated and
        # appended, never prepended, so treat a catalogue model as a hit when
        # it is a *whole-token* prefix of the parsed text (space boundary
        # required, so "K80" does not falsely match "K80E" - a different
        # model, not "K80" + a marketing suffix). Take the longest such prefix
        # (so "Mate 60 RS 非凡大师" outranks the shorter "Mate 60") and still
        # require it to be unique, preserving the no-fuzzy-matching guarantee.
        expected_spaced = spaced_model_key(model)
        prefixed = []
        for row in candidates:
            cat_spaced = spaced_model_key(row.get("model"))
            if cat_spaced and expected_spaced.startswith(cat_spaced + " "):
                # Variant suffixes are part of the model, never a marketing tail.
                # Otherwise "GT8 Pro" incorrectly collapses to catalogue "GT8".
                tail = expected_spaced[len(cat_spaced):].strip().casefold()
                first_tail = tail.split(" ", 1)[0] if tail else ""
                if first_tail in {
                    "pro", "pro+", "plus", "max", "ultra", "mini", "air",
                    "se", "e", "s", "i", "x", "t", "turbo", "fold", "flip",
                    "至尊", "至尊版", "典藏", "典藏版", "活力版", "长续航版",
                }:
                    continue
                prefixed.append((len(cat_spaced), row))
        if not prefixed:
            return None
        max_len = max(length for length, _ in prefixed)
        longest = [row for length, row in prefixed if length == max_len]
        return _unique_or_none(longest)


def _unique_or_none(rows: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    unique = {(canonical_brand(row.get("brand")), _text(row.get("model"))): row for row in rows}
    return next(iter(unique.values())) if len(unique) == 1 else None
