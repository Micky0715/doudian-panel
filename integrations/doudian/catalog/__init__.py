"""Versioned local catalogues backed by live DouDian metadata."""

from .properties import (
    DEFAULT_PHONE_PROPERTY_CATALOGUE,
    PhonePropertyCatalogue,
    PropertyCatalogueError,
)

__all__ = [
    "DEFAULT_PHONE_PROPERTY_CATALOGUE",
    "PhonePropertyCatalogue",
    "PropertyCatalogueError",
]
