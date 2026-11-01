"""land_credited_holds: kept holds (e)/(f) name the withheld path, never refuse, and read only the matching runs."""

import json

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx
from coordinator_core.ops.dispatch_emit import land_credited_holds as holds_mod
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.ops.dispatch_emit.land_credited_contract import HoldsHook, LandCreditedRequest

RUN = "ask-20261031T000000-aaaaaa"
SCRIPT = "scratch/run.js"


def _request():
    return LandCreditedRequest(run_id=RUN, plan_path="docs/plans/p.md", script_path=SCRIPT)


def _script(root, *paths):
    marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=tuple(paths)),)))
    (root / "scratch").mkdir(exist_ok=True)
    (root / SCRIPT).write_text(f"// run\n{marker}\n", encoding="utf-8")


def _halted(root, run, status="halted"):
    d = root / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "halted.json").write_text(json.dumps({"status": status}), encoding="utf-8")


def _quiet_peers(monkeypatch):
    monkeypatch.setattr(holds_mod, "_peer_claims", lambda root, paths, run_id="": {})


def test_hook_matches_contract_signature():
    hook: HoldsHook = holds_mod.holds
    assert callable(hook)


def test_credited_paths_read_from_the_marker(tmp_path):
    _script(tmp_path, "a/x.py", "a\\y.py")
    assert holds_mod.credited_paths(tmp_path, _request()) == ["a/x.py", "a/y.py"]


def test_traversal_marker_entry_is_credited_to_no_one(tmp_path):
    _script(tmp_path, "a/x.py", "../outside.py")
    assert holds_mod.credited_paths(tmp_path, _request()) == []


def test_unreadable_script_credits_nothing_and_holds_nothing(tmp_path):
    assert holds_mod.credited_paths(tmp_path, _request()) == []
    assert holds_mod.holds(tmp_path, _request()) == {}


def test_no_holds_returns_empty(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py")
    idx.rebuild(tmp_path)
    assert holds_mod.holds(tmp_path, _request()) == {}


def test_two_halted_runs_withhold_and_name_their_paths(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py", "b/y.py", "c/free.py")
    idx.rebuild(tmp_path)
    for rid, path in (("ask-20261031T010000-bbbbbb", "a/x.py"), ("ask-20261031T020000-cccccc", "b/y.py")):
        _halted(tmp_path, rid)
        idx.add_run(tmp_path, rid, [path])
    out = holds_mod.holds(tmp_path, _request())
    assert out == {
        "a/x.py": "withheld by ask-20261031T010000-bbbbbb",
        "b/y.py": "withheld by ask-20261031T020000-cccccc",
    }


def test_halted_runs_that_hold_no_credited_path_are_not_read(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py")
    idx.rebuild(tmp_path)
    for i in range(20):
        rid = f"ask-20261031T03{i:02d}00-dddddd"
        _halted(tmp_path, rid)
        idx.add_run(tmp_path, rid, [f"other/{i}.py"])
    from coordinator_core.ops.dispatch_emit import halted_run

    reads = []
    monkeypatch.setattr(halted_run, "_read", lambda d: reads.append(d) or {"status": "halted"})
    assert holds_mod.holds(tmp_path, _request()) == {}
    assert reads == []


def test_landed_or_resumed_holder_holds_nothing(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py")
    idx.rebuild(tmp_path)
    rid = "ask-20261031T010000-bbbbbb"
    _halted(tmp_path, rid, status="landed")
    idx.add_run(tmp_path, rid, ["a/x.py"])
    assert holds_mod.holds(tmp_path, _request()) == {}


def test_own_run_never_holds_its_own_paths(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py")
    idx.rebuild(tmp_path)
    _halted(tmp_path, RUN)
    idx.add_run(tmp_path, RUN, ["a/x.py"])
    assert holds_mod.holds(tmp_path, _request()) == {}


def test_never_commit_and_em_withhold_are_named(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "scratch/junk.txt", "a/x.py", "b/y.py")
    idx.rebuild(tmp_path)
    rec = tmp_path / "state" / "warp" / RUN
    rec.mkdir(parents=True)
    (rec / "withhold.jsonl").write_text(json.dumps({"path": "a/x.py", "reason": "em ruled out"}) + "\n", encoding="utf-8")
    out = holds_mod.holds(tmp_path, _request())
    assert out == {"scratch/junk.txt": "never-commit", "a/x.py": "withheld"}


def test_peer_claim_is_named_for_a_path_nothing_else_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(holds_mod, "_peer_claims", lambda root, paths, run_id="": {"b/y.py": ["s2", "s1"]} if "b/y.py" in paths else {})
    _script(tmp_path, "a/x.py", "b/y.py")
    idx.rebuild(tmp_path)
    assert holds_mod.holds(tmp_path, _request()) == {"b/y.py": "peer-held by s1,s2"}


def test_peer_claim_lookup_failure_holds_nothing(tmp_path, monkeypatch):
    from coordinator_core.session import claim_index

    def boom(*a, **k):
        raise RuntimeError("claim index down")

    monkeypatch.setattr(claim_index, "lookup", boom)
    assert holds_mod._peer_claims(tmp_path, ["a/x.py"]) == {}


def test_holds_never_refuse_a_credited_path_set(tmp_path, monkeypatch):
    _quiet_peers(monkeypatch)
    _script(tmp_path, "a/x.py")
    idx.rebuild(tmp_path)
    rid = "ask-20261031T010000-bbbbbb"
    _halted(tmp_path, rid)
    idx.add_run(tmp_path, rid, ["a/x.py"])
    out = holds_mod.holds(tmp_path, _request())
    assert isinstance(out, dict) and set(out) == {"a/x.py"}


def _fake_claims(monkeypatch, claimants):
    from coordinator_core.session import claim_index, liveness, touch_record

    class _Found(dict):
        recorded_kind = {"a/x.py": {s: "edit" for s in claimants}}

    monkeypatch.setattr(claim_index, "lookup", lambda paths, **k: _Found({"a/x.py": list(claimants)}))
    monkeypatch.setattr(touch_record, "kind_blocks_a_peer_commit", lambda kind: True)
    monkeypatch.setattr(liveness, "session_live", lambda sid, cwd: True)
    monkeypatch.setattr(liveness, "session_abandoned", lambda sid, cwd: False)


def test_run_token_session_is_never_a_peer_of_its_own_paths(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit import run_token

    _fake_claims(monkeypatch, ["driver", "peer"])
    monkeypatch.setattr(run_token, "_record_path", lambda root, rid, claimed=False, **k: tmp_path / f"{claimed}.json")
    (tmp_path / "True.json").write_text(json.dumps({"session_id": "driver"}), encoding="utf-8")
    assert holds_mod._peer_claims(tmp_path, ["a/x.py"], "ask-20261031T000000-aaaaaa") == {"a/x.py": ["peer"]}


def test_claim_read_stuck_on_a_fold_lock_holds_nothing_within_the_bound(tmp_path, monkeypatch, caplog):
    import threading
    import time

    from coordinator_core.session import claim_index

    release = threading.Event()
    monkeypatch.setattr(claim_index, "lookup", lambda paths, **k: release.wait(10))
    monkeypatch.setattr(holds_mod, "CLAIM_READ_WAIT_SECS", 0.05)
    start = time.monotonic()
    try:
        assert holds_mod._peer_claims(tmp_path, ["a/x.py"], "r") == {}
        assert time.monotonic() - start < 1.0
        assert any("claim read not answered" in r.getMessage() for r in caplog.records)
    finally:
        release.set()
