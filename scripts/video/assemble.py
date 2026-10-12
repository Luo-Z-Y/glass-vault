"""Cut the slides and recorded demos into the finished video, with its sound and subtitles.

    uv run python scripts/video/assemble.py

Reads video/scenes.json, the narration (narrate.py), the slide images
(slides.py) and the recorded demos (capture.py). A slide scene lasts as long as
its narration and shows step n during line n; a demo scene keeps the timing it
was recorded with, and stops with an error if its narration has changed since
(record it again with capture.py). A montage scene is a fast cut from demos:
each piece runs from [line, offset] to [line, offset] of a demo scene (seconds
from that line's start), at its "speed", marked as sped up. Writes to
video/out/:

  glass-vault-demo.mp4               narrated, with English subtitles as a track
  glass-vault-demo-no-narration.mp4  the same picture with only the app's own
                                     sounds (bank voice, chime, the customer's
                                     microphone), to record a voice-over onto
  glass-vault-demo.srt               the narration as subtitles, plus what the
                                     customer and the bank voice say
  script.md                          every line with its start time, to read from

Both soundtracks get the same fixed gain (the narrated mix measured to -16
LUFS), so the app's sounds are equally loud in each. The page's chime is
placed 0.45 s before each bank voice, as the app plays it, since the recording
may have held the voice back for the narrator.
"""

from __future__ import annotations

import json
import math
import re
import struct
import subprocess
import wave
from pathlib import Path
from typing import Any

from vidlib import (CLIPS, FPS, GAP, HEIGHT, NARRATION, OUT, RATE, SLIDES, WIDTH, ffmpeg, fingerprint, line_key,
                    load_manifest, load_scenes, run)

LEAD = 0.5  # seconds before a slide's first line
TAIL = 0.7  # seconds after a slide's last line, unless the scene sets "tail"
FADE = 0.25  # dip to the page's grey between scenes
END_FADE = 1.2  # fade to black at the very end
GREY = "0xF5F5F7"
LOUDNESS = -16.0  # LUFS, for the narrated mix
LEVELS = {"narration": 1.0, "bank": 0.9, "customer": 0.9, "chime": 0.25}
# Every segment in limited ("tv") range: the demo clips come from JPEG frames in full range, and
# segments joined without re-encoding must agree, or the slides play back washed out.
X264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-color_range", "tv",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-r", str(FPS)]
FONT = Path("/System/Library/Fonts/SFNS.ttf")  # for the speed badge; left out where the font is missing


def chime(path: Path) -> None:
    """The page's two-note chime (660 Hz then 880 Hz), which the muted recording could not hear."""
    samples = [0.0] * int(RATE * 0.55)
    for i, freq in enumerate((660, 880)):
        start = int(RATE * i * 0.18)
        for n in range(int(RATE * 0.32)):
            t = n / RATE
            # exponential ramps, as in app.js: 0.0001 -> 0.2 in 20 ms, -> 0.0001 at 300 ms
            gain = 0.0001 * (2000 ** (t / 0.02)) if t < 0.02 else 0.2 * (0.0005 ** ((t - 0.02) / 0.28)) if t < 0.3 else 0
            samples[start + n] += gain * math.sin(2 * math.pi * freq * t)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples))


def fades(duration: float, last: bool) -> str:
    """Dip to grey at both ends (to black at the very end), in RGB so every scene dips to the same grey."""
    out = f"fade=out:st={duration - END_FADE:.3f}:d={END_FADE}" if last else \
        f"fade=out:st={duration - FADE:.3f}:d={FADE}:color={GREY}"
    return f"format=gbrp,fade=in:st=0:d={FADE}:color={GREY},{out},scale=out_range=tv:out_color_matrix=bt709,format=yuv420p"


