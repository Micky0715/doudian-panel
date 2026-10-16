"""Stable, shop-scoped Playwright profile locations.

Never copy a legacy shared profile into these directories: its active shop is
not established by its filename and can change when another operator uses it.
The listing and installment workflows share one verified login per shop.
"""

import re
from pathlib import Path


def browser_profile_for_shop(work_dir, purpose, shop_id):
    """Return one private browser profile per shop for either workflow."""
    shop = str(shop_id or "")
    if not re.fullmatch(r"\d{5,12}", shop):
        raise ValueError("店铺短 ID 无效，不能选择浏览器登录资料")
    if purpose not in {"cpay", "listing"}:
        raise ValueError("未知的浏览器用途")
    return Path(work_dir).resolve() / "panel_data" / "browser_profiles" / "listing" / shop


def has_browser_login(profile_dir):
    """Only a local readiness hint; platform identity is checked after opening."""
    profile = Path(profile_dir)
    return (profile / "Default" / "Cookies").is_file() or (profile / "Cookies").is_file()
