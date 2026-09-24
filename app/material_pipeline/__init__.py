"""Cloud material planning used by the API-first listing workflow.

This package has no browser dependency.  It converts an explicitly identified
cloud main image and white-background images into an immutable upload plan.
"""

from .domain import CloudImage, MaterialBundle, MaterialOmission, MaterialPlan, MaterialPolicyError
from .planning import build_material_plan
from .feishu_catalog import FeishuMaterialCatalog, FeishuMaterialLayout
from .execution import CloudMaterialExecutor, ResolvedMaterialPlan
from .official_model_registry import OfficialModelRecord, OfficialModelRegistry

__all__ = [
    "CloudImage", "MaterialBundle", "MaterialOmission", "MaterialPlan", "MaterialPolicyError",
    "build_material_plan",
    "FeishuMaterialCatalog", "FeishuMaterialLayout", "CloudMaterialExecutor", "ResolvedMaterialPlan",
    "OfficialModelRecord", "OfficialModelRegistry",
]
