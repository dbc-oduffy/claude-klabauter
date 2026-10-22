"""Bulk own-path-claim release: many paths, or all, in one sink pass; peers untouched."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session import claim_index, claims, core, scope, touch_record
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    for args in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "t@example.com"],
        ["git", "config", "user.name", "t"],
    ):
        subprocess.run(args, cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"], cwd=tmp_path, **no_console_passthrough_kwargs()
    )
    return tmp_path


def _set_me(monkeypatch, sid="em-sid"):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", sid)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim(repo, sid, paths):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    sink = sdir / scope._TOUCH_RECORD_FILENAME
    for p in paths:
        touch_record.append_event(
            sink, session_id=sid, agent_id=None, verb=touch_record.VERB_TOUCH, path=p
        )


def _holders(repo, paths):
    base = core.sessions_dir(cwd=str(repo))
    return claim_index.lookup(list(paths), sessions_dir=base, cwd=str(repo))


def _count_sink_scans(monkeypatch):
    calls = []
    real = scope._release_from_touch_record

    def spy(*a, **kw):
        calls.append(a[0])
        return real(*a, **kw)

    monkeypatch.setattr(scope, "_release_from_touch_record", spy)
    return calls


def test_many_paths_released_in_one_call(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    paths = [f"src/f{i}.py" for i in range(25)]
    _claim(repo, "em-sid", paths)
    assert all(v == ["em-sid"] for v in _holders(repo, paths).values())

    assert claims._release_path_claims_bulk(paths, cwd=str(repo)) is True

    assert all(v == [] for v in _holders(repo, paths).values())


def test_sink_scan_count_does_not_grow_with_n(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    small = [f"a/f{i}.py" for i in range(3)]
    large = [f"b/f{i}.py" for i in range(60)]
    _claim(repo, "em-sid", small + large)

    scans = _count_sink_scans(monkeypatch)
    claims._release_path_claims_bulk(small, cwd=str(repo))
    n_small = len(scans)
    scans.clear()
    claims._release_path_claims_bulk(large, cwd=str(repo))

    assert n_small >= 1
    assert len(scans) == n_small


def test_peer_claim_on_named_path_is_untouched(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    _claim(repo, "em-sid", ["src/mine.py", "src/shared.py"])
    _claim(repo, "peer-sid", ["src/shared.py", "src/theirs.py"])

    claims._release_path_claims_bulk(
        ["src/mine.py", "src/shared.py", "src/theirs.py"], cwd=str(repo)
    )

    got = _holders(repo, ["src/mine.py", "src/shared.py", "src/theirs.py"])
    assert got["src/mine.py"] == []
    assert got["src/shared.py"] == ["peer-sid"]
    assert got["src/theirs.py"] == ["peer-sid"]


def test_all_own_mode_releases_everything_own_and_nothing_peer(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    own = [f"src/o{i}.py" for i in range(10)]
    _claim(repo, "em-sid", own)
    _claim(repo, "peer-sid", ["src/p.py"])

    assert claims._release_path_claims_bulk(None, cwd=str(repo)) is True

    assert all(v == [] for v in _holders(repo, own).values())
    assert _holders(repo, ["src/p.py"]) == {"src/p.py": ["peer-sid"]}


def test_empty_list_is_a_noop_not_release_all(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    _claim(repo, "em-sid", ["src/keep.py"])

    claims._release_path_claims_bulk([], cwd=str(repo))

    assert _holders(repo, ["src/keep.py"]) == {"src/keep.py": ["em-sid"]}
