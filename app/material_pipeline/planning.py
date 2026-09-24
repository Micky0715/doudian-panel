"""Turn validated material inputs into a cloud-only immutable plan."""

from __future__ import annotations

from .domain import MaterialBundle, MaterialPlan


def build_material_plan(bundle: MaterialBundle) -> MaterialPlan:
    """Validate and return one immutable, ordered main-area image plan."""
    bundle.validate()
    main_substituted = bundle.main is None
    main = bundle.white_images[0] if main_substituted else bundle.main
    white_images = bundle.white_images[1:] if main_substituted else bundle.white_images
    assert main is not None  # established by ``validate``
    return MaterialPlan(
        shop_id=bundle.shop_id.strip(),
        brand=bundle.brand.strip(),
        model=bundle.model.strip(),
        main=main,
        white_images=tuple(white_images),
        main_substituted=main_substituted,
        omitted_white_images=tuple(bundle.omitted_white_images),
        spec_image_candidates=tuple(
            bundle.spec_image_candidates or bundle.white_images
        ),
        model_folder_resolution=bundle.model_folder_resolution,
        resolved_model_folder=bundle.resolved_model_folder,
        official_model_name=bundle.official_model_name,
        official_model_source_url=bundle.official_model_source_url,
    )
