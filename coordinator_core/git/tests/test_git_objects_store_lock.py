"""git_objects._write_pack / _flush_pack take the object-store lock; a held lock sends them loose."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go

_REPO_ROOT = str(Path(__file__).resolve().parents[3])


@pytest.fixture(autouse=True)
def _unpaused(monkeypatch):
    """These tests pin the lock around the fold; the paused behaviour is pinned in test_pack_deletion_pause."""
    from coordinator_core.git import pack_deletion_pause

    monkeypatch.setattr(pack_deletion_pause, "PACK_DELETION_PAUSED", False)

_HOLDER_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from coordinator_core.git.object_store_lock import hold
    with hold(Path(sys.argv[1]), wait_s=30.0, holder_label="holder"):
        Path(sys.argv[2]).write_text("ready")
        sys.stdin.read()
    """
)


def _blob_sha(payload: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(payload) + payload).hexdigest()


def _pending(n: int, tag: bytes) -> dict:
    out = {}
    for i in range(n):
        payload = b"%s-%d" % (tag, i)
        out[_blob_sha(payload)] = (b"blob", payload)
    return out


def _packs(common: Path) -> list[str]:
    return sorted(p.name for p in (common / "objects" / "pack").iterdir())


@pytest.fixture()
def common(tmp_path: Path) -> Path:
    c = tmp_path / "repo" / ".git"
    (c / "objects" / "pack").mkdir(parents=True)
    (c / "HEAD").write_text("ref: refs/heads/main\n")
    return c


@pytest.fixture()
def held_elsewhere(common: Path, tmp_path: Path):
    ready = tmp_path / "ready"
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CHILD, str(common), str(ready)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while not ready.exists():
        assert time.monotonic() < deadline, "holder never took the lock"
        time.sleep(0.02)
    try:
        yield
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)


def _read_all(common: Path, pending: dict) -> None:
    for sha, (_kind, payload) in pending.items():
        got = go.read_object(common, sha)
        assert got is not None and got[1] == payload


def test_write_pack_uncontended_places_a_pack(common: Path):
    pending = _pending(12, b"free")
    go._write_pack(common, pending)
    assert any(n.endswith(".idx") for n in _packs(common))
    _read_all(common, pending)


def test_flush_pack_uncontended_places_a_pack_and_tail_marker(common: Path):
    pending = _pending(12, b"free-flush")
    go._flush_pack(common, pending)
    names = _packs(common)
    assert any(n.endswith(".idx") for n in names) and go._TAIL_MARKER in names
    _read_all(common, pending)


def test_write_pack_held_elsewhere_falls_back_loose(common: Path, held_elsewhere):
    pending = _pending(12, b"busy-write")
    go._write_pack(common, pending)
    assert _packs(common) == []
    _read_all(common, pending)


def test_flush_pack_held_elsewhere_never_touches_pack_dir(common: Path, held_elsewhere):
    pending = _pending(12, b"busy-flush")
    go._flush_pack(common, pending)
    assert _packs(common) == []
    _read_all(common, pending)


def test_flush_pack_held_elsewhere_keeps_existing_tail(common: Path):
    first = _pending(12, b"tail")
    go._flush_pack(common, first)
    before = _packs(common)
    marker_before = (common / "objects" / "pack" / go._TAIL_MARKER).read_text()

    ready = common.parent / "ready2"
    env = dict(os.environ)
    env["PYTHONPATH"] = _REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_CHILD, str(common), str(ready)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)
        second = _pending(12, b"tail-second")
        go._flush_pack(common, second)
        assert _packs(common) == before
        assert (common / "objects" / "pack" / go._TAIL_MARKER).read_text() == marker_before
        _read_all(common, first)
        _read_all(common, second)
    finally:
        holder.stdin.close()
        holder.communicate(timeout=60)
