"""Read-only integrity gate before any future Gaia browser upload."""

from __future__ import annotations

import os
from pathlib import Path

from ..domain.mapping_contract import GaiaMappingContractError
from ..domain.states import GaiaBatchState
from ..infrastructure.excel_inspector import ExcelInspectionError, ensure_allowed_path
from .create_batch import sha256_file
from .submission_guard import submission_idempotency_key


def inspect_handoff(batch: dict, *, requested_shop_id: str, outputs_dir: str,
                    allowed_source_roots, mapping_repository, quality_repository) -> dict:
    """Return a manifest only when every local input is still identical.

    This function never opens Gaia, changes batch state or uploads a file.
    The future browser runner must repeat these checks immediately before
    choosing a file and again before final confirmation.
    """
    if not isinstance(batch, dict):
        return {"ready": False, "errors": ["批次记录格式无效"]}
    errors = []
    batch_id = str(batch.get("id") or "")
    shop_id = str(batch.get("shop_id") or "")
    if batch.get("state") != GaiaBatchState.VALIDATION_PASSED.value:
        errors.append("批次尚未通过模板生成校验")
    if not requested_shop_id or requested_shop_id != shop_id:
        errors.append("目标店铺与批次店铺不一致")
    if str(batch.get("create_count")) != "1":
        errors.append("创建数量必须为 1")
    if str(batch.get("scene")) not in {"1", "2"}:
        errors.append("场域无效")
    summary = batch.get("summary") if isinstance(batch.get("summary"), dict) else {}
    if summary.get("blocking_errors") != 0 or not summary.get("generated_rows"):
        errors.append("批次存在阻断错误或没有生成行")

    source = str(batch.get("source_path") or "")
    try:
        source = ensure_allowed_path(source, allowed_source_roots)
        if not os.path.isfile(source) or sha256_file(source) != batch.get("source_hash"):
            errors.append("源模板不存在或哈希已变化")
    except (ExcelInspectionError, OSError):
        errors.append("源模板路径无效或不可读取")

    try:
        mapping, _ = mapping_repository.get(str(batch.get("mapping_version_id") or ""))
        if mapping["sha256"] != batch.get("mapping_hash"):
            errors.append("型号映射版本与批次记录不一致")
    except (GaiaMappingContractError, OSError) as exc:
        errors.append(str(exc))

    try:
        quality = quality_repository.get_required(str(batch.get("quality_label") or ""))
        if quality.dy_quality_id != batch.get("quality_id"):
            errors.append("成色 ID 在生成后发生变化，须重新生成")
    except GaiaMappingContractError as exc:
        errors.append(str(exc))

    root = os.path.realpath(outputs_dir)
    expected = os.path.realpath(os.path.join(root, batch_id, "gaia_upload.xlsx"))
    if (os.path.commonpath((root, expected)) != root
            or batch.get("output_path") != expected):
        errors.append("输出文件路径与批次不一致")
    elif not Path(expected).is_file():
        errors.append("输出文件不存在")
    else:
        try:
            if sha256_file(expected) != batch.get("output_hash"):
                errors.append("输出文件哈希不一致")
        except OSError:
            errors.append("输出文件不可读取")

    if errors:
        return {"ready": False, "errors": errors}
    return {
        "ready": True,
        "batch_id": batch_id,
        "shop_id": shop_id,
        "scene": int(batch["scene"]),
        "create_count": 1,
        "output_name": "gaia_upload.xlsx",
        "output_sha256": batch["output_hash"],
        "generated_rows": summary["generated_rows"],
        "idempotency_key": submission_idempotency_key(batch_id, batch["output_hash"]),
        "requires_human_confirmation": True,
        "gaia_submitted": False,
    }
