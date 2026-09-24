"""Validation shared by Feishu-backed main-area image selection.

The DouDian synchronous material endpoint has a 3 MiB size limit. The browser
path also requires its main-area pictures to be square and at least 600 px.
Keep those source rules here so a cloud file cannot bypass them merely because
it did not originate on disk.
"""

from __future__ import annotations

import struct


ALLOWED_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
MAX_SYNC_IMAGE_BYTES = 3 * 1024 * 1024
MIN_MAIN_AREA_PIXELS = 600


def image_dimensions(data: bytes) -> tuple[int, int]:
    """Return raster dimensions without loading an image-processing package."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        offset = 2
        while offset < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            offset += 1
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD8, 0xD9):
                continue
            if offset + 2 > len(data):
                break
            segment_length = struct.unpack(">H", data[offset:offset + 2])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                if offset + 7 <= len(data):
                    height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                    return width, height
                break
            offset += max(2, segment_length)
    # Extended WebP has explicit 24-bit canvas dimensions in VP8X.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8X" and len(data) >= 30:
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    return (0, 0)


def main_area_image_error(name: str, data: bytes) -> str | None:
    """Return a human-readable rejection reason, or ``None`` when valid."""
    clean_name = str(name or "").strip()
    suffix = "." + clean_name.rsplit(".", 1)[-1].casefold() if "." in clean_name else ""
    if suffix not in ALLOWED_IMAGE_SUFFIXES:
        return "图片格式不支持，仅支持 jpg/jpeg/png/webp"
    if not data:
        return "图片为空"
    if len(data) > MAX_SYNC_IMAGE_BYTES:
        return "图片超过抖店同步上传 3MB 限制"
    width, height = image_dimensions(data)
    if width < MIN_MAIN_AREA_PIXELS or height < MIN_MAIN_AREA_PIXELS or width != height:
        return "主图区图片必须为不小于 600×600 的正方形（当前 %d×%d）" % (width, height)
    return None
