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


def _claim_absolute(repo, sid, abs_paths):
    """Hand-write ``T`` lines with ABSOLUTE paths, the shape ``encode_line`` itself refuses."""
    import json

    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    with open(sdir / scope._TOUCH_RECORD_FILENAME, "ab") as fh:
        for p in abs_paths:
            rec = {"v": touch_record.SCHEMA_VERSION, "verb": "T", "ts": 1.0, "sid": sid, "agent": None, "path": p}
            fh.write((json.dumps(rec, separators=(",", ":")) + "\n").encode())


def _spy_spawns_and_appends(monkeypatch):
    spawns, appends = [], []
    real_git = scope._git_run
    real_append = touch_record.atomic_append.append_line

    def git_spy(args, cwd=None):
        spawns.append(args)
        return real_git(args, cwd)

    def append_spy(sink, encoded, *a, **kw):
        appends.append(str(sink))
        return real_append(sink, encoded, *a, **kw)

    monkeypatch.setattr(scope, "_git_run", git_spy)
    monkeypatch.setattr(touch_record.atomic_append, "append_line", append_spy)
    return spawns, appends


def _sink_lines(repo, sid):
    sink = Path(repo) / ".git" / "coordinator-sessions" / sid / scope._TOUCH_RECORD_FILENAME
    return sink.read_text().splitlines()


def test_full_release_of_many_claims_is_one_append_and_zero_spawns(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    paths = [f"d{i % 7}/f{i}.py" for i in range(2000)]
    _claim(repo, "em-sid", paths)
    _claim(repo, "peer-sid", ["d0/f0.py"])

    spawns, appends = _spy_spawns_and_appends(monkeypatch)
    assert claims._release_path_claims_bulk(None, cwd=str(repo)) is True

    own_sink = str(Path(repo) / ".git" / "coordinator-sessions" / "em-sid" / scope._TOUCH_RECORD_FILENAME)
    assert [a for a in appends if a == own_sink] == [own_sink]
    assert spawns == []
    got = _holders(repo, paths[:50])
    assert got["d0/f0.py"] == ["peer-sid"]
    assert all(v == [] for k, v in got.items() if k != "d0/f0.py")
    assert len([ln for ln in _sink_lines(repo, "em-sid") if '"verb":"R"' in ln]) == 2000


def test_absolute_claims_release_with_at_most_one_batched_spawn(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (tmp_path / "linkdir").symlink_to(real_dir, target_is_directory=True)
    plain = [str(tmp_path / f"abs{i}.txt") for i in range(30)]
    via_link = [str(tmp_path / "linkdir" / f"l{i}.txt") for i in range(30)]
    outside = ["/definitely/not/in/the/repo.txt"]
    _claim_absolute(repo, "em-sid", plain + via_link + outside)
    _claim(repo, "em-sid", ["rel/a.py", "rel/b.py"])

    spawns, _appends = _spy_spawns_and_appends(monkeypatch)
    scope.release_own_path_claims_bulk("em-sid", None, cwd=str(repo))

    assert len(spawns) <= 1
    claimed, degraded = scope._read_touch_record_as_legacy_lines(
        Path(repo) / ".git" / "coordinator-sessions" / "em-sid" / scope._TOUCH_RECORD_FILENAME
    )
    still = scope.project_self_scope(claimed)
    assert not degraded
    assert "rel/a.py" not in still and "rel/b.py" not in still
    assert set(still) <= set(outside)


def _agent_sink(repo, aid, owner_sid):
    adir = Path(repo) / ".git" / "coordinator-sessions" / ".agents" / aid
    adir.mkdir(parents=True, exist_ok=True)
    (adir / "em-session-id.txt").write_text(owner_sid + "\n", encoding="utf-8")
    return adir / scope._TOUCH_RECORD_FILENAME


def _write_absolute(sink, sid, aid, abs_paths):
    import json

    with open(sink, "ab") as fh:
        for p in abs_paths:
            rec = {"v": touch_record.SCHEMA_VERSION, "verb": "T", "ts": 1.0, "sid": sid, "agent": aid, "path": p}
            fh.write((json.dumps(rec, separators=(",", ":")) + "\n").encode())


def _still_held(sink):
    lines, degraded = scope._read_touch_record_as_legacy_lines(sink)
    assert not degraded
    return set(scope.project_self_scope(lines))


def test_absolute_agent_sink_claims_release_in_one_batched_pass(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (tmp_path / "linkdir").symlink_to(real_dir, target_is_directory=True)
    inside = [str(Path(repo) / "src" / f"a{i}.py") for i in range(40)]
    via_link = [str(tmp_path / "linkdir" / f"l{i}.txt") for i in range(40)]
    outside = ["/definitely/not/in/the/repo.txt"]
    own = _agent_sink(repo, "agent-own", "em-sid")
    peer = _agent_sink(repo, "agent-peer", "peer-sid")
    _write_absolute(own, "em-sid", "agent-own", inside + via_link + outside + ["rel/x.py"])
    _write_absolute(peer, "peer-sid", "agent-peer", inside)

    spawns, _appends = _spy_spawns_and_appends(monkeypatch)
    scope.release_own_path_claims_bulk("em-sid", None, cwd=str(repo))

    assert len(spawns) <= 1
    assert _still_held(own) == set(outside)
    assert _still_held(peer) == set(inside)


def test_named_path_releases_only_that_absolute_agent_claim(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _set_me(monkeypatch)
    a, b = str(Path(repo) / "src" / "a.py"), str(Path(repo) / "src" / "b.py")
    own = _agent_sink(repo, "agent-own", "em-sid")
    _write_absolute(own, "em-sid", "agent-own", [a, b])

    scope.release_own_path_claims_bulk("em-sid", ["src/a.py"], cwd=str(repo))

    assert _still_held(own) == {b}


def test_bulk_normaliser_never_spawns_per_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    real_dir = tmp_path / "realdir"
    real_dir.mkdir()
    (tmp_path / "linkdir").symlink_to(real_dir, target_is_directory=True)
    raws = [str(tmp_path / "linkdir" / f"l{i}.txt") for i in range(100)]
    spawns = []
    real_git = scope._git_run
    monkeypatch.setattr(scope, "_git_run", lambda a, cwd=None: (spawns.append(a), real_git(a, cwd))[1])

    norm = scope._BulkNormalizer(str(repo))
    norm.prefetch(raws)

    assert len(spawns) == 1
    assert all(norm(r) for r in raws)
    assert len(spawns) == 1


def test_append_events_writes_all_lines_once_and_drops_only_the_overlong(tmp_path, monkeypatch):
    sink = tmp_path / "s" / "touch-record.jsonl"
    events = [
        {"session_id": "s", "agent_id": None, "verb": touch_record.VERB_RELEASE, "path": "a.py", "name": None},
        {"session_id": "s", "agent_id": None, "verb": touch_record.VERB_RELEASE, "path": "x" * 5000, "name": None},
        {"session_id": "s", "agent_id": None, "verb": touch_record.VERB_RELEASE, "path": "b.py", "name": None},
    ]
    calls = []
    real = touch_record.atomic_append.append_line
    monkeypatch.setattr(
        touch_record.atomic_append, "append_line", lambda *a, **kw: (calls.append(a), real(*a, **kw))[1]
    )
    assert touch_record.append_events(sink, events) == 2
    assert touch_record.append_events(sink, []) == 0
    assert len(calls) == 1
    assert [ln.count("a.py") + ln.count("b.py") for ln in sink.read_text().splitlines()] == [1, 1]
