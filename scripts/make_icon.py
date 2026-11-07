"""Generate the application icon from the Onion mark.

The mark is the same one the GUI draws: a circle split down the middle, an onion
cross-section on the left and a two-star constellation on the right. Rendering it
here keeps the taskbar icon recognisably the same product instead of Python's
default.

Run once and commit the .ico; the build does not depend on Pillow at runtime.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
PRIMARY = (146, 206, 245, 255)  # M3 dark-theme primary, matches the UI accent
BACKGROUND = (15, 20, 23, 255)  # matches background_color in main.py


def star(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, width: int) -> None:
    """A four-point sparkle with concave sides, drawn as a polygon outline."""
    k = r * 0.19
    points = [
        (cx, cy - r),
        (cx + k, cy - k),
        (cx + r, cy),
        (cx + k, cy + k),
        (cx, cy + r),
        (cx - k, cy + k),
        (cx - r, cy),
        (cx - k, cy - k),
    ]
    draw.polygon(points, outline=PRIMARY, width=width)


def render(size: int = SIZE) -> Image.Image:
    scale = 4  # supersample, then downscale for smooth edges
    canvas = size * scale
    image = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    pad = canvas * 0.06
    cx = cy = canvas / 2
    outer_r = canvas / 2 - pad

    # Rounded-square plate so the icon reads on light and dark taskbars.
    plate_r = canvas * 0.22
    draw.rounded_rectangle(
        [pad * 0.4, pad * 0.4, canvas - pad * 0.4, canvas - pad * 0.4],
        radius=plate_r,
        fill=BACKGROUND,
    )

    # Outer silhouette.
    draw.ellipse(
        [cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r],
        outline=(*PRIMARY[:3], 92),
        width=max(2, int(canvas * 0.006)),
    )

    # Left half: concentric onion rings, decreasing weight outward-in.
    rings = [(0.855, 0.010), (0.700, 0.009), (0.548, 0.008), (0.394, 0.007),
             (0.250, 0.006), (0.123, 0.005)]
    for ratio, weight in rings:
        r = outer_r * ratio
        w = max(2, int(canvas * weight))
        draw.arc(
            [cx - r, cy - r, cx + r, cy + r],
            start=90,
            end=270,
            fill=(*PRIMARY[:3], 235),
            width=w,
        )

    # Right half: the two-star constellation.
    star(draw, cx + outer_r * 0.40, cy - outer_r * 0.36, outer_r * 0.30,
         max(3, int(canvas * 0.011)))
    star(draw, cx + outer_r * 0.63, cy + outer_r * 0.42, outer_r * 0.16,
         max(2, int(canvas * 0.008)))

    # Vertical split axis.
    draw.line(
        [(cx, cy - outer_r * 0.97), (cx, cy + outer_r * 0.97)],
        fill=(*PRIMARY[:3], 150),
        width=max(2, int(canvas * 0.006)),
    )

    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    out_dir = Path(__file__).resolve().parents[1] / "onion_harness" / "app" / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    image = render(SIZE)
    ico_path = out_dir / "onion.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    image.save(ico_path, format="ICO", sizes=sizes)
    png_path = out_dir / "onion.png"
    image.save(png_path, format="PNG")
    print(f"wrote {ico_path} ({ico_path.stat().st_size} bytes)")
    print(f"wrote {png_path} ({png_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()