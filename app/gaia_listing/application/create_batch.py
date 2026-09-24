import hashlib
import os
import uuid

from ..domain.models import GaiaBatch
from ..domain.states import GaiaBatchState
from ..infrastructure.excel_inspector import inspect_xlsx


def sha256_file(path, chunk_size=1024 * 1024):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_inspected_batch(repository, *, shop_id, scene, create_count,
                           source_path, profiles):
    if not str(shop_id).isdigit():
        raise ValueError("请选择有效店铺")
    scene = int(scene)
    create_count = int(create_count)
    if scene not in (1, 2):
        raise ValueError("场域只能为 1（直播间）或 2（直播货架）")
    if not 1 <= create_count <= 100:
        raise ValueError("创建数量必须在 1 到 100 之间")
    inspection = inspect_xlsx(source_path, profiles)
    if inspection["status"] != "recognized":
        raise ValueError("Excel 类型未唯一识别，不能创建生成任务")
    batch = GaiaBatch(
        id=uuid.uuid4().hex[:12],
        shop_id=str(shop_id),
        scene=scene,
        create_count=create_count,
        source_path=os.path.realpath(source_path),
        source_hash=sha256_file(source_path),
        state=GaiaBatchState.SOURCE_INSPECTED.value,
        summary={"recognized_profiles": 1, "blocking_errors": 0},
    )
    repository.save(batch)
    return batch, inspection
