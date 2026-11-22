"""The publish round's materialise step must not serialise per-file create latency.

Hang path (EM faulthandler repro, scratch/sweep-repro/fh/): in a 2368s `--no-commit` round,
`publish._extract_git_archive` spent 1499.5s in `tarfile.extractall` (10 of 10 stack snapshots sat in
`tarfile.makefile`'s `open(path, "wb")` / close), against 9.9s measured on a quiet box. One thread
creates ~5k files back to back, so per-file create latency on a loaded Windows box multiplies by the
file count with nothing to overlap it. Pinned here on tmp_path fixtures with a stalled create, bounded
by a join timeout; no real round, no git.

Run: python -m pytest coordinator/bin/tests/test_publish_clone_hang_path.py -q
"""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import tarfile
import threading
import time
import traceback
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent

_FILES = 24
_STALL_SECS = 0.15
_JOIN_BOUND_SECS = 30.0


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_clone_hang_path_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _fake_git_archive(monkeypatch) -> None:
    """Stub `git archive -o <path>` so it writes a real tar of `_FILES` small files."""
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda toplevel, sha=None: ())

    def _run(cmd, **_kwargs):
        out = Path(cmd[cmd.index("-o") + 1])
        with tarfile.open(out, "w") as tar:
            for index in range(_FILES):
                data = f"payload {index}\n".encode()
                info = tarfile.TarInfo(f"dir/file{index}.txt")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(publish.subprocess, "run", _run)


def _stall_file_creates(monkeypatch, stall_secs: float) -> None:
    """Every tarfile file create takes `stall_secs`, the way an antivirus-scanned NTFS create does."""
    real_open = tarfile.bltn_open

    def _slow_open(path, mode="r", *args, **kwargs):
        if "w" in mode:
            time.sleep(stall_secs)
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(tarfile, "bltn_open", _slow_open)


def _extract_bounded(tmp_path: Path) -> "tuple[float, Path | None, str]":
    """Run `_extract_git_archive` on a worker thread; return (elapsed, result, stuck-frame text)."""
    box: dict = {}

    def _target() -> None:
        try:
            box["result"] = publish._extract_git_archive(tmp_path, "SHA")
        except BaseException as exc:  # noqa: BLE001 - surfaced through the assertion below
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    start = time.perf_counter()
    worker.start()
    worker.join(_JOIN_BOUND_SECS)
    elapsed = time.perf_counter() - start
    stuck = ""
    if worker.is_alive():
        frame = sys._current_frames().get(worker.ident)
        stuck = "".join(traceback.format_stack(frame)) if frame is not None else ""
    if "error" in box:
        raise box["error"]
    return elapsed, box.get("result"), stuck


def test_extraction_unstalled_materialises_every_file(monkeypatch, tmp_path):
    """The accepting side: the tree lands complete when nothing is slow."""
    _fake_git_archive(monkeypatch)
    elapsed, shadow, _ = _extract_bounded(tmp_path)
    assert shadow is not None
    assert len(list((shadow / "dir").glob("file*.txt"))) == _FILES
    assert elapsed < _JOIN_BOUND_SECS


@pytest.mark.pending_fix
def test_extraction_overlaps_per_file_create_latency(monkeypatch, tmp_path):
    """Hang path: `_extract_git_archive` -> `tarfile.extractall` -> `makefile` -> `open(path, "wb")` runs
    the creates serially, so `_FILES * _STALL_SECS` is paid in full. Overlapped creates finish in a
    fraction of it."""
    _fake_git_archive(monkeypatch)
    _stall_file_creates(monkeypatch, _STALL_SECS)
    serial_secs = _FILES * _STALL_SECS
    elapsed, shadow, stuck = _extract_bounded(tmp_path)
    assert shadow is not None, f"extraction did not finish within {_JOIN_BOUND_SECS}s; blocked at:\n{stuck}"
    assert len(list((shadow / "dir").glob("file*.txt"))) == _FILES
    assert elapsed < serial_secs / 2, (
        f"{_FILES} file creates stalled {_STALL_SECS}s each took {elapsed:.2f}s (serial cost {serial_secs:.2f}s): "
        "publish._extract_git_archive blocks one thread in tarfile.extractall -> makefile -> open(path, 'wb') per file"
    )
