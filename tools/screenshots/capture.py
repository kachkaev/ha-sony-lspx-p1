"""Take the README screenshots from the demo Home Assistant (see demo_ha.py).

Drives a headless Chromium over the DevTools protocol, so the shots are identical from run
to run: same viewport, 2x pixel density, light colour scheme, English, and the same crop
rules (each card cut along its own edges plus a fixed margin of dashboard background).

    .venv/bin/python tools/screenshots/demo_ha.py      # in one terminal
    .venv/bin/python tools/screenshots/capture.py      # in another

Writes docs/images/card-*.png and docs/images/device-page.png. See README.md here.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OUT = REPO / "docs" / "images"
BASE = "http://127.0.0.1:8123"
DEBUG_PORT = 9333

# Crop rules (CSS pixels; the files are 2x):
VIEWPORT = (1440, 1100)  # three dashboard columns of ~420 px
CARD_MARGIN = 16  # dashboard background kept around each card
DIALOG_MARGIN = 24
DEVICE_VIEWPORT = (1240, 1100)  # device page: two columns of cards; the shot is the page right of the sidebar
SCALE = 2

# Finds elements through shadow roots, which document.querySelectorAll cannot.
DEEP_QUERY = """
function deepQueryAll(selector, root = document) {
  const found = [];
  const walk = (node) => {
    if (node.querySelectorAll) found.push(...node.querySelectorAll(selector));
    const all = node.querySelectorAll ? node.querySelectorAll("*") : [];
    for (const el of all) if (el.shadowRoot) walk(el.shadowRoot);
    if (node.shadowRoot) walk(node.shadowRoot);
  };
  walk(root);
  return [...new Set(found)];
}
"""


def find_chrome() -> str:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    candidates = sorted(
        glob.glob(
            os.path.expanduser(
                "~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        )
        + glob.glob(
            os.path.expanduser(
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell"
            )
        )
    )
    candidates += [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("chromium") or "",
        shutil.which("google-chrome") or "",
    ]
    for candidate in reversed(candidates):
        if candidate and os.path.exists(candidate):
            return candidate
    raise SystemExit("No Chromium found: set CHROME=/path/to/chrome")


class Page:
    """Just enough of the DevTools protocol for screenshots."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        self.ws = ws
        self.next_id = 0

    async def send(self, method: str, **params) -> dict:
        self.next_id += 1
        msg_id = self.next_id
        await self.ws.send_str(json.dumps({"id": msg_id, "method": method, "params": params}))
        while True:
            msg = json.loads((await self.ws.receive()).data)
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def evaluate(self, expression: str):
        result = await self.send(
            "Runtime.evaluate", expression=DEEP_QUERY + expression, awaitPromise=True, returnByValue=True
        )
        if "exceptionDetails" in result:
            raise RuntimeError(result["exceptionDetails"])
        return result["result"].get("value")

    async def wait_for(self, expression: str, seconds: float = 30) -> None:
        for _ in range(int(seconds * 4)):
            if await self.evaluate(expression):
                return
            await asyncio.sleep(0.25)
        raise TimeoutError(expression)

    async def goto(self, url: str) -> None:
        await self.send("Page.navigate", url=url)
        await self.wait_for("!!document.querySelector('home-assistant')?.hass?.states")

    async def shoot(self, rect: dict, margin: int, path: Path) -> None:
        x = max(0, rect["x"] - margin)
        y = max(0, rect["y"] - margin)
        clip = {"x": x, "y": y, "width": rect["width"] + 2 * margin, "height": rect["height"] + 2 * margin, "scale": 1}
        shot = await self.send("Page.captureScreenshot", format="png", clip=clip, captureBeyondViewport=True)
        path.write_bytes(base64.b64decode(shot["data"]))  # noqa: ASYNC240
        print(f"wrote {path.relative_to(REPO)} ({int(clip['width'] * SCALE)}x{int(clip['height'] * SCALE)})")


RECT = "(el => { const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, width: r.width, height: r.height}; })"


async def capture(scheme: str, base: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = tempfile.mkdtemp(prefix="lspx-shots-")
    chrome = subprocess.Popen(  # noqa: ASYNC220
        [
            find_chrome(),
            "--headless=new",
            f"--remote-debugging-port={DEBUG_PORT}",
            f"--user-data-dir={profile}",
            "--hide-scrollbars",
            "--lang=en-GB",
            "--no-first-run",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(50):
                try:
                    async with session.put(f"http://127.0.0.1:{DEBUG_PORT}/json/new?about:blank") as resp:
                        target = await resp.json()
                    break
                except aiohttp.ClientError:
                    await asyncio.sleep(0.2)
            async with session.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
                page = Page(ws)
                await page.send("Page.enable")
                await page.send("Runtime.enable")
                width, height = VIEWPORT
                await page.send(
                    "Emulation.setDeviceMetricsOverride",
                    width=width,
                    height=height,
                    deviceScaleFactor=SCALE,
                    mobile=False,
                )
                await page.send(
                    "Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": scheme}]
                )

                # Dashboard cards: compact, standard, full (in that order on the demo dashboard).
                await page.goto(f"{base}/projector/projector")
                await page.wait_for(
                    "deepQueryAll('sony-lspx-p1-card').filter(c => c.shadowRoot?.querySelector('ha-card')).length >= 3"
                )
                await asyncio.sleep(4)  # icons, fonts, slider positions
                for index, layout in enumerate(("compact", "standard", "full")):
                    rect = await page.evaluate(f"{RECT}(deepQueryAll('sony-lspx-p1-card')[{index}])")
                    await page.shoot(rect, CARD_MARGIN, OUT / f"card-{layout}.png")

                # The popover opened from the standard card's header.
                await page.evaluate(
                    "deepQueryAll('sony-lspx-p1-card')[1].shadowRoot.querySelector('[data-action=details]').click()"
                )
                await asyncio.sleep(2)
                rect = await page.evaluate(f"{RECT}(deepQueryAll('dialog[open]')[0])")
                await page.shoot(rect, DIALOG_MARGIN, OUT / "card-popover.png")
                await page.evaluate("deepQueryAll('dialog[open]')[0].close()")

                # The device page.
                device_id = await page.evaluate(
                    "Object.values(document.querySelector('home-assistant').hass.devices)"
                    ".find(d => d.manufacturer === 'Sony')?.id"
                )
                width, height = DEVICE_VIEWPORT
                await page.send(
                    "Emulation.setDeviceMetricsOverride",
                    width=width,
                    height=height,
                    deviceScaleFactor=SCALE,
                    mobile=False,
                )
                await page.goto(f"{base}/config/devices/device/{device_id}")
                await page.wait_for("deepQueryAll('ha-config-device-page').length > 0")
                await asyncio.sleep(4)
                rect = await page.evaluate(f"{RECT}(deepQueryAll('ha-config-device-page')[0])")
                await page.shoot(rect, 0, OUT / "device-page.png")
    finally:
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scheme", choices=("light", "dark"), default="light")
    parser.add_argument("--base", default=BASE)
    args = parser.parse_args()
    asyncio.run(capture(args.scheme, args.base))


if __name__ == "__main__":
    main()
