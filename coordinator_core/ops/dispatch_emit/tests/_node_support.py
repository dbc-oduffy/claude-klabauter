"""Helpers for tests that drive a composed workflow script under node.

Linux caps a single argv or environment string at MAX_ARG_STRLEN (128 KiB), and a composed script
exceeds it. The program therefore goes on stdin (`node -`) and large payloads travel as files whose
path is passed in the environment."""

from __future__ import annotations

import atexit
import os
import tempfile

_made: list[str] = []


def _cleanup() -> None:
    for p in _made:
        try:
            os.unlink(p)
        except OSError:
            pass


atexit.register(_cleanup)


def payload_file(text: str) -> str:
    """Path of a temp file holding `text` (utf-8); removed at interpreter exit."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    _made.append(path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path
