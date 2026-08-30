"""Regression coverage for coordinator-harvest-deferrals' `_refuse_if_live_
foreign_plan_holder` guard — the deferral-harvest half of the session-shape
misdetection incident (cross-repo memo `2026-08-10-example-retrieval-repo-em-wsc-
misdetection-wrote-to-a-live-peers-plan.md`): a misresolved governing plan
would have this script mint improvement-queue / lessons-outbox entries from
a LIVE PEER session's deferred rows, and the harvest's own idempotency key
(`harvest-key: <plan_id>:<row id>`) would then cause the peer's later
legitimate close of that SAME plan to see those rows as already-harvested
and silently lose them.

Purpose: pins the guard fires ONLY on a positively-established live foreign
claim holder (reusing `plan_status_transition._refuse_if_live_foreign_
holder` verbatim), and is terminal-safe (proceeds) on every ambiguity — a
guard blocking on absence of evidence would wedge every ordinary close.

All tests invoke the real entry point (`harvest_mod.main`), never a bare
flag/field assertion — the guard must actually refuse (rc=1, loud stderr,
zero directive dispatch) or actually proceed (rc=0, normal harvest report)
through the CLI's own `main()`.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import textwrap
from pathlib import Path

import pytest


def _script_path() -> str:
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coordinator-harvest-deferrals.py")


def _load_harvest_module():
    path = _script_path()
    loader = importlib.machinery.SourceFileLoader("_test_harvest_deferrals_live_foreign_cli", path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harvest_mod():
    return _load_harvest_module()


_PLAN_BODY = """---
title: "Peer plan"
status: approved
deliverable_id: "dlv-peer-plan-abc123"
plan_id: "pln-peer-plan-abc123"
---

# Peer plan

## Tasks

```yaml plan-tasks
- id: D1
  title: A deferred row
  change_kind: code-edit
  surface: some/surface.py
  deferred: true
  pm_approved: true
  body: |
    Deferred row body.
```
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


def _plan(tmp_path: Path, name: str, deliverable_id: str = "dlv-peer-plan-abc123") -> Path:
    p = tmp_path / "docs" / "plans" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    body = _PLAN_BODY.replace("dlv-peer-plan-abc123", deliverable_id).replace(
        "pln-peer-plan-abc123", f"pln-{deliverable_id}"
    )
    p.write_text(body, encoding="utf-8")
    return p


def _handoff(tmp_path: Path, name: str, claimed_by: str, deployment_state: str, deliverable_id: str) -> Path:
    p = tmp_path / "state" / "handoffs" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_handoff_body(claimed_by, deployment_state, deliverable_id), encoding="utf-8")
    return p


def _pin_closing_session(monkeypatch, sid: str) -> None:
    """Pin the id `_refuse_if_live_foreign_holder` compares the claim against.

    BOTH names, deliberately. The guard moved from `resolve_session_id` to
    `attributable_session_id` on 2026-08-30 (the warm branch must not degrade to the
    engine owner's environment), and this test suite runs the claude-klabauter CLI against the
    PUBLISHED klabauter engine, so during a publish-lag window the two trees disagree
    about which one the guard calls. Pinning both is correct against either.

    `cwd=None` is not decoration: `attributable_session_id(cwd)` forwards its argument
    to `resolve_session_id(cwd)`, so a zero-arg stub raises TypeError inside the
    guard's own `except Exception` — which resolves the sid to None and PROCEEDS. The
    guard would then be silently disabled and this test would fail against a correct
    implementation.
    """
    monkeypatch.setattr(
        "coordinator_core.session.core.resolve_session_id", lambda cwd=None: sid
    )
    monkeypatch.setattr(
        "coordinator_core.session.core.attributable_session_id", lambda cwd=None: sid
    )


def test_live_foreign_holder_refuses_no_harvest_dispatch(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "peer-plan.md")
    _handoff(tmp_path, "peer-handoff.md", "peer-sid-live", "in_flight", "dlv-peer-plan-abc123")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: sid == "peer-sid-live")
    _pin_closing_session(monkeypatch, "closing-sid")

    called = {"harvest": False}
    monkeypatch.setattr(harvest_mod, "_harvest", lambda *a, **kw: called.__setitem__("harvest", True))

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    err = capsys.readouterr().err

    assert rc == 1
    assert "refusing to stamp implemented" in err
    assert "peer-sid-live" in err
    assert called["harvest"] is False


def test_self_held_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "self-plan.md", deliverable_id="dlv-self-plan-xyz")
    _handoff(tmp_path, "self-handoff.md", "closing-sid", "in_flight", "dlv-self-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    _pin_closing_session(monkeypatch, "closing-sid")

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out


def test_dead_holder_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "dead-plan.md", deliverable_id="dlv-dead-plan-xyz")
    _handoff(tmp_path, "dead-handoff.md", "dead-sid", "in_flight", "dlv-dead-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: False)
    _pin_closing_session(monkeypatch, "closing-sid")

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out


def test_terminal_deployment_state_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "shipped-plan.md", deliverable_id="dlv-shipped-plan-xyz")
    _handoff(tmp_path, "shipped-handoff.md", "peer-sid-live", "shipped", "dlv-shipped-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    _pin_closing_session(monkeypatch, "closing-sid")

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out


def test_ambiguous_zero_handoffs_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "lonely-plan.md", deliverable_id="dlv-lonely-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    _pin_closing_session(monkeypatch, "closing-sid")

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out


def test_ambiguous_multiple_handoffs_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    plan = _plan(tmp_path, "dup-plan.md", deliverable_id="dlv-dup-plan-xyz")
    _handoff(tmp_path, "dup-handoff-1.md", "peer-sid-live", "in_flight", "dlv-dup-plan-xyz")
    _handoff(tmp_path, "dup-handoff-2.md", "peer-sid-live", "in_flight", "dlv-dup-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr("coordinator_core.session.liveness.session_live", lambda sid, cwd=None: True)
    _pin_closing_session(monkeypatch, "closing-sid")

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out


def test_unresolvable_repo_root_proceeds(harvest_mod, tmp_path, monkeypatch, capsys):
    """`_repo_root()` returning `None` (no resolvable git worktree) proceeds
    rather than refuses — this harvest sweep is best-effort, never a hard
    gate on plan closure."""
    plan = _plan(tmp_path, "no-root-plan.md", deliverable_id="dlv-no-root-plan-xyz")

    monkeypatch.setattr(harvest_mod, "_repo_root", lambda: None)

    rc = harvest_mod.main(["--plan", str(plan), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Queued 1 deferred items: D1" in out
