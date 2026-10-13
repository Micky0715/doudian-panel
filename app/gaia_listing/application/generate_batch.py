"""Validated Gaia template preview and generation; never submits to Gaia."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from openpyxl import load_workbook

from ..domain.template_contract import GAIA_TEMPLATE_HEADERS
from ..domain.states import GaiaBatchState
from ..infrastructure.excel_inspector import inspect_xlsx
from ..infrastructure.template_writer import GaiaTemplateWriteError, write_generated_template
from .create_batch import create_inspected_batch, sha256_file
from .template_generation import build_template_rows


@dataclass(frozen=True)
class PreparedGaiaTemplate:
    inspection: dict
    mapping: dict
    models: list
    rows: list
    quality_label: str
    quality_id: str
    source_hash: str
    header_row: int
    quality_labels: tuple[str, ...] = ()
    quality_ids: tuple[str, ...] = ()

    def preview(self, limit: int = 20) -> dict:
        return {
            "row_count": len(self.rows),
            "mapping_version_id": self.mapping["id"],
            "mapping_sha256": self.mapping["sha256"],
            "quality_label": self.quality_label,
            "quality_id": self.quality_id,
            "quality_labels": list(self.quality_labels or (self.quality_label,)),
            "quality_ids": list(self.quality_ids or (self.quality_id,)),
            "source_sha256": self.source_hash,
            "columns": list(GAIA_TEMPLATE_HEADERS),
            "rows": [
                {"excel_row": index + self.header_row + 1, "brand": model.brand, "model": model.model,
                 "category": model.category, "values": list(row.values())}
                for index, (model, row) in enumerate(zip(self.models[:limit], self.rows[:limit]))
            ],
            "truncated": len(self.rows) > limit,
        }


def prepare_gaia_template(*, source_path, mapping_version_id, quality_label, scene,
                          selected_sku_ids,
                          profiles, mapping_repository, quality_repository) -> PreparedGaiaTemplate:
    if str(scene) not in {"1", "2"}:
        raise ValueError("场域只能为 1（直播间）或 2（直播货架）")
    inspection = inspect_xlsx(source_path, profiles)
    matched = inspection["profile_matches"]
    if inspection["status"] != "recognized" or {item["id"] for item in matched} != {"gaia_publish_template"}:
        raise ValueError("文件不是唯一识别的盖亚双维成色发品模板")
    mapping, catalogue = mapping_repository.load_records(mapping_version_id)
    labels = quality_label if isinstance(quality_label, list) else [quality_label]
    if not labels or any(not isinstance(label, str) or not label.strip() for label in labels):
        raise ValueError("请至少选择一种成色")
    labels = [label.strip() for label in labels]
    if len(labels) != len(set(labels)):
        raise ValueError("成色不能重复选择")
    qualities = [quality_repository.get_required(label) for label in labels]
    if len(catalogue) != mapping.get("record_count"):
        raise ValueError("型号映射版本行数不一致，请重新上传")
    if not isinstance(selected_sku_ids, list):
        raise ValueError("请提供本次待生成的标品 SKU ID 清单")
    requested = [str(value or "").strip() for value in selected_sku_ids]
    if not requested or any(not value for value in requested):
        raise ValueError("本次待生成的标品 SKU ID 不能为空")
    if len(requested) != len(set(requested)):
        raise ValueError("本次标品 SKU ID 清单存在重复")
    if len(requested) * len(qualities) > 2000:
        raise ValueError("单个盖亚文件最多生成 2000 行标品 SKU 与成色组合")
    by_sku = {item.standard_sku_id: item for item in catalogue}
    missing = [value for value in requested if value not in by_sku]
    if missing:
        sample = "、".join(missing[:10])
        raise ValueError("以下标品 SKU ID 不在当前映射版本中：%s%s" % (
            sample, " 等" if len(missing) > 10 else "",
        ))
    selected_models = [by_sku[value] for value in requested]
    rows = build_template_rows(scene=scene, models=selected_models, quality=qualities)
    models = [model for model in selected_models for _ in qualities]
    source_hash = sha256_file(source_path)
    workbook = load_workbook(source_path, read_only=True, data_only=True)
    try:
        sheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.active
        header_row = next((index for index, values in enumerate(
            sheet.iter_rows(min_row=1, max_row=10, max_col=8, values_only=True), start=1)
            if tuple(values) == GAIA_TEMPLATE_HEADERS), 0)
        if header_row and any(any(value not in (None, "") for value in values)
                              for values in sheet.iter_rows(min_row=header_row + 1, max_col=8, values_only=True)):
            raise GaiaTemplateWriteError("模板已有 A:H 数据，拒绝覆盖；请重新下载空模板")
    finally:
        workbook.close()
    if not header_row:
        raise GaiaTemplateWriteError("模板未找到完整 A:H 表头")
    quality_labels = tuple(item.label for item in qualities)
    quality_ids = tuple(item.dy_quality_id for item in qualities)
    return PreparedGaiaTemplate(
        inspection, mapping, models, rows,
        quality_labels[0] if len(quality_labels) == 1 else "、".join(quality_labels),
        quality_ids[0] if len(quality_ids) == 1 else ",".join(quality_ids),
        source_hash, header_row, quality_labels, quality_ids,
    )


def _verify_output(path: Path, rows: list) -> None:
    """Read back generated A:H data before marking the batch validated."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook["Sheet1"] if "Sheet1" in workbook.sheetnames else workbook.active
        iterator = sheet.iter_rows(min_row=1, max_col=8, values_only=True)
        headers = ()
        for _ in range(10):
            headers = tuple(next(iterator, ()))
            if headers == GAIA_TEMPLATE_HEADERS:
                break
        if headers != GAIA_TEMPLATE_HEADERS:
            raise GaiaTemplateWriteError("输出文件表头不一致")
        for index, expected in enumerate(rows, start=2):
            actual = tuple(str(value or "") for value in next(iterator, ()))
            if actual != expected.values():
                raise GaiaTemplateWriteError("输出文件第 %s 行回读不一致" % index)
        if any(value not in (None, "") for value in next(iterator, ())):
            raise GaiaTemplateWriteError("输出文件存在未预期的额外数据行")
    finally:
        workbook.close()


