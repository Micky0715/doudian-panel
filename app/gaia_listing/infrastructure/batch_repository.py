import json
import os
import tempfile
import time
from contextlib import contextmanager

from ..domain.models import GaiaBatch


class GaiaBatchRepository:
    """File-backed batch store isolated from the existing Doudian task state."""

    def __init__(self, root):
        self.root = os.path.realpath(root)
        os.makedirs(self.root, exist_ok=True)

    def _path(self, batch_id):
        if not batch_id or any(ch not in "0123456789abcdef" for ch in batch_id.lower()):
            raise ValueError("invalid batch id")
        return os.path.join(self.root, batch_id + ".json")

    @contextmanager
    def _lock(self, batch_id):
        lock_path = self._path(batch_id) + ".lock"
        for _ in range(40):
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                time.sleep(0.05)
        else:
            raise RuntimeError("批次正在由其他任务处理；如进程异常退出，请人工核查锁文件")
        try:
            yield
        finally:
            os.close(fd)
            os.unlink(lock_path)

    def _save_unlocked(self, batch: GaiaBatch):
        target = self._path(batch.id)
        fd, temp_path = tempfile.mkstemp(prefix=batch.id + "_", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(batch.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return target

    def save(self, batch: GaiaBatch):
        with self._lock(batch.id):
            return self._save_unlocked(batch)

    def mutate(self, batch_id, change):
        """Atomically read, check and replace one batch across local processes."""
        with self._lock(batch_id):
            current = self.get(batch_id)
            if current is None:
                return None
            updated = change(current)
            if not isinstance(updated, GaiaBatch) or updated.id != batch_id:
                raise ValueError("批次更新结果无效")
            self._save_unlocked(updated)
            return updated.to_dict()

    def get(self, batch_id):
        path = self._path(batch_id)
        if not os.path.isfile(path):
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
