"""Read-only Gaia goods-count probe used before workbook generation.

The Gaia upload limit is applied after template rows expand to current goods.
This module queries the same paged goods list used by the Gaia UI.  It never
opens the upload dialog and contains no mutation request.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


GAIA_GOODS_URL = "https://gaiya.jd.com/goods/list"
GAIA_GOODS_API = "pop.erp.goods.info.page"


def _normalized(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def _request_body(response) -> dict:
    try:
        body = response.request.post_data_json
    except Exception:
        body = None
    if isinstance(body, dict):
        return body
    try:
        encoded = parse_qs(urlparse(response.url).query).get("body", [""])[0]
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError):
        decoded = None
    return decoded if isinstance(decoded, dict) else {}


def _matching_response(response, model: str) -> bool:
    if GAIA_GOODS_API not in response.url:
        return False
    body = _request_body(response)
    return (body.get("pageNo") == 1
            and _normalized(body.get("goodsSpec")) == _normalized(model))


def _read_total(response, model: str) -> int:
    if response.status >= 400:
        raise RuntimeError("盖亚货品查询 HTTP %s" % response.status)
    try:
        payload = response.json()
    except Exception as exc:
        raise RuntimeError("盖亚货品查询没有返回有效 JSON") from exc
    if str(payload.get("code")) != "200":
        message = str(payload.get("message") or payload.get("msg") or "未知错误")
        raise RuntimeError("盖亚货品查询失败：%s" % message[:300])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("盖亚货品查询缺少 data")
    total = data.get("totalCount")
    rows = data.get("pageList")
    if (not isinstance(total, int) or isinstance(total, bool) or total < 0
            or not isinstance(rows, list)):
        raise RuntimeError("盖亚货品查询数量或列表格式已变化")
    unexpected = []
    for item in rows:
        if not isinstance(item, dict):
            raise RuntimeError("盖亚货品查询第一页包含无效记录")
        if _normalized(item.get("goodsSpec")) != _normalized(model):
            unexpected.append(str(item.get("goodsSpec") or "").strip())
    unexpected = sorted(set(unexpected))
    if unexpected:
        raise RuntimeError("盖亚货品查询混入其他型号：%s" % "、".join(unexpected[:5]))
    if total > 0 and not rows:
        raise RuntimeError("盖亚货品查询有总数但第一页为空")
    return total


def probe_model_goods_counts(*, profile_dir: str, models: list[dict], lock_path: str,
                             headless: bool = True) -> dict:
    """Return exact current Gaia totals for explicitly supplied full models."""
    if not isinstance(models, list) or not models:
        raise ValueError("缺少本次需要查询的完整型号")
    requested = []
    seen = set()
    for item in models:
        brand = str(item.get("brand") or "").strip() if isinstance(item, dict) else ""
        model = str(item.get("model") or "").strip() if isinstance(item, dict) else ""
        key = (_normalized(brand), _normalized(model))
        if not brand or not model or key in seen:
            raise ValueError("本次盖亚实时货品查询包含空型号或重复型号")
        seen.add(key)
        requested.append({"brand": brand, "model": model})

    lock = Path(lock_path).resolve()
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("盖亚浏览器正在处理另一批，请等待其结束后重新预览") from exc

        profile = Path(profile_dir).resolve()
        profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(profile, 0o700)
        results = []
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile), headless=headless,
                viewport={"width": 1440, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(GAIA_GOODS_URL, wait_until="domcontentloaded", timeout=30000)
                field = page.locator("#advanced_search_goodsSpec")
                try:
                    field.wait_for(state="visible", timeout=15000)
                except Exception as exc:
                    raise RuntimeError("未进入盖亚货品列表；请重新登录盖亚后再试") from exc
                searches = page.get_by_role("button", name=re.compile(r"搜\s*索")).filter(visible=True)
                if searches.count() != 1:
                    raise RuntimeError("盖亚货品列表搜索按钮不唯一，页面结构可能已变化")
                search = searches.first
                for item in requested:
                    model = item["model"]
                    field.fill(model)
                    if field.input_value().strip() != model:
                        raise RuntimeError("盖亚货品型号输入回读不一致")
                    with page.expect_response(
                            lambda response, expected=model: _matching_response(response, expected),
                            timeout=25000) as captured:
                        search.click(timeout=8000)
                    total = _read_total(captured.value, model)
                    results.append({**item, "total_count": total})
            finally:
                context.close()
        return {
            "source": "gaia_goods_list_api",
            "api_name": GAIA_GOODS_API,
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "models": results,
            "read_only": True,
        }
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


if __name__ == "__main__":
    import sys
    response = probe_model_goods_counts(
        profile_dir=sys.argv[1], lock_path=sys.argv[2],
        models=[{"brand": "unknown", "model": value} for value in sys.argv[3:]],
        headless=False,
    )
    print(json.dumps(response, ensure_ascii=False, indent=2))