def generate_gaia_batch(*, repository, outputs_dir, shop_id, scene, create_count,
                        source_path, profiles, prepared: PreparedGaiaTemplate):
    if int(create_count) != 1:
        raise ValueError("本阶段每行创建数量固定为 1")
    batch, _ = create_inspected_batch(
        repository, shop_id=shop_id, scene=scene, create_count=create_count,
        source_path=source_path, profiles=profiles,
    )
    batch.mapping_version_id = prepared.mapping["id"]
    batch.mapping_hash = prepared.mapping["sha256"]
    batch.quality_label = prepared.quality_label
    batch.quality_id = prepared.quality_id
    batch.quality_labels = list(prepared.quality_labels or (prepared.quality_label,))
    batch.quality_ids = list(prepared.quality_ids or (prepared.quality_id,))
    output = Path(outputs_dir) / batch.id / "gaia_upload.xlsx"
    temporary = output.with_suffix(".tmp.xlsx")
    try:
        if batch.source_hash != prepared.source_hash or sha256_file(source_path) != batch.source_hash:
            raise GaiaTemplateWriteError("源模板在校验后发生变化")
        write_generated_template(template_path=source_path, output_path=str(temporary), rows=prepared.rows)
        _verify_output(temporary, prepared.rows)
        if sha256_file(source_path) != batch.source_hash:
            raise GaiaTemplateWriteError("源模板在生成期间发生变化")
        os.replace(temporary, output)
        batch.output_path = os.path.realpath(output)
        batch.output_hash = sha256_file(output)
        batch.state = GaiaBatchState.VALIDATION_PASSED.value
        products = list(dict.fromkeys((model.brand, model.model) for model in prepared.models))
        batch.summary = {
            "generated_rows": len(prepared.rows),
            "expected_product_count": len(products),
            "expected_products": [{"brand": brand, "model": model} for brand, model in products],
            "blocking_errors": 0,
        }
    except Exception as exc:
        batch.state = GaiaBatchState.VALIDATION_FAILED.value
        batch.errors = [{"code": "template_generation_failed", "message": str(exc)}]
        if temporary.exists():
            temporary.unlink()
        repository.save(batch)
        raise
    repository.save(batch)
    return batch
