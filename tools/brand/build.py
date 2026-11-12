"""Build the LSPX-P1 brand icon, the README header lockup and (optionally) the glTF model from scene.html.

Everything is relative to this folder or passed as arguments. Needs Python 3.9+ with Pillow, a
Chromium-based browser that can run headless (Chrome, Chromium or Playwright's chrome-headless-shell),
and network access to cdn.jsdelivr.net (three.js 0.170.0) and brands.home-assistant.io (HA logo).

  python build.py                       # out/brand/icon.png, icon@2x.png and out/readme-header.png
  python build.py --mocks               # + light/dark previews and GitHub README mock-ups
  python build.py --glb                 # + lspx-p1.glb next to this script
  python build.py --shadow subtle       # other shadow strengths: none, extra-subtle, subtle, medium, strong
  python build.py --chrome /path/to/chrome --out /some/dir
"""

import argparse
import base64
import glob
import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
HA_LOGO_URL = "https://brands.home-assistant.io/homeassistant/icon@2x.png"  # downloaded at build time, not committed

RENDER_PX = 2048  # the scene is rendered at this size, then downsampled
ICON_PAD = 0.02  # padding on each side of the trimmed icon

# README header lockup (optical alignment, in units of the HA house height HH)
HH = 500  # HA house height in the lockup image (px)
BOX_SCALE = 1.10  # the projector body is 10% taller than the house...
BOX_TOP_OVER = 0.04  # ...its top vertex 4% above the roof apex (so its bottom vertex is 6% below the base)
GAP = 0.30  # gap between the logos


def find_chrome(explicit):
    if explicit:
        return explicit
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    cands = sorted(
        glob.glob(
            os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        )
    )
    cands += sorted(
        glob.glob(
            os.path.expanduser(
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        )
    )
    cands += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ]
    cands += [shutil.which(n) for n in ("chrome-headless-shell", "chromium", "chromium-browser", "google-chrome")]
    for c in reversed([c for c in cands if c]):
        if os.path.exists(c):
            return c
    raise SystemExit("No Chromium found: pass --chrome or set CHROME")


def chrome_args(chrome, size, extra):
    return [
        chrome,
        "--headless",
        "--hide-scrollbars",
        "--force-device-scale-factor=1",
        f"--window-size={size},{size}",
        "--default-background-color=00000000",
        "--enable-unsafe-swiftshader",
        "--virtual-time-budget=30000",
        *extra,
    ]


def render(chrome, shadow, tmp):
    out = Path(tmp) / f"raw-{shadow}.png"
    url = (HERE / "scene.html").as_uri() + f"?size={RENDER_PX}&shadow={shadow}"
    subprocess.run(chrome_args(chrome, RENDER_PX, [f"--screenshot={out}", url]), check=True, capture_output=True)
    img = Image.open(out).convert("RGBA")
    if img.getchannel("A").getbbox() is None:
        raise SystemExit("Empty render: is cdn.jsdelivr.net reachable?")
    return img


def square_icon(img):
    """Square, trimmed to every non-transparent pixel (so no shadow is clipped), with ICON_PAD padding."""
    img = img.crop(img.getchannel("A").getbbox())
    side = round(max(img.size) / (1 - 2 * ICON_PAD))
    out = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    out.alpha_composite(img, ((side - img.width) // 2, (side - img.height) // 2))
    return out.resize((1024, 1024), Image.LANCZOS)


def edge_alpha(img):
    a = img.getchannel("A")
    w, h = a.size
    return max(a.crop(b).getextrema()[1] for b in [(0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h)])


def lockup(icon1024, ha_png_bytes):
    ha = Image.open(io.BytesIO(ha_png_bytes)).convert("RGBA")
    ha = ha.crop(ha.getchannel("A").point(lambda a: 255 if a > 4 else 0).getbbox())
    ha = ha.resize((round(ha.width * HH / ha.height), HH), Image.LANCZOS)
    mark = icon1024
    body = mark.getchannel("A").point(lambda a: 255 if a > 90 else 0).getbbox()  # the box without its shadow
    full = mark.getchannel("A").getbbox()
    k = round(HH * BOX_SCALE) / (body[3] - body[1])
    mark = mark.resize((round(mark.width * k), round(mark.height * k)), Image.LANCZOS)
    body = [round(v * k) for v in body]
    full = [round(v * k) for v in full]
    my = -round(HH * BOX_TOP_OVER) - body[1]
    mx = ha.width + round(HH * GAP) - body[0]
    top = min(0, my + full[1])
    bottom = max(HH, my + full[3])
    canvas = Image.new("RGBA", (mx + full[2], bottom - top), (0, 0, 0, 0))
    canvas.alpha_composite(ha, (0, -top))
    canvas.alpha_composite(mark, (mx, my - top))
    return canvas


def on_bg(img, bg, pad=40):
    t = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), bg)
    t.alpha_composite(img, (pad, pad))
    return t.convert("RGB")


