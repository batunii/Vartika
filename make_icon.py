"""Draw the application icon.

A vārttika is a note written against an existing rule, so the mark is exactly
that: lines of text with one of them annotated in the margin. Kept to two
shapes because the icon is mostly seen at 16 pixels, where anything finer
turns to mud.

    python make_icon.py        # writes icon.ico and icon.png
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZES = [256, 128, 64, 48, 32, 16]

BACKDROP = (27, 30, 36)       # the app's panel colour
TEXT = (176, 186, 200)
ACCENT = (110, 168, 254)      # the app's accent blue


def draw(size: int) -> Image.Image:
    # Drawn large and downsampled: small text bars alias badly otherwise.
    scale = 8
    px = size * scale
    image = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)

    radius = int(px * 0.22)
    pen.rounded_rectangle([0, 0, px - 1, px - 1], radius=radius, fill=BACKDROP)

    # Four lines of "text", with the second one shortened to leave room for
    # the annotation sitting against it.
    left = px * 0.30
    right = px * 0.80
    thickness = px * 0.070
    top = px * 0.28
    gap = px * 0.145
    for row in range(4):
        y = top + row * gap
        end = right - (px * 0.16 if row == 1 else 0)
        pen.rounded_rectangle([left, y, end, y + thickness],
                              radius=thickness / 2, fill=TEXT)

    # The mark in the margin: a bold stroke against the annotated line.
    mark_x = px * 0.17
    mark_top = top + gap - px * 0.03
    mark_bottom = top + gap * 2 + thickness + px * 0.03
    width = px * 0.075
    pen.rounded_rectangle([mark_x, mark_top, mark_x + width, mark_bottom],
                          radius=width / 2, fill=ACCENT)

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    images = [draw(s) for s in SIZES]
    largest = images[0]
    largest.save(HERE / "icon.png")
    # Pillow writes every requested size into one .ico.
    largest.save(HERE / "icon.ico", sizes=[(s, s) for s in SIZES])
    print(f"wrote {HERE / 'icon.png'} and {HERE / 'icon.ico'} ({', '.join(map(str, SIZES))}px)")


if __name__ == "__main__":
    main()
