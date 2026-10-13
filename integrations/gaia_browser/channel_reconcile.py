"""Read back newly created Gaia channel products before assigning price rules.

The browser query is scoped to the exact shop and model. A single ambiguous or
missing product stops the price-rule stage; it never chooses the first row.
"""

from __future__ import annotations

import re
import time
import unicodedata
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

from app.gaia_listing.application.price_uplift import PriceUpliftError
from .upload_form import GAIA_SHOP_NAMES


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s·・/（）()\-—_]+", "", value)


def _title_matches(title: str, brand: str, model: str) -> bool:
    aliases = {"apple": "苹果", "samsung": "三星", "xiaomi": "小米", "honor": "荣耀"}
    brand_key = _key(brand)
    prefixes = {"", brand_key, _key(aliases.get(brand_key, ""))}
    expected = {_key(f"{prefix}{model}二手手机") for prefix in prefixes}
    return _key(title) in expected


def _read_visible_rows(page, shop_id: str) -> list[dict]:
    rows = []
    for row in page.locator("table tbody tr").filter(visible=True).all():
        cells = [text.strip() for text in row.locator("td").all_inner_texts()]
        if len(cells) != 16:
            continue
        match = re.search(r"店铺ID\s*[：:]\s*(\d+)", cells[2])
        if not match or match.group(1) != shop_id:
            raise PriceUpliftError("盖亚渠道商品搜索返回了其他店铺，停止关联")
        if not re.fullmatch(r"[1-9]\d{9,19}", cells[0]):
            raise PriceUpliftError("盖亚渠道商品 ID 格式异常")
        rows.append({"product_id": cells[0], "shop_id": match.group(1),
                     "scene": cells[3], "title": cells[5], "strategy": cells[11],
                     "created_at": cells[12], "status": cells[13],
                     "quality_dimension": cells[14]})
    return rows


def _wait_for_fresh_page_rows(page, shop_id: str, seen_ids: set[str],
                              expected_count: int, timeout_seconds: float = 10) -> list[dict]:
    """Wait for the next page's data, not merely for its pagination click.

    Gaia changes the active page before its asynchronous table request finishes.
    Until then the old rows remain visible and must not be counted twice.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        rows = _read_visible_rows(page, shop_id)
        ids = [row["product_id"] for row in rows]
        if (len(rows) == expected_count and len(set(ids)) == expected_count
                and not seen_ids.intersection(ids)):
            return rows
        page.wait_for_timeout(100)
    raise PriceUpliftError("盖亚渠道商品翻页后新结果未完整加载，拒绝关联")


def _matching_search_response(response, shop_id: str, model: str) -> bool:
    parts = urlsplit(response.url)
    if response.request.method != "GET" or parts.path != "/api":
        return False
    params = parse_qs(parts.query)
    if params.get("apiName") != ["pop.erp.goods.product.spu.query.product.list"]:
        return False
    try:
        body = json.loads(params["body"][0])
    except (KeyError, ValueError, TypeError, IndexError):
        return False
    return (body.get("shopIds") == [str(shop_id)]
            and body.get("channelSpuName") == model
            and body.get("pageNo") == 1)


def _wait_for_search_rows(page, shop_id: str, model: str, expected_total: int,
                          timeout_seconds: float = 12) -> tuple[list[dict], int]:
    """Wait past Gaia's old table rows after an asynchronous search response."""
    deadline = time.monotonic() + timeout_seconds
    last_problem = "搜索结果仍在加载"
    while time.monotonic() < deadline:
        total_text = page.locator(".ant-pagination-total-text").filter(visible=True)
        pagination = page.locator(".ant-pagination").filter(visible=True)
        if expected_total == 0:
            if (total_text.count() == 0 and pagination.count() == 0
                    and page.get_by_text("暂无数据", exact=True).filter(visible=True).count() == 1):
                return [], 0
            last_problem = "页面尚未呈现本次查询的空结果"
            page.wait_for_timeout(100)
            continue
        total_match = (re.search(r"共\s*(\d+)\s*条", total_text.inner_text())
                       if total_text.count() == 1 else None)
        size_match = (re.search(r"(\d+)\s*条/页", pagination.inner_text())
                      if pagination.count() == 1 else None)
        if not total_match or not size_match:
            last_problem = "结果总数或每页条数尚未加载"
            page.wait_for_timeout(100)
            continue
        total, page_size = int(total_match.group(1)), int(size_match.group(1))
        if total != expected_total:
            last_problem = f"页面总数 {total} 与本次接口结果 {expected_total} 不一致"
            page.wait_for_timeout(100)
            continue
        try:
            rows = _read_visible_rows(page, shop_id)
        except PriceUpliftError as exc:
            # The old, unfiltered rows remain visible briefly after Search.
            last_problem = str(exc)
            page.wait_for_timeout(100)
            continue
        if total > 400:
            last_problem = "同店同型号结果超过 400 条，拒绝不完整关联"
        elif (len(rows) == min(total, page_size)
                and any(_key(model) in _key(row["title"]) for row in rows)):
            return rows, total
        else:
            last_problem = "搜索结果行数或型号仍与筛选条件不一致"
        page.wait_for_timeout(100)
    raise PriceUpliftError("盖亚渠道商品搜索未完成或筛选未生效：" + last_problem)


