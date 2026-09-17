"""Shared LS Companion icon artwork."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw


_BOLT_COLORS = {
    "closed": ((148, 163, 184), (148, 163, 184)),
    "open": ((30, 64, 175), (96, 205, 255)),
    "scaling": ((180, 110, 0), (255, 225, 70)),
}


def _mix_color(start: tuple[int, int, int], end: tuple[int, int, int], amount: float):
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + ((b - a) * amount)) for a, b in zip(start, end))


def create_lightning_icon(
    width: int = 64,
    height: int = 64,
    *,
    state: str = "open",
    pulse: float = 1.0,
) -> Image.Image:
    """Render the shared app icon for a closed, open, or scaling LS state."""
    if state not in _BOLT_COLORS:
        raise ValueError(f"Unknown lightning icon state: {state}")
    image = Image.new("RGBA", (width, height), color=(0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    stroke = max(1, round(min(width, height) / 16))
    inset = max(1, round(min(width, height) / 16))
    draw.ellipse(
        (inset, inset, width - inset, height - inset),
        fill=(15, 23, 42),
        outline=(255, 255, 255),
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
    draw.polygon(points, fill=_mix_color(*_BOLT_COLORS[state], pulse))
    return image


def lightning_icon_png(size: int = 64) -> bytes:
    stream = BytesIO()
    create_lightning_icon(size, size).save(stream, format="PNG")
    return stream.getvalue()
