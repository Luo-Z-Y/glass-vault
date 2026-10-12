"""Shared paths and helpers for the demo-video scripts in scripts/video/.

The video is described by video/scenes.json: one entry per scene, each with the
lines the narrator says and what is on screen (a slide or a recorded demo).
Every narration line has a key such as "design-03" (scene id, line number);
the same key names its audio file, its subtitle and, for re-voicing, the file
to record (video/voice/design-03.wav).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
VIDEO = ROOT / "video"
OUT = VIDEO / "out"
SCENES = VIDEO / "scenes.json"
VOICE = VIDEO / "voice"  # recordings that replace the AI narrator, one file per line key
NARRATION = OUT / "narration"
SLIDES = OUT / "slides"
CLIPS = OUT / "clips"

RATE = 24_000  # every audio file is converted to 24 kHz mono 16-bit, the rate TRTC speaks at
GAP = 0.35  # seconds of silence after each narration line
WIDTH, HEIGHT, FPS = 1920, 1080, 30


def load_scenes() -> list[dict[str, Any]]:
    return json.loads(SCENES.read_text())["scenes"]


def line_key(scene_id: str, index: int) -> str:
    return f"{scene_id}-{index + 1:02d}"


def fingerprint(spec: dict[str, Any], manifest: dict[str, Any]) -> str:
    """What a demo take depends on besides its narration: its steps and setup, and how long the
    customer's spoken requests are. capture.py stores it; assemble.py recomputes it to tell a stale take."""
    said = {step["hold_mic"]: manifest[step["hold_mic"]]["seconds"] for step in spec["steps"] if "hold_mic" in step}
    keys = ("steps", "setup", "prescroll", "keep_state")
    return hashlib.sha256(json.dumps([{k: spec.get(k) for k in keys}, said], sort_keys=True).encode()).hexdigest()[:16]


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def load_manifest() -> dict[str, dict[str, Any]]:
    """The narration manifest written by narrate.py: line key -> text, seconds, source."""
    path = NARRATION / "manifest.json"
    if not path.exists():
        raise SystemExit("No narration yet: run scripts/video/narrate.py first.")
    return json.loads(path.read_text())


def ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise SystemExit("ffmpeg is needed: brew install ffmpeg")
    return path


def run(*args: str | Path) -> None:
    """Run a command quietly; if it fails, stop with what it printed."""
    try:
        subprocess.run([str(a) for a in args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"{Path(str(args[0])).name} failed:\n{e.stderr.strip()}") from None


def to_wav(src: Path, dst: Path, filters: str | None = None) -> None:
    """Any audio file ffmpeg can read, as 24 kHz mono 16-bit WAV, through `filters` if given."""
    af = ["-af", filters] if filters else []
    run(ffmpeg(), "-y", "-loglevel", "error", "-i", src, *af, "-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", dst)
