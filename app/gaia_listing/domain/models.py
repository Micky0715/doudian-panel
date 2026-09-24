from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List

from .states import GaiaBatchState


@dataclass
class GaiaBatch:
    id: str
    shop_id: str
    scene: int
    create_count: int
    source_path: str
    source_hash: str
    state: str = GaiaBatchState.SOURCE_SELECTED.value
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    summary: Dict[str, int] = field(default_factory=dict)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    output_path: str = ""
    output_hash: str = ""
    mapping_version_id: str = ""
    mapping_hash: str = ""
    quality_label: str = ""
    quality_id: str = ""
    gaia_receipt: Dict[str, Any] = field(default_factory=dict)
    doudian_draft_ids: List[str] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)
