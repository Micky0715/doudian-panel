"""Generate the interactive idle-eye assets from the animated character only.

The closed-box source is the transparent first frame already extracted from
``character-box-open-transparent.webm``. The complete forward irises are
sampled from a later frame of that same WebM. No legacy illustration, CSS
painted pupil, or separately generated face is allowed in this pipeline.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


ROOT = Path(__file__).resolve().parents[1]
CHARACTER_DIR = ROOT / "assets" / "character"
IDLE_SOURCE = CHARACTER_DIR / "character-idle.webp"
BOX_VIDEO = CHARACTER_DIR / "character-box-open-transparent.webm"
FORWARD_IRIS_SOURCE = CHARACTER_DIR / "character-animation-forward-frame.png"
EYE_SHAPE_REFERENCE = ROOT / "assets" / "illustrations" / "aihuishou-ops-mascot-v4-wanwu.png"


@dataclass(frozen=True)
class EyeSpec:
    name: str
    box: tuple[int, int, int, int]
    outline: tuple[tuple[float, float], ...]
    closed_iris_center: tuple[float, float]
    closed_iris_radii: tuple[float, float]
    animation_iris_center: tuple[float, float]
    forward_center: tuple[float, float]
    iris_render_size: int
    reference_patch: tuple[int, int, int, int]
    target_patch: tuple[int, int, int, int]
    residual_cleanup: Optional[tuple[tuple[float, float], ...]] = None


# Coordinates are calibrated independently against the 642 x 702 first frame.
# The outline points follow each animation eye white and deliberately exclude
# the dark eyelid stroke. They are also mirrored by the inline SVG clipPaths.
EYES = (
    EyeSpec(
        name="left",
        box=(318, 179, 362, 226),
        outline=(
            (.50, .03), (.27, .05), (.10, .24), (.05, .50),
            (.06, .75), (.25, .94), (.53, .97), (.79, .89),
            (.94, .68), (.95, .42), (.83, .17), (.65, .05),
        ),
        closed_iris_center=(350.0, 212.0),
        closed_iris_radii=(12.5, 14.0),
        animation_iris_center=(345.0, 187.0),
        forward_center=(340.0, 202.0),
        iris_render_size=39,
        reference_patch=(430, 370, 543, 489),
        target_patch=(311, 172, 369, 233),
    ),
    EyeSpec(
        name="right",
        box=(398, 167, 437, 214),
        outline=(
            (.49, .03), (.26, .06), (.10, .27), (.06, .53),
            (.08, .77), (.28, .94), (.54, .96), (.78, .86),
            (.93, .65), (.94, .39), (.82, .16), (.64, .05),
        ),
        closed_iris_center=(408.0, 202.0),
        closed_iris_radii=(11.5, 13.5),
        animation_iris_center=(401.0, 180.0),
        forward_center=(417.5, 190.5),
        iris_render_size=41,
        reference_patch=(586, 336, 689, 454),
        target_patch=(391, 160, 444, 221),
        # The source iris crosses the lower-left sclera boundary.  The regular
        # eye mask intentionally excludes that boundary to protect the eyelid,
        # so a tiny, independently calibrated interior cleanup is required.
        residual_cleanup=(
            (398.5, 197.0), (402.5, 191.5), (410.5, 191.5),
            (416.0, 198.0), (417.0, 207.5), (416.0, 211.5),
            (408.0, 212.0), (402.0, 209.0), (398.5, 204.0),
        ),
    ),
)


def _load_animation_frame() -> Image.Image:
    """Load the native-size frame extracted from ``BOX_VIDEO`` at 1.20s."""
    if not BOX_VIDEO.is_file():
        raise FileNotFoundError(BOX_VIDEO)
    frame = Image.open(FORWARD_IRIS_SOURCE).convert("RGBA")
    if frame.size != (642, 702):
        raise ValueError(f"Unexpected animation frame size: {frame.size}")
    return frame


def _apply_reference_eye_patch(
    image: Image.Image,
    reference: Image.Image,
    spec: EyeSpec,
) -> Image.Image:
    """Transfer only the requested reference eye shape onto the animation face."""
    target_left, target_top, target_right, target_bottom = spec.target_patch
    target_size = (target_right - target_left, target_bottom - target_top)
    patch = reference.crop(spec.reference_patch).resize(target_size, Image.Resampling.LANCZOS)

    # Feather only the outer skin-to-skin seam.  The eye white and eyelid in
    # the centre remain untouched reference pixels, preserving their shape.
    matte = Image.new("L", target_size, 0)
    inset = 3
    ImageDraw.Draw(matte).rounded_rectangle(
        (inset, inset, target_size[0] - inset - 1, target_size[1] - inset - 1),
        radius=target_size[0] // 3,
        fill=255,
    )
    matte = matte.filter(ImageFilter.GaussianBlur(2.0))
    patch.putalpha(matte)
    result = image.copy()
    result.alpha_composite(patch, (target_left, target_top))
    return result


def _eye_mask(spec: EyeSpec, canvas_size=(642, 702), feather=.45) -> Image.Image:
    mask = Image.new("L", canvas_size, 0)
    left, top, right, bottom = spec.box
    width = right - left
    height = bottom - top
    points = [(left + x * width, top + y * height) for x, y in spec.outline]
    ImageDraw.Draw(mask).polygon(points, fill=255)
    return mask.filter(ImageFilter.GaussianBlur(feather))


def _remove_iris(image: Image.Image, spec: EyeSpec) -> Image.Image:
    """Continue the source sclera beneath its original downward-looking iris."""
    rgba = np.asarray(image).copy()
    rgb = rgba[:, :, :3].astype(np.float64)
    eye_alpha = np.asarray(_eye_mask(spec))
    yy, xx = np.mgrid[:rgba.shape[0], :rgba.shape[1]]
    cx, cy = spec.closed_iris_center
    rx, ry = spec.closed_iris_radii
    iris_area = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
    eye_area = eye_alpha >= 32

    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1)
    sclera = eye_area & ~iris_area & (minimum > 128) & (saturation < .34)
    sample_y, sample_x = np.where(sclera)
    target_y, target_x = np.where(eye_area & iris_area)
    if len(sample_x) < 32:
        raise RuntimeError(f"Insufficient sclera samples for {spec.name} eye")
    sample_points = np.column_stack((sample_x, sample_y))
    sample_colours = rgb[sample_y, sample_x]
    for target_y_value, target_x_value in zip(target_y, target_x):
        distance_squared = (
            (sample_points[:, 0] - target_x_value) ** 2
            + (sample_points[:, 1] - target_y_value) ** 2
        )
        nearest = np.argpartition(distance_squared, min(31, len(distance_squared) - 1))[:32]
        weights = 1.0 / (distance_squared[nearest] + 3.0)
        colour = np.average(sample_colours[nearest], axis=0, weights=weights)
        rgba[target_y_value, target_x_value, :3] = np.clip(colour, 0, 255)
    cleaned = Image.fromarray(rgba)
    if spec.residual_cleanup:
        cleaned = _repair_residual(cleaned, spec)
    return cleaned


def _repair_residual(image: Image.Image, spec: EyeSpec) -> Image.Image:
    """Remove the source pupil edge missed by the conservative eye mask.

    This is an original-resolution sclera reconstruction, not a white patch:
    each replacement colour is interpolated from nearby real sclera pixels and
    blended through a 1 px feathered, eye-specific cleanup mask.
    """
    rgba = np.asarray(image).copy()
    rgb = rgba[:, :, :3].astype(np.float64)
    cleanup = Image.new("L", image.size, 0)
    ImageDraw.Draw(cleanup).polygon(spec.residual_cleanup, fill=255)
    cleanup = cleanup.filter(ImageFilter.GaussianBlur(.65))
    cleanup_alpha = np.asarray(cleanup).astype(np.float64) / 255.0

    eye_alpha = np.asarray(_eye_mask(spec))
    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1)
    source_area = (
        (eye_alpha >= 80)
        & (cleanup_alpha < .02)
        & (minimum > 145)
        & (saturation < .24)
    )
    sample_y, sample_x = np.where(source_area)
    target_y, target_x = np.where(cleanup_alpha > .01)
    if len(sample_x) < 32:
        raise RuntimeError(f"Insufficient cleanup samples for {spec.name} eye")
    sample_points = np.column_stack((sample_x, sample_y))
    sample_colours = rgb[sample_y, sample_x]
    for target_y_value, target_x_value in zip(target_y, target_x):
        distance_squared = (
            (sample_points[:, 0] - target_x_value) ** 2
            + (sample_points[:, 1] - target_y_value) ** 2
        )
        nearest = np.argpartition(distance_squared, min(47, len(distance_squared) - 1))[:48]
        weights = 1.0 / (distance_squared[nearest] + 4.0)
        fill = np.average(sample_colours[nearest], axis=0, weights=weights)
        blend = cleanup_alpha[target_y_value, target_x_value]
        rgb[target_y_value, target_x_value] = (
            rgb[target_y_value, target_x_value] * (1.0 - blend)
            + fill * blend
        )
    rgba[:, :, :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    return Image.fromarray(rgba)


def _extract_iris(frame: Image.Image, spec: EyeSpec, output: Path, side=32) -> Image.Image:
    """Extract the animation's brown iris, black pupil and native highlight."""
    center_x, center_y = spec.animation_iris_center
    half = side // 2
    crop = frame.crop((
        round(center_x - half), round(center_y - half),
        round(center_x + half), round(center_y + half),
    )).convert("RGBA")
    pixels = np.asarray(crop.convert("RGB")).astype(np.float64)
    luminance = pixels[:, :, 0] * .2126 + pixels[:, :, 1] * .7152 + pixels[:, :, 2] * .0722
    dark_y, dark_x = np.where(luminance < 190)
    # Restrict measurements to the centre so the eye outline cannot join the
    # iris mask. The actual pixels, including the white catchlight, remain.
    central = ((dark_x - half) ** 2 / 13.5 ** 2 + (dark_y - half) ** 2 / 15.0 ** 2) <= 1
    dark_x = dark_x[central]
    dark_y = dark_y[central]
    if len(dark_x) < 80:
        raise RuntimeError(f"Unable to locate the animation iris for {spec.name} eye")
    radius_x = min(13.0, (dark_x.max() - dark_x.min() + 1) / 2 + .2)
    radius_y = min(14.5, (dark_y.max() - dark_y.min() + 1) / 2 + .25)
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).ellipse(
        (half - radius_x, half - radius_y, half + radius_x, half + radius_y),
        fill=255,
    )
    mask = mask.filter(ImageFilter.GaussianBlur(.4))
    crop.putalpha(mask)
    crop.save(output, "WEBP", lossless=True, method=6)
    return crop


