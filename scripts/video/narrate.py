"""Voice every narration line in video/scenes.json, one WAV per line.

    uv run python scripts/video/narrate.py            # synthesise what changed
    uv run python scripts/video/narrate.py --force    # synthesise every line again

A line recorded by a person wins: video/voice/<key>.wav (or .m4a, .mp3, .aiff)
is used as is, converted to 24 kHz mono. Every other line is spoken by the AI
narrator, a TRTC voice (GV_TRTC_* and GV_TENCENT_SECRET_* in .env) that is not
the bank's own voice, so the two are never confused; without TRTC, macOS `say`
stands in. Output: video/out/narration/<key>.wav and manifest.json (each
line's text, length in seconds and source), which the later steps read.

The customer's spoken requests (voice_requests in scenes.json, played into the
demo's microphone) are voiced the same way, as video/out/narration/<name>.wav.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from glassvault.voice import voice_from_env
from vidlib import NARRATION, SCENES, VOICE, line_key, load_scenes, to_wav, wav_seconds

NARRATOR_VOICE = "v-male-P6q7LzD8"  # TRTC "Gentle Mentor"; the bank voice is female
RECORDED = (".wav", ".m4a", ".mp3", ".aiff")


def narrator():
    tts, _ = voice_from_env()
    return replace(tts, voice=NARRATOR_VOICE, deadline=60.0) if tts else None


def say(text: str, dst: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "line.aiff"
        subprocess.run(["say", "-v", "Daniel", "-o", str(aiff), text], check=True)
        to_wav(aiff, dst)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="synthesise every AI line again")
    args = parser.parse_args()

    NARRATION.mkdir(parents=True, exist_ok=True)
    manifest_path = NARRATION / "manifest.json"
    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    tts = narrator()
    engine = f"trtc:{NARRATOR_VOICE}" if tts else "say:Daniel"
    lines = [(line_key(scene["id"], i), text) for scene in load_scenes() for i, text in enumerate(scene["narration"])]
    lines += list(json.loads(SCENES.read_text()).get("voice_requests", {}).items())
    manifest = {}
    for key, text in lines:
        dst = NARRATION / f"{key}.wav"
        recorded = next((VOICE / f"{key}{ext}" for ext in RECORDED if (VOICE / f"{key}{ext}").exists()), None)
        if recorded:
            to_wav(recorded, dst)
            source = f"recorded:{recorded.name}"
        else:
            source = engine
            digest = hashlib.sha256(f"{engine}\n{text}".encode()).hexdigest()[:16]
            fresh = old.get(key, {}).get("digest") == digest and dst.exists()
            if args.force or not fresh:
                if tts:
                    dst.write_bytes(tts.synthesize(text))
                else:
                    say(text, dst)
                print(f"  {key}  {text[:60]}")
        manifest[key] = {"text": text, "seconds": round(wav_seconds(dst), 3), "source": source}
        if not recorded:
            manifest[key]["digest"] = digest
    for stale in set(old) - set(manifest):
        (NARRATION / f"{stale}.wav").unlink(missing_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    total = sum(manifest[key]["seconds"] for key, _ in lines)
    print(f"{len(lines)} lines, {total / 60:.1f} min of speech ({engine})")


if __name__ == "__main__":
    main()
