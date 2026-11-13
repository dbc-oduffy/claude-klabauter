"""Tests for coordinator_core.ops.rag_freshness."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops import rag_freshness as rf


def _verdict(state, remedy=("project-rag", "refresh")):
    return lambda repo: rf.FreshnessVerdict(state=state, remedy=remedy, source="stub")


@pytest.mark.parametrize("state", ["stale", "absent"])
def test_stale_or_absent_queues_one_refresh(tmp_path, state):
    calls = []
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict(state), spawn_fn=lambda a, c: calls.append((a, c)), marker_root=tmp_path / "m"
    )
    assert out["state"] == state and out["refresh_queued"] is True
    assert calls == [(["project-rag", "refresh"], str(tmp_path))]


@pytest.mark.parametrize("state", ["fresh", "unknown"])
def test_fresh_or_unknown_queues_nothing(tmp_path, state):
    calls = []
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict(state), spawn_fn=lambda a, c: calls.append(a), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False and calls == []


def test_live_marker_suppresses_spawn_and_expired_marker_allows_one(tmp_path):
    calls = []
    kw = dict(verdict_fn=_verdict("stale"), spawn_fn=lambda a, c: calls.append(a), marker_root=tmp_path / "m")
    assert rf.freshness_precondition(str(tmp_path), now=1000.0, **kw)["refresh_queued"] is True
    live = rf.freshness_precondition(str(tmp_path), now=1000.0 + rf.MARKER_TTL_SECONDS - 1, **kw)
    assert live["refresh_queued"] is False and "already queued" in live["message"]
    assert len(calls) == 1
    expired = rf.freshness_precondition(str(tmp_path), now=1000.0 + rf.MARKER_TTL_SECONDS + 1, **kw)
    assert expired["refresh_queued"] is True and len(calls) == 2


def test_missing_settings_home_is_typed_refusal_failing_open(tmp_path, monkeypatch):
    import coordinator_core._settings_home as sh

    def boom():
        raise RuntimeError("unset")

    monkeypatch.setattr(sh, "settings_home", boom)
    with pytest.raises(rf.FreshnessRefusal) as ei:
        rf.queue_refresh(str(tmp_path), ["x"], spawn_fn=lambda a, c: None)
    assert ei.value.key == rf.SETTINGS_HOME_KEY and rf.SETTINGS_HOME_KEY in str(ei.value)
    out = rf.freshness_precondition(str(tmp_path), verdict_fn=_verdict("stale"), spawn_fn=lambda a, c: None)
    assert out["refresh_queued"] is False and rf.SETTINGS_HOME_KEY in out["message"]


def test_spawn_failure_fails_open_and_clears_marker(tmp_path):
    def bad(a, c):
        raise OSError("nope")

    root = tmp_path / "m"
    out = rf.freshness_precondition(str(tmp_path), verdict_fn=_verdict("absent"), spawn_fn=bad, marker_root=root)
    assert out["refresh_queued"] is False and "spawn failed" in out["message"]
    calls = []
    again = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict("absent"), spawn_fn=lambda a, c: calls.append(a), marker_root=root
    )
    assert again["refresh_queued"] is True and len(calls) == 1


def test_verdict_failure_fails_open(tmp_path):
    def bad(repo):
        raise RuntimeError("down")

    out = rf.freshness_precondition(str(tmp_path), verdict_fn=bad, marker_root=tmp_path / "m")
    assert out["state"] == "unknown" and out["refresh_queued"] is False


def test_stale_without_remedy_queues_nothing(tmp_path):
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict("stale", remedy=()), spawn_fn=lambda a, c: pytest.fail("spawned"), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False and "no remedy" in out["message"]


def _fixture():
    path = Path(__file__).parent / "fixtures" / "rag_freshness_verdict_v1.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _fake_capacity(monkeypatch, events, admitted=True):
    from coordinator_core.bash_guards import _heavy_command_class as cap

    monkeypatch.setattr(cap, "_load_box_memory_host", lambda: "host")

    def admit(key, peak, host=None, register_pid=None, **kw):
        events.append(("admit", key))
        return cap.SpawnAdmission(admitted, "held" if not admitted else "ok", key, peak)

    monkeypatch.setattr(cap, "admit_before_spawn", admit)
    monkeypatch.setattr(cap, "register_spawn", lambda key, peak, pid, host=None: events.append(("register", key)))
    monkeypatch.setattr(cap, "release_spawn", lambda key, pid, host=None: events.append(("release", key)))


def _popen(monkeypatch, events, seen):
    def fake(argv, **kw):
        events.append(("popen", None))
        seen.update(argv=argv, **kw)
        return SimpleNamespace(pid=4242)

    monkeypatch.setattr(subprocess, "Popen", fake)


@pytest.mark.parametrize(
    "slug,state,queued_argv,action",
    [
        ("repo_stale", "stale", True, "refresh_from_release"),
        ("repo_absent", "absent", False, "register_publish_repo"),
        ("repo_current", "fresh", False, "none"),
        ("repo_unknown", "unknown", False, ""),
    ],
)
def test_fixture_rows_map_to_states(slug, state, queued_argv, action):
    v = rf.verdict_from_reply(_fixture(), [slug])
    assert v.state == state and v.action == action and bool(v.remedy) is queued_argv


def test_register_publish_repo_is_surfaced_never_spawned(tmp_path):
    out = rf.freshness_precondition(
        str(tmp_path),
        verdict_fn=lambda r: rf.verdict_from_reply(_fixture(), ["repo_absent"]),
        spawn_fn=lambda a, c: pytest.fail("spawned"),
        marker_root=tmp_path / "m",
    )
    assert out["state"] == "absent" and out["refresh_queued"] is False and "set-publish-repo" in out["message"]


def _mutated(fn):
    reply = copy.deepcopy(_fixture())
    fn(reply["repos"][0])
    return reply


@pytest.mark.parametrize(
    "reply",
    [
        {**_fixture(), "schema_version": 2},
        {**_fixture(), "schema_version": True},
        {**_fixture(), "schema_version": "1"},
        {"repos": []},
        [],
        _mutated(lambda r: r.update(verdict="weird")),
        _mutated(lambda r: r.update(reason="brand_new_reason")),
        _mutated(lambda r: r.update(reason=None)),
        _mutated(lambda r: r["remedy"].update(action="rebuild_everything")),
        _mutated(lambda r: r["remedy"].update(action="none")),
        _mutated(lambda r: r["remedy"].update(argv=[])),
        _mutated(lambda r: r.pop("remedy")),
    ],
)
def test_unrecognised_or_malformed_reply_reads_unknown_and_queues_nothing(tmp_path, reply):
    v = rf.verdict_from_reply(reply, ["repo_stale"])
    assert v.state == "unknown" and not v.remedy
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=lambda r: v, spawn_fn=lambda a, c: pytest.fail("spawned"), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False


def test_current_with_reason_or_action_is_not_fresh():
    reply = copy.deepcopy(_fixture())
    cur = reply["repos"][2]
    cur["reason"] = "behind_head"
    assert rf.verdict_from_reply(reply, ["repo_current"]).state == "unknown"
    cur["reason"] = None
    cur["remedy"]["action"] = "refresh_from_release"
    assert rf.verdict_from_reply(reply, ["repo_current"]).state == "unknown"


def test_row_selection_requires_exactly_one_match():
    assert rf.verdict_from_reply(_fixture(), ["nobody"]).state == "unknown"
    assert rf.verdict_from_reply(_fixture(), ["repo_stale", "repo_current"]).state == "unknown"
    assert rf.verdict_from_reply({"schema_version": 1, "repos": []}, ["repo_stale"]).state == "unknown"


def _rag(monkeypatch, found=True):
    import coordinator_core.install.rag_fleet_registration as reg

    monkeypatch.setattr(
        reg, "detect_example_retrieval_repo", lambda: SimpleNamespace(root=Path("/opt/rag"), interpreter="/opt/rag/py") if found else None
    )
    monkeypatch.setattr(rf, "_slugs_for", lambda root: ["repo_stale"])


def test_missing_cache_reads_unknown_and_queues_one_verdict_spawn(tmp_path, monkeypatch):
    _rag(monkeypatch)
    spawns = []
    kw = dict(
        spawn_fn=lambda a, c: pytest.fail("remedy spawned"),
        verdict_spawn_fn=lambda a, c: spawns.append(a),
        marker_root=tmp_path / "m",
        now=1000.0,
    )
    out = rf.freshness_precondition(str(tmp_path), **kw)
    assert out["state"] == "unknown" and out["refresh_queued"] is False
    rf.freshness_precondition(str(tmp_path), **kw)
    assert len(spawns) == 1 and spawns[0][1:4] == ["-m", "coordinator_core.ops.rag_freshness", "--fetch"]
    rf.freshness_precondition(str(tmp_path), **{**kw, "now": 1000.0 + rf.VERDICT_MARKER_TTL_SECONDS + 1})
    assert len(spawns) == 2


def test_unresolvable_cli_reads_unknown_and_spawns_nothing(tmp_path, monkeypatch):
    _rag(monkeypatch, found=False)
    out = rf.freshness_precondition(
        str(tmp_path), verdict_spawn_fn=lambda a, c: pytest.fail("spawned"), marker_root=tmp_path / "m"
    )
    assert out["state"] == "unknown" and out["refresh_queued"] is False
    assert rf.fetch_and_cache(str(tmp_path), marker_root=tmp_path / "m") == "failed: cli_unresolved"


def test_live_cache_is_read_without_spawn_and_queues_remedy(tmp_path, monkeypatch):
    _rag(monkeypatch)
    cache = rf._marker_path(str(tmp_path), tmp_path / "m", ".verdict.json")
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"fetched_at": 1000.0, "reply": _fixture()}), encoding="utf-8")
    remedies = []
    out = rf.freshness_precondition(
        str(tmp_path),
        spawn_fn=lambda a, c: remedies.append(a),
        verdict_spawn_fn=lambda a, c: pytest.fail("verdict respawned on a live cache"),
        marker_root=tmp_path / "m",
        now=1000.0 + rf.VERDICT_CACHE_TTL_SECONDS - 1,
    )
    assert out["state"] == "stale" and out["refresh_queued"] is True
    assert remedies == [["/opt/rag/bin/python3", "/opt/rag/project_rag_cli.py", "refresh-from-release", "--repo", "repo_stale"]]
    spawns = []
    expired = rf.freshness_precondition(
        str(tmp_path),
        spawn_fn=lambda a, c: pytest.fail("remedy on expired cache"),
        verdict_spawn_fn=lambda a, c: spawns.append(a),
        marker_root=tmp_path / "m",
        now=1000.0 + rf.VERDICT_CACHE_TTL_SECONDS + 1,
    )
    assert expired["state"] == "unknown" and len(spawns) == 1


def test_fetch_and_cache_writes_only_on_clean_json_exit(tmp_path, monkeypatch):
    _rag(monkeypatch)
    seen = {}

    def run(code, out):
        def fn(argv, **kw):
            seen.update(argv=argv, **kw)
            return SimpleNamespace(returncode=code, stdout=out)

        return fn

    root = tmp_path / "m"
    cache = rf._marker_path(str(tmp_path), root, ".verdict.json")
    assert rf.fetch_and_cache(str(tmp_path), marker_root=root, run_fn=run(1, json.dumps(_fixture()))) == "failed: exit 1"
    assert rf.fetch_and_cache(str(tmp_path), marker_root=root, run_fn=run(0, "not json")) == "failed: non-json stdout"
    assert not cache.exists()
    assert rf.fetch_and_cache(str(tmp_path), marker_root=root, run_fn=run(0, json.dumps(_fixture())), now=5.0) == "written"
    assert json.loads(cache.read_text(encoding="utf-8"))["fetched_at"] == 5.0
    assert seen["argv"] == ["/opt/rag/py", "/opt/rag/project_rag_cli.py", "freshness-verdict", "--cwd", str(tmp_path)]
    assert seen["shell"] is False


def test_verdict_spawn_is_admitted_closed_argv_detached(monkeypatch, tmp_path):
    events, seen = [], {}
    _fake_capacity(monkeypatch, events)
    _popen(monkeypatch, events, seen)
    rf.default_verdict_spawn_fn(rf._worker_argv(str(tmp_path)), str(tmp_path))
    assert [e[0] for e in events] == ["admit", "popen", "register", "release"]
    assert events[0][1] == rf.VERDICT_ADMISSION_KEY
    assert isinstance(seen["argv"], list) and not seen.get("shell")
    assert seen["stdin"] is subprocess.DEVNULL and seen["cwd"] == str(tmp_path)
    assert all(seen[k] == v for k, v in rf.detach_kwargs().items())


def test_remedy_spawn_is_admitted_and_held_admission_never_spawns(monkeypatch, tmp_path):
    events, seen = [], {}
    _fake_capacity(monkeypatch, events)
    _popen(monkeypatch, events, seen)
    rf.default_spawn_fn(["a", "b"], str(tmp_path))
    assert events[0] == ("admit", rf.REMEDY_ADMISSION_KEY) and seen["argv"] == ["a", "b"]
    held, popens = [], []
    _fake_capacity(monkeypatch, held, admitted=False)
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: popens.append(a))
    out = rf.freshness_precondition(
        str(tmp_path), verdict_fn=_verdict("stale", remedy=("x",)), marker_root=tmp_path / "m"
    )
    assert out["refresh_queued"] is False and "spawn failed" in out["message"] and popens == []


def test_detach_kwargs_branches():
    assert rf.detach_kwargs("posix") == {"start_new_session": True}
    flags = rf.detach_kwargs("nt")["creationflags"]
    assert flags & 0x00000008 and flags & 0x00000200 and "start_new_session" not in rf.detach_kwargs("nt")
