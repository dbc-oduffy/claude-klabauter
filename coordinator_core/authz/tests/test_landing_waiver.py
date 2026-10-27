from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.authz import landing_waiver as lw
from coordinator_core.group_em import nomination, session_registry

NOW = 1_800_000_000.0
HOLDER, OTHER = "sid-holder", "sid-other"
GUARD = "block-em-halted-withheld-commit"


@pytest.fixture
def env(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = tmp_path / "group-em"
    records.mkdir()
    live = {HOLDER, OTHER}
    monkeypatch.setattr(nomination, "entry_status", lambda *a, **k: {"status": "verified"})
    monkeypatch.setattr(
        nomination, "is_live", lambda rec: nomination.LivenessResult(rec.get("session_id") in live, "live")
    )
    monkeypatch.setattr(
        session_registry,
        "read_rows",
        lambda directory=None: [
            session_registry.RegistryRow(session_id=s, name=s, pid=1, cwd=str(repo), status="")
            for s in (HOLDER, OTHER)
        ],
    )
    monkeypatch.setenv("COORDINATOR_SESSION_ID", HOLDER)
    nomination.claim(str(repo), HOLDER, directory=records, prompt_id="p-1")
    return {"repo": repo, "dir": records, "live": live, "mp": monkeypatch}


def _record(env, **kw):
    args = dict(
        root=env["repo"], approver="group-em", ruling_ref="PM turn 51", guards=[GUARD],
        paths=["a.py", "b.py"], run_ids=["run-1"], directory=env["dir"], now=NOW,
    )
    args.update(kw)
    return lw.record_waiver(**args)


def _match(env, hits=None, guard=GUARD, root=None):
    hits = {"a.py": "run-1"} if hits is None else hits
    return lw.matching_waiver(root or env["repo"], guard, hits, directory=env["dir"], now=NOW)


def test_matches_then_consumed_once(env):
    assert _record(env).ok
    waiver = _match(env)
    assert waiver is not None
    assert lw.consume(waiver, now=NOW) is True
    assert _match(env) is None
    consumed = waiver.path.with_name(waiver.path.name[:-5] + ".consumed.json")
    assert not waiver.path.exists()
    assert json.loads(consumed.read_text())["consumed_at"] is not None


def test_two_consumes_of_one_match_exactly_one_wins(env):
    _record(env)
    waiver = _match(env)
    assert [lw.consume(waiver), lw.consume(waiver)] == [True, False]


def test_hands_on_mode_reads_no_waiver(env):
    _record(env)
    assert lw.matching_waiver(env["repo"], GUARD, {"a.py": "run-1"}, directory=env["dir"], now=NOW, hands_on=True) is None
    assert _match(env) is not None


def test_apm_approver_refused(env):
    assert _record(env, approver="apm").reason == lw.APPROVER_NOT_ALLOWED


def test_subset_of_paths_matches_extra_path_denies(env):
    _record(env)
    assert _match(env, {"a.py": "run-1", "b.py": "run-1"}) is not None
    assert _match(env, {"a.py": "run-1", "c.py": "run-1"}) is None


def test_runless_landing_finding_matches_on_path_and_guard(env):
    _record(env)
    assert _match(env, {"a.py": ""}) is not None
    assert _match(env, {"c.py": ""}) is None


def test_other_run_id_other_guard_other_root_deny(env, tmp_path):
    _record(env)
    assert _match(env, {"a.py": "run-2"}) is None
    assert _match(env, guard="other-guard") is None
    assert _match(env, root=tmp_path / "elsewhere") is None
    assert _match(env, {}) is None


def test_forged_record_denies(env):
    result = _record(env)
    path = result.path
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "approver": "intern"}))
    assert _match(env) is None
    path.write_text(json.dumps({**rec, "issuer_session_id": OTHER}))
    assert _match(env) is None


def test_planted_under_other_key_denies(env):
    result = _record(env)
    planted = result.path.parent.parent / OTHER
    planted.mkdir()
    (planted / "x.json").write_text(result.path.read_text())
    result.path.unlink()
    assert _match(env) is None


def test_displaced_holder_waiver_stops_authorizing(env):
    _record(env)
    env["live"].discard(HOLDER)
    assert _match(env) is None


def test_malformed_file_fails_closed(env):
    result = _record(env)
    result.path.write_text("{not json")
    assert _match(env) is None
    result.path.write_text(json.dumps(["list"]))
    assert _match(env) is None


def test_record_refusals(env):
    assert _record(env, approver="intern").reason == lw.APPROVER_NOT_ALLOWED
    assert _record(env, paths=[]).reason == lw.FIELD_EMPTY
    assert _record(env, run_ids=[]).reason == lw.FIELD_EMPTY
    assert _record(env, ruling_ref=" ").reason == lw.FIELD_EMPTY
    env["mp"].setenv("COORDINATOR_SESSION_ID", OTHER)
    assert _record(env).reason == lw.NOT_GROUP_EM


def test_non_waivable_guard_refused_and_never_matches(env):
    pm_owned = "check-test-suite-invocation"
    assert _record(env, guards=[pm_owned]).reason == lw.GUARD_NOT_WAIVABLE
    assert _record(env, guards=[GUARD, pm_owned]).reason == lw.GUARD_NOT_WAIVABLE
    result = _record(env)
    path = result.path
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "guards": [pm_owned]}))
    assert _match(env, guard=pm_owned) is None
    for guard in ("import-closure-untracked", "peer-hunks-co-declared"):
        assert guard in lw.WAIVABLE_GUARDS


def test_zero_spawns(env, monkeypatch):
    _record(env)

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)
    waiver = _match(env)
    assert waiver is not None
    lw.consume(waiver)
