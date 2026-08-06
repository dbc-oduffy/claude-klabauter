"""_polyglot_git_scan.py — shared INDEX-blob scanning helpers for the
polyglot/docstring-header regression suites in this directory.

Extracted from `test_no_bin_polyglot_invariant.py` (which defined this
machinery first) so the header-reading algorithm exists exactly once —
`test_no_bin_docstring_header.py` imports it rather than re-deriving a
second copy. Both suites need the same primitive: enumerate tracked files
under `coordinator/` via `git ls-files` and read a candidate's INDEX-staged
blob (`git show :<path>`, not the working tree) — concurrent migrations
land in the worktree while this scan runs, so a worktree-based read would
see a moving target; the INDEX is what actually ships in the next commit.
Matches the posture of `test_shebang_index_mode_invariant.py`.

Any third suite in this directory that needs "enumerate tracked files
under coordinator/ and read their INDEX blobs" should import from here,
not copy `_git`/`blob_header` again.
"""
from __future__ import annotations

import os
import subprocess

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(_TESTS_DIR))
)  # .../coordinator/bin/tests -> .../coordinator/bin -> .../coordinator -> repo root
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", _REPO_ROOT, *args],
        capture_output=True,
        text=True,
        check=False,
        creationflags=_NO_WINDOW,
    )


def tracked_files_under_coordinator() -> list[str]:
    """Return repo-root-relative paths of every tracked file under coordinator/."""
    result = _git("ls-files", "--", "coordinator")
    return [line for line in result.stdout.splitlines() if line.strip()]


def tracked_bin_direct_children() -> list[str]:
    """Return repo-root-relative paths of tracked files directly under
    coordinator/bin/ (non-recursive — matches the .cmd launcher's own
    co-location convention: every existing `<name>.cmd` sibling lives
    directly under coordinator/bin/, never in a subdirectory)."""
    result = _git("ls-files", "--", "coordinator/bin")
    out = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        rel = line[len("coordinator/bin/") :]
        if "/" not in rel:
            out.append(line)
    return out


# Cache of full blob text, keyed by path — avoids re-invoking `git show` for
# the same path across multiple assertions/suites in a single test run.
_BLOB_CACHE: dict[str, str] = {}


def _blob_text(path: str) -> str:
    if path not in _BLOB_CACHE:
        result = _git("show", f":{path}")
        _BLOB_CACHE[path] = result.stdout
    return _BLOB_CACHE[path]


def blob_header(path: str, n: int) -> list[str]:
    """Return the first `n` lines of the INDEX-staged blob at `path` (not
    the working tree — see module docstring for why)."""
    text = _blob_text(path)
    lines = text.split("\n") if text else []
    return lines[:n]


def blob_first_line(path: str) -> str:
    header = blob_header(path, 1)
    return header[0] if header else ""


def blob_full_text(path: str) -> str:
    """Return the FULL INDEX-staged blob text at `path` — needed for
    structural (ast-based) parsing, unlike `blob_header`, which truncates to
    a header window."""
    return _blob_text(path)
