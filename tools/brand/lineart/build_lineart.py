"""Line-art LSPX-P1 icons (a flat alternative to the 3D render): black-and-white technical-drawing style.

Uses the box model and projection in iso_model.py (80 x 131 x 131 mm, rounded side
profile, the manual's ~24 deg dimetric view), with:
- line weights taken from the manual's pictogram: silhouette ~2.6x the inner lines;
- the panel/band seams drawn as the manual's double line, on both side panels;
- an optional perforation that wraps the whole visible band (front, rounded edges, top).

Run: python build_lineart.py [--png]   (needs Pillow; rsvg-convert on PATH for trimming and the optional PNGs)
"""

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import iso_model as iso
from iso_model import EX, EY, EZ, D, H, P, R, W, f, hull, poly, rounded_poly

OUT_W = 9.0  # silhouette, in final ~256-unit space (manual: ~16 px at 1030 px height, too thin to survive 24 px)
IN_W = 3.5  # seams, window, button (manual ratio 16:6 = 2.6:1)
SEAM_GAP = 6.0  # mm between the two lines of a seam
INK = "#1d1d1f"
BAND = "#e7e7e4"
GLASS = "#2a2b30"
DOT = "#a8a8ae"

# view direction (towards the viewer) for the visibility of band points: solves X*EX + Y*EY + Z*EZ = 0
_s, _k = -EY[1] / EY[0] * 0 + math.sin(iso.THETA), iso.K
VIEW = (1.0, -1.0, 2 * _s / _k)


def facing(ny, nz):
    """How much a band normal (0, ny, nz) faces the viewer (0 = grazing, at the silhouette)."""
    v = VIEW
    ln = math.hypot(*v)
    return (ny * v[1] + nz * v[2]) / ln


# --------------------------------------------------------------- profile helpers
PERIM_STRAIGHT = D - 2 * R


def profile_at(t):
    """Point and outward normal on the side profile by arc length t (from the bottom-front corner start)."""
    arc = math.pi * R / 2
    segs = [  # (kind, data); walking: bottom-front arc, bottom, bottom-back arc, back, top-back arc, top, top-front arc, front
        ("arc", ((R, R), 180)),
        ("line", ((R, 0), (D - R, 0), (0, -1))),
        ("arc", ((D - R, R), 270)),
        ("line", ((D, R), (D, H - R), (1, 0))),
        ("arc", ((D - R, H - R), 0)),
        ("line", ((D - R, H), (R, H), (0, 1))),
        ("arc", ((R, H - R), 90)),
        ("line", ((0, H - R), (0, R), (-1, 0))),
    ]
    total = 4 * arc + 2 * PERIM_STRAIGHT + 2 * (H - 2 * R)
    t %= total
    for kind, data in segs:
        ln = arc if kind == "arc" else math.dist(data[0], data[1])
        if t <= ln:
            if kind == "arc":
                (cy, cz), a0 = data
                a = math.radians(a0 + 90 * t / arc)
                return (cy + R * math.cos(a), cz + R * math.sin(a)), (math.cos(a), math.sin(a))
            (y0, z0), (y1, z1), n = data
            u = t / ln
            return (y0 + (y1 - y0) * u, z0 + (z1 - z0) * u), n
        t -= ln
    raise AssertionError


def visible_arc(x, margin=0.0):
    """Projected polyline of the profile at width x, only where the band faces the viewer."""
    prof = iso.profile(n=40)
    flags = []
    for y, z in prof:
        cy = min(max(y, R), D - R)
        cz = min(max(z, R), H - R)
        ny, nz = y - cy, z - cz
        ln = math.hypot(ny, nz) or 1
        flags.append(facing(ny / ln, nz / ln) > margin)
    # rotate so the run of visible points is contiguous
    start = next(i for i in range(len(prof)) if not flags[i] and flags[(i + 1) % len(prof)])
    order = [(start + 1 + i) % len(prof) for i in range(len(prof))]
    return [P(x, *prof[i]) for i in order if flags[i]][: sum(flags)]


def polyline(pts):
    return "M" + " L".join(f"{f(x)} {f(y)}" for x, y in pts)


