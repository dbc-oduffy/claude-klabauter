"""dispatch.terminal_commit re-takes the expectation for a drifted path whose bytes are the run's
own last recorded write and re-lands once; a path whose bytes match no run write still refuses."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    _WITH_BASE,
    SID,
    _fire,
    _git,
    _record_at,
    _review,
    _sha,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _commit_calls(seen: list) -> list:
    return [s.get("expected_content_sha256") for s in seen]


def test_the_runs_own_earlier_write_is_re_measured_and_lands(repo, monkeypatch):
    """Cases (a)/(b): the review read A, the run's own write B predates the review stamp, so the
    first expectation is A and the first commit_v2 refuses; B is the session's newest write."""
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert "refused" not in out
    assert _commit_calls(seen) == [{"a.py": _sha("A\n")}, {"a.py": _sha("B\n")}]


def test_a_foreign_rewrite_after_the_runs_last_write_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("C\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("A\n"), 900.0)
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    head = _git(["rev-parse", "HEAD"], repo)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert out["blob_drift"][0]["path"] == "a.py"
    assert len(seen) == 1
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_path_with_no_run_write_on_record_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("C\n", encoding="utf-8", newline="")
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"
    assert len(seen) == 1


def test_only_the_run_owned_path_is_re_measured_in_a_mixed_drift(repo):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    (repo / "b.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    _record_at(repo, "b.py", _sha("OWN\n"), 1000.0)
    drift = [
        {"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("B\n")},
        {"path": "b.py", "basis": "content-sha256", "expected": _sha("OWN0\n"), "actual": _sha("FOREIGN\n")},
        {"path": "c.py", "basis": "blob", "expected": "x", "actual": "y"},
    ]
    got = terminal_commit._own_drift_actuals(repo, None, drift, ["a.py", "b.py", "c.py"], SID, None)
    assert got == {"a.py": _sha("B\n")}


def test_a_refire_keeps_the_runs_write_older_than_the_refire_start(repo, tmp_path):
    """The run record's `started_at` is the refire's; the run's own earlier write still counts."""
    run_dir = repo / "state" / "warp" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run-stage.json").write_text(
        json.dumps({"session_id": SID, "started_at": "2999-01-01T00:00:00Z"}), encoding="utf-8"
    )
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("B\n"), 1000.0)
    drift = [{"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("B\n")}]
    got = terminal_commit._own_drift_actuals(repo, run_dir, drift, ["a.py"], SID, None)
    assert got == {"a.py": _sha("B\n")}


def test_the_fix_pass_agents_are_named_to_the_attribution(repo, monkeypatch):
    from coordinator_core.ops.dispatch_emit import drift_attribution

    seen: dict = {}

    def spy(root, run_dir, paths, declared, **kw):
        seen.update(kw)
        return {"run_owned": [], "outside_declared": [], "foreign": list(paths)}

    monkeypatch.setattr(drift_attribution, "classify", spy)
    drift = [{"path": "a.py", "basis": "content-sha256", "expected": "e", "actual": "f"}]
    terminal_commit._own_drift_actuals(
        repo, None, drift, ["a.py"], SID, {"a.py": "agent-1", "b.py": "review-record"}
    )
    assert seen["agent_ids"] == ["agent-1"]
    assert seen["session_id"] == SID
