#!/usr/bin/env python3
"""Render Rewind's logo as an animated presenter that reacts to a voiceover.

  .venv/bin/pip install imageio-ffmpeg numpy       # video tools, not needed by Rewind itself
  .venv/bin/python scripts/video/presenter.py --audio voiceover.mp3

Writes docs/video/build/presenter-alpha.mov (transparent, ProRes 4444: DaVinci Resolve, Final Cut, Premiere) and
presenter-green.mp4 (on chroma green, for CapCut's chroma key). Put it in a bottom corner over the screen recording.
The checkpoint dot pulses with the voice, the chevrons lean back towards it, and a soft ring breathes while it speaks.
"""

from __future__ import annotations

import argparse
import math
import subprocess
from pathlib import Path

import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).parents[2]
INDIGO, WHITE, GREEN, RING = (80, 64, 214), (255, 255, 255), (0, 177, 64), (155, 143, 255)
# The app icon, on a 48x48 grid (docs/brand/rewind-icon.svg).
FRONT = [(27, 12), (18, 24), (27, 36), (32.5, 36), (23.5, 24), (32.5, 12)]
TRAIL = [(37.5, 12), (28.5, 24), (37.5, 36), (43, 36), (34, 24), (43, 12)]


def loudness(audio: Path, fps: int) -> np.ndarray:
    """One value per video frame, 0..1: how loud the voice is, smoothed like a VU meter."""
    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-i", str(audio), "-f", "s16le",
                          "-ac", "1", "-ar", "16000", "-"], capture_output=True, check=True).stdout
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
    per = 16000 // fps
    frames = len(samples) // per
    rms = np.sqrt(np.mean(samples[: frames * per].reshape(frames, per) ** 2, axis=1))
    rms = np.clip(rms / max(np.percentile(rms, 95), 1e-6), 0, 1)
    rms = np.clip((rms - 0.2) / 0.8, 0, 1)  # a noise floor, so pauses read as pauses
    out, level = np.zeros_like(rms), 0.0
    for i, v in enumerate(rms):  # fast attack, slow release
        level = level + (v - level) * (0.6 if v > level else 0.12)
        out[i] = level
    return out


def frame(size: int, t: float, a: float) -> Image.Image:
    """The presenter at time t (seconds) with voice level a (0..1), RGBA, drawn at 3x and downsampled."""
    s3 = size * 3
    img = Image.new("RGBA", (s3, s3), (0, 0, 0, 0))
    breathe = 0.008 * math.sin(2 * math.pi * t / 3.2)
    tile = s3 * 0.62 * (1 + 0.04 * a + breathe)
    c = s3 / 2
    x0, y0 = c - tile / 2, c - tile / 2

    def g(x: float, y: float, dx: float = 0) -> tuple[float, float]:
        return x0 + (x + dx) / 48 * tile, y0 + y / 48 * tile

    # Speaking ring, then a soft shadow, then the tile.
    if a > 0.02:
        ring = Image.new("RGBA", (s3, s3), (0, 0, 0, 0))
        r = tile * (0.6 + 0.07 * a)
        ImageDraw.Draw(ring).ellipse((c - r, c - r, c + r, c + r), outline=(*RING, int(150 * a)),
                                     width=max(3, int(s3 * 0.012)))
        img.alpha_composite(ring.filter(ImageFilter.GaussianBlur(s3 * 0.006)))
    shadow = Image.new("RGBA", (s3, s3), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((x0, y0 + tile * 0.05, x0 + tile, y0 + tile * 1.05),
                                             radius=tile * 0.25, fill=(20, 14, 60, 90))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(s3 * 0.03)))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((x0, y0, x0 + tile, y0 + tile), radius=tile * 0.25, fill=INDIGO)

    lean = -0.5 * a  # chevrons lean back towards the checkpoint while speaking; the trailing one closes in
    trail = Image.new("RGBA", (s3, s3), (0, 0, 0, 0))
    ImageDraw.Draw(trail).polygon([g(x, y, lean * 3) for x, y in TRAIL], fill=(*WHITE, int(255 * (0.5 + 0.3 * a))))
    img.alpha_composite(trail)
    d.polygon([g(x, y, lean) for x, y in FRONT], fill=WHITE)
    cx, cy = g(11.5, 24)
    r = tile / 48 * 4 * (1 + 0.25 * a)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=WHITE)
    return img.resize((size, size), Image.LANCZOS)


def render(audio: Path, out: Path, size: int, fps: int) -> None:
    levels = loudness(audio, fps)
    out.mkdir(parents=True, exist_ok=True)
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    common = ["-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{size}x{size}", "-r", str(fps),
              "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a", "-shortest", "-c:a", "aac"]
    alpha = subprocess.Popen([ff, *common, "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                              str(out / "presenter-alpha.mov")], stdin=subprocess.PIPE)
    green = subprocess.Popen([ff, *common, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "16",
                              str(out / "presenter-green.mp4")], stdin=subprocess.PIPE)
    backdrop = Image.new("RGBA", (size, size), (*GREEN, 255))
    for i, a in enumerate(levels):
        img = frame(size, i / fps, float(a))
        alpha.stdin.write(img.tobytes())
        green.stdin.write(Image.alpha_composite(backdrop, img).tobytes())
    for p in (alpha, green):
        p.stdin.close()
        p.wait()
    print(f"{len(levels)} frames ({len(levels) / fps:.1f}s) -> {out}/presenter-alpha.mov, presenter-green.mp4")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--audio", required=True, type=Path, help="the voiceover (mp3, wav, m4a, aiff)")
    p.add_argument("--out", type=Path, default=ROOT / "docs" / "video" / "build")
    p.add_argument("--size", type=int, default=360, help="pixels; scale it in the editor")
    p.add_argument("--fps", type=int, default=30)
    a = p.parse_args()
    render(a.audio, a.out, a.size, a.fps)


if __name__ == "__main__":
    main()
