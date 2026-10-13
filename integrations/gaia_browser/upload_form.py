"""Shared verified Gaia form preparation. The caller owns the final action."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from app.gaia_listing.application.handoff_preflight import inspect_handoff
from app.gaia_listing.infrastructure.batch_repository import GaiaBatchRepository
from app.gaia_listing.infrastructure.model_mapping_repository import ModelMappingRepository
from app.gaia_listing.infrastructure.quality_mapping_repository import QualityMappingRepository
from .probe import GAIA_CHANNEL_URL, _emit, _has_text, _screenshot, _visible_unique
from .file_attachment import FileAttachmentError, wait_for_attachment


# The browser dropdown contains shop names, not the short IDs stored in batches.
# Keep this explicit: an unknown shop must stop instead of selecting a similar name.
GAIA_SHOP_NAMES = {
    "129675602": "爱回收官方旗舰店",
    "636781": "爱回收二手手机旗舰店",
    "7970406": "爱回收严选二手数码",
    "51165770": "爱回收严选二手优品",
    # Gaia uses a shorter channel-shop name than the panel/DouDian display name.
    # The live channel filter readback confirms this option has shop ID 25595926.
    "25595926": "拍拍二手手机旗舰店",
    "168976032": "拍拍严选手机优品",
    "5576543": "拍拍严选二手手机",
    "156450040": "拍拍二手数码",
    "208501840": "返航新生二手手机旗舰店",
    "223523435": "悦玥潮品生活坊",
    # Channel names differ from panel names; the search options identify these IDs.
    "221724193": "芯动严选二手手机",
    "221729127": "拍机堂Mall线上店",
    "293816952": "新生严选二手手手",
}


def _load_verified_handoff(work_dir: Path, batch_id: str, shop_id: str) -> tuple[dict, dict]:
    data_root = work_dir / "panel_data" / "gaia_listing"
    repository = GaiaBatchRepository(str(data_root / "batches"))
    batch = repository.get(batch_id)
    if batch is None:
        raise RuntimeError("盖亚批次不存在")
    result = inspect_handoff(
        batch,
        requested_shop_id=shop_id,
        outputs_dir=str(data_root / "outputs"),
        allowed_source_roots=(
            os.path.expanduser(os.environ.get("GAIA_DOWNLOADS_DIR", "~/Downloads")),
            str(data_root / "imports"),
        ),
        mapping_repository=ModelMappingRepository(str(data_root / "imports" / "model_mappings")),
        quality_repository=QualityMappingRepository(str(work_dir / "config" / "gaia_quality_mapping.json")),
    )
    if not result.get("ready"):
        raise RuntimeError("上传前完整性校验失败：" + "；".join(result.get("errors") or []))
    return batch, result


def _one_visible_dialog(page: Page):
    dialogs = page.get_by_role("dialog").filter(visible=True)
    if dialogs.count() != 1:
        dialogs = page.locator(".el-dialog, .ant-modal").filter(visible=True)
    if dialogs.count() != 1:
        raise RuntimeError("批量操作弹窗无法唯一定位")
    return dialogs.first


def _wait_for_channel_page(page: Page, login_wait: int, check_cancelled=None) -> None:
    deadline = time.monotonic() + max(0, int(login_wait))
    waiting_logged = False
    while not (_has_text(page, "抖音渠道") or _has_text(page, "双维成色发品")):
        if check_cancelled:
            check_cancelled()
        if time.monotonic() >= deadline:
            raise RuntimeError("未进入盖亚渠道商品页；请在专用浏览器窗口登录并确认页面权限")
        if not waiting_logged:
            _emit("waiting_channel_page_or_login", url=page.url, seconds=int(login_wait))
            waiting_logged = True
        page.wait_for_timeout(1000)


def _scroll_shop_dropdown(dropdown) -> dict:
    """Advance Gaia's virtual list, whose holder uses overflow: hidden."""
    return dropdown.evaluate("""root => {
        const elements = [root, ...root.querySelectorAll('*')];
        const scrollable = elements.find(element =>
            element.classList?.contains('rc-virtual-list-holder') &&
            element.scrollHeight > element.clientHeight + 2) ||
            elements.find(element =>
                element.scrollHeight > element.clientHeight + 2 &&
                ['auto', 'scroll'].includes(getComputedStyle(element).overflowY));
        if (!scrollable) return {before: 0, after: 0, max: 0};
        const before = scrollable.scrollTop;
        const max = Math.max(0, scrollable.scrollHeight - scrollable.clientHeight);
        scrollable.scrollTop = Math.min(max, before + Math.max(1, scrollable.clientHeight * 0.8));
        scrollable.dispatchEvent(new Event('scroll', {bubbles: true}));
        return {before, after: scrollable.scrollTop, max};
    }""")


