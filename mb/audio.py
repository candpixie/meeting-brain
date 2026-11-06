import signal
import subprocess
from pathlib import Path

import numpy as np

from . import config


def load(path):
    """Any audio/video file -> float32 mono 16 kHz."""
    out = subprocess.run(
        [config.FFMPEG, "-loglevel", "error", "-i", str(path), "-f", "f32le", "-ac", "1",
         "-ar", str(config.SR), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.float32).copy()


def to_opus(src, dst, kbps=16):
    """Speech-grade Opus: ~7 MB per hour at 16 kbps."""
    subprocess.run([config.FFMPEG, "-loglevel", "error", "-y", "-i", str(src), "-vn", "-ac", "1",
                    "-c:a", "libopus", "-b:a", f"{kbps}k", "-application", "voip", str(dst)], check=True)
    return Path(dst)


def write_clip(samples, dst, kbps=24):
    """Save a numpy clip as Opus (used for training clips you corrected)."""
    p = subprocess.Popen([config.FFMPEG, "-loglevel", "error", "-y", "-f", "f32le", "-ac", "1",
                          "-ar", str(config.SR), "-i", "-", "-c:a", "libopus", "-b:a", f"{kbps}k", str(dst)],
                         stdin=subprocess.PIPE)
    p.communicate(np.asarray(samples, dtype=np.float32).tobytes())
    return Path(dst)


def record(dst, device=":0"):
    """Record the default mic straight to Opus until Ctrl-C. `mb devices` lists inputs."""
    p = subprocess.Popen([config.FFMPEG, "-loglevel", "error", "-y", "-f", "avfoundation", "-i", device,
                          "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(dst)],
                         stdin=subprocess.PIPE)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.send_signal(signal.SIGINT)  # lets ffmpeg finalize the file
        p.wait()
    return Path(dst)


def devices():
    r = subprocess.run([config.FFMPEG, "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True)
    return "\n".join(l for l in r.stderr.splitlines() if "AVFoundation" in l and "]" in l)


def compress_lecture(src, dst=None, height=720, fps=10, crf=30, fast=False):
    """Long lecture video -> small mkv. Slides barely move, so low fps + HEVC shrinks it a lot.
    fast=True uses the Apple hardware encoder (much quicker, somewhat bigger file)."""
    src = Path(src)
    dst = Path(dst) if dst else src.with_name(src.stem + ".small.mkv")
    v = (["-c:v", "hevc_videotoolbox", "-q:v", "45", "-tag:v", "hvc1"] if fast
         else ["-c:v", "libx265", "-crf", str(crf), "-preset", "medium", "-x265-params", "log-level=error"])
    subprocess.run([config.FFMPEG, "-loglevel", "error", "-stats", "-y", "-i", str(src),
                    "-vf", f"scale=-2:'min({height},ih)',fps={fps}", *v,
                    "-c:a", "libopus", "-b:a", "24k", "-ac", "1", str(dst)], check=True)
    return dst
