import hashlib


def submission_idempotency_key(batch_id, output_hash):
    return hashlib.sha256((str(batch_id) + ":" + str(output_hash)).encode("utf-8")).hexdigest()


def can_submit(batch, *, feature_enabled, confirmed):
    """Fail-closed gate. This does not call Gaia and cannot create products."""
    if not feature_enabled:
        return False, "盖亚真实上传功能未启用"
    if batch.get("state") != "confirmed" or not confirmed:
        return False, "批次尚未完成人工确认"
    if not batch.get("output_path") or not batch.get("output_hash"):
        return False, "缺少已校验的生成文件或文件哈希"
    if batch.get("summary", {}).get("blocking_errors", 0):
        return False, "批次仍有阻断错误"
    return True, "允许提交"
