"""Narrow DouDian material-centre upload boundary."""

from .gateway import (
    DoudianMaterialGateway,
    DoudianMaterialProtocolError,
    MAX_SYNC_IMAGE_BYTES,
    UploadedMaterial,
)

__all__ = [
    "DoudianMaterialGateway", "DoudianMaterialProtocolError",
    "MAX_SYNC_IMAGE_BYTES", "UploadedMaterial",
]
