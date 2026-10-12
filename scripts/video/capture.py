"""Record the demo scenes of video/scenes.json from the live web demo, paced by the narration.

    uv run --with playwright python scripts/video/capture.py               # every demo scene
    uv run --with playwright python scripts/video/capture.py demo-rogue    # just these

It starts the demo server on a spare port (or uses --url), then for each demo
scene resets the demo, runs the scene's setup through the API, opens the page
in a muted, headless Google Chrome (a 1600×900 page drawn at 1.2×, so frames
are 1920 wide) and runs the scene's steps while recording the screen with the
DevTools screencast. The narration is not played: each `say` step only reserves
the line's length (from narrate.py) on the timeline, so the page moves in time
with it. The demo calls the real LLMs and Tencent RTC, so each run costs a
little API credit, and LLM latency varies from run to run.

Steps (each a JSON object in the scene's "steps"):
  {"say": n, "wait": false}       start narration line n; wait for its end unless wait is false
  {"wait_say": n}                 wait for line n to end
  {"at": [n, f]}                  wait until fraction f of line n has been spoken
  {"pause": s}
  {"type": text, "over": n}       type into #input (or "into"), spread over 80% of line n, or at "cps"
  {"click": sel, "after": s}      move the cursor there and click; "voice": s also waits for the
                                  bank voice this starts and stops it at its first pause after s
  {"select": sel, "value": v}
  {"wait_for": sel, "timeout": s}
  {"highlight": sel, "seconds": s, "block": true}   a red box; "block" waits it out
  {"zoom": sel, "seconds": s}     the final cut shows that part of the screen enlarged
  {"scroll": sel}                 scroll it to the middle of the window
  {"wait_voice": s}               wait for the bank voice (a reply to a voice turn reads itself);
                                  with s, stop it at its first pause after s seconds
  {"hold_mic": name}              push to talk, saying voice_requests[name]
Scene keys: "setup" (API turns before recording; "approve": true signs a
confirmation), "prescroll" (scroll before recording starts), "only_if": "adp".

Per scene, video/out/clips/<scene>/ gets clip.mp4 (silent, 30 fps),
events.json (when each narration line, sound and zoom starts, and the line
lengths it was paced with) and the bank voice's audio as the page fetched it.
A take is written to <scene>.partial first and replaces the old one only once
it is complete. "only_if": "adp" scenes are skipped unless Help runs on
Tencent ADP (--include-help records them anyway).

Editing, as a video editor would: the bank voice starts at most 2.5 s late
when it would otherwise talk over the narrator, and a confirmation is
stopped at the end of a sentence rather than read in full. Nothing is
audible while recording: Chrome runs with --mute-audio, and the customer's
microphone is a WAV file. (Chrome's own fake microphone,
--use-file-for-fake-audio-capture, hangs getUserMedia in headless Chrome on
macOS, so the page's getUserMedia plays the file through Web Audio instead;
recording, resampling and TRTC recognition run unchanged.)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

from vidlib import (CLIPS, FPS, GAP, HEIGHT, NARRATION, OUT, ROOT, WIDTH, ffmpeg, line_key, load_manifest,
                    load_scenes, run)

VIEWPORT = {"width": 1600, "height": 900}
SCALE = 1.2
# Chrome's own scale factor, not Playwright's emulated one: the screencast then sends device
# pixels (1920 wide) instead of CSS pixels. 987 high leaves 900 for the page in headless Chrome.
WINDOW = [f"--force-device-scale-factor={SCALE}", "--window-size=1600,987"]
PARK = (1560, 450)  # where the cursor rests: the grey margin right of the consoles
MAX_VOICE_DELAY = 2.5  # seconds the bank voice may wait for the narrator (app.js falls back at 8 s)

# Injected into every page: a visible cursor (headless Chrome draws none), red highlight
# boxes, a log of what the page plays (bank voice, chime) and when the microphone is on,
# and the microphone itself.
OVERLAY = """
(() => {
  window.__gv = [];
  window.__gvQuietUntil = 0;
  const now = () => (performance.timeOrigin + performance.now()) / 1000;
  const log = (kind, extra) => window.__gv.push({ kind, t: now(), ...extra });
  const play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    if (!this.__gvSeen) {
      this.__gvSeen = true;
      for (const ev of ["playing", "pause", "ended"]) this.addEventListener(ev, () => log(ev, { src: this.src }));
    }
    const wait = Math.min(window.__gvQuietUntil - now(), %(max_delay)s);
    if (wait <= 0) return play.call(this);
    return new Promise((ok, fail) => setTimeout(() => play.call(this).then(ok, fail), wait * 1000));
  };
  const osc = AudioContext.prototype.createOscillator;
  let lastChime = 0;
  AudioContext.prototype.createOscillator = function () {
    if (now() - lastChime > 1) { lastChime = now(); log("chime"); }
    return osc.call(this);
  };
  // The microphone: the customer's recorded request (window.__gvMic), played into a MediaStream.
  navigator.mediaDevices.getUserMedia = async () => {
    const ctx = new AudioContext();
    const audio = await ctx.decodeAudioData(await (await fetch(`/__video/mic-${window.__gvMic}.wav`)).arrayBuffer());
    const source = ctx.createBufferSource();
    const sink = ctx.createMediaStreamDestination();
    source.buffer = audio;
    source.connect(sink);
    source.start();
    return sink.stream;
  };
  addEventListener("DOMContentLoaded", () => {
    const style = document.createElement("style");
    style.textContent = `
      #__cursor { position: fixed; left: 0; top: 0; width: 26px; height: 26px; z-index: 2147483647;
                  pointer-events: none; transform: translate(-3px, -2px); filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
      .__ripple { position: fixed; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%%;
                  border: 3px solid #ff3333; z-index: 2147483646; pointer-events: none; animation: __rip .6s ease-out forwards; }
      @keyframes __rip { from { transform: scale(.3); opacity: 1 } to { transform: scale(1.4); opacity: 0 } }
      .__hl { position: fixed; z-index: 2147483645; pointer-events: none; border: 4px solid #ff3333; border-radius: 16px;
              box-shadow: 0 0 0 6px rgba(255,51,51,.18); opacity: 0; transition: opacity .3s; }`;
    document.head.appendChild(style);
    const cur = document.createElement("div");
    cur.id = "__cursor";
    cur.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M3 2l7.5 19 2.6-7.9L21 10.5z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    cur.style.left = "-40px";
    document.body.appendChild(cur);
    addEventListener("mousemove", (e) => { cur.style.left = e.clientX + "px"; cur.style.top = e.clientY + "px"; }, true);
    addEventListener("mousedown", (e) => {
      const r = document.createElement("div");
      r.className = "__ripple"; r.style.left = e.clientX + "px"; r.style.top = e.clientY + "px";
      document.body.appendChild(r); setTimeout(() => r.remove(), 700);
    }, true);
    const mic = document.getElementById("mic");
    if (mic) new MutationObserver(() => log(mic.classList.contains("listening") ? "mic-on" : "mic-off"))
      .observe(mic, { attributes: true, attributeFilter: ["class"] });
  });
  window.__gvHighlight = (target, seconds) => {
    const box = document.createElement("div");
    box.className = "__hl";
    document.body.appendChild(box);
    const place = () => {
      if (!box.isConnected) return;
      if (!target.isConnected) return box.remove(); // a bubble that was replaced: no stray ring
      const r = target.getBoundingClientRect();
      Object.assign(box.style, { left: r.left - 8 + "px", top: r.top - 8 + "px", width: r.width + 16 + "px", height: r.height + 16 + "px" });
      requestAnimationFrame(place);
    };
    place();
    requestAnimationFrame(() => (box.style.opacity = 1));
    setTimeout(() => { box.style.opacity = 0; setTimeout(() => box.remove(), 400); }, seconds * 1000);
  };
})();
""" % {"max_delay": MAX_VOICE_DELAY}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api(base: str, path: str, body: dict | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{path} -> {e.code}: {e.read().decode()[:300]}") from None


def start_server() -> tuple[subprocess.Popen, str]:
    """The demo server on a spare port, logging to video/out/server.log."""
    port = free_port()
    log_path = OUT / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        proc = subprocess.Popen([sys.executable, "-m", "glassvault.web.server"], cwd=ROOT,
                                env={**os.environ, "GV_PORT": str(port)}, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://localhost:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit("the demo server exited:\n" + log_path.read_text()[-2000:])
        try:
            urllib.request.urlopen(base + "/api/state", timeout=5).read()
            return proc, base
        except OSError:
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("the demo server did not start:\n" + log_path.read_text()[-2000:])


def pauses(wav: Path) -> list[float]:
    """Where the speech in `wav` pauses (seconds from its start)."""
    found = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(wav), "-af", "silencedetect=noise=-35dB:d=0.2",
                            "-f", "null", "-"], capture_output=True, text=True).stderr
    return [float(line.split("silence_start: ")[1]) for line in found.splitlines() if "silence_start: " in line]


def mic_file(out: Path, name: str) -> Path:
    """The customer's request as the microphone hears it: half a second of silence before and after,
    at the narration's rate (24 kHz mono; the page resamples it)."""
    with wave.open(str(NARRATION / f"{name}.wav")) as w:
        rate, frames = w.getframerate(), w.readframes(w.getnframes())
    dst = out / f"mic-{name}.wav"
    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\0\0" * (rate // 2) + frames + b"\0\0" * (rate // 2))
    return dst


