"""Pickup ceremony regressions: `jshipped: reopen-and-proceed` reaches a real reopen,
and the deliverable cascade never closes the live continuation of a shipped predecessor."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.pickup_assemble.apply as pa_apply
from coordinator_core.frontmatter.primitives import read_fm_field_unquoted, split_frontmatter
from coordinator_core.ops.deliverable_cascade import _drop_source_successors
from coordinator_core.tests.git_seed import seeded_repo
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SHIPPED = (
    'title: "Shipped baton"\n'
    "created: 2026-11-20\n"
    "branch: work/test/2026-01-01\n"
    "status: open\n"
    'predecessor: "none"\n'
    "kind: session-handoff\n"
    "deployment_state: shipped\n"
    "advanced_by: dlv-1\n"
    "advanced_at: '2026-11-20T10:57:58Z'\n"
    "category: infra\n"
    'summary: "Work remains."\n'
    "pickup_ready: false\n"
    "shipped_in: 6df93654\n"
    "shipped_in_kind: ship-commit\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15,
                   stdin=subprocess.DEVNULL, **no_console_creationflags())


def _seed(repo: Path, name: str, fm: str) -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _fm(path: Path) -> dict:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    return {k: read_fm_field_unquoted(split.fm_text, k) for k in (
        "deployment_state", "pickup_ready", "status", "claimed_by", "shipped_in", "shipped_in_kind",
        "advanced_by", "reopened_reason", "reopened_from_shipped_in", "reopened_at")}


def test_reopen_and_proceed_reopens_and_claims_a_shipped_handoff(tmp_path, monkeypatch):
    for key in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "repo"
    seeded_repo(repo, branch="work/test/2026-01-01", email="t@example.com", name="T")
    path = _seed(repo, "h1.md", _SHIPPED)

    decisions = {"jshipped": {"disposition": "reopen-and-proceed", "decision_note": "stamp was premature"}}
    code, report = pa_apply.apply("state/handoffs/h1.md", session_id="sid-reopen", repo_root=repo,
                                  decisions=decisions)

    assert code == pa_apply.APPLY_EXIT_OK, report
    state = _fm(path)
    assert state["deployment_state"] == "in_flight"
    assert state["status"] == "claimed"
    assert state["claimed_by"] == "sid-reopen"
    assert state["pickup_ready"] == "true"
    assert state["reopened_reason"] == "stamp was premature"
    assert state["reopened_from_shipped_in"] == "6df93654"
    assert state["reopened_at"]
    assert state["shipped_in"] is None and state["shipped_in_kind"] is None and state["advanced_by"] is None


def test_shipped_claim_without_reopen_still_refuses(tmp_path):
    from coordinator_core.ops.handoff_transition import _claim

    repo = tmp_path / "repo"
    seeded_repo(repo, branch="work/test/2026-01-01", email="t@example.com", name="T")
    path = _seed(repo, "h1.md", _SHIPPED)
    result = _claim(str(path), "sid", "2026-11-20T11:00:00Z", repo, repo / ".git")
    assert result["exit_code"] == 1 and "terminal" in result["error"]


def test_continuation_of_shipped_predecessor_is_not_a_cascade_candidate(tmp_path):
    predecessor = "2026-11-19_201910_pred-78e5c1.md"
    live = tmp_path / "state" / "handoffs" / "2026-11" / "2026-11-20-continuation.md"
    live.parent.mkdir(parents=True)
    live.write_text("---\n---\n", encoding="utf-8")
    sibling = tmp_path / "state" / "handoffs" / "2026-11" / "2026-11-20-unrelated.md"
    candidates = [
        {"path": live, "fm": {"predecessor": f"state/handoffs/2026-11/{predecessor}"}},
        {"path": sibling, "fm": {"predecessor": "state/handoffs/2026-11/other.md"}},
    ]
    kept = _drop_source_successors(candidates, [f"archive/handoffs/{predecessor}"], tmp_path)
    assert [c["path"] for c in kept] == [sibling]


def test_unfinished_successor_stays_pickup_ready_through_the_cascade(tmp_path):
    from coordinator_core.ops.deliverable_cascade import cascade_terminal_sync

    repo = tmp_path / "repo"
    seeded_repo(repo, branch="work/test/2026-01-01", email="t@example.com", name="T")
    did = "dlv-20261119-handoff-5fc746"
    base = ('created: 2026-11-19\nbranch: work/test/2026-01-01\nstatus: open\nkind: session-handoff\n'
            f'category: infra\nsummary: "s"\ndeliverable_id: "{did}"\n')
    pred = _seed(repo, "2026-11-19_pred.md",
                 'title: "Pred"\n' + base + 'predecessor: "none"\ndeployment_state: continued\n'
                 'continued_into: "state/handoffs/2026-11-20-cont.md"\n')
    cont = _seed(repo, "2026-11-20-cont.md",
                 'title: "Cont"\n' + base + 'predecessor: "state/handoffs/2026-11-19_pred.md"\n'
                 'deployment_state: ready_to_fire\npickup_ready: true\n')

    # The redrive reports a recorded-shipped_in source as kind "plan"; the path decides.
    result = cascade_terminal_sync(
        {"deliverable_id": did, "source_kind": "plan", "source_path": "state/handoffs/2026-11-19_pred.md",
         "ship_sha": "6df93654"},
        repo_root=repo / ".git",
    )
    assert result["advanced"] == []
    state = _fm(cont)
    assert state["deployment_state"] == "ready_to_fire" and state["pickup_ready"] == "true"
    assert pred.exists()


def test_pickup_readiness_conflict_is_surfaced(tmp_path):
    from coordinator_core.baton_assemble.apply import _pickup_readiness_conflict
    from coordinator_core.session_ceremonies import _completed

    bad = tmp_path / "state" / "handoffs" / "h.md"
    bad.parent.mkdir(parents=True)
    bad.write_text("---\ndeployment_state: shipped\npickup_ready: false\nshipped_in: abc1234\n---\n", encoding="utf-8")
    conflict = _pickup_readiness_conflict(tmp_path, "state/handoffs/h.md")
    assert conflict and any("terminal" in p for p in conflict["problems"])

    good = tmp_path / "state" / "handoffs" / "g.md"
    good.write_text("---\ndeployment_state: ready_to_fire\npickup_ready: true\n---\n", encoding="utf-8")
    assert _pickup_readiness_conflict(tmp_path, "state/handoffs/g.md") is None

    req = {"ceremony": "handoff", "phase": "apply"}
    ok = {"exit_code": 0, "decision_object": {"pickup_readiness": {"verdict": "ready"}}}
    assert _completed(req, ok) is True
    assert _completed(req, {"exit_code": 0, "decision_object": {"pickup_readiness": {"verdict": "conflict"}}}) is False
