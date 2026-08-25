"""
coordinator_core.ops.ceremony.tests.test_wsc_tail_consumed_stamp_state_read

C5 (docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-failed.md,
AC9): `_handler`'s never-evaluated `consumed_handoff_stamp` arm (the
`chain_terminal and not stamp_outcome.stamped` / `committed_sha is None`
branch) used to print a hardcoded, unconditional
"baton is still `deployment_state: in_flight`" literal for EVERY candidate in
`initial_consumed`, regardless of what its frontmatter actually said --
including a baton that was already `shipped`. The loop already held the
frontmatter as the discarded `_fm`; this reads it instead of dropping it, and
stops emitting the `archive-stamp-cli ship-handoff` remediation when the
observed state is already terminal (`HANDOFF_TERMINAL_DEPLOYMENT`) -- telling
an operator to re-ship an already-shipped baton was the same defect class one
clause to the right.

Deliberately a NEW file, not an addition to `test_wsc_tail_parity.py` --
narrower scope, same reasoning `test_wsc_tail_sha_unverified.py` gives for
being standalone. Fixture setup mirrors `test_wsc_tail_parity.py`'s
`WscTailRepo`/`wsc_tail_repo` shape, kept minimal and local here (no exported
name in that module to import from without touching it, which is out of this
chunk's scope).
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any

import pytest

import coordinator_core.ops.ceremony.wsc_tail as wsc_tail_mod
from .fixtures.pipeline_result import make_pipeline_result

# Declared, not excused: this file spawns a real process (git) because the
# property under test depends on a real consumed-handoff resolve over a real
# repo tree, same rationale `test_wsc_tail_sha_unverified.py` gives.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _unique_session_id() -> str:
    return f"test-session-{uuid.uuid4().hex[:8]}"


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def state_read_repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "wsc-tail-consumed-state-read@makima.test"], root)
    _git(["config", "user.name", "WSC Tail Consumed State Read Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    (root / "state" / "handoffs").mkdir(parents=True)
    (root / "state" / "handoffs" / ".gitkeep").write_text("", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", "chore: initial skeleton"], root)
    return root


def _seed_handoff(root: Path, name: str, *, consumed_by: str, deployment_state: str | None) -> str:
    """Mirrors `test_wsc_tail_parity.py::WscTailRepo.seed_handoff`, extended
    with an optional `deployment_state` field -- the one axis this test needs
    that the shared fixture does not expose (it never writes the field at
    all, matching a fresh in-flight baton)."""
    relpath = f"state/handoffs/{name}"
    path = root / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'title: "Test Handoff {name}"',
        "created: 2026-07-15",
        "branch: work/test/2026-07-15",
        "status: open",
        "category: infra",
        'summary: "Test handoff summary for schema post-cutoff compliance."',
        "predecessor: null",
        "consumed_at: 2026-07-15T10:00:00Z",
        f"claimed_by: {consumed_by}",
    ]
    if deployment_state is not None:
        lines.append(f"deployment_state: {deployment_state}")
    path.write_text("---\n" + "\n".join(lines) + "\n---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-m", f"add handoff {name}"], root)
    return relpath


def _run_never_evaluated_arm(root: Path, sid: str, monkeypatch) -> dict:
    """Drives `_handler` through the fourth (never-evaluated) arm: a
    chain-terminal pass whose `commit_pipeline` fails outright, so the stamp
    step never runs at all and `committed_sha` stays `None` -- same shape
    `test_wsc_tail_parity.py::
    test_chain_terminal_commit_abort_stamps_never_evaluated_and_labels_nodes`
    drives, kept local here per this file's own docstring."""
    (root / "tasks" / "feature").mkdir(parents=True)
    (root / "tasks" / "feature" / "todo.md").write_text("content", encoding="utf-8")

    failed_outcome = make_pipeline_result(
        commit_failed=True,
        diagnostics=["forced failure for consumed-stamp state-read regression test"],
    )
    monkeypatch.setattr(wsc_tail_mod, "run_commit_pipeline", lambda *_a, **_kw: failed_outcome)

    return _run(
        wsc_tail_mod._handler(
            {
                "sid": sid,
                "subject": "workstream-complete: feature",
                "stage_paths": ["tasks/feature/todo.md"],
                "caller_paths": ["tasks/feature/todo.md"],
            },
            repo_root=(root / ".git").resolve(),
        )
    )


