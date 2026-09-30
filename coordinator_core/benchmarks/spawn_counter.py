"""
coordinator_core.benchmarks.spawn_counter

Seam-independent, origin-attributed process-spawn counter shared by the
whole-op spawn-count pins.

Negative spec:
  - Does NOT count through the `git_native._git` seam. `run_git`
    (`coordinator_core/git/run.py`) reaches git WITHOUT going through `_git`,
    so a `_git`-scoped counter undercounts (see
    `test_commit_authored_new_file.py`'s own module docstring for the exact
    incident: a `_git`-scoped counter read 3 while the leg issued 4
    processes). This counter patches `subprocess.Popen` in the `subprocess`
    module itself, seam-independently, and does NOT also patch
    `subprocess.run` (which constructs a `Popen`, so patching both would
    double-count every `run()`-shaped spawn).
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, NamedTuple

import pytest


class _AttributedSpawn(NamedTuple):
    argv: tuple[str, ...]
    origin: str


#: `_LEGITIMIZED_SITES` key shape (`(relpath, enclosing, argv0, ordinal)`'s
_GIT_NATIVE_SUFFIX = "coordinator_core/ops/ceremony/git_native.py"
_RUN_GIT_SUFFIX = "coordinator_core/git/run.py"
_COMMIT_SIGNING_SUFFIX = "coordinator_core/git/commit_signing.py"


def _attribute_frame(frame) -> str | None:
    filename = Path(frame.f_code.co_filename).as_posix()
    if filename.endswith(_GIT_NATIVE_SUFFIX) and frame.f_code.co_name == "_invoke":
        return "_git._invoke"
    if filename.endswith(_RUN_GIT_SUFFIX) and frame.f_code.co_name == "run_git":
        return "run_git"
    if (
        filename.endswith(_COMMIT_SIGNING_SUFFIX)
        and frame.f_code.co_name == "write_signed_commit_object"
    ):
        return "write_signed_commit_object"
    return None


@contextmanager
def _count_spawns_attributed(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[_AttributedSpawn]]:
    recorded: list[_AttributedSpawn] = []
    real_popen = subprocess.Popen

    class _Popen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            origin = "unattributed"
            frame = __import__("sys")._getframe(1)
            while frame is not None:
                site = _attribute_frame(frame)
                if site is not None:
                    origin = site
                    break
                frame = frame.f_back
            argv = tuple(str(x) for x in args) if isinstance(args, (list, tuple)) else (str(args),)
            recorded.append(_AttributedSpawn(argv=argv, origin=origin))
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    try:
        yield recorded
    finally:
        monkeypatch.undo()