def with_gaia_upload_form(
    *, work_dir: str, batch_id: str, shop_id: str, profile_dir: str,
    artifact_dir: str, login_wait: int = 120, headless: bool = False,
    on_ready=None, verify_handoff=None, check_cancelled=None, before_create=None,
) -> dict:
    """Attach and read back a verified workbook, then delegate the final action.

    With no callback, cancel. Final submission exists only in submit.py.
    """
    root = Path(work_dir).resolve()
    checkpoint = check_cancelled or (lambda: None)
    checkpoint()
    verify = verify_handoff or (lambda: _load_verified_handoff(root, batch_id, shop_id))
    batch, handoff = verify()
    shop_name = GAIA_SHOP_NAMES.get(str(shop_id))
    if not shop_name:
        raise RuntimeError("店铺 ID 没有经过审核的盖亚店铺名称映射")
    output_path = Path(str(batch.get("output_path") or "")).resolve()
    if output_path.suffix.lower() != ".xlsx" or not output_path.is_file():
        raise RuntimeError("已校验的盖亚 Excel 输出文件不存在")

    artifacts = Path(artifact_dir).resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = Path(profile_dir).resolve()
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if artifacts == profile or artifacts in profile.parents or profile in artifacts.parents:
        raise ValueError("截图目录与浏览器登录目录必须分开")
    os.chmod(artifacts, 0o700)
    os.chmod(profile, 0o700)

    result = {
        "success": False,
        "batch_id": batch_id,
        "shop_id": str(shop_id),
        "shop_name": shop_name,
        "create_count": 1,
        "output_name": output_path.name,
        "output_size": output_path.stat().st_size,
        "output_sha256": handoff["output_sha256"],
        "file_attached": False,
        "confirm_enabled": False,
        "dialog_cancelled": False,
        "submitted": False,
        "artifact_dir": str(artifacts),
    }

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=headless,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        dialog = None
        try:
            checkpoint()
            _emit("upload_preflight_browser_opened", batch_id=batch_id, shop_id=str(shop_id))
            try:
                page.goto(GAIA_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                _emit("navigation_warning", message=str(exc)[:300])
            checkpoint()
            _wait_for_channel_page(page, login_wait, check_cancelled=checkpoint)
            checkpoint()

            if _has_text(page, "抖音渠道"):
                _visible_unique(page, "抖音渠道", roles=("tab", "button", "link")).click(timeout=8000)
                page.get_by_text("双维成色发品", exact=True).wait_for(state="visible", timeout=20000)

            if before_create is not None:
                checkpoint()
                result["before_channel_snapshot"] = before_create(page, batch)

            create_button = _visible_unique(page, "双维成色发品", roles=("button",))
            checkpoint()
            if not create_button.is_enabled():
                raise RuntimeError("双维成色发品按钮不可点击")
            create_button.click(timeout=8000)
            page.wait_for_timeout(400)
            dialog = _one_visible_dialog(page)
            _emit("upload_preflight_dialog_opened",
                  screenshot=_screenshot(page, artifacts, "01_dialog_opened"))

            shop_control = _visible_unique(dialog, "请选择", roles=("combobox",))
            checkpoint()
            shop_selector = shop_control.locator("xpath=..")
            if "ant-select-selector" not in (shop_selector.get_attribute("class") or ""):
                raise RuntimeError("店铺下拉结构与预期不一致")
            shop_selector.click(timeout=8000)
            all_dropdowns = page.locator(".ant-select-dropdown")
            deadline = time.monotonic() + 8
            visible_dropdowns = []
            while time.monotonic() < deadline:
                checkpoint()
                visible_dropdowns = [
                    all_dropdowns.nth(index) for index in range(all_dropdowns.count())
                    if all_dropdowns.nth(index).is_visible()
                ]
                if visible_dropdowns:
                    break
                page.wait_for_timeout(100)
            if not visible_dropdowns:
                raise RuntimeError("店铺下拉没有打开")

            # Other page filters leave dropdown nodes in the DOM. Identify the
            # shop-name list by its overlap with reviewed shop labels.
            known_names = set(GAIA_SHOP_NAMES.values())
            scored = []
            for candidate in visible_dropdowns:
                text = candidate.inner_text()
                score = sum(1 for name in known_names if name in text)
                scored.append((score, candidate, text))
            best_score = max(item[0] for item in scored)
            matching_dropdowns = [item for item in scored if item[0] == best_score and best_score > 0]
            if len(matching_dropdowns) != 1:
                raise RuntimeError("店铺名称下拉无法唯一识别，拒绝按页面顺序猜测")
            dropdown = matching_dropdowns[0][1]
            _screenshot(page, artifacts, "02_shop_list_opened")

            seen_labels = set()
            exact = None
            for _ in range(30):
                checkpoint()
                for line in dropdown.inner_text().splitlines():
                    line = line.strip()
                    if line:
                        seen_labels.add(line)
                current = dropdown.get_by_text(shop_name, exact=True).filter(visible=True)
                if current.count() == 1:
                    exact = current
                    break
                if current.count() > 1:
                    raise RuntimeError("目标店铺出现多个完全一致选项，拒绝选择")
                position = _scroll_shop_dropdown(dropdown)
                page.wait_for_timeout(180)
                if position["after"] <= position["before"] or position["after"] >= position["max"]:
                    for line in dropdown.inner_text().splitlines():
                        line = line.strip()
                        if line:
                            seen_labels.add(line)
                    current = dropdown.get_by_text(shop_name, exact=True).filter(visible=True)
                    if current.count() == 1:
                        exact = current
                    break
            if exact is None:
                _screenshot(page, artifacts, "02_shop_not_found")
                raise RuntimeError(
                    "盖亚店铺列表中未找到 %s（已扫描：%s）" %
                    (shop_name, "、".join(sorted(seen_labels)) or "空")
                )
            checkpoint()
            exact.click(timeout=8000)
            page.wait_for_timeout(250)
            selected_label = dialog.get_by_text(shop_name, exact=True).filter(visible=True)
            selected_text = shop_name if selected_label.count() == 1 else ""
            if selected_text != shop_name:
                raise RuntimeError("店铺选择回读不一致：%s" % selected_text)
            result["selected_shop"] = selected_text
            _emit("upload_preflight_shop_selected", shop=selected_text)

            quantity_input = dialog.get_by_placeholder("请输入1-10的整数", exact=True)
            if quantity_input.count() != 1:
                raise RuntimeError("创建数量输入框无法唯一定位")
            quantity_input.fill("1", timeout=8000)
            if quantity_input.input_value() != "1":
                raise RuntimeError("创建数量回读不是 1")
            result["quantity_readback"] = "1"

            choose_file = _visible_unique(dialog, "选择文件", roles=("button",))
            checkpoint()
            verify()
            with page.expect_file_chooser(timeout=8000) as chooser_info:
                choose_file.click(timeout=8000)
            chooser_info.value.set_files(str(output_path))
            result["attachment_readback"] = wait_for_attachment(
                page, dialog, expected_name=output_path.name, expected_size=result["output_size"],
                batch_id=batch_id, phase="after_attach",
                check_cancelled=checkpoint,
            )
            result["file_attached"] = True
            result["attached_filename"] = output_path.name

            # Read only. There is intentionally no click call on this locator.
            confirm = dialog.get_by_role("button", name=re.compile(r"确\s*定")).filter(visible=True)
            if confirm.count() != 1:
                raise RuntimeError("确定按钮无法唯一定位")
            result["confirm_enabled"] = bool(confirm.is_enabled())
            result["ready_to_confirm"] = result["confirm_enabled"]
            result["screenshot_ready"] = _screenshot(page, artifacts, "02_file_attached_no_submit")
            _emit(
                "upload_preflight_file_attached",
                shop=selected_text,
                quantity="1",
                filename=output_path.name,
                confirm_enabled=result["confirm_enabled"],
                screenshot=result["screenshot_ready"],
                submitted=False,
            )
            if not result["confirm_enabled"]:
                raise RuntimeError("店铺、数量和文件均已回读，但盖亚确定按钮仍不可用")

            if on_ready is not None:
                checkpoint()
                verify()
                return on_ready(page, dialog, confirm, result)

            cancel = dialog.get_by_role("button", name=re.compile(r"取\s*消")).filter(visible=True)
            if cancel.count() != 1:
                raise RuntimeError("取消按钮无法唯一定位；未触发任何提交")
            cancel.click(timeout=8000)
            dialog.wait_for(state="hidden", timeout=8000)
            result["dialog_cancelled"] = True
            result["screenshot_cancelled"] = _screenshot(page, artifacts, "03_cancelled")
            result["success"] = True
            _emit("upload_preflight_cancelled", screenshot=result["screenshot_cancelled"], submitted=False)
            return result
        except Exception as exc:
            if isinstance(exc, FileAttachmentError):
                # Retain evidence before cancelling. No cookies, file contents or URLs.
                evidence = artifacts / "file_readback_error.json"
                evidence.write_text(json.dumps(exc.readback, ensure_ascii=False, indent=2), encoding="utf-8")
                evidence.chmod(0o600)
                try:
                    _screenshot(page, artifacts, "03_file_readback_blocked")
                except Exception:
                    pass
            try:
                if dialog is not None and dialog.is_visible():
                    cancel = dialog.get_by_role("button", name=re.compile(r"取\s*消")).filter(visible=True)
                    if cancel.count() == 1:
                        cancel.click(timeout=3000)
                        result["dialog_cancelled"] = True
            except Exception:
                pass
            raise
        finally:
            context.close()