def _select_exact_shop_filter(page, shop_id: str, shop_name: str) -> None:
    """Search the channel-product filter by name, then verify its actual ID.

    Gaia renders the human-readable name in the visible virtual list, while
    the associated hidden role=option contains the numeric DouDian shop ID.
    The list is virtualized, so an unsearched dropdown is not a complete list.
    """
    search_input = page.locator("#advanced_search_shopIds")
    if search_input.count() != 1:
        raise PriceUpliftError("盖亚渠道商品店铺筛选框结构已变化")
    select = search_input.locator("xpath=ancestor::div[contains(@class,'ant-select-multiple')][1]")
    if select.count() != 1:
        raise PriceUpliftError("盖亚渠道商品店铺筛选控件结构已变化")
    selected = select.locator(".ant-select-selection-item[title]")
    if selected.count():
        if selected.count() != 1 or selected.first.get_attribute("title") != shop_name:
            raise PriceUpliftError("盖亚渠道商品已筛选其他店铺，拒绝混合查询")
        return

    select.locator(".ant-select-selector").click(timeout=8000)
    search_input.fill(shop_name)
    dropdowns = page.locator(".ant-select-dropdown").filter(visible=True)
    try:
        dropdowns.first.wait_for(state="visible", timeout=8000)
    except Exception as exc:
        raise PriceUpliftError("盖亚渠道商品店铺搜索列表未打开") from exc
    if dropdowns.count() != 1:
        raise PriceUpliftError("盖亚渠道商品店铺搜索列表不唯一")
    dropdown = dropdowns.first
    try:
        page.wait_for_function(
            """name => [...document.querySelectorAll('.ant-select-dropdown [role=option]')]
                .some(option => option.getAttribute('aria-label') === name)""",
            arg=shop_name, timeout=8000,
        )
    except Exception as exc:
        raise PriceUpliftError(
            "盖亚渠道商品店铺搜索未找到 %s（ID %s），禁止查询或提交" %
            (shop_name, shop_id)
        ) from exc
    id_options = [option for option in dropdown.locator("[role='option']").all()
                  if option.get_attribute("aria-label") == shop_name]
    if len(id_options) != 1 or id_options[0].inner_text().strip() != str(shop_id):
        raise PriceUpliftError(
            "盖亚渠道商品店铺搜索未唯一匹配 %s（ID %s），禁止查询或提交" %
            (shop_name, shop_id)
        )
    visible_options = [option for option in dropdown.locator(".ant-select-item-option[title]").filter(visible=True).all()
                       if option.get_attribute("title") == shop_name]
    if len(visible_options) != 1:
        raise PriceUpliftError("盖亚渠道商品店铺可见选项不唯一")
    visible_options[0].click(timeout=8000)
    selected = select.locator(".ant-select-selection-item[title]")
    if selected.count() != 1 or selected.first.get_attribute("title") != shop_name:
        raise PriceUpliftError("盖亚渠道商品店铺筛选回读不一致")


