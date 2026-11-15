"""Helpers for tests that drive a composed workflow script under node.

Linux caps a single argv or environment string at MAX_ARG_STRLEN (128 KiB), and a composed script
exceeds it. The program therefore goes on stdin (`node -`) and large payloads travel as files whose
path is passed in the environment."""

from __future__ import annotations

import os
import tempfile

_made: list[str] = []


def release_payload_files() -> None:
    """Unlink every file `payload_file` made since the last release (the conftest autouse fixture
    calls it per test: an atexit hook runs after the session's managed-temp leak check)."""
    while _made:
        try:
            os.unlink(_made.pop())
        except OSError:
            pass


def payload_file(text: str) -> str:
    """Path of a temp file holding `text` (utf-8); removed when the calling test ends."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    _made.append(path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path
