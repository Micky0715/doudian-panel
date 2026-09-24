"""Local-only batch lifecycle controls; never submits to Gaia."""

from datetime import datetime

from ..domain.models import GaiaBatch
from ..domain.states import GaiaBatchState, can_transition


def cancel_batch_record(data: dict, *, shop_id: str, reason: str) -> GaiaBatch:
    """Cancel a not-yet-submitted batch, preserving an audit event.

    The caller must execute this inside the repository's atomic mutate lock.
    """
    if not isinstance(data, dict):
        raise ValueError("批次记录无效")
    if not shop_id or str(data.get("shop_id")) != str(shop_id):
        raise ValueError("目标店铺与批次不一致")
    reason = str(reason or "").strip()
    if not 2 <= len(reason) <= 300:
        raise ValueError("取消原因需填写 2～300 字")
    batch = GaiaBatch(**data)
    if batch.state == GaiaBatchState.CANCELLED.value:
        return batch
    if not can_transition(batch.state, GaiaBatchState.CANCELLED.value):
        raise RuntimeError("批次已提交或已进入后续处理，不能标记为取消；请人工核查盖亚结果")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    previous = batch.state
    batch.state = GaiaBatchState.CANCELLED.value
    batch.updated_at = now
    batch.events.append({"at": now, "action": "cancel", "from": previous,
                         "to": batch.state, "reason": reason})
    return batch
