"""Create and validate short-lived, fail-closed browser execution plans."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import ExecutionItem, ExecutionPlan


SCHEMA_VERSION = 2
DEFAULT_TTL_SECONDS = 6 * 60 * 60
VALID_ACTIONS = {"save_draft", "publish"}


class ExecutionPlanError(RuntimeError):
    pass


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_execution_plan(
    report: Mapping[str, Any],
    source_report: str | Path,
    *,
    requested_ids: Iterable[str] | None = None,
    action: str | None = None,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> ExecutionPlan:
    if int(report.get("schema_version") or 0) != 1:
        raise ExecutionPlanError("unsupported preflight report schema")
    shop_id = str(report.get("shop_id") or "").strip()
    if not shop_id.isdigit():
        raise ExecutionPlanError("invalid preflight shop id")
    decisions = report.get("decisions")
    if not isinstance(decisions, list):
        raise ExecutionPlanError("preflight decisions missing")

    by_id: dict[str, Mapping[str, Any]] = {}
    actual_counts = {"ready": 0, "manual_required": 0, "out_of_scope": 0}
    for decision in decisions:
        if not isinstance(decision, Mapping):
            raise ExecutionPlanError("invalid preflight decision")
        product_id = str(decision.get("product_id") or "").strip()
        if not product_id.isdigit() or product_id in by_id:
            raise ExecutionPlanError("invalid or duplicate product id in preflight")
        state = str(decision.get("decision") or "")
        if state not in actual_counts:
            raise ExecutionPlanError("invalid preflight decision state")
        actual_counts[state] += 1
        by_id[product_id] = decision
    for state, count in actual_counts.items():
        if int(report.get(state) or 0) != count:
            raise ExecutionPlanError(f"preflight {state} count mismatch")

    requested_action = str(action or "").strip()
    if requested_action and requested_action not in VALID_ACTIONS:
        raise ExecutionPlanError("invalid execution action")
    if requested_ids is None:
        selected_ids = [
            product_id for product_id, decision in by_id.items()
            if decision.get("decision") == "ready"
            and (not requested_action or _decision_action(decision) == requested_action)
        ]
    else:
        selected_ids = [str(value).strip() for value in requested_ids]
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise ExecutionPlanError("requested product ids are empty or duplicated")
    items: list[ExecutionItem] = []
    selected_actions: set[str] = set()
    for product_id in selected_ids:
        decision = by_id.get(product_id)
        if decision is None:
            raise ExecutionPlanError(f"product {product_id} is absent from preflight")
        if decision.get("decision") != "ready":
            raise ExecutionPlanError(f"product {product_id} is not ready")
        decision_action = _decision_action(decision)
        if requested_action and decision_action != requested_action:
            raise ExecutionPlanError(
                f"product {product_id} is not ready for {requested_action}"
            )
        selected_actions.add(decision_action)
        item = _execution_item(decision, decision_action)
        items.append(item)
    if not items:
        suffix = f" for {requested_action}" if requested_action else ""
        raise ExecutionPlanError("preflight contains no ready products" + suffix)
    if len(selected_actions) != 1:
        raise ExecutionPlanError("execution plan cannot mix publish and save_draft products")
    plan_action = next(iter(selected_actions))

    source = Path(source_report).expanduser().resolve()
    if not source.is_file():
        raise ExecutionPlanError("preflight source report does not exist")
    created_at = int(time.time() if now is None else now)
    ttl = int(ttl_seconds)
    if ttl <= 0:
        raise ExecutionPlanError("execution plan ttl must be positive")
    plan = ExecutionPlan(
        schema_version=SCHEMA_VERSION,
        shop_id=shop_id,
        action=plan_action,
        created_at=created_at,
        expires_at=created_at + ttl,
        source_report=str(source),
        source_report_sha256=_sha256_file(source),
        items=tuple(items),
    )
    checksum = _sha256_bytes(_canonical_bytes(plan.to_dict(include_checksum=False)))
    return ExecutionPlan(**{**plan.__dict__, "checksum": checksum})


def _decision_action(decision: Mapping[str, Any]) -> str:
    value = str(decision.get("execution_mode") or "").strip()
    if value not in VALID_ACTIONS:
        raise ExecutionPlanError("ready decision has invalid execution_mode")
    return value


def _execution_item(decision: Mapping[str, Any], action: str) -> ExecutionItem:
    required_text = ("name", "brand", "model", "platform_model", "condition", "image_dir")
    if action == "publish":
        required_text += ("screen_shape",)
    missing = [name for name in required_text if not str(decision.get(name) or "").strip()]
    if missing:
        raise ExecutionPlanError("ready decision missing fields: " + ",".join(missing))
    detail_count = int(decision.get("detail_image_count") or 0)
    main_count = int(decision.get("main_image_count") or 0)
    auxiliary_count = int(decision.get("auxiliary_image_count") or 0)
    if detail_count != 14:
        raise ExecutionPlanError("ready decision does not have 14 detail images")
    if not ((main_count == 1 and auxiliary_count >= 3) or (main_count == 0 and auxiliary_count >= 4)):
        raise ExecutionPlanError("ready decision image counts are unsafe")
    return ExecutionItem(
        product_id=str(decision["product_id"]),
        name=str(decision["name"]),
        brand=str(decision["brand"]),
        model=str(decision["model"]),
        platform_model=str(decision["platform_model"]),
        screen_shape=str(decision["screen_shape"]),
        condition=str(decision["condition"]),
        image_dir=str(Path(str(decision["image_dir"])).expanduser().resolve()),
        main_image_count=main_count,
        auxiliary_image_count=auxiliary_count,
        detail_image_count=detail_count,
    )


def store_execution_plan(plan: ExecutionPlan, root: str | Path) -> Path:
    directory = Path(root).expanduser().resolve() / plan.shop_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan.created_at}_{plan.checksum[:12]}.json"
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    os.chmod(path, 0o600)
    return path


def load_execution_plan(
    path: str | Path,
    *,
    expected_shop_id: str = "",
    now: int | None = None,
) -> ExecutionPlan:
    source = Path(path).expanduser().resolve()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExecutionPlanError(f"cannot read execution plan: {exc}") from exc
    if int(value.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ExecutionPlanError("unsupported execution plan schema")
    checksum = str(value.get("checksum") or "")
    unsigned = dict(value)
    unsigned.pop("checksum", None)
    if checksum != _sha256_bytes(_canonical_bytes(unsigned)):
        raise ExecutionPlanError("execution plan checksum mismatch")
    shop_id = str(value.get("shop_id") or "")
    if expected_shop_id and shop_id != str(expected_shop_id):
        raise ExecutionPlanError("execution plan shop mismatch")
    current = int(time.time() if now is None else now)
    if current > int(value.get("expires_at") or 0):
        raise ExecutionPlanError("execution plan expired")
    report_path = Path(str(value.get("source_report") or "")).expanduser().resolve()
    if not report_path.is_file() or _sha256_file(report_path) != value.get("source_report_sha256"):
        raise ExecutionPlanError("preflight source report changed or disappeared")
    raw_items = value.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ExecutionPlanError("execution plan has no items")
    items = tuple(ExecutionItem(**item) for item in raw_items)
    if len({item.product_id for item in items}) != len(items):
        raise ExecutionPlanError("execution plan contains duplicate products")
    plan = ExecutionPlan(
        schema_version=SCHEMA_VERSION,
        shop_id=shop_id,
        action=str(value.get("action") or ""),
        created_at=int(value["created_at"]),
        expires_at=int(value["expires_at"]),
        source_report=str(report_path),
        source_report_sha256=str(value["source_report_sha256"]),
        items=items,
        checksum=checksum,
    )
    if plan.action not in VALID_ACTIONS:
        raise ExecutionPlanError("invalid execution action")
    for item in items:
        _execution_item(item.to_dict(), plan.action)
    return plan
