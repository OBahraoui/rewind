#!/usr/bin/env python3
"""Time the narration's sentences to a voiceover and write subtitles (SRT).

  .venv/bin/python scripts/video/subtitles.py --audio voiceover.mp3

Reads docs/video/narration.txt and writes docs/video/build/subtitles.srt. Each sentence starts where its share of the
text says it should, snapped to the nearest real pause in the voice, so captions line up with any voice and pace.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np

ROOT = Path(__file__).parents[2]
RATE, STEP = 16000, 0.01  # analysis: 10 ms windows


def pauses(audio: Path) -> tuple[list[float], float, float, float]:
    """Midpoints of pauses (>= 180 ms of quiet), and where speech starts and ends, in seconds."""
    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(audio), "-f", "s16le", "-ac", "1",
                          "-ar", str(RATE), "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    n = int(RATE * STEP)
    rms = np.sqrt(np.mean(x[: len(x) // n * n].reshape(-1, n) ** 2, axis=1))
    quiet = rms < max(np.percentile(rms, 90) * 0.08, 1e-4)
    loud = np.flatnonzero(~quiet)
    start, end = (loud[0] * STEP, loud[-1] * STEP) if len(loud) else (0.0, len(rms) * STEP)
    mids, run = [], 0
    for i, q in enumerate(quiet):
        if q:
            run += 1
            continue
        if run * STEP >= 0.18:
            mids.append((i - run / 2) * STEP)
        run = 0
    return mids, start, end, len(rms) * STEP


def sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", " ".join(text.split())) if s.strip()]


def stamp(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def lines(s: str, width: int = 46) -> str:
    """At most two balanced lines."""
    if len(s) <= width:
        return s
    words, best = s.split(), None
    for i in range(1, len(s.split())):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if best is None or abs(len(a) - len(b)) < abs(len(best[0]) - len(best[1])):
            best = (a, b)
    return "\n".join(best)


def srt(audio: Path, text: str) -> str:
    cuts, start, end, _ = pauses(audio)
    parts = sentences(text)
    total = sum(len(p) for p in parts)
    times, acc = [start], 0
    for p in parts[:-1]:
        acc += len(p)
        expected = start + (end - start) * acc / total
        near = [c for c in cuts if abs(c - expected) < 1.5 and c > times[-1] + 0.6]
        times.append(min(near, key=lambda c: abs(c - expected)) if near else expected)
    times.append(end)
    return "\n".join(f"{i + 1}\n{stamp(times[i])} --> {stamp(times[i + 1] - 0.05)}\n{lines(p)}\n"
                     for i, p in enumerate(parts))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--audio", required=True, type=Path)
    p.add_argument("--text", type=Path, default=ROOT / "docs" / "video" / "narration.txt")
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "video" / "build" / "subtitles.srt")
    a = p.parse_args()
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(srt(a.audio, a.text.read_text()))
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
