"""One read-only attachment contract for preparation and final confirmation.

Upload widgets may reset input.files after copying the File into their list.
Read the live, scoped upload list instead of treating an empty input as loss.
This only proves the UI selection is ready, never server-side acceptance.
"""

import time

from .probe import _emit


# Scope all evidence to the active dialog. Never search the whole dialog text
# for a filename (help text, old messages and download links are not uploads).
ATTACHMENT_SNAPSHOT_JS = r"""root => {
    const visible = element => {
        if (!element || !element.isConnected || !element.getClientRects().length) return false;
        for (let node = element; node && node.nodeType === 1; node = node.parentElement) {
            const style = getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        }
        return true;
    };
    const entries = [...root.querySelectorAll('.ant-upload-list-item, .el-upload-list__item')]
        .filter(visible).map(row => {
            const labels = [...row.querySelectorAll('.ant-upload-list-item-name, .el-upload-list__item-name')]
                .filter(visible);
            const names = labels.map(label => ({
                text: (label.textContent || '').trim(), title: (label.getAttribute('title') || '').trim()
            }));
            const classes = row.classList;
            const explicit = (row.getAttribute('data-status') || '').toLowerCase();
            const text = (row.innerText || '').trim();
            const busy = row.getAttribute('aria-busy') === 'true'
                || [...row.querySelectorAll('[role="progressbar"], .ant-upload-list-item-progress, .el-progress')].some(visible);
            let state = 'selected'; // Local selection (beforeUpload=false) has no done class.
            if (classes.contains('ant-upload-list-item-error') || classes.contains('ant-upload-list-item-removed') || classes.contains('is-fail')
                || classes.contains('is-error') || ['error', 'fail', 'failed', 'removed'].includes(explicit)
                || /上传失败|上传错误|文件错误|上传异常|已删除|已移除/.test(text)) state = 'error';
            else if (busy || classes.contains('ant-upload-list-item-uploading') || classes.contains('is-uploading')
                || classes.contains('is-ready') || ['uploading', 'pending', 'ready'].includes(explicit)
                || /上传中|正在上传|等待上传/.test(text)) state = 'uploading';
            else if (explicit && !['done', 'success', 'selected'].includes(explicit)) state = 'unknown';
            else if (classes.contains('ant-upload-list-item-done') || classes.contains('is-success')
                || ['done', 'success'].includes(explicit)) state = 'done';
            return {names, state};
        });
    const inputs = [...root.querySelectorAll('input[type="file"]')]
        .flatMap(input => [...(input.files || [])].map(file => ({name: file.name, size: file.size})));
    return {entries, inputs};
}"""


def assess_attachment(snapshot, *, expected_name, expected_size, require_list=False):
    """Reject ambiguity/conflict; only transient rendering/uploading can wait."""
    entries, inputs = snapshot.get("entries", []), snapshot.get("inputs", [])
    def answer(state, message, source=""):
        return {"state": state, "message": message, "source": source,
                "expected_name": expected_name, "expected_size": expected_size,
                "snapshot": snapshot}

    if not expected_name or not isinstance(expected_size, int) or expected_size <= 0:
        return answer("invalid", "缺少已校验输出文件的名称或大小")
    if len(inputs) > 1:
        return answer("invalid", "文件选择框包含多个文件，拒绝提交")
    if inputs and (inputs[0].get("name") != expected_name or inputs[0].get("size") != expected_size):
        return answer("invalid", "文件选择框中的文件名称或大小与本批次不一致")
    if len(entries) > 1:
        return answer("invalid", "上传列表包含多个文件，拒绝提交")
    if entries:
        entry = entries[0]
        names = entry.get("names", [])
        if len(names) != 1:
            return answer("invalid", "上传列表文件名无法唯一识别")
        label = names[0]
        # textContent is the full DOM text even when CSS truncates it visually.
        candidates = [label.get(key, "").strip() for key in ("text", "title") if label.get(key, "").strip()]
        if not candidates or any(name != expected_name for name in candidates):
            return answer("invalid", "上传列表文件名与本批次不一致")
        if entry.get("state") == "error":
            return answer("invalid", "上传列表显示文件失败或已移除")
        if entry.get("state") == "uploading":
            return answer("pending", "文件仍在上传，等待完成", "upload_list")
        if entry.get("state") not in {"selected", "done"}:
            return answer("invalid", "上传列表状态无法确认")
        return answer("ready", "上传列表已精确回读文件名且无上传中或失败状态", "upload_list")
    if inputs and not require_list:
        return answer("ready", "原生文件选择框名称与大小一致", "file_input")
    return answer("pending", "上传列表中的文件已消失" if require_list else "尚未回读到所选文件")


class FileAttachmentError(RuntimeError):
    def __init__(self, message, readback):
        super().__init__(message)
        self.readback = readback


def wait_for_attachment(page, dialog, *, expected_name, expected_size,
                        batch_id, phase, require_list=False, timeout_ms=8000, check_cancelled=None):
    """Require two consecutive ready snapshots; poll only, never retry upload."""
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000
    last_ready = None
    while True:
        if check_cancelled:
            check_cancelled()
        try:
            snapshot = dialog.evaluate(ATTACHMENT_SNAPSHOT_JS, timeout=1000)
        except Exception as exc:
            result = {"state": "invalid", "message": "无法读取当前上传控件", "source": "",
                      "expected_name": expected_name, "expected_size": expected_size,
                      "error_type": type(exc).__name__, "snapshot": {}}
            _emit("gaia_file_readback_blocked", batch_id=batch_id, phase=phase, **result)
            raise FileAttachmentError(result["message"], result) from exc
        result = assess_attachment(snapshot, expected_name=expected_name,
                                   expected_size=expected_size, require_list=require_list)
        if result["state"] == "ready" and result == last_ready:
            _emit("gaia_file_readback", batch_id=batch_id, phase=phase, **result)
            return result
        last_ready = result if result["state"] == "ready" else None
        if result["state"] == "invalid" or time.monotonic() >= deadline:
            if result["state"] == "ready":
                result = dict(result, state="pending", message="文件回读未稳定，请核对上传列表")
            _emit("gaia_file_readback_blocked", batch_id=batch_id, phase=phase, **result)
            raise FileAttachmentError("文件回读未通过：" + result["message"], result)
        page.wait_for_timeout(150)
