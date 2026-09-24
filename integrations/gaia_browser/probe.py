"""Safe, non-submitting Playwright probe for Gaia channel-list controls."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


GAIA_CHANNEL_URL = "https://gaiya.jd.com/product/channelList"


def _emit(stage: str, **details) -> None:
    print(json.dumps({"stage": stage, **details}, ensure_ascii=False), flush=True)


def _visible_unique(page: Page, label: str, *, roles: tuple[str, ...] = ()):
    """Prefer semantic locators and refuse ambiguous matches."""
    for role in roles:
        locator = page.get_by_role(role, name=label, exact=True)
        visible = locator.filter(visible=True)
        count = visible.count()
        if count == 1:
            return visible
        if count > 1:
            raise RuntimeError(f"{label} 出现 {count} 个可见{role}控件，拒绝猜测点击")
    locator = page.get_by_text(label, exact=True).filter(visible=True)
    count = locator.count()
    if count != 1:
        raise RuntimeError(f"{label} 可见匹配数量为 {count}，需要人工核对页面结构")
    return locator


def _screenshot(page: Page, directory: Path, stage: str) -> str:
    path = directory / f"{stage}.png"
    page.screenshot(path=str(path), full_page=False)
    path.chmod(0o600)
    return str(path)


def _has_text(page: Page, label: str) -> bool:
    return page.get_by_text(label, exact=False).filter(visible=True).count() > 0


def probe_gaia(*, profile_dir: str, artifact_dir: str, login_wait: int = 120,
               headless: bool = False) -> dict:
    """Navigate and open the batch dialog; never choose files or confirm.

    Requires an operator-controlled, separate Gaia browser profile. This
    function intentionally contains no upload, form submission, or product
    creation action. A successful result only proves UI navigation.
    """
    artifacts = Path(artifact_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if artifacts == profile or artifacts in profile.parents or profile in artifacts.parents:
        raise ValueError("截图目录与浏览器登录目录必须分开")
    os.chmod(artifacts, 0o700)
    os.chmod(profile, 0o700)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            _emit("browser_opened", profile=str(profile))
            try:
                page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                _emit("navigation_warning", message=str(exc)[:300])
            deadline = time.monotonic() + max(0, int(login_wait))
            waiting_logged = False
            while not (_has_text(page, "抖音渠道") or _has_text(page, "双维成色发品")):
                if time.monotonic() >= deadline:
                    raise RuntimeError("未进入盖亚渠道商品页；请在专用浏览器窗口登录并确认页面权限")
                if not waiting_logged:
                    _emit("waiting_channel_page_or_login", url=page.url, seconds=int(login_wait))
                    waiting_logged = True
                page.wait_for_timeout(1000)
            _emit("channel_page_visible", url=page.url,
                  screenshot=_screenshot(page, artifacts, "01_channel_page"))

            # The channel page can already show the Douyin tab. Clicking it is
            # navigation only; ambiguity stops the probe without side effects.
            if _has_text(page, "抖音渠道"):
                tab = _visible_unique(page, "抖音渠道", roles=("tab", "button", "link"))
                tab.click(timeout=8000)
                page.get_by_text("双维成色发品", exact=True).wait_for(state="visible", timeout=20000)
                _emit("douyin_tab_clicked", url=page.url,
                      screenshot=_screenshot(page, artifacts, "02_douyin_tab"))

            create_button = _visible_unique(page, "双维成色发品", roles=("button",))
            if not create_button.is_enabled():
                raise RuntimeError("双维成色发品按钮可见但不可点击")
            create_button.click(timeout=8000)
            page.wait_for_timeout(500)
            fields = {
                "batch_dialog": _has_text(page, "批量操作"),
                "shop_label": _has_text(page, "店铺名称"),
                "quantity_label": _has_text(page, "创建数量"),
                "file_label": _has_text(page, "上传文件"),
                "choose_file_button": _has_text(page, "选择文件"),
            }
            _emit("batch_dialog_opened", fields=fields,
                  screenshot=_screenshot(page, artifacts, "03_batch_dialog"))
            if not all(fields.values()):
                raise RuntimeError("批量操作弹窗缺少预期字段；未触碰上传或确定按钮")

            dialogs = page.get_by_role("dialog").filter(visible=True)
            if dialogs.count() != 1:
                dialogs = page.locator(".el-dialog, .ant-modal").filter(visible=True)
            if dialogs.count() != 1:
                raise RuntimeError("批量操作弹窗无法唯一定位，拒绝继续测试表单按钮")
            dialog = dialogs.first
            shop_control = _visible_unique(dialog, "请选择", roles=("combobox",))
            shop_selector = shop_control.locator("xpath=..")
            if "ant-select-selector" not in (shop_selector.get_attribute("class") or ""):
                raise RuntimeError("店铺下拉结构与预期不一致")
            shop_selector.click(timeout=8000)
            dropdown = page.locator(".ant-select-dropdown").filter(visible=True)
            dropdown.first.wait_for(state="visible", timeout=8000)
            option_count = dropdown.first.get_by_role("option").count()
            if option_count < 1:
                raise RuntimeError("店铺下拉已打开但没有可选择选项")
            option_details = dropdown.first.locator(
                '[role="option"], .ant-select-dropdown-menu-item'
            ).evaluate_all("""elements => elements.map((element) => ({
                text: (element.innerText || element.textContent || '').trim(),
                title: element.getAttribute('title') || '',
                value: element.getAttribute('data-value') || element.getAttribute('value') || '',
                key: element.getAttribute('data-key') || ''
            })).filter((item, index, all) => item.text &&
                all.findIndex((candidate) => candidate.text === item.text) === index)""")
            _emit("shop_dropdown_opened", option_count=option_count,
                  option_details=option_details,
                  screenshot=_screenshot(page, artifacts, "04_shop_dropdown"))

            quantity_input = dialog.get_by_placeholder("请输入1-10的整数", exact=True)
            if quantity_input.count() != 1:
                raise RuntimeError("创建数量输入框无法唯一定位")
            quantity_input.fill("1", timeout=8000)
            if quantity_input.input_value() != "1":
                raise RuntimeError("创建数量回读不是 1")
            _emit("quantity_set_to_one", value="1")

            # Intercept the native file chooser but deliberately do not select
            # a file. This verifies the button is wired without uploading.
            choose_file = _visible_unique(dialog, "选择文件", roles=("button",))
            with page.expect_file_chooser(timeout=8000):
                choose_file.click(timeout=8000)
            _emit("file_chooser_opened", buttons=dialog.get_by_role("button").all_text_contents(),
                  screenshot=_screenshot(page, artifacts, "05_no_file_selected"))
            confirm = dialog.get_by_role("button", name=re.compile(r"确\s*定")).filter(visible=True)
            if confirm.count() != 1:
                raise RuntimeError("确定按钮无法唯一定位；未触发提交")
            if confirm.is_enabled():
                raise RuntimeError("未选店铺及文件时确定按钮可点击，需人工检查页面防护")
            _emit("file_chooser_opened_without_file", confirm_enabled=False)

            cancel = dialog.get_by_role("button", name=re.compile(r"取\s*消")).filter(visible=True)
            if cancel.count() != 1:
                raise RuntimeError("取消按钮无法唯一定位；弹窗保持原状")
            cancel.click(timeout=8000)
            dialog.wait_for(state="hidden", timeout=8000)
            _emit("dialog_cancelled", screenshot=_screenshot(page, artifacts, "06_dialog_cancelled"))
            return {"success": True, "fields": fields, "url": page.url,
                    "artifact_dir": str(artifacts), "shop_dropdown_clicked": True,
                    "quantity_set_to_one": True, "file_chooser_clicked": True,
                    "submitted": False}
        finally:
            context.close()