# --------------------------------------------------------------- geometry
class Model:
    def __init__(self):
        prof = iso.profile()
        self.silhouette = hull([P(0, y, z) for y, z in prof] + [P(W, y, z) for y, z in prof])
        self.panel = [P(W, y, z) for y, z in prof]
        # seams: each side panel meets the band in a double line (panel edge + band edge)
        self.seams = [visible_arc(W - SEAM_GAP), visible_arc(SEAM_GAP * 1.6)]
        self.window3d = (12, W - 12, 26, 46)  # x0, x1, y0, y1 on the top
        x0, x1, y0, y1 = self.window3d
        self.window = [P(x, y, H) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
        # power button: a capsule on the front face, low down
        bx0, bx1, bz, br = W / 2 - 10, W / 2 + 10, 22, 3.4
        cap = []
        for i in range(13):
            a = math.radians(90 + 180 * i / 12)
            cap.append(P(bx0 + br * math.cos(a), 0, bz + br * math.sin(a)))
        for i in range(13):
            a = math.radians(-90 + 180 * i / 12)
            cap.append(P(bx1 + br * math.cos(a), 0, bz + br * math.sin(a)))
        self.button = cap
        self.button3d = (bx0 - br, bx1 + br, bz - br, bz + br)

    def mesh(self, pitch=9.0, r=1.55, margin=0.12):
        """Perforation dots over the whole visible band, as projected ellipses."""
        out = []
        perim = 4 * math.pi * R / 2 + 2 * (D - 2 * R) + 2 * (H - 2 * R)
        n_t = int(perim / pitch)
        xs = []
        x = SEAM_GAP + pitch * 0.9
        while x < W - SEAM_GAP - pitch * 0.9 + 1e-6:
            xs.append(x)
            x += pitch
        off = (W - (xs[-1] + xs[0])) / 2
        xs = [x + off for x in xs]
        wx0, wx1, wy0, wy1 = self.window3d
        bx0, bx1, bz0, bz1 = self.button3d
        for i in range(n_t):
            t = (i + 0.5) * perim / n_t
            (y, z), (ny, nz) = profile_at(t)
            fc = facing(ny, nz)
            if fc < margin:
                continue
            # tangent along the profile (direction of increasing t)
            ty, tz = -nz, ny
            for j, x in enumerate(xs):
                xx = x + (pitch / 2 if i % 2 else 0)  # staggered rows, like a perforated sheet
                if not (SEAM_GAP + pitch * 0.6 <= xx <= W - SEAM_GAP - pitch * 0.6):
                    continue
                if (
                    z > H - 0.01
                    and wx0 - pitch * 0.7 < xx < wx1 + pitch * 0.7
                    and wy0 - pitch * 0.7 < y < wy1 + pitch * 0.7
                ):
                    continue
                if (
                    y < 0.01
                    and bx0 - pitch * 0.6 < xx < bx1 + pitch * 0.6
                    and bz0 - pitch * 0.6 < z < bz1 + pitch * 0.6
                ):
                    continue
                cx, cy = P(xx, y, z)
                ax, ay = EX[0] * r, EX[1] * r
                bx, by = (EY[0] * ty + EZ[0] * tz) * r, (EY[1] * ty + EZ[1] * tz) * r
                out.append(f'<circle r="1" transform="matrix({f(ax)} {f(ay)} {f(bx)} {f(by)} {f(cx)} {f(cy)})"/>')
        return out


M = Model()


def icon(
    *,
    band="#ffffff",
    panel="#ffffff",
    window_fill="none",
    button_fill="none",
    dots=False,
    seams=True,
    out_w=OUT_W,
    in_w=IN_W,
    ink=INK,
    dot=DOT,
):
    o = [f'<path d="{poly(M.silhouette)}" fill="{band}"/>']
    if dots:
        o.append(f'<g fill="{dot}">' + "".join(M.mesh()) + "</g>")
    o.append(
        f'<path d="{rounded_poly(M.window, 3)}" fill="{window_fill}" stroke="{ink}" stroke-width="{f(in_w)}" stroke-linejoin="round"/>'
    )
    o.append(
        f'<path d="{poly(M.button)}" fill="{button_fill}" stroke="{ink}" stroke-width="{f(in_w)}" stroke-linejoin="round"/>'
    )
    if seams:
        for s in M.seams:
            o.append(
                f'<path d="{polyline(s)}" fill="none" stroke="{ink}" stroke-width="{f(in_w)}" stroke-linecap="round"/>'
            )
    o.append(
        f'<path d="{poly(M.panel)}" fill="{panel}" stroke="{ink}" stroke-width="{f(in_w)}" stroke-linejoin="round"/>'
    )
    o.append(
        f'<path d="{poly(M.silhouette)}" fill="none" stroke="{ink}" stroke-width="{f(out_w)}" stroke-linejoin="round"/>'
    )
    return "\n".join(o)


VARIANTS = [
    ("a1-line", lambda: icon()),
    ("a2-line-mesh", lambda: icon(dots=True)),
    ("a3-two-tone", lambda: icon(band=BAND)),
    ("a4-two-tone-glass", lambda: icon(band=BAND, window_fill=GLASS, button_fill=INK)),
    ("a5-line-glass", lambda: icon(window_fill=GLASS, button_fill=INK)),
]


def main():
    png = "--png" in sys.argv[1:]
    for name, fn in VARIANTS:
        content = fn()
        vb = iso.trimmed_viewbox(content)
        text = iso.svg_doc(content, vb)
        (HERE / f"{name}.svg").write_text(text)
        if png:
            iso.rsvg(text, 256).save(HERE / f"{name}.png", optimize=True)
            iso.rsvg(text, 512).save(HERE / f"{name}@2x.png", optimize=True)
        print(name, [round(v, 1) for v in vb])


if __name__ == "__main__":
    main()
