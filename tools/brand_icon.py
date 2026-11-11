"""Draw the integration's brand icon (an original drawing, not a Sony or Home Assistant logo).

A portable ultra short throw projector standing upright, casting an image onto the wall above it.
Run: .venv/bin/python tools/brand_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "custom_components" / "sony_lspx_p1" / "brand"
SCALE = 4  # draw large, then downsample for smooth edges


def draw(size: int) -> Image.Image:
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1):
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    # Projected image on the wall
    d.rounded_rectangle(box(0.10, 0.06, 0.90, 0.40), radius=0.04 * s, fill=(242, 140, 40, 255))
    d.rounded_rectangle(box(0.15, 0.11, 0.85, 0.35), radius=0.025 * s, fill=(255, 196, 120, 255))
    # Light beam from the projection window up to the image
    d.polygon(
        [(0.36 * s, 0.56 * s), (0.64 * s, 0.56 * s), (0.88 * s, 0.40 * s), (0.12 * s, 0.40 * s)],
        fill=(255, 214, 160, 150),
    )
    # Projector body
    d.rounded_rectangle(
        box(0.30, 0.54, 0.70, 0.95),
        radius=0.07 * s,
        fill=(236, 236, 232, 255),
        outline=(60, 60, 60, 255),
        width=int(0.025 * s),
    )
    # Projection window on top
    d.rounded_rectangle(box(0.36, 0.585, 0.64, 0.64), radius=0.02 * s, fill=(40, 40, 44, 255))
    # Speaker grille dots
    for i in range(3):
        for j in range(4):
            cx, cy = (0.415 + j * 0.057) * s, (0.74 + i * 0.055) * s
            r = 0.012 * s
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(150, 150, 150, 255))

    # Trim to the drawing (the brand guidelines ask for no empty padding), keep it square.
    left, top, right, bottom = img.getbbox()
    side = max(right - left, bottom - top)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(img.crop((left, top, right, bottom)), ((side - (right - left)) // 2, (side - (bottom - top)) // 2))
    return square.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    draw(256).save(OUT / "icon.png", optimize=True)
    draw(512).save(OUT / "icon@2x.png", optimize=True)
    print("wrote", OUT)
