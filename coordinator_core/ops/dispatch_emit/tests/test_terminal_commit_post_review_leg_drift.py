"""A run's own reverify/verify/evidence leg rewrites a declared path after the expectation was taken:
terminal commit re-takes the expectation from that leg agent's ledger and lands; bytes no session
agent recorded still refuse blob-drift."""

from __future__ import annotations

import pytest

from coordinator_core.session_hub_path import hub_for_common
from coordinator_core.ops.dispatch_emit import drift_attribution, terminal_commit
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
from coordinator_core.session import touch_record

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

LEG_AGENT = "a1b2c3d4e5f6a7b8c"


def _leg_agent_write(repo, rel: str, digest: str, at: float, *, subagent_type: str = "coordinator:executor",
                     owner: str = SID, agent_id: str = LEG_AGENT) -> None:
    """The run's leg agent (a type the review-writer denylist skips) wrote ``rel`` in its own sink."""
    hub = hub_for_common(repo / ".git")
    agent_dir = hub / ".agents" / agent_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "em-session-id.txt").write_text(owner + "\n", encoding="utf-8")
    ledger = hub / SID / "dispatched-agents.txt"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    with ledger.open("a", encoding="utf-8", newline="") as fh:
        fh.write(f"{agent_id}\tclaude\t{subagent_type}\t1000\n")
    touch_record.append_event(
        touch_record.sink_path(agent_dir), session_id=SID, agent_id=agent_id, verb="T", path=rel,
        content_hash=digest, kind="w", timestamp=at,
    )


def test_a_leg_agents_post_review_write_is_re_measured_and_lands(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3000.0)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is True, out
    assert "refused" not in out
    assert [s.get("expected_content_sha256") for s in seen] == [{"a.py": _sha("A\n")}, {"a.py": _sha("B\n")}]


def test_a_foreign_rewrite_after_the_leg_agents_write_still_refuses(repo, monkeypatch):
    (repo / "a.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3000.0)
    head = _git(["rev-parse", "HEAD"], repo)
    out, seen = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert len(seen) == 1
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_an_agent_another_session_owns_is_no_attribution(repo, monkeypatch):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    _record_at(repo, "a.py", _sha("A\n"), 1000.0)
    _review(repo, {"a.py": _sha("A\n")}, 2000.0)
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3000.0, owner="some-other-session")
    out, _ = _fire(repo, monkeypatch, _WITH_BASE)
    assert out["refused"] == "blob-drift"


def test_the_attribution_names_every_session_agent_whatever_its_type(repo):
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3000.0, subagent_type="coordinator:exit-criterion-falsifier")
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3001.0, owner="other", agent_id="ffffffffffffffff0")
    assert drift_attribution.session_agent_ids(repo, SID) == [LEG_AGENT]
    assert drift_attribution.session_agent_ids(repo, None) == []


def test_own_drift_actuals_takes_a_leg_agents_bytes_and_leaves_foreign(repo):
    (repo / "a.py").write_text("B\n", encoding="utf-8", newline="")
    (repo / "b.py").write_text("FOREIGN\n", encoding="utf-8", newline="")
    _leg_agent_write(repo, "a.py", _sha("B\n"), 3000.0)
    _leg_agent_write(repo, "b.py", _sha("OWN\n"), 3000.0)
    drift = [
        {"path": "a.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("B\n")},
        {"path": "b.py", "basis": "content-sha256", "expected": _sha("A\n"), "actual": _sha("FOREIGN\n")},
    ]
    got = terminal_commit._own_drift_actuals(repo, None, drift, ["a.py", "b.py"], SID, None)
    assert got == {"a.py": _sha("B\n")}
