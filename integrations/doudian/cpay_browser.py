"""Strict browser enrollment of one Gaia batch in a DouDian installment activity.

Only the exact activity card, one installment term and verified batch product IDs
may be used. A lost submission response is intentionally not retried.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from app.gaia_listing.application.price_uplift import PRICE_RULE_NAMES
from .browser_profiles import has_browser_login
from .cpay_terms import required_cpay_terms


CPAY_HOME = "https://fxg.jinritemai.com/ffa/cpay/home"


class CpayEnrollmentError(RuntimeError):
    pass


def validate_request(shop_id: str, installments, activity_name: str, product_ids: list[str]):
    try:
        required_cpay_terms(installments)
    except (ValueError, TypeError) as exc:
        raise CpayEnrollmentError("分期免息只能选择 6、12、24 期") from exc
    if str(shop_id) not in PRICE_RULE_NAMES:
        raise CpayEnrollmentError("分期免息店铺 ID 不在已核实规则中")
    if not isinstance(activity_name, str) or not activity_name.strip() or "全店报名" in activity_name:
        raise CpayEnrollmentError("活动名称为空或指向全店报名")
    if not product_ids or len(product_ids) != len(set(product_ids)) or any(
        not re.fullmatch(r"\d{10,20}", str(value)) for value in product_ids
    ):
        raise CpayEnrollmentError("商品 ID 缺失、重复或格式不正确")
    return activity_name.strip(), [str(value) for value in product_ids]


def _only(locator, label: str):
    count = locator.count()
    if count != 1:
        raise CpayEnrollmentError(f"{label}命中 {count} 处，拒绝猜测")
    return locator.first


def _shop_name(shop_id: str) -> str:
    name = PRICE_RULE_NAMES[str(shop_id)][12].split("【", 1)[0]
    if not name:
        raise CpayEnrollmentError("无法确认目标店铺名称")
    return name


def _check_shop(page, shop_id: str):
    # Tea's current-account token identifies the active shop, unlike the
    # switch-store list in localStorage (which includes inactive shops).
    current_ids = page.evaluate("""() => {
      const ids = new Set();
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i) || '';
        if (!key.startsWith('__tea_cache_tokens_')) continue;
        try {
          const raw = String(JSON.parse(localStorage.getItem(key) || '{}').user_unique_id || '');
          const match = raw.match(/(?:^|_)(\\d{5,12})$/);
          if (match) ids.add(match[1]);
        } catch (_) {}
      }
      return [...ids];
    }""")
    known = set(PRICE_RULE_NAMES)
    matched = set(current_ids or []) & known
    if matched:
        if matched != {str(shop_id)}:
            raise CpayEnrollmentError(f"抖店当前账号短 ID 为 {sorted(matched)}，与目标 {shop_id} 不一致")
        return
    # Fallback only to the visible current-shop header, never product rows.
    name = _shop_name(shop_id)
    header = page.locator("header, [class*='Header'], [class*='header']").filter(visible=True)
    matches = [item for item in header.all() if name in item.inner_text()]
    if not matches:
        raise CpayEnrollmentError(f"抖店当前登录店铺不是 {name}【{shop_id}】，请先在专用浏览器切店")


def _activity_card(page, activity_name: str):
    titles = page.get_by_text(activity_name, exact=True).filter(visible=True)
    try:
        titles.first.wait_for(state="visible", timeout=20000)
    except Exception as exc:
        raise CpayEnrollmentError(f"未找到活动卡片：{activity_name}") from exc
    title = _only(titles, "活动卡片名称")
    card = title.locator("xpath=ancestor::*[.//button[normalize-space()='新建活动']][1]")
    card = _only(card, "活动卡片")
    if "全店报名" in card.inner_text():
        raise CpayEnrollmentError("活动卡片为全店报名，拒绝操作")
    return card


def _product_dialog(page):
    confirm = _only(page.get_by_role("button", name="选择", exact=True).filter(visible=True),
                    "添加商品确认按钮")
    panel = confirm.locator(
        "xpath=ancestor::*[.//input[@placeholder='请输入商品名称/ID']][1]")
    return _only(panel, "添加商品面板")


def _wait_create_page(context, page):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        create = next((item for item in context.pages
                       if "/cpay/stages-interest/create" in item.url), None)
        if create is not None:
            create.get_by_text("创建活动", exact=True).first.wait_for(state="visible", timeout=10000)
            return create
        page.wait_for_timeout(250)
    raise CpayEnrollmentError("未进入分期免息创建页")


def probe_cpay_session(profile_dir: str, shop_id: str, activity_name: str) -> dict:
    """Read-only preflight before Gaia creates products or imports a price rule."""
    profile = Path(profile_dir).resolve()
    if not has_browser_login(profile):
        raise CpayEnrollmentError(
            f"店铺 {shop_id} 的抖店专用浏览器尚未登录；请在执行电脑运行 "
            f"python3 -m scripts.doudian_browser_login_window --shop-id {shop_id} --purpose listing"
        )
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(CPAY_HOME, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("分期免息", exact=True).first.wait_for(state="visible", timeout=20000)
            _check_shop(page, shop_id)
            _activity_card(page, activity_name)
            return {"shop_id": str(shop_id), "activity_name": activity_name, "ready": True}
        finally:
            context.close()


def _select_product(page, product_id: str):
    dialog = _product_dialog(page)
    filter_group = _only(dialog.get_by_text("请选择筛选项", exact=True).filter(visible=True),
                         "商品筛选提示").locator("xpath=..")
    combo = _only(filter_group.get_by_role("combobox").filter(visible=True), "商品筛选项")
    combo.click()
    # The selector must explicitly offer ID search. Name search can hit other goods.
    id_option = page.get_by_text("按商品ID搜索", exact=True).filter(visible=True)
    id_option.first.wait_for(state="visible", timeout=10000)
    _only(id_option, "商品 ID 筛选项").click()
    search = _only(dialog.get_by_placeholder("请输入商品名称/ID"), "商品 ID 输入框")
    search.fill(product_id)
    search.press("Enter")
    row = _only(dialog.get_by_role("row").filter(has_text=re.compile(
        rf"(?<!\d){re.escape(product_id)}(?!\d)")), f"商品 {product_id} 搜索结果")
    row_text = row.inner_text()
    if len(re.findall(rf"(?<!\d){re.escape(product_id)}(?!\d)", row_text)) != 1:
        raise CpayEnrollmentError(f"商品 {product_id} 搜索结果不唯一")
    _only(row.get_by_role("checkbox"), f"商品 {product_id} 勾选框").check()


def enroll_cpay_batch(*, profile_dir: str, batch_id: str, shop_id: str, installments,
                      activity_name: str, product_ids: list[str], before_submit=None) -> dict:
    """Attempt once. The caller persists intent before the irreversible submit."""
    activity_name, product_ids = validate_request(shop_id, installments, activity_name, product_ids)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(Path(profile_dir).resolve()), headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(CPAY_HOME, wait_until="domcontentloaded", timeout=30000)
            page.get_by_text("分期免息", exact=True).first.wait_for(state="visible", timeout=20000)
            _check_shop(page, shop_id)
            card = _activity_card(page, activity_name)
            _only(card.get_by_role("button", name="新建活动", exact=True), "新建活动按钮").click()
            create = _wait_create_page(context, page)
            _check_shop(create, shop_id)
            required = required_cpay_terms(installments)
            merchant_activity_name = f"盖亚-{shop_id}-{max(required)}期-{batch_id[:8]}"
            _only(create.get_by_placeholder("请输入活动名称，不超过25个字"),
                  "商家活动名称").fill(merchant_activity_name)
            _only(create.get_by_text("部分商品参与", exact=True).filter(visible=True), "部分商品参与").click()
            if create.get_by_role("button", name="添加商品", exact=True).count() != 1:
                raise CpayEnrollmentError("未切换到部分商品参与；禁止创建全店活动")
            for term in (3, 6, 12, 24):
                checkbox = _only(create.get_by_role("checkbox", name=re.compile(rf"^{term}期免息")), f"{term}期免息")
                if term in required:
                    checkbox.check()
                else:
                    checkbox.uncheck()
            checked = [term for term in (3, 6, 12, 24) if
                       create.get_by_role("checkbox", name=re.compile(rf"^{term}期免息")).is_checked()]
            if checked != list(required):
                raise CpayEnrollmentError("免息期数回读不等于运营选择的期数")
            _only(create.get_by_role("button", name="添加商品", exact=True), "添加商品按钮").click()
            for product_id in product_ids:
                _select_product(create, product_id)
            dialog = _product_dialog(create)
            _only(dialog.get_by_role("button", name="选择", exact=True), "商品选择确认按钮").click()
            dialog.wait_for(state="hidden", timeout=10000)
            selected = _only(create.get_by_text(re.compile(r"已选数量\d+个")).filter(visible=True), "已选数量")
            count_match = re.search(r"已选数量\s*(\d+)\s*个", selected.inner_text())
            if not count_match or int(count_match.group(1)) != len(product_ids):
                raise CpayEnrollmentError("活动已选商品数量与本批 ID 数量不一致")
            for product_id in product_ids:
                _only(create.get_by_text(re.compile(rf"(?<!\d){re.escape(product_id)}(?!\d)")).filter(visible=True),
                      f"已选商品 {product_id}")
            if before_submit:
                before_submit()
            _only(create.get_by_role("button", name="提交", exact=True), "活动提交按钮").click()
            create.wait_for_timeout(1200)
            # Do not call a click a success. A changed URL or toast is only an
            # acknowledgement; the caller must treat the membership as unverified.
            feedback = create.locator("[role='alert'], .ant-message, .semi-toast").filter(visible=True).all_inner_texts()
            return {"outcome": "submitted_unverified", "feedback": "；".join(feedback)[:300],
                    "shop_id": str(shop_id), "installment_terms": list(required),
                    "activity_name": activity_name, "merchant_activity_name": merchant_activity_name,
                    "product_ids": product_ids}
        finally:
            context.close()