def snapshot_channel_products(page, shop_id: str, expected_products: list[dict]) -> dict:
    """Capture complete exact-shop/model searches; fail if paging is incomplete."""
    if not expected_products or not re.fullmatch(r"\d+", str(shop_id)):
        raise PriceUpliftError("缺少本批商品型号或目标店铺")
    name_input = page.locator("#advanced_search_channelSpuName")
    shop_name = GAIA_SHOP_NAMES.get(str(shop_id))
    if not shop_name or name_input.count() != 1:
        raise PriceUpliftError("盖亚渠道商品筛选框结构已变化")
    search = page.get_by_role("button", name=re.compile(r"搜\s*索")).filter(visible=True)
    if search.count() != 1:
        raise PriceUpliftError("盖亚渠道商品搜索按钮不唯一")
    _select_exact_shop_filter(page, str(shop_id), shop_name)
    result = {}
    for product in expected_products:
        brand, model = str(product.get("brand") or ""), str(product.get("model") or "")
        if not brand or not model:
            raise PriceUpliftError("本批商品缺少品牌或型号")
        name_input.fill(model)
        try:
            with page.expect_response(
                lambda response: _matching_search_response(response, str(shop_id), model),
                timeout=15000,
            ) as received:
                search.click(timeout=8000)
            if received.value.status >= 400:
                raise PriceUpliftError("盖亚渠道商品查询接口返回错误")
            payload = received.value.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if (not isinstance(data, dict) or not isinstance(data.get("totalCount"), int)
                    or payload.get("code") != 200):
                raise PriceUpliftError("盖亚渠道商品查询接口结果无效")
            expected_total = data["totalCount"]
            if expected_total > 400:
                raise PriceUpliftError("同店同型号结果超过 400 条，拒绝不完整关联")
        except PriceUpliftError:
            raise
        except Exception as exc:
            raise PriceUpliftError("盖亚渠道商品查询未取得接口响应") from exc
        items, total = _wait_for_search_rows(page, str(shop_id), model, expected_total)
        if not total:
            result[_key(brand + model)] = {"brand": brand, "model": model, "items": []}
            continue
        if total and not items:
            raise PriceUpliftError("盖亚渠道商品第一页为空，拒绝关联")
        page_size = len(items)
        while len(items) < total:
            next_button = page.locator(".ant-pagination-next button").filter(visible=True)
            if next_button.count() != 1 or next_button.is_disabled():
                raise PriceUpliftError("盖亚渠道商品分页未完整读取")
            next_button.click(timeout=8000)
            items.extend(_wait_for_fresh_page_rows(
                page, str(shop_id), {item["product_id"] for item in items},
                min(page_size, total - len(items)),
            ))
        if len(items) != total or len({item["product_id"] for item in items}) != total:
            raise PriceUpliftError("盖亚渠道商品结果数或商品 ID 重复")
        result[_key(brand + model)] = {"brand": brand, "model": model, "items": items}
    return result


def reconcile_channel_products(before: dict, after: dict, *, shop_id: str,
                               expected_products: list[dict], submitted_at: datetime,
                               observed_at: datetime) -> list[str]:
    """Accept only one newly appeared, exact-title, same-shop product/model."""
    ids = []
    if not before or not after or observed_at < submitted_at:
        raise PriceUpliftError("商品创建前后快照不完整或时间顺序错误")
    for expected in expected_products:
        brand, model = expected["brand"], expected["model"]
        key = _key(brand + model)
        old = before.get(key, {}).get("items")
        fresh = after.get(key, {}).get("items")
        if not isinstance(old, list) or not isinstance(fresh, list):
            raise PriceUpliftError(f"{brand} {model} 缺少完整的前后商品快照")
        old_ids = {item["product_id"] for item in old}
        candidates = []
        for item in fresh:
            if item["product_id"] in old_ids:
                continue
            try:
                created = datetime.strptime(item["created_at"], "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise PriceUpliftError("新商品创建时间无法回读") from exc
            if not (submitted_at - timedelta(seconds=30) <= created <= observed_at + timedelta(seconds=30)):
                continue
            if (item["shop_id"] == str(shop_id) and item["scene"] == "直播货架"
                    and item["status"] == "待上架" and item["strategy"] == "D且E且G"
                    and item["quality_dimension"] == "二维成色"
                    and _title_matches(item["title"], brand, model)):
                candidates.append(item["product_id"])
        if len(candidates) != 1:
            raise PriceUpliftError(f"{brand} {model} 本次新增商品不能唯一确认（{len(candidates)} 个）")
        ids.append(candidates[0])
    if len(ids) != len(set(ids)):
        raise PriceUpliftError("多个型号关联到同一抖音商品 ID")
    return ids