MOCK = """<!doctype html><html><head><meta charset="utf-8"><style>
body {{ margin:0; background:{bg}; color:{fg}; font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif; }}
a {{ color:{link}; text-decoration:none; }}
.box {{ width:880px; margin:24px auto; border:1px solid {border}; border-radius:6px; }}
.head {{ background:{head}; border-bottom:1px solid {border}; padding:8px 16px; font-size:14px; font-weight:600; border-radius:6px 6px 0 0; }}
.md {{ padding:32px; }}
h1 {{ font-size:2em; font-weight:600; margin:0 0 16px; padding-bottom:.3em; border-bottom:1px solid {border}; line-height:1.25; }}
p {{ margin:0 0 16px; }}
</style></head><body><div class="box"><div class="head">README.md</div><div class="md">
<h1>Sony LSPX-P1 Projector for Home Assistant</h1>
<p align="center"><img src="{src}" alt="Home Assistant logo and Sony LSPX-P1 projector" height="{h}"></p>
<p>Control the <a href="#">Sony LSPX-P1 portable ultra short throw projector</a> from Home Assistant over Bluetooth,
without the smartphone app and without putting the projector on Wi-Fi. Everything on the app's control panel is
available, plus picture settings and battery status.</p>
</div></div></body></html>"""
THEMES = {
    "light": dict(bg="#ffffff", fg="#1f2328", border="#d1d9e0", head="#f6f8fa", link="#0969da"),
    "dark": dict(bg="#0d1117", fg="#f0f6fc", border="#3d444d", head="#151b23", link="#4493f8"),
}


def export_glb(chrome, dest):
    url = (HERE / "scene.html").as_uri() + "?size=64&shadow=none&export=glb"
    dom = subprocess.run(
        chrome_args(chrome, 64, ["--dump-dom", url]), check=True, capture_output=True, text=True
    ).stdout
    m = re.search(r'<pre id="glb">([^<]+)</pre>', dom)
    if not m:
        raise SystemExit("glTF export failed (no <pre id=glb> in the page)")
    dest.write_bytes(base64.b64decode(html.unescape(m.group(1))))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chrome")
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--shadow", default="medium", choices=["none", "extra-subtle", "subtle", "medium", "strong"])
    ap.add_argument("--mocks", action="store_true", help="also write light/dark previews and README mock-ups")
    ap.add_argument("--glb", action="store_true", help="also export lspx-p1.glb next to this script")
    ap.add_argument("--readme-height", type=int, default=112, help="img height used in the README mock-ups")
    a = ap.parse_args()
    chrome = find_chrome(a.chrome)
    out = Path(a.out)
    (out / "brand").mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        icon = square_icon(render(chrome, a.shadow, tmp))
        for name, size in (("icon.png", 256), ("icon@2x.png", 512)):
            im = icon.resize((size, size), Image.LANCZOS)
            assert edge_alpha(im) == 0, f"{name}: shadow touches the edge"
            im.save(out / "brand" / name, optimize=True)

        req = urllib.request.Request(HA_LOGO_URL, headers={"User-Agent": "lspx-p1-icon-build"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ha = r.read()
        header = lockup(icon, ha)
        header.save(out / "readme-header.png", optimize=True)

        if a.mocks:
            for theme, c in THEMES.items():
                on_bg(header, c["bg"]).save(out / f"readme-header-on-{theme}.png", optimize=True)
                page = Path(tmp) / f"mock-{theme}.html"
                shutil.copy(out / "readme-header.png", Path(tmp) / "readme-header.png")
                page.write_text(MOCK.format(src="readme-header.png", h=a.readme_height, **c))
                subprocess.run(
                    [
                        chrome,
                        "--headless",
                        "--hide-scrollbars",
                        "--force-device-scale-factor=2",
                        "--window-size=944,520",
                        f"--screenshot={out / f'readme-mock-{theme}.png'}",
                        page.as_uri(),
                    ],
                    check=True,
                    capture_output=True,
                )

    if a.glb:
        export_glb(chrome, HERE / "lspx-p1.glb")
    print("wrote", out)


if __name__ == "__main__":
    main()