class Scene:
    """One demo scene being recorded: the page, the screencast and the timeline."""

    def __init__(self, page: Page, spec: dict[str, Any], manifest: dict[str, Any], out: Path) -> None:
        self.page, self.spec, self.manifest, self.out = page, spec, manifest, out
        self.t0 = 0.0
        self.free = 0.0  # when the narration channel is next free (epoch seconds)
        self.starts: dict[int, float] = {}
        self.ends: dict[int, float] = {}
        self.lines: list[dict[str, Any]] = []
        self.audio: list[dict[str, Any]] = []
        self.zooms: list[dict[str, Any]] = []
        self.voices: dict[str, Path] = {}  # script hash -> the bank voice's WAV
        self.scripts: dict[str, str] = {}  # script hash -> the script it speaks
        self.last_voice_turn = 0.0  # when the customer last let go of the microphone
        self.frames = out / "frames"

    # --- screencast -------------------------------------------------------------------------

    async def start(self) -> None:
        self.frames.mkdir(parents=True)
        cdp = await self.page.context.new_cdp_session(self.page)

        def frame(params: dict[str, Any]) -> None:
            t = params["metadata"]["timestamp"]
            (self.frames / f"{t:.4f}.jpg").write_bytes(base64.b64decode(params["data"]))
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

        cdp.on("Page.screencastFrame", frame)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "maxWidth": 1920, "maxHeight": 1080})
        self.cdp = cdp
        await self.page.mouse.move(*PARK)
        await asyncio.sleep(0.5)
        self.t0 = self.free = time.time()

    async def stop(self) -> float:
        await self.cdp.send("Page.stopScreencast")
        return time.time() - self.t0

    def encode(self, duration: float) -> None:
        """The frames as clip.mp4 from t0, each shown until the next one; then the frames go."""
        frames = sorted(self.frames.glob("*.jpg"), key=lambda f: float(f.stem))
        times = [float(f.stem) - self.t0 for f in frames]
        first = max((i for i, t in enumerate(times) if t <= 0), default=0)
        frames, times = frames[first:], [max(t, 0.0) for t in times[first:]]
        times.append(duration)
        listing = self.out / "frames.txt"
        try:
            with listing.open("w") as f:
                for frame, t, nxt in zip(frames, times, times[1:]):
                    f.write(f"file '{frame}'\nduration {max(nxt - t, 0.001):.4f}\n")
                f.write(f"file '{frames[-1]}'\n")
            run(ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
                "-vf", f"scale={WIDTH}:{HEIGHT}:flags=lanczos,fps={FPS},format=yuv420p",
                "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-t", f"{duration:.3f}", self.out / "clip.mp4")
        finally:
            listing.unlink(missing_ok=True)
        shutil.rmtree(self.frames)

    # --- timeline ---------------------------------------------------------------------------

    async def until(self, t: float) -> None:
        if t > time.time():
            await asyncio.sleep(t - time.time())

    async def quiet(self) -> None:
        """Tell the page when the narrator is done, so the bank voice does not talk over him."""
        await self.page.evaluate("t => { window.__gvQuietUntil = t; }", self.free)

    async def say(self, n: int, wait: bool = True) -> None:
        key = line_key(self.spec["id"], n - 1)
        line = self.manifest[key]
        start = max(time.time(), self.free)
        await self.until(start)
        self.lines.append({"key": key, "start": round(start - self.t0, 3), "seconds": line["seconds"], "text": line["text"]})
        self.starts[n] = start
        self.free = self.ends[n] = start + line["seconds"] + GAP
        await self.quiet()
        if wait:
            await self.until(self.free)

    def seconds(self, n: int) -> float:
        return self.manifest[line_key(self.spec["id"], n - 1)]["seconds"]

    # --- page actions -----------------------------------------------------------------------

    async def point(self, selector: str) -> tuple[float, float]:
        target = self.page.locator(selector).first
        await target.scroll_into_view_if_needed()
        box = await target.bounding_box()
        if not box:
            raise RuntimeError(f"{selector} is not visible")
        x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
        await self.page.mouse.move(x, y, steps=24)
        await asyncio.sleep(0.15)
        return x, y

    async def click(self, selector: str) -> tuple[float, float]:
        x, y = await self.point(selector)
        await self.page.mouse.down()
        await asyncio.sleep(0.08)
        await self.page.mouse.up()
        return x, y

    async def type(self, text: str, into: str, per_char: float) -> None:
        x, y = await self.click(into)
        await self.page.mouse.move(x - 60, y + 70, steps=8)  # off the text being typed
        for ch in text:
            await self.page.keyboard.type(ch)
            await asyncio.sleep(per_char)

    async def highlight(self, selector: str, seconds: float, block: bool) -> None:
        target = self.page.locator(selector).first
        if await target.count() == 0:
            print(f"    (nothing to highlight: {selector})")
            return
        await target.evaluate("(el, s) => window.__gvHighlight(el, s)", seconds)
        await asyncio.sleep(seconds if block else min(seconds, 1.2))

    async def zoom(self, selector: str, seconds: float) -> None:
        """Mark a stretch of the clip to be shown enlarged: the element, padded to 16:9."""
        box = await self.page.locator(selector).first.bounding_box()
        if not box:
            print(f"    (nothing to zoom into: {selector})")
            return
        pad = 24
        bw, bh = (box["width"] + 2 * pad) * SCALE, (box["height"] + 2 * pad) * SCALE
        cx, cy = (box["x"] - pad) * SCALE + bw / 2, (box["y"] - pad) * SCALE + bh / 2
        w = min(max(bw, bh * 16 / 9, WIDTH / 2.2), WIDTH)  # 16:9, enlarged at most 2.2×
        h = w * 9 / 16
        left = min(max(cx - w / 2, 0), WIDTH - w)
        top = min(max(cy - h / 2, 0), HEIGHT - h)
        self.zooms.append({"start": round(time.time() - self.t0, 3), "seconds": seconds,
                           "x": round(left), "y": round(top), "w": round(w), "h": round(h)})

    async def log(self) -> list[dict[str, Any]]:
        return await self.page.evaluate("window.__gv")

    async def wait_log(self, kinds: set[str], after: float, timeout: float) -> dict[str, Any] | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            hit = next((e for e in await self.log() if e["kind"] in kinds and e["t"] > after), None)
            if hit:
                return hit
            await asyncio.sleep(0.1)
        return None

    async def bank_voice(self, since: float, play_for: float | None) -> None:
        """Wait for the bank voice that started after `since` and let it finish, or with
        `play_for`, stop it at its first pause after that many seconds (the end of a sentence,
        not mid-word). The narration channel stays busy until then."""
        playing = await self.wait_log({"playing"}, since, 20)
        if not playing:
            raise RuntimeError("the bank voice did not play")
        if play_for is None:
            await self.wait_log({"ended", "pause"}, playing["t"], 60)
        else:
            key = playing["src"].rsplit("/", 1)[-1]
            for _ in range(30):
                if key in self.voices:
                    break
                await asyncio.sleep(0.1)
            stop = next((p for p in pauses(self.voices[key]) if p >= play_for), play_for) if key in self.voices else play_for
            await self.until(playing["t"] + stop + 0.1)
            await self.page.evaluate("stopSpeaking()")
        self.free = max(self.free, time.time() + 0.3)

    async def hold_mic(self, name: str) -> None:
        with wave.open(str(self.out / f"mic-{name}.wav")) as w:
            seconds = w.getnframes() / w.getframerate()
        await self.page.evaluate("n => { window.__gvMic = n; }", name)
        await self.point("#mic")
        pressed = time.time()
        await self.page.mouse.down()
        on = await self.wait_log({"mic-on"}, pressed - 0.1, 10)
        if not on:
            raise RuntimeError("the microphone did not come on")
        await asyncio.sleep(seconds + 0.3)
        await self.page.mouse.up()
        self.audio.append({"kind": "customer", "file": f"mic-{name}.wav", "start": round(on["t"] - self.t0, 3),
                           "text": self.manifest[name]["text"]})
        self.free = max(self.free, time.time())
        self.last_voice_turn = time.time()
        await self.page.mouse.move(*PARK, steps=12)

    async def run(self, step: dict[str, Any]) -> None:
        page = self.page
        if "say" in step:
            await self.say(step["say"], step.get("wait", True))
        elif "wait_say" in step:
            await self.until(self.ends[step["wait_say"]])
        elif "at" in step:
            n, fraction = step["at"]
            await self.until(self.starts[n] + self.seconds(n) * fraction)
        elif "pause" in step:
            await asyncio.sleep(step["pause"])
        elif "type" in step:
            text = step["type"]
            per_char = self.seconds(step["over"]) * 0.8 / len(text) if "over" in step else 1 / step.get("cps", 16)
            await self.type(text, step.get("into", "#input"), per_char)
        elif "click" in step:
            clicked = time.time()
            await self.click(step["click"])
            if "voice" in step:
                await self.bank_voice(clicked, step["voice"])
            await asyncio.sleep(step.get("after", 0.3))
        elif "select" in step:
            await self.point(step["select"])
            await page.locator(step["select"]).select_option(step["value"])
            await asyncio.sleep(0.4)
        elif "wait_for" in step:
            await page.locator(step["wait_for"]).first.wait_for(state="visible", timeout=step.get("timeout", 30) * 1000)
            await asyncio.sleep(0.3)
        elif "highlight" in step:
            await self.highlight(step["highlight"], step.get("seconds", 3), step.get("block", False))
        elif "zoom" in step:
            await self.zoom(step["zoom"], step.get("seconds", 4))
        elif "scroll" in step:
            await page.locator(step["scroll"]).first.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
            await page.mouse.move(*PARK, steps=8)
            await asyncio.sleep(1.0)
        elif "wait_voice" in step:
            play_for = None if step["wait_voice"] is True else step["wait_voice"]
            await self.bank_voice(self.last_voice_turn or self.t0, play_for)
        elif "hold_mic" in step:
            await self.hold_mic(step["hold_mic"])
        else:
            raise ValueError(f"unknown step {step}")

    async def finish(self) -> dict[str, Any]:
        await self.until(self.free)
        await asyncio.sleep(0.6)
        duration = await self.stop()
        # The bank voice as the page played it: each audio element from "playing" to its end.
        log = await self.log()
        for e in log:
            if e["kind"] == "chime":
                self.audio.append({"kind": "chime", "start": round(e["t"] - self.t0, 3)})
            if e["kind"] != "playing":
                continue
            end = next((x["t"] for x in log if x["kind"] in ("pause", "ended") and x["t"] > e["t"] and x.get("src") == e["src"]),
                       self.t0 + duration)
            key = e["src"].rsplit("/", 1)[-1]
            if key in self.voices:
                with wave.open(str(self.voices[key])) as w:
                    full = w.getnframes() / w.getframerate()
                self.audio.append({"kind": "bank", "file": self.voices[key].name, "start": round(e["t"] - self.t0, 3),
                                   "seconds": round(min(end - e["t"], full), 3), "full_seconds": round(full, 3),
                                   "script": self.scripts.get(key, "")})
        return {"scene": self.spec["id"], "duration": round(duration, 3), "lines": self.lines,
                "audio": sorted(self.audio, key=lambda a: a["start"]), "zooms": self.zooms}


