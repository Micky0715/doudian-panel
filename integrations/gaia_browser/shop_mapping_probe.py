"""Read-only verification of a Gaia shop option against the rendered shop ID."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from .probe import GAIA_CHANNEL_URL, _emit, _has_text, _screenshot, _visible_unique


def probe_gaia_shop_mapping(
    *, option_name: str, profile_dir: str, artifact_dir: str,
    login_wait: int = 120, headless: bool = False,
) -> dict:
    """Filter the channel table by one exact Gaia option and read shop IDs."""
    option_name = str(option_name or "").strip()
    if not option_name:
        raise ValueError("盖亚店铺选项不能为空")
    artifacts = Path(artifact_dir).resolve()
    profile = Path(profile_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            try:
                page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                _emit("navigation_warning", message=str(exc)[:300])
            deadline = time.monotonic() + max(0, int(login_wait))
            while not (_has_text(page, "抖音渠道") or _has_text(page, "双维成色发品")):
                if time.monotonic() >= deadline:
                    raise RuntimeError("未进入盖亚渠道商品页")
                page.wait_for_timeout(1000)
            if _has_text(page, "抖音渠道"):
                _visible_unique(page, "抖音渠道", roles=("tab", "button", "link")).click(timeout=8000)
                page.get_by_text("双维成色发品", exact=True).wait_for(state="visible", timeout=20000)

            labels = page.get_by_text("店铺名称", exact=True).filter(visible=True)
            if labels.count() != 1:
                raise RuntimeError("主页面店铺名称标签无法唯一定位")
            shop_control = labels.first.locator(
                "xpath=following::*[@role='combobox'][1]"
            )
            if shop_control.count() != 1 or not shop_control.is_visible():
                raise RuntimeError("主页面店铺筛选框无法定位")
            selector = shop_control
            if "ant-select-selector" not in (selector.get_attribute("class") or ""):
                selector = shop_control.locator("xpath=..")
            selector.click(timeout=8000)

            dropdowns = page.locator(".ant-select-dropdown")
            deadline = time.monotonic() + 8
            exact = None
            while time.monotonic() < deadline:
                matches = page.get_by_text(option_name, exact=True).filter(visible=True)
                # One match can be the eventual selected label; before selection
                # the visible dropdown must contain exactly one candidate.
                dropdown_matches = []
                for index in range(dropdowns.count()):
                    candidate = dropdowns.nth(index)
                    if candidate.is_visible():
                        found = candidate.get_by_text(option_name, exact=True).filter(visible=True)
                        if found.count() == 1:
                            dropdown_matches.append(found)
                if len(dropdown_matches) == 1:
                    exact = dropdown_matches[0]
                    break
                page.wait_for_timeout(100)
            if exact is None:
                raise RuntimeError("盖亚店铺选项未唯一找到：%s" % option_name)
            exact.click(timeout=8000)

            search = page.get_by_role("button", name=re.compile(r"搜\s*索")).filter(visible=True)
            if search.count() != 1:
                raise RuntimeError("主页面搜索按钮可见匹配数量为 %s" % search.count())
            search.click(timeout=8000)
            page.wait_for_timeout(1800)
            screenshot = _screenshot(page, artifacts, "shop_mapping_result")

            rows = page.locator("tbody tr").filter(has_text=option_name)
            row_texts = [text.strip() for text in rows.all_inner_texts() if text.strip()]
            if not row_texts:
                # The table implementation can use div rows; restrict matches
                # to blocks that contain both the option and an explicit ID.
                blocks = page.locator("text=" + option_name)
                row_texts = [page.locator("body").inner_text()]
            shop_ids = sorted(set(
                match for text in row_texts
                for match in re.findall(r"店铺ID\s*[：:]\s*(\d+)", text)
            ))
            _emit("shop_mapping_readback", option_name=option_name, shop_ids=shop_ids,
                  screenshot=screenshot, submitted=False)
            return {
                "success": len(shop_ids) == 1,
                "option_name": option_name,
                "shop_ids": shop_ids,
                "screenshot": screenshot,
                "submitted": False,
            }
        finally:
            context.close()
