"""`approved_body_sha`: stamped at approval, refused by every execution gate once the
plan body changes after plan review. Frontmatter edits never move it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter import primitives as prim
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_CHANGED,
    APPROVED_BODY_NOT_APPLICABLE,
    APPROVED_BODY_OK,
    APPROVED_BODY_UNVERIFIABLE,
    check_approved_body,
    stamp_approved_body_sha,
)

_TASKS = (
    "## Tasks\n\n```yaml plan-tasks\n- id: C1\n  title: Row\n  change_kind: script-edit\n"
    "  surface: pkg/row.py\n  writes:\n    - pkg/row.py\n```\n"
)
_BODY = "\n# Plan\n\n" + _TASKS


def _plan_text(status="approved", body=_BODY, extra=""):
    return f"---\ntitle: t\nstatus: {status}\n{extra}---\n{body}"


def _approved(body=_BODY):
    return stamp_approved_body_sha(_plan_text(body=body))


def _edited(text: str) -> str:
    return text + "\nA new row was added after review.\n"


def test_stamp_then_check_is_ok():
    text = _approved()
    assert "approved_body_sha:" in text
    assert check_approved_body(text)[0] == APPROVED_BODY_OK


def test_body_edit_is_changed_and_names_plan_review():
    state, message = check_approved_body(_edited(_approved()))
    assert state == APPROVED_BODY_CHANGED
    assert "plan review" in message


def test_frontmatter_only_edit_does_not_trip():
    text = _approved().replace("title: t", "title: renamed").replace(
        "status: approved", "status: executing"
    )
    assert check_approved_body(text)[0] == APPROVED_BODY_OK


def test_absent_field_on_approved_plan_is_unverifiable_not_changed():
    assert check_approved_body(_plan_text())[0] == APPROVED_BODY_UNVERIFIABLE
    assert check_approved_body(_plan_text(status="draft"))[0] == APPROVED_BODY_NOT_APPLICABLE


def test_restamp_replaces_in_place():
    once = _approved()
    twice = stamp_approved_body_sha(_edited(once))
    assert twice.count("approved_body_sha:") == 1
    assert check_approved_body(twice)[0] == APPROVED_BODY_OK


# --- writers ---------------------------------------------------------------


def test_blitz_land_approval_writes_the_stamp(tmp_path, monkeypatch):
    from coordinator_core.roadmap import blitz_land as bl

    monkeypatch.setattr(bl, "_link_baton_to_plan", lambda *a, **k: False)
    (tmp_path / ".git").mkdir()
    baton = tmp_path / "state" / "handoffs" / "b-1.md"
    baton.parent.mkdir(parents=True)
    baton.write_text(
        "---\nkind: roadmap-baton\ntitle: b-1\nstub_id: b-1\nstatus: open\n"
        "deployment_state: ready_to_fire\nbaton_role: work\ndeliverable_id: dlv-b-1\n---\n\nbody\n",
        encoding="utf-8",
    )
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(_plan_text(status="draft", extra="deliverable_id: dlv-b-1\n"), encoding="utf-8")

    out = bl.approve_ready(tmp_path, "state/handoffs/b-1.md", "docs/plans/p.md", {})

    assert out["stamped"] is True
    text = plan.read_text(encoding="utf-8")
    assert "status: approved" in text
    assert check_approved_body(text)[0] == APPROVED_BODY_OK


def test_disposition_writeback_carries_a_verified_stamp_only():
    from coordinator_core.ops.plan_tasks_mutate import _carry_prep_certificate

    ok = _approved()
    carried = _carry_prep_certificate(ok, _edited(ok))
    assert check_approved_body(carried)[0] == APPROVED_BODY_OK

    stale = _edited(ok)
    not_carried = _carry_prep_certificate(stale, _edited(stale))
    assert check_approved_body(not_carried)[0] == APPROVED_BODY_CHANGED


# --- gates -----------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True,
        **no_console_creationflags())


def test_exec_auth_authorize_invocation_refuses_changed_body(tmp_path):
    from coordinator_core.review_assemble.exec_auth_stamp import (
        EXIT_BUSINESS_FAIL,
        stamp_invocation_authorization,
    )

    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(_edited(_approved()), encoding="utf-8")
    before = plan.read_text(encoding="utf-8")

    code, result = stamp_invocation_authorization(
        str(plan), None, "/execute-plan", repo_root=tmp_path
    )

    assert code == EXIT_BUSINESS_FAIL
    assert "plan review" in result["error"]
    assert plan.read_text(encoding="utf-8") == before


def test_exec_auth_authorize_invocation_warns_when_unverifiable(tmp_path):
    from coordinator_core.review_assemble.exec_auth_stamp import (
        EXIT_OK,
        stamp_invocation_authorization,
    )

    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(_plan_text(), encoding="utf-8")
    _git(tmp_path, "init", "-q")

    code, result = stamp_invocation_authorization(
        str(plan), None, "/execute-plan", repo_root=tmp_path
    )

    assert code == EXIT_OK
    assert "unverifiable" in result["warning"]


@pytest.fixture
def me(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "me-sid")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _claim_repo(tmp_path: Path, text: str) -> Path:
    _git(tmp_path, "init", "-q")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(text, encoding="utf-8")
    return plan


def test_claim_plan_for_execution_refuses_changed_body_and_leaves_no_claim(tmp_path, me, capsys):
    from coordinator_core.session import claims

    plan = _claim_repo(tmp_path, _edited(_approved()))

    assert claims.claim_plan("p", cwd=str(tmp_path), for_execution=True) is False

    assert "plan review" in capsys.readouterr().err
    assert "status: approved" in plan.read_text(encoding="utf-8")
    assert not (tmp_path / ".git" / "coordinator-sessions" / "plan-claims" / "p").exists()


def test_claim_plan_for_execution_warns_on_absent_stamp(tmp_path, me, capsys):
    from coordinator_core.session import claims

    _claim_repo(tmp_path, _plan_text())

    assert claims.claim_plan("p", cwd=str(tmp_path), for_execution=True) is True
    assert "unverifiable" in capsys.readouterr().err


def test_dispatch_emit_plan_route_refuses_changed_body(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
    from coordinator_core.ops.dispatch_emit.tests.test_every_emit_route_composes_review import (
        _V5_FRAGMENT,
        _patch_loaders,
    )

    _patch_loaders(monkeypatch, _V5_FRAGMENT)
    plan = tmp_path / "plan.md"
    plan.write_text(_edited(_approved()), encoding="utf-8")
    out = tmp_path / "out.mjs"

    with pytest.raises(ValueError, match="plan review"):
        _dispatch_emit({"plan_path": str(plan), "output_path": str(out)}, repo_root=tmp_path)

    assert not out.exists()


def test_pickup_execution_stamp_match_is_stale_substantive_on_changed_body(tmp_path):
    from coordinator_core.pickup_brief import compute_execution_stamp_match

    text = _edited(_approved()).replace(
        "status: approved\n", "status: approved\nexecution_authorized_sha: " + "a" * 40 + "\n"
    )
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(text, encoding="utf-8")

    gate, _ = compute_execution_stamp_match(
        tmp_path, {"execution_authorized_sha": "a" * 40}, "docs/plans/p.md"
    )

    assert gate["verdict"] == "stale-substantive"
    assert "plan review" in gate["next_move"]
