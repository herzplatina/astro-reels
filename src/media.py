#!/usr/bin/env python3
"""Small shared helpers around the ffmpeg toolchain.

Kept separate so the music tools can use them without importing the renderer,
which would pull in Pillow and the whole validation stack for the sake of one
subprocess call.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def require_tools(*tools: str) -> None:
    """Fail with an instruction rather than a traceback from deep in a call.

    Without this the first sign of a missing ffmpeg is a FileNotFoundError
    raised from inside subprocess, several frames from anything the reader
    recognises.
    """
    wanted = tools or ("ffmpeg", "ffprobe")
    missing = [tool for tool in wanted if not shutil.which(tool)]
    if missing:
        raise SystemExit(
            f"Required tool{'s' if len(missing) > 1 else ''} not found: "
            f"{', '.join(missing)}.\nInstall with: brew install ffmpeg"
        )


def probe_duration(path: Path) -> float:
    """Length of a media file in seconds, or 0.0 if it cannot be determined.

    Returns rather than raises: callers use this to choose a music offset or a
    preview point, and neither is worth failing a render over.
    """
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return 0.0
