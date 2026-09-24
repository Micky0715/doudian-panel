"""Exact local SKU-colour image matching; no platform writes."""

from .catalogue import ColorGalleryError, ColorImage, ColorImagePlan, resolve_color_images
from .cloud_catalogue import (
    CloudColorImage, CloudColorImagePlan, resolve_cloud_color_images,
)

__all__ = [
    "ColorGalleryError", "ColorImage", "ColorImagePlan", "resolve_color_images",
    "CloudColorImage", "CloudColorImagePlan", "resolve_cloud_color_images",
]