def slide_scene(scene: dict[str, Any], manifest: dict[str, Any], seg: Path, last: bool) -> dict[str, Any]:
    """A slide scene's lines laid end to end, and its video: step n from line n's start."""
    lines, t = [], LEAD
    for i in range(len(scene["narration"])):
        key = line_key(scene["id"], i)
        lines.append({"key": key, "start": t})
        t += manifest[key]["seconds"] + GAP
    duration = t + scene.get("tail", TAIL) - GAP
    listing = seg.with_suffix(".txt")
    try:
        with listing.open("w") as f:
            bounds = [0.0] + [line["start"] for line in lines[1:]] + [duration]
            for step, (a, b) in enumerate(zip(bounds, bounds[1:]), start=1):
                f.write(f"file '{SLIDES / f'{scene['slide']}-{step}.png'}'\nduration {b - a:.3f}\n")
            f.write(f"file '{SLIDES / f'{scene['slide']}-{len(lines)}.png'}'\n")
        run(ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
            "-vf", f"scale={WIDTH}:{HEIGHT},fps={FPS},{fades(duration, last)}", *X264,
            "-t", f"{duration:.3f}", seg)
    finally:
        listing.unlink(missing_ok=True)
    return {"duration": duration, "lines": lines, "audio": []}


def stale_take(scene: dict[str, Any], events: dict[str, Any], manifest: dict[str, Any]) -> str | None:
    """Why a demo take no longer matches its scene (narration, steps or spoken requests), or None."""
    expected = [line_key(scene["id"], i) for i in range(len(scene["narration"]))]
    if [line["key"] for line in events["lines"]] != expected:
        return "its narration lines changed"
    for line in events["lines"]:
        now = manifest[line["key"]]
        if line.get("text") != now["text"] or abs(line.get("seconds", -1) - now["seconds"]) > 0.05:
            return f"{line['key']} changed"
    if events.get("fingerprint", fingerprint(scene, manifest)) != fingerprint(scene, manifest):
        return "its steps, setup or spoken requests changed"
    return None


def check_overlaps(scene: dict[str, Any], events: dict[str, Any]) -> None:
    """Warn where narration, the customer or the bank voice would talk over each other."""
    spans = [(line["start"], line["start"] + line["seconds"], line["key"]) for line in events["lines"]]
    spans += [(a["start"], a["start"] + a.get("seconds", 0.5), a["kind"]) for a in events["audio"] if a["kind"] != "chime"]
    spans.sort()
    latest = None
    for span in spans:
        if latest and latest[1] - span[0] > 0.05:
            print(f"  warning: in {scene['id']}, {latest[2]} runs {latest[1] - span[0]:.1f} s into {span[2]}")
        if not latest or span[1] > latest[1]:
            latest = span
    if latest and latest[1] > events["duration"] - FADE:
        print(f"  warning: {latest[2]} runs past the end of {scene['id']}")


def preflight(scenes: list[dict[str, Any]], manifest: dict[str, Any], voice_requests: dict[str, str]) -> None:
    """Before encoding anything: stop, listing every problem, if narration or a demo take is out of date."""
    voiced = [(line_key(sc["id"], i), text) for sc in scenes for i, text in enumerate(sc["narration"])]
    unvoiced = [key for key, text in [*voiced, *voice_requests.items()] if manifest.get(key, {}).get("text") != text]
    problems = [f"not voiced as written: {', '.join(unvoiced)}; run narrate.py"] if unvoiced else []
    for sc in scenes:
        clip = CLIPS / sc["id"]
        if not sc.get("demo") or unvoiced:
            continue
        if not (clip / "events.json").exists():
            if not sc.get("only_if"):
                problems.append(f"{sc['id']} is not recorded; run capture.py {sc['id']}")
            continue
        why = stale_take(sc, json.loads((clip / "events.json").read_text()), manifest)
        if why:
            problems.append(f"{sc['id']} is out of date ({why}); run capture.py {sc['id']}")
    if problems:
        raise SystemExit("\n".join(problems) + "\nthen make.py --no-capture")


