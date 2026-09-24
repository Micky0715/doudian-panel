"""Print the bright sclera bounds in the v4 eye-shape reference."""

from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
IMAGE = ROOT / "assets" / "illustrations" / "aihuishou-ops-mascot-v4-wanwu.png"


def component(mask, seed):
    height, width = mask.shape
    queue = deque([seed])
    seen = np.zeros_like(mask, dtype=bool)
    seen[seed[1], seed[0]] = True
    points = []
    while queue:
        x, y = queue.popleft()
        if not mask[y, x]:
            continue
        points.append((x, y))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < width and 0 <= ny < height and not seen[ny, nx]:
                seen[ny, nx] = True
                queue.append((nx, ny))
    return points


def main():
    image = np.asarray(Image.open(IMAGE).convert("RGB"), dtype=np.float64)
    maximum = image.max(axis=2)
    minimum = image.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1)
    light = (minimum > 180) & (saturation < .16)
    for name, seed in (("left", (485, 405)), ("right", (635, 365))):
        points = component(light, seed)
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        print(name, len(points), (min(xs), min(ys), max(xs) + 1, max(ys) + 1))


if __name__ == "__main__":
    main()
