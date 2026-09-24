"""Render forward and four extreme gaze positions for visual QA."""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from scripts.generate_character_eye_assets import CHARACTER_DIR, EYES, _eye_mask


OUTPUT_DIR = Path("/private/tmp/mascot-eye-proof")
OUTPUT = OUTPUT_DIR / "mascot-eye-validation.png"
VISIBLE_IRIS = {"left": (21, 26), "right": (19, 26)}
DIRECTIONS = (
    ("FORWARD", 0.0, 0.0),
    ("LEFT", -1.0, 0.0),
    ("RIGHT", 1.0, 0.0),
    ("UP", 0.0, -1.0),
    ("DOWN", 0.0, 1.0),
)


def on_background(image):
    background = Image.new("RGBA", image.size, "#fafbf8")
    background.alpha_composite(image)
    return background.convert("RGB")


def render_direction(base, irises, direction_x, direction_y):
    result = base.copy()
    for spec, iris in zip(EYES, irises):
        iris = iris.resize(
            (spec.iris_render_size, spec.iris_render_size),
            Image.Resampling.LANCZOS,
        )
        eye_width = spec.box[2] - spec.box[0]
        eye_height = spec.box[3] - spec.box[1]
        source_width, source_height = VISIBLE_IRIS[spec.name]
        iris_width = source_width * spec.iris_render_size / 32
        iris_height = source_height * spec.iris_render_size / 32
        max_x = ((eye_width - iris_width) / 2 - 3) * .85
        max_y = ((eye_height - iris_height) / 2 - 3) * .75
        if spec.name == "right" and direction_y > 0:
            max_y = max(0, max_y - 1.5)
        target_x = direction_x * max_x
        target_y = direction_y * max_y
        nose_direction = 1 if spec.name == "left" else -1
        if target_x * nose_direction > 0:
            target_x *= .93
        center_x = spec.forward_center[0] + target_x
        center_y = spec.forward_center[1] + target_y
        left = round(center_x - iris.width / 2)
        top = round(center_y - iris.height / 2)
        eye_alpha = _eye_mask(spec).crop(
            (left, top, left + iris.width, top + iris.height)
        )
        clipped = iris.copy()
        clipped.putalpha(Image.fromarray(np.minimum(
            np.asarray(iris.getchannel("A")),
            np.asarray(eye_alpha),
        ).astype(np.uint8)))
        result.alpha_composite(clipped, (left, top))
    return result


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = Image.open(CHARACTER_DIR / "character-idle-eye-base.webp").convert("RGBA")
    irises = [
        Image.open(CHARACTER_DIR / "left-iris.webp").convert("RGBA"),
        Image.open(CHARACTER_DIR / "right-iris.webp").convert("RGBA"),
    ]
    on_background(base).save(OUTPUT_DIR / "01-base-only.png")
    sheet = Image.new("RGB", (963, 750), "#fafbf8")
    draw = ImageDraw.Draw(sheet)
    for index, (label, direction_x, direction_y) in enumerate(DIRECTIONS):
        panel = render_direction(base, irises, direction_x, direction_y)
        full_panel = on_background(panel)
        full_panel.save(OUTPUT_DIR / f"{index + 2:02d}-{label.lower()}.png")
        panel = full_panel.resize((321, 351))
        column = index % 3
        row = index // 3
        x = column * 321
        y = row * 375
        sheet.paste(panel, (x, y + 24))
        draw.text((x + 10, y + 5), label, fill="#183421")
    sheet.save(OUTPUT)
    print(OUTPUT)


if __name__ == "__main__":
    main()