def demo_scene(scene: dict[str, Any], manifest: dict[str, Any], seg: Path, last: bool) -> dict[str, Any] | None:
    clip = CLIPS / scene["id"]
    if not (clip / "clip.mp4").exists():
        if scene.get("only_if"):
            return None
        raise SystemExit(f"{scene['id']} is not recorded: run capture.py {scene['id']}")  # preflight catches this first
    events = json.loads((clip / "events.json").read_text())
    check_overlaps(scene, events)
    # Enlarged stretches: the marked element, scaled up (at most 2.2×) on the page's grey, laid over the clip.
    zooms = events.get("zooms", [])
    graph = f"[0:v]split={len(zooms) + 1}[v0]" + "".join(f"[z{i}]" for i in range(len(zooms))) + ";"
    for i, z in enumerate(zooms):
        k = min(WIDTH * 0.92 / z["w"], HEIGHT * 0.92 / z["h"], 2.2)
        a, b = z["start"], z["start"] + z["seconds"]
        if k < 1.15:  # too big to enlarge: leave the clip as it is
            graph += f"[z{i}]nullsink;[v{i}]null[v{i + 1}];"
            continue
        w, h = round(z["w"] * k / 2) * 2, round(z["h"] * k / 2) * 2
        graph += (f"[z{i}]crop={z['w']}:{z['h']}:{z['x']}:{z['y']},scale={w}:{h}:flags=lanczos,"
                  f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color={GREY},setsar=1,format=yuva420p,"
                  f"fade=in:st={a}:d=0.2:alpha=1,fade=out:st={b - 0.2:.3f}:d=0.2:alpha=1[zz{i}];"
                  f"[v{i}][zz{i}]overlay=enable='between(t,{a},{b})'[v{i + 1}];")
    graph += f"[v{len(zooms)}]{fades(events['duration'], last)}[out]"
    run(ffmpeg(), "-y", "-loglevel", "error", "-i", clip / "clip.mp4", "-filter_complex", graph, "-map", "[out]", *X264, seg)
    for a in events["audio"]:
        if "file" in a:
            a["path"] = str(clip / a["file"])
    return events


def montage_scene(scene: dict[str, Any], manifest: dict[str, Any], parts: dict[str, dict[str, Any]], seg: Path,
                  last: bool) -> dict[str, Any]:
    """Pieces of rendered demo scenes, sped up as asked, with this scene's narration over them."""
    trims, t, badges = [], 0.0, []
    for i, piece in enumerate(scene["montage"]):
        src = parts.get(piece["scene"])
        if not src or "segment" not in src:
            raise SystemExit(f"{scene['id']} cuts from {piece['scene']}, which is not recorded")

        def at(ref: list[float]) -> float:
            n, offset = ref
            return src["lines"][n - 1]["start"] + offset

        a, b, speed = at(piece["from"]), at(piece["to"]), piece.get("speed", 1)
        trims.append(f"[{i}:v]trim={a:.3f}:{b:.3f},setpts=(PTS-STARTPTS)/{speed}[p{i}]")
        if speed != 1:
            badges.append((t, t + (b - a) / speed, speed))
        t += (b - a) / speed
    lines, end = [], LEAD
    for i in range(len(scene["narration"])):
        key = line_key(scene["id"], i)
        lines.append({"key": key, "start": end})
        end += manifest[key]["seconds"] + GAP
    duration = max(t, end + scene.get("tail", TAIL) - GAP)
    graph = ";".join(trims) + ";" + "".join(f"[p{i}]" for i in range(len(trims)))
    graph += f"concat=n={len(trims)}:v=1:a=0,tpad=stop_mode=clone:stop_duration={duration - t + 0.1:.3f}"
    if FONT.exists():
        for a, b, speed in badges:
            graph += (f",drawtext=fontfile={FONT}:text='{speed:g}× speed':fontsize=34:fontcolor=white:box=1:"
                      f"boxcolor=0x141414@0.85:boxborderw=16:x=w-tw-64:y=64:enable='between(t,{a:.3f},{b:.3f})'")
    graph += f",{fades(duration, last)}[out]"
    inputs = [arg for piece in scene["montage"] for arg in ("-i", parts[piece["scene"]]["segment"])]
    run(ffmpeg(), "-y", "-loglevel", "error", *inputs, "-filter_complex", graph, "-map", "[out]", *X264,
        "-t", f"{duration:.3f}", seg)
    return {"duration": duration, "lines": lines, "audio": []}


def srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3_600_000:02d}:{ms // 60_000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def subtitles(start: float, end: float, text: str, width: int = 42) -> list[tuple[float, float, str]]:
    """`text` as cues of at most two lines of about `width` characters, timed by their share of the text."""
    words, cues, line, lines = text.split(), [], "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    lines.append(line)
    chunks = ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]
    total, t = sum(len(c) for c in chunks), start
    for chunk in chunks:
        share = (end - start) * len(chunk) / total
        cues.append((t, t + share, chunk))
        t += share
    return cues


def spoken(script: str, seconds: float, full: float, sentences: int | None = None) -> str:
    """The sentences of `script` heard in the first `seconds` of its `full` reading: the first
    `sentences` if the recording says how many, else an estimate from their lengths."""
    every = re.split(r"(?<=[.!?])\s+", script)
    if seconds >= full - 0.3 or not script:
        return script
    if sentences is None:
        sentences, used, target = 0, 0, len(script) * seconds / full
        for sentence in every:
            if sentences and used + len(sentence) / 2 > target:
                break
            sentences, used = sentences + 1, used + len(sentence) + 1
    return " ".join(every[:sentences]) + (" …" if sentences < len(every) else "")


def mix(sounds: list[dict[str, Any]], duration: float, dst: Path) -> None:
    """Every sound at its time and level, in one mono track at RATE, not yet normalised."""
    if not sounds:
        run(ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=mono",
            "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", dst)
        return
    inputs, chains = [], []
    for i, s in enumerate(sounds):
        inputs += ["-i", s["path"]]
        trim = f"atrim=0:{s['seconds']:.3f}," if "seconds" in s else ""
        chains.append(f"[{i}:a]aresample={RATE},{trim}volume={LEVELS[s['kind']]},adelay={round(s['start'] * 1000)}:all=1[a{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(sounds)))
    graph += f"amix=inputs={len(sounds)}:normalize=0:dropout_transition=0,apad,atrim=0:{duration:.3f}[out]"
    script = dst.with_suffix(".filter")
    try:
        script.write_text(graph)
        run(ffmpeg(), "-y", "-loglevel", "error", *inputs, "-filter_complex_script", script, "-map", "[out]",
            "-c:a", "pcm_s16le", dst)
    finally:
        script.unlink(missing_ok=True)


def loudness(wav: Path) -> float:
    """Integrated loudness of `wav` in LUFS."""
    found = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(wav), "-af", "ebur128", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    return float(re.findall(r"I:\s+(-?[\d.]+) LUFS", found)[-1])


def main() -> None:
    manifest = load_manifest()
    work = OUT / "work"
    work.mkdir(parents=True, exist_ok=True)
    chime(work / "chime.wav")
    voice_requests = json.loads((OUT.parent / "scenes.json").read_text()).get("voice_requests", {})

    scenes = load_scenes()
    preflight(scenes, manifest, voice_requests)
    for old in work.glob("*.mp4"):  # segments of earlier builds, perhaps of scenes since renamed
        old.unlink()
    # Slides and demos first, then the montages that cut from the demos.
    parts: dict[str, dict[str, Any]] = {}
    for n, scene in sorted(enumerate(scenes), key=lambda ns: "montage" in ns[1]):
        seg = work / f"{n:02d}-{scene['id']}.mp4"
        last = n == len(scenes) - 1
        if "montage" in scene:
            part = montage_scene(scene, manifest, parts, seg, last)
        elif "slide" in scene:
            part = slide_scene(scene, manifest, seg, last)
        else:
            part = demo_scene(scene, manifest, seg, last)
        if part is None:
            print(f"  {scene['id']}: not recorded (Help is not on Tencent ADP), left out")
            continue
        parts[scene["id"]] = {**part, "segment": seg}

    segments, timeline, cues, t = [], [], [], 0.0
    for scene in scenes:
        part = parts.get(scene["id"])
        if not part:
            continue
        segments.append(part["segment"])
        for line in part["lines"]:
            start = t + line["start"]
            timeline.append({"kind": "narration", "key": line["key"], "path": str(NARRATION / f"{line['key']}.wav"),
                             "start": start, "scene": scene["id"]})
            cues += subtitles(start, start + manifest[line["key"]]["seconds"], manifest[line["key"]]["text"])
        for a in part["audio"]:
            if a["kind"] == "chime":
                continue
            timeline.append({**a, "start": t + a["start"]})
            if a["kind"] == "bank":
                timeline.append({"kind": "chime", "path": str(work / "chime.wav"), "start": t + a["start"] - 0.45})
            if a["kind"] == "customer":  # the file starts with half a second of silence
                text = a.get("text") or voice_requests.get(a["file"][4:-4], "")
                cues += subtitles(t + a["start"] + 0.5, t + a["start"] + 0.5 + manifest.get(a["file"][4:-4], {}).get("seconds", 3),
                                  f"[Customer] {text}")
            elif a["kind"] == "bank":
                heard = spoken(a.get("script", ""), a["seconds"], a.get("full_seconds", a["seconds"]), a.get("sentences"))
                cues += subtitles(t + a["start"], t + a["start"] + a["seconds"], f"[Bank voice] {heard}")
        t += part["duration"]
    duration = t

    listing = work / "segments.txt"
    picture = work / "picture.mp4"
    try:
        listing.write_text("".join(f"file '{s}'\n" for s in segments))
        run(ffmpeg(), "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", picture)
    finally:
        listing.unlink(missing_ok=True)

    srt = OUT / "glass-vault-demo.srt"
    srt.write_text("".join(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n\n"
                           for n, (a, b, text) in enumerate(sorted(cues), start=1)))

    (work / "timeline.json").write_text(json.dumps(timeline, indent=1) + "\n")  # for checking the mix
    full, bare = work / "full.wav", work / "bare.wav"
    mix(timeline, duration, full)
    mix([s for s in timeline if s["kind"] != "narration"], duration, bare)
    gain = LOUDNESS - loudness(full)
    finish = f"volume={gain:.2f}dB,alimiter=limit=0.89:level=0,aresample=48000,aformat=channel_layouts=stereo"
    out = OUT / "glass-vault-demo.mp4"
    run(ffmpeg(), "-y", "-loglevel", "error", "-i", picture, "-i", full, "-i", srt, "-map", "0:v", "-map", "1:a",
        "-map", "2:s", "-af", finish, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-c:s", "mov_text",
        "-metadata:s:s:0", "language=eng", "-movflags", "+faststart", out)
    run(ffmpeg(), "-y", "-loglevel", "error", "-i", picture, "-i", bare, "-map", "0:v", "-map", "1:a", "-af", finish,
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT / "glass-vault-demo-no-narration.mp4")

    lines = ["# Glass Vault demo: narration script", "",
             "Each line with the time it starts in the video. To re-voice, record each line as",
             "`video/voice/<key>.wav` and build again (see video/README.md).", ""]
    current = None
    for s in sorted((s for s in timeline if s["kind"] == "narration"), key=lambda s: s["start"]):
        if s["scene"] != current:
            current = s["scene"]
            lines += ["", f"## {current}", ""]
        m, sec = divmod(s["start"], 60)
        lines.append(f"- `{s['key']}` {int(m)}:{sec:04.1f}  {manifest[s['key']]['text']}")
    lines += ["", "## The customer, into the microphone", ""]
    lines += [f"- `{key}`  {text}" for key, text in voice_requests.items()]
    (OUT / "script.md").write_text("\n".join(lines) + "\n")
    print(f"{out.relative_to(OUT.parent.parent)}: {int(duration // 60)} min {duration % 60:.0f} s")


if __name__ == "__main__":
    main()
