"""Build the demo video from scratch: narration, slides, demo recordings, final cut.

    uv run --with playwright python scripts/video/make.py                        # everything
    uv run --with playwright python scripts/video/make.py --no-capture           # reuse the recorded demos
    uv run --with playwright python scripts/video/make.py --capture demo-rogue   # record only these demos

Runs narrate.py, slides.py, capture.py and assemble.py in turn (see each for
details). Recording the demos is the slow step (about five minutes, live LLM
and Tencent RTC calls); --no-capture skips it, which is right when only slides
or slide narration changed. A re-voiced demo line has a new length, so a demo
whose lines changed needs recording again: --capture <scene> records just
those (assemble.py stops and names any demo that is out of date).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-capture", action="store_true", help="reuse the demo recordings in video/out/clips")
    parser.add_argument("--capture", nargs="+", metavar="SCENE", help="record only these demo scenes")
    args = parser.parse_args()
    capture = [] if args.no_capture else [["capture.py", *(args.capture or [])]]
    for step in [["narrate.py"], ["slides.py"], *capture, ["assemble.py"]]:
        print(f"== {' '.join(step)}")
        subprocess.run([sys.executable, str(HERE / step[0]), *step[1:]], cwd=HERE, check=True)


if __name__ == "__main__":
    main()
