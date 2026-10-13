import os
import json
import tempfile
import shutil
from pathlib import Path
from zipfile import BadZipFile

from flask import Blueprint, jsonify, request, send_file

from ..infrastructure.excel_inspector import (
    ExcelInspectionError,
    discover_excel_files,
    ensure_allowed_path,
    inspect_xlsx,
    load_profiles,
)
from ..infrastructure.batch_repository import GaiaBatchRepository
from ..infrastructure.model_mapping_repository import (
    MAX_MAPPING_BYTES,
    ModelMappingRepository,
)
from ..domain.mapping_contract import GaiaMappingContractError
from ..application.create_batch import create_inspected_batch
from ..application.create_batch import sha256_file
from ..application.batch_lifecycle import cancel_batch_record
from ..application.generate_batch import prepare_gaia_template, generate_gaia_batch
from ..application.handoff_preflight import inspect_handoff
from ..application.model_resolution import (
    resolve_model_records,
    resolve_standard_sku,
    resolve_operator_models,
)
from ..infrastructure.quality_mapping_repository import QualityMappingRepository
from ..infrastructure.template_writer import GaiaTemplateWriteError
from ..infrastructure.template_repository import TemplateRepository, MAX_TEMPLATE_BYTES
from ..application.upload_execution import GaiaUploadExecution
from ..application.price_uplift import PriceUpliftError, prepare_price_uplift
from ..infrastructure.price_uplift_template import (
    inspect_price_uplift_template, write_price_uplift_template,
)


