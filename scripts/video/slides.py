"""Render every slide of video/slides.html at every narration step, as 1920×1080 PNGs.

    uv run --with playwright python scripts/video/slides.py

Writes video/out/slides/<slide>-<step>.png for each slide scene in
video/scenes.json, one per narration line (step n shows the elements with
data-step <= n). Uses the installed Google Chrome, headless and muted.
"""

from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from vidlib import HEIGHT, SLIDES, VIDEO, WIDTH, load_scenes


async def main() -> None:
    SLIDES.mkdir(parents=True, exist_ok=True)
    for old in SLIDES.glob("*.png"):  # a slide or step that no longer exists must not linger
        old.unlink()
    url = (VIDEO / "slides.html").as_uri()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", args=["--mute-audio"])
        page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
        await page.goto(url)
        for scene in load_scenes():
            if "slide" not in scene:
                continue
            for step in range(1, len(scene["narration"]) + 1):
                await page.evaluate(f"location.hash = '{scene['slide']}/{step}'")
                await page.wait_for_timeout(150)
                await page.screenshot(path=SLIDES / f"{scene['slide']}-{step}.png")
        await browser.close()
    print(f"slides in {SLIDES.relative_to(VIDEO.parent)}")


if __name__ == "__main__":
    asyncio.run(main())