def _composite_clipped(base: Image.Image, iris: Image.Image, spec: EyeSpec) -> Image.Image:
    result = base.copy()
    if iris.size != (spec.iris_render_size, spec.iris_render_size):
        iris = iris.resize(
            (spec.iris_render_size, spec.iris_render_size),
            Image.Resampling.LANCZOS,
        )
    center_x, center_y = spec.forward_center
    left = round(center_x - iris.width / 2)
    top = round(center_y - iris.height / 2)
    eye_alpha = _eye_mask(spec).crop((left, top, left + iris.width, top + iris.height))
    clipped = iris.copy()
    clipped.putalpha(Image.fromarray(np.minimum(
        np.asarray(iris.getchannel("A")),
        np.asarray(eye_alpha),
    ).astype(np.uint8)))
    result.alpha_composite(clipped, (left, top))
    return result


def main():
    source = Image.open(IDLE_SOURCE).convert("RGBA")
    if source.size != (642, 702):
        raise ValueError(f"Unexpected idle artwork size: {source.size}")
    animation_frame = _load_animation_frame()
    eye_shape_reference = Image.open(EYE_SHAPE_REFERENCE).convert("RGBA")

    base = source.copy()
    for spec in EYES:
        base = _remove_iris(base, spec)
        base = _apply_reference_eye_patch(base, eye_shape_reference, spec)
    base.save(
        CHARACTER_DIR / "character-idle-eye-base.webp",
        "WEBP",
        lossless=True,
        method=6,
    )

    irises = []
    for spec in EYES:
        irises.append(_extract_iris(
            animation_frame,
            spec,
            CHARACTER_DIR / f"{spec.name}-iris.webp",
        ))

    forward = base.copy()
    for spec, iris in zip(EYES, irises):
        forward = _composite_clipped(forward, iris, spec)
    forward.save(
        CHARACTER_DIR / "character-idle-forward.webp",
        "WEBP",
        lossless=True,
        method=6,
    )


if __name__ == "__main__":
    main()