def create_gaia_blueprint(work_dir, known_shop_ids=None):
    bp = Blueprint("gaia_listing", __name__, url_prefix="/api/gaia")
    downloads_dir = os.path.expanduser(os.environ.get("GAIA_DOWNLOADS_DIR", "~/Downloads"))
    imports_dir = os.path.join(work_dir, "panel_data", "gaia_listing", "imports")
    template_imports_dir = os.path.join(imports_dir, "templates")
    profiles_path = os.path.join(work_dir, "config", "gaia_excel_profiles.json")
    batches_dir = os.path.join(work_dir, "panel_data", "gaia_listing", "batches")
    outputs_dir = os.path.join(work_dir, "panel_data", "gaia_listing", "outputs")
    price_dir = Path(work_dir) / "panel_data" / "gaia_listing" / "price_uplift"
    os.makedirs(imports_dir, exist_ok=True)
    os.makedirs(template_imports_dir, exist_ok=True)
    repository = GaiaBatchRepository(batches_dir)
    mapping_repository = ModelMappingRepository(os.path.join(imports_dir, "model_mappings"))
    template_repository = TemplateRepository(template_imports_dir, profiles_path)
    quality_repository = QualityMappingRepository(os.path.join(work_dir, "config", "gaia_quality_mapping.json"))
    known_shop_ids = {str(value) for value in (known_shop_ids or [])}

    def upload_enabled():
        override = os.environ.get("GAIA_LISTING_V1_ENABLED")
        if override is not None:
            return override == "1"
        try:
            with open(os.path.join(work_dir, "config", "gaia_execution.json"), encoding="utf-8") as source:
                return json.load(source).get("submit_enabled") is True
        except (OSError, ValueError):
            return False

    def inspect_upload(batch, shop_id):
        result = inspect_handoff(
            batch, requested_shop_id=shop_id, outputs_dir=outputs_dir,
            allowed_source_roots=(downloads_dir, imports_dir),
            mapping_repository=mapping_repository, quality_repository=quality_repository,
        )
        if result["ready"]:
            metadata, _ = mapping_repository.get(batch["mapping_version_id"])
            if not metadata.get("mainland_filter_applied"):
                return {"ready": False, "errors": ["价格规则映射尚未执行国行筛选，禁止上传"]}
        return result

    execution = GaiaUploadExecution(
        work_dir, repository, inspect_upload, upload_enabled,
        price_template=lambda: current_price_template()[0],
    )

    def current_price_template():
        target = price_dir / "template.xlsx"
        if target.is_file():
            inspect_price_uplift_template(str(target))
            return target, "managed"
        download_root = Path(downloads_dir).expanduser()
        if not download_root.is_dir():
            return None, "missing"
        candidates = sorted(
            (candidate for candidate in download_root.glob("直播商品价格浮动规则数据模版*.xlsx")
             if candidate.is_file() and not candidate.is_symlink()),
            key=lambda candidate: (candidate.stat().st_mtime_ns, candidate.name), reverse=True,
        )
        for candidate in candidates:
            try:
                if candidate.stat().st_size > 2 * 1024 * 1024:
                    continue
                inspect_price_uplift_template(str(candidate))
                price_dir.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix="template_", suffix=".xlsx", dir=price_dir)
                try:
                    with os.fdopen(fd, "wb") as output, candidate.open("rb") as source:
                        shutil.copyfileobj(source, output)
                    inspect_price_uplift_template(temporary)
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return target, "downloads"
            except (PriceUpliftError, BadZipFile, OSError, ValueError):
                continue
        return None, "missing"

    @bp.get("/capabilities")
    def capabilities():
        return jsonify({
            "success": True,
            "module": "gaia_listing",
            "phase": "C1",
            "features": {
                "excel_discovery": True,
                "excel_inspection": True,
                "managed_template_upload": True,
                "model_mapping_upload": True,
                "model_mapping_download_import": True,
                "template_generation": True,
                "gaia_handoff_preflight": True,
                "batch_cancel": True,
                "gaia_stop": True,
                "gaia_submit": upload_enabled(),
                "doudian_handoff": False,
                "price_uplift_preparation": True,
                "price_uplift_auto_import": True,
            },
            "contracts": {
                "mapping_policy": "explicit_mainland_only_v2",
                "template_role": "gaia_dual_quality_upload",
                "price_export_role": "standard_sku_mapping_only",
            },
            "safety": {
                "source_files_read_only": True,
                "gaia_submit_requires_confirmation": True,
                "gaia_submit_enabled": upload_enabled(),
            },
            "message": "填写店铺、成色、手机型号和分期数后点击开始，自动上传盖亚并将核实的新商品导入对应分期规则；不会启动抖店发布",
        })

    @bp.get("/qualities/status")
    def quality_status():
        labels = ("99新", "95新", "9新", "准新机")
        status = {}
        for label in labels:
            try:
                quality_repository.get_required(label)
                status[label] = {"configured": True}
            except GaiaMappingContractError as exc:
                status[label] = {"configured": False, "message": str(exc)}
        return jsonify({"success": True, "qualities": status})

    @bp.get("/model-mappings/latest")
    def latest_model_mapping():
        try:
            mapping = mapping_repository.latest()
        except (GaiaMappingContractError, OSError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "mapping": mapping})

    @bp.get("/templates/current")
    def current_template():
        try:
            template = template_repository.current()
        except (ExcelInspectionError, OSError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "template": template})

    @bp.post("/templates")
    def upload_template():
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"success": False, "message": "请选择盖亚发品模板"}), 400
        if request.content_length and request.content_length > MAX_TEMPLATE_BYTES + 1024 * 1024:
            return jsonify({"success": False, "message": "发品模板超过 16 MB 限制"}), 413
        try:
            template = template_repository.save_upload(upload)
        except (ExcelInspectionError, OSError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "template": template}), 201

    @bp.post("/model-mappings")
    def upload_model_mapping():
        upload = request.files.get("file")
        if upload is None:
            return jsonify({"success": False, "message": "请选择型号映射表文件"}), 400
        if request.content_length and request.content_length > MAX_MAPPING_BYTES + 1024 * 1024:
            return jsonify({"success": False, "message": "型号映射表超过 32 MB 限制"}), 413
        try:
            metadata = mapping_repository.save_upload(upload)
        except GaiaMappingContractError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "mapping": metadata}), 201

    @bp.post("/model-mappings/import-download")
    def import_downloaded_model_mapping():
        """Import a download or reuse a hash-verified managed version."""
        data = request.get_json(silent=True) or {}
        try:
            requested = Path(str(data.get("path") or "")).expanduser().resolve()
            if requested.parent == mapping_repository.root:
                # Discovery also returns managed copies when macOS denies Downloads.
                # Reuse only immutable known versions, never arbitrary files in imports.
                metadata, stored = mapping_repository.get(requested.stem)
                if requested != stored.resolve():
                    raise GaiaMappingContractError("文件路径与已保存的映射版本不一致")
                if (not metadata.get("mainland_filter_applied")
                        or metadata.get("mapping_policy") != "explicit_mainland_only_v2"):
                    raise GaiaMappingContractError("已保存映射未通过最新国行筛选，请在高级设置上传价格规则原表")
                return jsonify({"success": True, "mapping": metadata, "reused": True,
                                "source_kind": "managed_mapping"}), 200
            path = ensure_allowed_path(str(data.get("path") or ""), [downloads_dir])
            inspection = inspect_xlsx(path, load_profiles(profiles_path))
            profile_ids = {item["id"] for item in inspection["profile_matches"]}
            if inspection["status"] != "recognized" or profile_ids != {"gaia_goods_price_mapping"}:
                raise ExcelInspectionError("文件不是唯一识别的盖亚货品维度标品映射表")
            metadata = mapping_repository.save_path(path)
        except (ExcelInspectionError, GaiaMappingContractError, OSError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "mapping": metadata, "inspection": inspection}), 201

    @bp.post("/model-mappings/resolve")
    def resolve_model_mapping():
        """Read-only catalogue resolution; it never chooses among SKU variants."""
        data = request.get_json(silent=True) or {}
        try:
            _, records = mapping_repository.load_records(str(data.get("mapping_version_id") or ""))
            standard_sku_id = str(data.get("standard_sku_id") or "").strip()
            if standard_sku_id:
                result = resolve_standard_sku(records, standard_sku_id)
            else:
                result = resolve_model_records(
                    records, brand=str(data.get("brand") or ""),
                    model=str(data.get("model") or ""),
                )
        except (GaiaMappingContractError, OSError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        status = 200 if result.records else 404
        return jsonify({"success": bool(result.records), "resolution": result.as_dict()}), status

    @bp.post("/model-mappings/resolve-batch")
    def resolve_model_mapping_batch():
        """One explicit phone model selects all its distinct mainland SKU IDs."""
        data = request.get_json(silent=True) or {}
        rows = data.get("models")
        if not isinstance(rows, list) or not rows:
            return jsonify({"success": False, "message": "请填写至少一个手机型号"}), 400
        if len(rows) > 2000:
            return jsonify({"success": False, "message": "单次最多匹配 2000 个手机型号"}), 400
        try:
            _, records = mapping_repository.load_records(
                str(data.get("mapping_version_id") or "")
            )
            result = resolve_operator_models(records, rows)
            return jsonify(result), (200 if result["success"] else 409)
        except (GaiaMappingContractError, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400

    @bp.get("/excel/latest")
    def latest_excel():
        profiles = load_profiles(profiles_path)
        result = []
        warnings = []
        seen_paths = set()
        managed_mappings_dir = str(mapping_repository.root)
        search_sources = (
            (downloads_dir, "downloads"),
            (template_imports_dir, "managed_template"),
            (managed_mappings_dir, "managed_mapping"),
        )
        for directory, source_kind in search_sources:
            try:
                candidates = discover_excel_files(directory)
            except OSError as exc:
                warnings.append({"source": source_kind, "message": str(exc)})
                continue
            for candidate in candidates:
                if candidate.path in seen_paths:
                    continue
                seen_paths.add(candidate.path)
                item = candidate.to_dict()
                item["source_kind"] = source_kind
                try:
                    inspected = inspect_xlsx(candidate.path, profiles)
                    item.update({
                        "status": inspected["status"],
                        "profile_matches": inspected["profile_matches"],
                    })
                except ExcelInspectionError as exc:
                    item.update({"status": "invalid", "error": str(exc), "profile_matches": []})
                result.append(item)
        result.sort(key=lambda item: item.get("modified_at", ""), reverse=True)
        return jsonify({"success": True, "directory": downloads_dir,
                        "search_directories": [item[0] for item in search_sources],
                        "warnings": warnings, "files": result})

    @bp.post("/excel/inspect")
    def inspect_excel():
        data = request.get_json(silent=True) or {}
        try:
            path = ensure_allowed_path(str(data.get("path") or ""), [downloads_dir, imports_dir])
            inspected = inspect_xlsx(path, load_profiles(profiles_path))
        except ExcelInspectionError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "inspection": inspected})

    @bp.post("/batches")
    def create_batch():
        data = request.get_json(silent=True) or {}
        shop_id = str(data.get("shop_id") or "")
        if known_shop_ids and shop_id not in known_shop_ids:
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        try:
            source_path = ensure_allowed_path(str(data.get("source_path") or ""), [downloads_dir, imports_dir])
            batch, inspection = create_inspected_batch(
                repository,
                shop_id=shop_id,
                scene=data.get("scene", 2),
                create_count=data.get("create_count", 1),
                source_path=source_path,
                profiles=load_profiles(profiles_path),
            )
        except (ExcelInspectionError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "batch": batch.to_dict(), "inspection": inspection}), 201

    def _prepared_request(data):
        shop_id = str(data.get("shop_id") or "")
        if not shop_id.isdigit() or (known_shop_ids and shop_id not in known_shop_ids):
            raise ValueError("请选择有效店铺")
        if str(data.get("create_count", 1)) != "1":
            raise ValueError("本阶段每行创建数量固定为 1")
        path = ensure_allowed_path(str(data.get("source_path") or ""), [downloads_dir, imports_dir])
        profiles = load_profiles(profiles_path)
        selected_sku_ids = data.get("selected_sku_ids")
        if "models" in data:
            metadata, records = mapping_repository.load_records(str(data.get("mapping_version_id") or ""))
            if not metadata.get("mainland_filter_applied"):
                raise ValueError("请使用包含标品名称、已筛选国行的价格规则表")
            resolution = resolve_operator_models(records, data["models"])
            if not resolution["success"]:
                raise ValueError(resolution["message"])
            if selected_sku_ids != resolution["selected_sku_ids"]:
                raise ValueError("型号和标品 ID 清单不一致，请重新匹配")
        if "quality_labels" in data and not isinstance(data["quality_labels"], list):
            raise ValueError("成色必须是多选清单")
        prepared = prepare_gaia_template(
            source_path=path, mapping_version_id=str(data.get("mapping_version_id") or ""),
            quality_label=(data.get("quality_labels") if "quality_labels" in data
                           else str(data.get("quality_label") or "")), scene=data.get("scene", 2),
            selected_sku_ids=selected_sku_ids,
            profiles=profiles, mapping_repository=mapping_repository,
            quality_repository=quality_repository,
        )
        return shop_id, path, profiles, prepared

    @bp.post("/batches/preview")
    def preview_batch():
        data = request.get_json(silent=True) or {}
        try:
            _, _, _, prepared = _prepared_request(data)
        except (ExcelInspectionError, GaiaMappingContractError, GaiaTemplateWriteError, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "preview": prepared.preview()})

    @bp.post("/batches/generate")
    def generate_batch():
        data = request.get_json(silent=True) or {}
        try:
            shop_id, source_path, profiles, prepared = _prepared_request(data)
            if (data.get("expected_source_sha256") != prepared.source_hash
                    or (data.get("expected_quality_ids") != list(prepared.quality_ids)
                        if "expected_quality_ids" in data
                        else data.get("expected_quality_id") != prepared.quality_id)):
                raise ValueError("模板或成色配置在预览后发生变化，请重新预览")
            batch = generate_gaia_batch(
                repository=repository, outputs_dir=outputs_dir, shop_id=shop_id,
                scene=data.get("scene", 2), create_count=1, source_path=source_path,
                profiles=profiles, prepared=prepared,
            )
        except (ExcelInspectionError, GaiaMappingContractError, GaiaTemplateWriteError, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        return jsonify({"success": True, "batch": batch.to_dict(), "preview": prepared.preview(),
                        "download_url": "/api/gaia/batches/%s/download" % batch.id}), 201

    @bp.get("/batches/<batch_id>/download")
    def download_batch(batch_id):
        try:
            batch = repository.get(batch_id)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if not batch or batch.get("state") not in {
            "validation_passed", "confirmed", "gaia_submitted", "gaia_accepted", "gaia_rejected", "manual_required",
        }:
            return jsonify({"success": False, "message": "批次不存在或尚未通过校验"}), 404
        expected = os.path.realpath(os.path.join(outputs_dir, batch_id, "gaia_upload.xlsx"))
        if batch.get("output_path") != expected or not os.path.isfile(expected):
            return jsonify({"success": False, "message": "输出文件不存在"}), 409
        if sha256_file(expected) != batch.get("output_hash"):
            return jsonify({"success": False, "message": "输出文件哈希不一致"}), 409
        return send_file(expected, as_attachment=True, download_name="gaia_%s.xlsx" % batch_id)

    @bp.get("/batches/<batch_id>/handoff-preflight")
    def handoff_preflight(batch_id):
        try:
            batch = repository.get(batch_id)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if batch is None:
            return jsonify({"success": False, "message": "批次不存在"}), 404
        requested_shop_id = str(request.args.get("shop_id") or "")
        if known_shop_ids and requested_shop_id not in known_shop_ids:
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        result = inspect_handoff(
            batch, requested_shop_id=requested_shop_id, outputs_dir=outputs_dir,
            allowed_source_roots=(downloads_dir, imports_dir),
            mapping_repository=mapping_repository,
            quality_repository=quality_repository,
        )
        return jsonify({"success": result["ready"], "preflight": result}), (200 if result["ready"] else 409)

    @bp.get("/batches/<batch_id>")
    def get_batch(batch_id):
        try:
            batch = repository.get(batch_id)
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        if batch is None:
            return jsonify({"success": False, "message": "批次不存在"}), 404
        batch = execution.recover_interrupted(batch)
        return jsonify({"success": True, "batch": batch})

    @bp.post("/batches/<batch_id>/start")
    def start_upload(batch_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求格式无效"}), 400
        shop_id = str(data.get("shop_id") or "")
        if not shop_id.isdigit() or (known_shop_ids and shop_id not in known_shop_ids):
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        try:
            batch = execution.start(batch_id, shop_id=shop_id,
                                    expected_hash=data.get("expected_output_hash"),
                                    confirmed=data.get("confirmed"),
                                    installments=data.get("installments"),
                                    activity_name=data.get("activity_name"))
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "batch": batch}), 202

    @bp.post("/batches/<batch_id>/stop")
    def stop_upload(batch_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求格式无效"}), 400
        shop_id = str(data.get("shop_id") or "")
        if not shop_id.isdigit() or (known_shop_ids and shop_id not in known_shop_ids):
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        try:
            batch = execution.stop(batch_id, shop_id=shop_id,
                                   expected_hash=data.get("expected_output_hash"))
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "batch": batch})

    @bp.post("/batches/<batch_id>/price-uplift/resume")
    def resume_price_uplift(batch_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求格式无效"}), 400
        shop_id = str(data.get("shop_id") or "")
        if not shop_id.isdigit() or (known_shop_ids and shop_id not in known_shop_ids):
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        try:
            batch = execution.resume_price_only(
                batch_id, shop_id=shop_id, expected_hash=data.get("expected_output_hash"),
                confirmed=data.get("confirmed"),
            )
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "batch": batch}), 202

    @bp.post("/batches/<batch_id>/cancel")
    def cancel_batch(batch_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({"success": False, "message": "请求格式无效"}), 400
        shop_id = str(data.get("shop_id") or "")
        if not shop_id.isdigit() or (known_shop_ids and shop_id not in known_shop_ids):
            return jsonify({"success": False, "message": "请选择有效店铺"}), 400
        try:
            batch = repository.mutate(batch_id, lambda current: cancel_batch_record(
                current, shop_id=shop_id, reason=data.get("reason")))
        except ValueError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        except RuntimeError as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        if batch is None:
            return jsonify({"success": False, "message": "批次不存在"}), 404
        return jsonify({"success": True, "batch": batch})

    @bp.post("/price-uplift/template")
    def upload_price_uplift_template():
        upload = request.files.get("file")
        if upload is None or not (upload.filename or "").lower().endswith(".xlsx"):
            return jsonify({"success": False, "message": "请选择官方价格上浮导入模板 .xlsx"}), 400
        if request.content_length and request.content_length > 2 * 1024 * 1024:
            return jsonify({"success": False, "message": "模板不能超过 2 MB"}), 413
        price_dir.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="template_", suffix=".xlsx", dir=price_dir)
        try:
            with os.fdopen(fd, "wb") as destination:
                upload.save(destination)
            if os.path.getsize(temporary) > 2 * 1024 * 1024:
                raise PriceUpliftError("模板不能超过 2 MB")
            inspect_price_uplift_template(temporary)
            os.replace(temporary, price_dir / "template.xlsx")
        except (PriceUpliftError, BadZipFile, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return jsonify({"success": True, "message": "价格上浮模板已校验保存"}), 201

    @bp.get("/price-uplift/template/current")
    def current_price_uplift_template():
        try:
            template, source = current_price_template()
        except (PriceUpliftError, BadZipFile, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        if template is None:
            return jsonify({"success": False, "message": "未找到有效官方模板，请手动选择上传"}), 404
        return jsonify({"success": True, "source": source, "name": template.name})

    @bp.post("/batches/<batch_id>/price-uplift/preview")
    def price_uplift_preview(batch_id):
        data = request.get_json(silent=True) or {}
        try:
            batch = repository.get(batch_id)
            if batch is None:
                return jsonify({"success": False, "message": "盖亚批次不存在"}), 404
            result = prepare_price_uplift(batch, shop_id=str(data.get("shop_id") or ""),
                                          installments=data.get("installments"))
            template, _ = current_price_template()
            if template is None:
                raise PriceUpliftError("请先上传官方价格上浮导入模板")
        except (PriceUpliftError, BadZipFile, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return jsonify({"success": True, "preview": result})

    @bp.post("/batches/<batch_id>/price-uplift/download")
    def price_uplift_download(batch_id):
        data = request.get_json(silent=True) or {}
        try:
            batch = repository.get(batch_id)
            if batch is None:
                return jsonify({"success": False, "message": "盖亚批次不存在"}), 404
            result = prepare_price_uplift(batch, shop_id=str(data.get("shop_id") or ""),
                                          installments=data.get("installments"))
            template, _ = current_price_template()
            if template is None:
                raise PriceUpliftError("请先上传官方价格上浮导入模板")
            output = price_dir / f"{batch_id}_{result['installments']}.xlsx"
            write_price_uplift_template(str(template), str(output), result["product_ids"])
        except (PriceUpliftError, BadZipFile, OSError, ValueError) as exc:
            return jsonify({"success": False, "message": str(exc)}), 409
        return send_file(output, as_attachment=True,
                         download_name=f"price_uplift_{batch_id}_{result['installments']}.xlsx")

    return bp
