"""Shared LS Companion icon artwork."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw


def create_lightning_icon(width: int = 64, height: int = 64) -> Image.Image:
    image = Image.new("RGBA", (width, height), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(1, round(min(width, height) / 16))
    inset = max(1, round(min(width, height) / 16))
    draw.ellipse(
        (inset, inset, width - inset, height - inset),
        fill=(15, 23, 42),
        outline=(14, 165, 233),
        width=stroke,
    )
    points = [
        (width * 0.55, height * 0.18),
        (width * 0.30, height * 0.52),
        (width * 0.48, height * 0.52),
        (width * 0.42, height * 0.82),
        (width * 0.70, height * 0.44),
        (width * 0.52, height * 0.44),
    ]
    draw.polygon(points, fill=(56, 189, 248))
    return image


def lightning_icon_png(size: int = 64) -> bytes:
    stream = BytesIO()
    create_lightning_icon(size, size).save(stream, format="PNG")
    return stream.getvalue()