def test_never_evaluated_arm_reports_shipped_for_shipped_baton(
    state_read_repo, monkeypatch
):
    """AC9, state half: the misfiring case. A baton whose step-1 frontmatter
    already reads `deployment_state: shipped` must be reported as `shipped`,
    never the old hardcoded `in_flight` literal."""
    root = state_read_repo
    sid = _unique_session_id()
    relpath = _seed_handoff(
        root, "2026-07-15_100000_shipped.md", consumed_by=sid, deployment_state="shipped"
    )

    result = _run_never_evaluated_arm(root, sid, monkeypatch)

    assert result["commit_failed"] is True
    assert result["committed_sha"] is None

    failed_entries = result["tail_results"]["consumed_handoff_stamp"]["failed"]
    assert any(
        relpath in e and "never-evaluated" in e and "deployment_state: shipped" in e
        for e in failed_entries
    ), failed_entries
    # Never the stale literal for a baton that is not in_flight.
    assert not any("deployment_state: in_flight" in e for e in failed_entries), failed_entries


def test_never_evaluated_arm_terminal_state_omits_ship_handoff_remediation(
    state_read_repo, monkeypatch
):
    """AC9, remediation half: once the observed state is already terminal
    (`shipped` is in `HANDOFF_TERMINAL_DEPLOYMENT`), the message must not
    tell the operator to `archive-stamp-cli ship-handoff` an already-shipped
    baton."""
    root = state_read_repo
    sid = _unique_session_id()
    relpath = _seed_handoff(
        root, "2026-07-15_100000_shipped.md", consumed_by=sid, deployment_state="shipped"
    )

    result = _run_never_evaluated_arm(root, sid, monkeypatch)

    failed_entries = result["tail_results"]["consumed_handoff_stamp"]["failed"]
    matching = [e for e in failed_entries if relpath in e and "never-evaluated" in e]
    assert matching, failed_entries
    assert not any("archive-stamp-cli ship-handoff" in e for e in matching), matching


def test_never_evaluated_arm_reports_in_flight_and_keeps_remediation(
    state_read_repo, monkeypatch
):
    """AC9 non-terminal control: an `in_flight` baton (no `deployment_state`
    key at all, the field's absent-key semantics -- see `handoff_stamp.py`'s
    terminal-state doc comment) still reports `in_flight` AND still carries
    the ship-handoff remediation, since there IS something to remediate."""
    root = state_read_repo
    sid = _unique_session_id()
    relpath = _seed_handoff(
        root, "2026-07-15_100000_inflight.md", consumed_by=sid, deployment_state=None
    )

    result = _run_never_evaluated_arm(root, sid, monkeypatch)

    failed_entries = result["tail_results"]["consumed_handoff_stamp"]["failed"]
    matching = [
        e for e in failed_entries
        if relpath in e and "never-evaluated" in e and "deployment_state: in_flight" in e
    ]
    assert matching, failed_entries
    assert any("archive-stamp-cli ship-handoff" in e for e in matching), matching


def test_never_evaluated_arm_reports_present_non_terminal_state_and_keeps_remediation(
    state_read_repo, monkeypatch
):
    """AC9 non-terminal, present-key case: a baton whose frontmatter carries
    a present-but-non-terminal `deployment_state` (e.g. `awaiting_gate`, not
    absent) must report that literal state -- not fall back to the
    absent-key `in_flight` default -- and must still keep the ship-handoff
    remediation, since `awaiting_gate` is not in
    `HANDOFF_TERMINAL_DEPLOYMENT` either. Closes the coverage gap the
    control test above leaves: it only exercises the absent-key path, not a
    present-but-non-terminal one."""
    root = state_read_repo
    sid = _unique_session_id()
    relpath = _seed_handoff(
        root,
        "2026-07-15_100000_awaiting_gate.md",
        consumed_by=sid,
        deployment_state="awaiting_gate",
    )

    result = _run_never_evaluated_arm(root, sid, monkeypatch)

    failed_entries = result["tail_results"]["consumed_handoff_stamp"]["failed"]
    matching = [
        e for e in failed_entries
        if relpath in e and "never-evaluated" in e and "deployment_state: awaiting_gate" in e
    ]
    assert matching, failed_entries
    assert any("archive-stamp-cli ship-handoff" in e for e in matching), matching
    assert not any("deployment_state: in_flight" in e for e in matching), matching
