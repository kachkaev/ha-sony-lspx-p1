"""Shared model for the line-art icons: an original isometric drawing of the LSPX-P1 (80 x 131 x 131 mm).

Drawn after the proportions and viewing angle of the pictograms in Sony's Reference Guide (pages 9GB / 11GB),
but every outline is computed from the box model below; nothing is traced.

The body is modelled as the real 80 x 131 x 131 mm box: the side profile is a rounded square
(131 x 131 mm, corner radius ~22 mm), extruded across the 80 mm width. It is projected with
the manual's near-symmetric dimetric angle (both ground axes ~24 deg from horizontal). Every
outline comes from this model; nothing is traced.

Used by build_lineart.py.
"""

import io
import math
import subprocess
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent

# ----------------------------------------------------------------- model
W, D, H = 80.0, 131.0, 131.0  # width (front face), depth, height in mm
R = 22.0  # rounding of the side profile
THETA = math.radians(24)
S = 1.15  # projection scale, chosen so the trimmed drawing is ~256 units tall
K = 1.05  # vertical stretch (the manual draws height a touch taller than isometric)
EX = (S * math.cos(THETA), S * math.sin(THETA))  # +X: along the front face, towards the near edge
EY = (S * math.cos(THETA), -S * math.sin(THETA))  # +Y: from the front to the back
EZ = (0.0, -K * S)  # +Z: up

SW = 14  # outline width in final units (~1.3 px at 24 px)
INK = "#2e2e33"
DOT = "#9b9ba1"


def f(v):
    return f"{v:.2f}".rstrip("0").rstrip(".")


def P(x, y, z, o=(0.0, 0.0)):
    return (o[0] + x * EX[0] + y * EY[0] + z * EZ[0], o[1] + x * EX[1] + y * EY[1] + z * EZ[1])


def profile(n=28, part="all"):
    """Rounded-square side profile in (Y, Z), counter-clockwise from the bottom-front corner arc."""
    corners = [  # centre, start angle (deg) of each 90-degree arc, walking around the profile
        ((R, R), 180),  # bottom-front
        ((D - R, R), 270),  # bottom-back
        ((D - R, H - R), 0),  # top-back
        ((R, H - R), 90),  # top-front
    ]
    pts = []
    for (cy, cz), a0 in corners:
        for i in range(n + 1):
            a = math.radians(a0 + 90 * i / n)
            pts.append((cy + R * math.cos(a), cz + R * math.sin(a), a0, i / n))
    if part == "all":
        return [(y, z) for y, z, *_ in pts]
    # 'top': from the middle of the top-front arc, over the top, to the middle of the top-back arc
    # 'front': from the middle of the bottom-front arc, up the front, to the middle of the top-front arc
    out = []
    for y, z, a0, t in pts:
        if part == "top" and ((a0 == 90 and t <= 0.5) or (a0 == 0 and t >= 0.5)):
            out.append((y, z))
        if part == "front" and ((a0 == 90 and t >= 0.5) or (a0 == 180 and t <= 0.5)):
            out.append((y, z))
    return out


def hull(points):
    pts = sorted(set((round(x, 3), round(y, 3)) for x, y in points))
    if len(pts) < 3:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]


def poly(pts):
    return "M" + " L".join(f"{f(x)} {f(y)}" for x, y in pts) + " Z"


def rounded_poly(pts, r):
    n = len(pts)
    segs = []
    for i in range(n):
        p0, p1, p2 = pts[i - 1], pts[i], pts[(i + 1) % n]

        def towards(a, b, d):
            dx, dy = b[0] - a[0], b[1] - a[1]
            ln = math.hypot(dx, dy)
            return (a[0] + dx / ln * d, a[1] + dy / ln * d)

        segs.append((towards(p1, p0, r), p1, towards(p1, p2, r)))
    d = f"M{f(segs[0][2][0])} {f(segs[0][2][1])}"
    for a, c, b in segs[1:] + segs[:1]:
        d += f" L{f(a[0])} {f(a[1])} Q{f(c[0])} {f(c[1])} {f(b[0])} {f(b[1])}"
    return d + " Z"


class Box:
    def __init__(self, o=(0.0, 0.0)):
        self.o = o
        prof = profile()
        self.silhouette = hull([P(0, y, z, o) for y, z in prof] + [P(W, y, z, o) for y, z in prof])
        self.panel = [P(W, y, z, o) for y, z in prof]  # right side panel (visible)
        self.top = hull([P(x, y, z, o) for x in (0, W) for y, z in profile(part="top")])
        self.front = hull([P(x, y, z, o) for x in (0, W) for y, z in profile(part="front")])
        # projection window: on the top, just behind the rounded front edge
        self.window = [P(x, y, H, o) for x, y in ((12, 26), (W - 12, 26), (W - 12, 46), (12, 46))]
        # power button: small pill low on the front face
        self.button = (P(W / 2 - 9, 0, 24, o), P(W / 2 + 9, 0, 24, o))
        # fabric dot grid on the front face (4 across x 4 up)
        self.dots = [P(x, 0, z, o) for z in (48, 66, 84, 102) for x in (17, 32, 48, 63)]

    def window_path(self, r=3.5):
        return rounded_poly(self.window, r)


# ------------------------------------------------------------- rendering
def svg_doc(content, vb):
    x, y, s = vb
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{f(x)} {f(y)} {f(s)} {f(s)}">\n{content}\n</svg>\n'


def rsvg(svg_text, size):
    png = subprocess.run(
        ["rsvg-convert", "-w", str(size), "-h", str(size), "-f", "png"],
        input=svg_text.encode(),
        capture_output=True,
        check=True,
    ).stdout
    return Image.open(io.BytesIO(png)).convert("RGBA")


def trimmed_viewbox(content):
    pad, scale, big = 400, 4, 1000
    img = rsvg(svg_doc(content, (-pad, -pad, big)), big * scale)
    l, t, r, b = img.getchannel("A").point(lambda a: 255 if a > 6 else 0).getbbox()
    l, t, r, b = (v / scale - pad for v in (l, t, r, b))
    side = max(r - l, b - t)
    return (l - (side - (r - l)) / 2, t - (side - (b - t)) / 2, side)