async def record(browser_type, base: str, spec: dict[str, Any], manifest: dict[str, Any]) -> None:
    out = CLIPS / spec["id"]
    tmp = CLIPS / f"{spec['id']}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    mics = [mic_file(tmp, step["hold_mic"]) for step in spec["steps"] if "hold_mic" in step]

    api(base, "/api/reset", {})
    for s in spec.get("setup", []):
        outcome = api(base, "/api/message", {"text": s["message"]})["outcome"]
        if s.get("approve"):
            challenge = outcome.get("challenge")
            if not challenge:
                raise SystemExit(f"setup {s['message']!r} gave {outcome['kind']} ({outcome.get('message')!r}), "
                                 f"not a confirmation; run capture.py {spec['id']} again")
            api(base, "/api/approve", {"nonce": challenge["nonce"], "simulated": True})
        api(base, "/api/cancel", {})

    browser = await browser_type.launch(channel="chrome", args=["--mute-audio", "--autoplay-policy=no-user-gesture-required", *WINDOW])
    try:
        context = await browser.new_context(no_viewport=True)
        await context.add_init_script(OVERLAY)
        page = await context.new_page()
        if mics:
            await page.route("**/__video/mic-*.wav", lambda route: route.fulfill(
                path=tmp / route.request.url.rsplit("/", 1)[-1], content_type="audio/wav"))
        scene = Scene(page, spec, manifest, tmp)

        async def keep_voice(response) -> None:
            if response.url.endswith("/api/message") and response.ok:
                o = (await response.json())["outcome"]
                if o.get("speech"):
                    scene.scripts[o["speech"]] = (o.get("confirmation") or o.get("answer") or {}).get("spoken_script", "")
            if "/api/voice/" in response.url and response.ok:
                key = response.url.rsplit("/", 1)[-1]
                path = tmp / f"bank-{key[:12]}.wav"
                path.write_bytes(await response.body())
                scene.voices[key] = path

        page.on("response", lambda r: asyncio.ensure_future(keep_voice(r)))
        problems: list[str] = []
        page.on("console", lambda m: m.type == "error" and problems.append(m.text))
        page.on("pageerror", lambda e: problems.append(str(e)))
        await page.goto(base)
        await page.wait_for_function("document.querySelector('#llm-status')?.textContent !== '…'")
        if spec.get("prescroll"):
            await page.locator(spec["prescroll"]).first.evaluate("el => el.scrollIntoView({block: 'center'})")
        await asyncio.sleep(1.0)
        await scene.start()
        for step in spec["steps"]:
            try:
                await scene.run(step)
            except Exception:
                chat = await page.locator(".chat .msg").all_inner_texts()
                print(f"  {spec['id']} failed at {step}\n    chat: {chat[-2:]}\n    console: {problems[-3:]}")
                raise
        events = await scene.finish()
    finally:
        await browser.close()
    (tmp / "events.json").write_text(json.dumps(events, indent=2) + "\n")
    scene.encode(events["duration"])
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)
    print(f"  {spec['id']}: {events['duration']:.1f} s, {len(events['audio'])} sounds, {len(events['zooms'])} zooms")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenes", nargs="*", help="scene ids (default: every demo scene)")
    parser.add_argument("--url", help="a demo server that is already running")
    parser.add_argument("--include-help", action="store_true", help="record Help even without Tencent ADP")
    args = parser.parse_args()

    manifest = load_manifest()
    proc, base = (None, args.url.rstrip("/")) if args.url else start_server()
    try:
        adp = (api(base, "/api/state").get("faq") or {}).get("engine") == "adp"
        async with async_playwright() as p:
            for spec in load_scenes():
                if not spec.get("demo") or (args.scenes and spec["id"] not in args.scenes):
                    continue
                if spec.get("only_if") == "adp" and not (adp or args.include_help):
                    print(f"  {spec['id']}: skipped (Help is not on Tencent ADP; see scripts/adp_setup.py)")
                    shutil.rmtree(CLIPS / spec["id"], ignore_errors=True)
                    continue
                await record(p.chromium, base, spec, manifest)
    finally:
        if proc:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
