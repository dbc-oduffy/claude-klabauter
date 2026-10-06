"""Pins for published_engine_pin.resolve_published_engine_pin against real git."""

import subprocess
import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2]
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from coordinator_core.git.git_state import format_source_sha_suffix  # noqa: E402
from percolate import published_engine_pin as pep  # noqa: E402

pytestmark = pytest.mark.spawns_process

REF = "refs/remotes/origin/candidate"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path


def _commit(repo: Path, subject: str) -> str:
    _git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def engine(tmp_path):
    e = _repo(tmp_path / "engine")
    a = _commit(e, "A")
    _commit(e, "B")
    return e, a


def _mirror(tmp_path, subjects):
    m = _repo(tmp_path / "mirror")
    for s in subjects:
        tip = _commit(m, s)
    _git(m, "update-ref", REF, tip)
    return m, tip


def test_tip_stamped(tmp_path, engine):
    e, a = engine
    m, tip = _mirror(tmp_path, ["old", "round" + format_source_sha_suffix(a)])
    pin = pep.resolve_published_engine_pin(e, m, REF)
    assert pin.source_sha == a
    assert pin.mirror_commit == tip
    assert pin.engine_toplevel == e


def test_walks_back_past_unstamped_tip(tmp_path, engine):
    e, a = engine
    m, tip = _mirror(tmp_path, ["round" + format_source_sha_suffix(a), "unstamped"])
    pin = pep.resolve_published_engine_pin(e, m, REF)
    assert pin.source_sha == a
    assert pin.mirror_commit == _git(m, "rev-parse", REF + "~1")


def test_no_stamp_within_limit_raises(tmp_path, engine, monkeypatch):
    e, a = engine
    m, _ = _mirror(tmp_path, ["round" + format_source_sha_suffix(a), "x", "y"])
    monkeypatch.setattr(pep, "_STAMP_WALK_LIMIT", 2)
    with pytest.raises(pep.PublishedEnginePinError, match="publish a klabauter round"):
        pep.resolve_published_engine_pin(e, m, REF)


def test_stamp_sha_absent_from_engine_raises(tmp_path, engine):
    e, _ = engine
    m, _ = _mirror(tmp_path, ["round" + format_source_sha_suffix("ab" * 20)])
    with pytest.raises(pep.PublishedEnginePinError, match="fetch claude-klabauter"):
        pep.resolve_published_engine_pin(e, m, REF)


def test_unresolvable_ref_raises(tmp_path, engine):
    e, _ = engine
    m, _ = _mirror(tmp_path, ["x"])
    with pytest.raises(pep.PublishedEnginePinError, match="does not resolve"):
        pep.resolve_published_engine_pin(e, m, "refs/remotes/origin/nope")


def test_success_path_is_exactly_two_git_spawns(tmp_path, engine, monkeypatch):
    e, a = engine
    m, _ = _mirror(tmp_path, ["round" + format_source_sha_suffix(a)])
    calls = []
    real = subprocess.run

    def recording(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(pep.subprocess, "run", recording)
    pep.resolve_published_engine_pin(e, m, REF)
    assert len(calls) == 2
    assert all(c[0] == "git" and "--no-optional-locks" in c for c in calls)
