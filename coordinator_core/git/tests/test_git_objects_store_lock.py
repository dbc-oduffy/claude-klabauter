"""git_objects._write_pack / _flush_pack take the object-store lock; a held lock sends them loose."""

from __future__ import annotations

import hashlib
import textwrap
from pathlib import Path

import pytest

from coordinator_core.git import git_objects as go

_REPO_ROOT = str(Path(__file__).resolve().parents[3])


@pytest.fixture(autouse=True)
def _unpaused(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _read_all(common: Path, pending: dict) -> None:
    for sha, (_kind, payload) in pending.items():
        got = go.read_object(common, sha)
        assert got is not None and got[1] == payload


def test_write_pack_uncontended_places_a_pack(common: Path) -> None:
    pending = _pending(12, b"free")
    go._write_pack(common, pending)
    assert any(n.endswith(".idx") for n in _packs(common))
    _read_all(common, pending)


def test_flush_pack_uncontended_places_a_pack_and_tail_marker(common: Path) -> None:
    pending = _pending(12, b"free-flush")
    go._flush_pack(common, pending)
    names = _packs(common)
    assert any(n.endswith(".idx") for n in names) and go._TAIL_MARKER in names
    _read_all(common, pending)
