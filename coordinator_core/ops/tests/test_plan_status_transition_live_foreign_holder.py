"""Regression coverage for `plan_status_transition._refuse_if_live_foreign_
holder` — the Leg 2 defence-in-depth guard closing the incident where a
session-shape misdetection resolved a LIVE PEER's plan as the closing
session's own governing plan and stamped it `implemented` (cross-repo memo
`2026-08-10-example-retrieval-repo-em-wsc-misdetection-wrote-to-a-live-peers-plan.md`).

Purpose: pins the guard fires ONLY on a positively-established live foreign
claim holder, and is terminal-safe (proceeds) on every ambiguity — see
`_refuse_if_live_foreign_holder`'s own negative-spec for why it is keyed on
liveness, not provenance equality.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import plan_status_transition as pst
from coordinator_core.ops.plan_status_transition import main

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]

_GIT_ENV_KEYS = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git_env() -> dict:
    return {**os.environ, **_GIT_ENV_KEYS}


def _ensure_git_repo(tmp_path: Path) -> None:
    if (tmp_path / ".git").exists():
        return
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15)
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
    )


def _write_and_commit(tmp_path: Path, name: str, body: str) -> Path:
    _ensure_git_repo(tmp_path)
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    subprocess.run(["git", "add", "--", name], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=str(tmp_path), capture_output=True, env=_git_env(), timeout=15,
    )
    return p


_PLAN_BODY = """---
title: "Peer plan"
status: approved
deliverable_id: "dlv-peer-plan-abc123"
---

## Body
"""


def _handoff_body(claimed_by: str, deployment_state: str, deliverable_id: str = "dlv-peer-plan-abc123") -> str:
    return f"""---
title: "Peer handoff"
status: claimed
kind: session-handoff
deployment_state: {deployment_state}
claimed_by: {claimed_by}
deliverable_id: "{deliverable_id}"
---

## Body
"""


def test_live_foreign_holder_refuses_no_write_no_commit(tmp_path, monkeypatch, capsys):
    plan = _write_and_commit(tmp_path, "docs/plans/peer-plan.md", _PLAN_BODY)
    _write_and_commit(
        tmp_path, "state/handoffs/peer-handoff.md", _handoff_body("peer-sid-live", "in_flight")
    )

    monkeypatch.setattr(pst, "session_live", None, raising=False)
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: sid == "peer-sid-live")
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    err = capsys.readouterr().err

    assert rc == 1
    assert "refusing to stamp implemented" in err
    assert "peer-sid-live" in err
    text = plan.read_text(encoding="utf-8")
    assert "status: approved" in text
    r = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(tmp_path), capture_output=True, text=True, env=_git_env(), timeout=15,
    )
    assert r.stdout.strip() == ""


def test_self_held_proceeds(tmp_path, monkeypatch, capsys):
    plan = _write_and_commit(tmp_path, "docs/plans/self-plan.md", _PLAN_BODY.replace("dlv-peer-plan-abc123", "dlv-self-plan-xyz"))
    _write_and_commit(
        tmp_path,
        "state/handoffs/self-handoff.md",
        _handoff_body("closing-sid", "in_flight", deliverable_id="dlv-self-plan-xyz"),
    )

    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    assert rc in (0, 2)
    assert "implemented" in plan.read_text(encoding="utf-8")


def test_dead_holder_proceeds(tmp_path, monkeypatch):
    plan = _write_and_commit(tmp_path, "docs/plans/dead-plan.md", _PLAN_BODY.replace("dlv-peer-plan-abc123", "dlv-dead-plan-xyz"))
    _write_and_commit(
        tmp_path,
        "state/handoffs/dead-handoff.md",
        _handoff_body("dead-sid", "in_flight", deliverable_id="dlv-dead-plan-xyz"),
    )

    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    assert rc in (0, 2)
    assert "implemented" in plan.read_text(encoding="utf-8")


def test_terminal_deployment_state_proceeds(tmp_path, monkeypatch):
    plan = _write_and_commit(tmp_path, "docs/plans/shipped-plan.md", _PLAN_BODY.replace("dlv-peer-plan-abc123", "dlv-shipped-plan-xyz"))
    _write_and_commit(
        tmp_path,
        "state/handoffs/shipped-handoff.md",
        _handoff_body("peer-sid-live", "shipped", deliverable_id="dlv-shipped-plan-xyz"),
    )

    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    assert rc in (0, 2)
    assert "implemented" in plan.read_text(encoding="utf-8")


def test_ambiguous_zero_handoffs_proceeds(tmp_path, monkeypatch):
    plan = _write_and_commit(tmp_path, "docs/plans/lonely-plan.md", _PLAN_BODY.replace("dlv-peer-plan-abc123", "dlv-lonely-plan-xyz"))

    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    assert rc in (0, 2)
    assert "implemented" in plan.read_text(encoding="utf-8")


def test_ambiguous_multiple_handoffs_proceeds(tmp_path, monkeypatch):
    plan = _write_and_commit(tmp_path, "docs/plans/dup-plan.md", _PLAN_BODY.replace("dlv-peer-plan-abc123", "dlv-dup-plan-xyz"))
    _write_and_commit(
        tmp_path, "state/handoffs/dup-handoff-1.md", _handoff_body("peer-sid-live", "in_flight", deliverable_id="dlv-dup-plan-xyz")
    )
    _write_and_commit(
        tmp_path, "state/handoffs/dup-handoff-2.md", _handoff_body("peer-sid-live", "in_flight", deliverable_id="dlv-dup-plan-xyz")
    )

    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    monkeypatch.setattr("coordinator_core.session.core.resolve_session_id", lambda: "closing-sid")

    rc = main(["stamp-implemented", "--plan", str(plan)])
    assert rc in (0, 2)
    assert "implemented" in plan.read_text(encoding="utf-8")
