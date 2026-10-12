# Demo video

The submission video is built from this folder by the scripts in `scripts/video/`,
so it can be rebuilt after any change to the app, the slides or the script.

| File | What it is |
|---|---|
| `scenes.json` | Every scene in order: the narration lines and what is on screen (a slide, or a demo recorded from the live app with the steps to perform) |
| `slides.html` | The slides, 1920×1080. `slides.html#design/3` shows slide `design` as it looks during its third line |
| `voice/` | Your own recordings, if any (not in git): `<key>.wav` replaces the AI narrator for that line |
| `out/` | Everything built (not in git) |

## Build

Needs ffmpeg, Google Chrome, and the TRTC keys in `.env` (the narrator is a TRTC
voice; without them macOS `say` reads the lines). The demo scenes call the real
LLMs and Tencent RTC, so a full build spends a little API credit.

```bash
uv run --with playwright python scripts/video/make.py
```

The demo scenes run the real app: the LLMs, Tencent RTC and Tencent ADP (Help is
recorded only when ADP is set up). Nothing is audible while it records.

Steps, each runnable on its own: `narrate.py` (one WAV per line), `slides.py`
(slide images), `capture.py` (records the demo scenes, about five minutes),
`assemble.py` (the final cut). `make.py --no-capture` reuses the recorded demos.

Output in `video/out/`:

- `glass-vault-demo.mp4`: narrated, with English subtitles as a track
- `glass-vault-demo-no-narration.mp4`: the same picture with only the app's own sounds
- `glass-vault-demo.srt`: the subtitles, to upload with the video
- `script.md`: every line with its start time

## Re-voicing

Each narration line has a key, its scene id and line number (`design-03`); they
are listed in `out/script.md`. Two ways to use your own voice:

1. **Record line by line** (best): save each line as `video/voice/<key>.wav` (or
   `.m4a`, `.mp3`), then run `make.py` again. Slides take each line's new length;
   demo scenes are recorded again so the app keeps pace with your voice. Lines
   you have not recorded stay in the AI voice. Recordings are trimmed and brought
   to the AI narrator's loudness. The customer's spoken requests are recorded the
   same way, one file per key of `voice_requests` in `scenes.json`
   (`video/voice/multistep-request.wav`, `video/voice/rogue-request.wav`); they
   are listed at the end of `out/script.md`. A demo with a re-voiced line or
   request is out of date: `make.py --capture <scene>` records it again (and
   `assemble.py` names any that need it).
2. **Voice over the finished picture**: record over
   `out/glass-vault-demo-no-narration.mp4` in any editor, reading from
   `out/script.md` with its timings.
