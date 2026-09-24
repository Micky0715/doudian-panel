"""Attach a verified workbook and cancel; never performs final submission."""

from .upload_form import GAIA_SHOP_NAMES, with_gaia_upload_form


def preflight_gaia_upload(*, work_dir, batch_id, shop_id, profile_dir,
                          artifact_dir, login_wait=120, headless=False):
    # No callback is exposed: this public preflight always takes the cancel path.
    return with_gaia_upload_form(
        work_dir=work_dir, batch_id=batch_id, shop_id=shop_id,
        profile_dir=profile_dir, artifact_dir=artifact_dir,
        login_wait=login_wait, headless=headless,
    )
