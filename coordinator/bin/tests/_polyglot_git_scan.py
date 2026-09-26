from __future__ import annotations

import os
import subprocess

from coordinator_core.win_portability import no_console_creationflags

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(_TESTS_DIR))
)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", _REPO_ROOT, *args],
        capture_output=True,
        text=True,
        check=False,
        **no_console_creationflags(),
    )


def tracked_files_under_coordinator() -> list[str]:
    result = _git("ls-files", "--", "coordinator")
    return [line for line in result.stdout.splitlines() if line.strip()]


def tracked_bin_direct_children() -> list[str]:
    result = _git("ls-files", "--", "coordinator/bin")
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[len("coordinator/bin/") :]
        if "/" not in rel:
            out.append(line)
    return out


_BLOB_CACHE: dict[str, str] = {}


def _blob_text(path: str) -> str:
    if path not in _BLOB_CACHE:
        result = _git("show", f":{path}")
        _BLOB_CACHE[path] = result.stdout
    return _BLOB_CACHE[path]


def blob_header(path: str, n: int) -> list[str]:
    text = _blob_text(path)
    lines = text.split("\n") if text else []
    return lines[:n]


def blob_first_line(path: str) -> str:
    header = blob_header(path, 1)
    return header[0] if header else ""


def blob_full_text(path: str) -> str:
    return _blob_text(path)
