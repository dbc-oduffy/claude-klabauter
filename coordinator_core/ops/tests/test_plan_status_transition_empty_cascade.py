"""
coordinator_core.ops.tests.test_plan_status_transition_empty_cascade

Purpose: pin item 11 (DoE inbox-blitz thread, plan row C12). `_run_cascade`
returned exit_code 2 -- "cascade resolved no downstream artifact, needs
attention" -- identically for two different cases:

  1. Zero candidates matched for every targeted kind (`handoff`, `sizing`):
     the deliverable legitimately never had a downstream artifact. Nothing
     needs attention; the flip itself is the whole story.
  2. One or more candidates matched but every one was refused: something
     DID exist and this run could not advance it. That is the "needs
     attention" signal exit_code 2 exists to carry.

`deliverable_cascade._handler`'s own result already distinguishes the two
via `candidates_matched` (0 in case 1, >0 in case 2 — see its own docstring's
"Failure posture"). This module now reads that field instead of collapsing
both into exit_code 2.

Mirrors this file's sibling `test_plan_status_transition.py`'s own
`_run_cascade`-level test shape (`test_run_cascade_prints_commit_notice_with_
distinct_framing` et al.) — same `_write` helper, same
`cascade_mod._handler` monkeypatch seam, called directly rather than through
the full CLI, since `_run_cascade` is the unit this item's fix touches.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.tests.test_plan_status_transition import _write


def test_empty_cascade_for_every_kind_exits_0_with_note(tmp_path, monkeypatch, capsys) -> None:
    """Case 1: zero candidates matched for both targeted kinds -- a
    legitimately empty cascade. The flip succeeded and there is nothing to
    advance, so this must exit 0 with a one-line explanatory note, not 2."""
    from coordinator_core.ops import deliverable_cascade as cascade_mod
    from coordinator_core.ops import plan_status_transition as pst

    (tmp_path / "docs" / "plans").mkdir(parents=True)
    p = _write(
        tmp_path,
        "docs/plans/2026-09-26-empty-cascade-test.md",
        "---\ntitle: T\nstatus: executing\n---\n\nBody.\n",
    )

    async def _fake_handler(params, repo_root=None):
        return {
            "exit_code": 1,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "error": (
                f"deliverable.cascade_terminal: no live {params['target_kind']} "
                "carries this deliverable_id — nothing to advance"
            ),
        }

    monkeypatch.setattr(cascade_mod, "_handler", _fake_handler)

    rc = pst._run_cascade(str(p), "dlv-empty-cascade-000")
    err = capsys.readouterr().err

    assert rc == 0
    assert "legitimately has none" in err
    assert "needs attention" not in err


def test_matched_but_all_refused_still_exits_2(tmp_path, monkeypatch, capsys) -> None:
    """Case 2: candidates existed for one targeted kind but every one was
    refused. This IS the needs-attention signal, and must stay exit_code 2."""
    from coordinator_core.ops import deliverable_cascade as cascade_mod
    from coordinator_core.ops import plan_status_transition as pst

    (tmp_path / "docs" / "plans").mkdir(parents=True)
    p = _write(
        tmp_path,
        "docs/plans/2026-09-26-refused-cascade-test.md",
        "---\ntitle: T\nstatus: executing\n---\n\nBody.\n",
    )

    async def _fake_handler(params, repo_root=None):
        if params["target_kind"] == "handoff":
            return {
                "exit_code": 1,
                "candidates_matched": 1,
                "advanced": [],
                "refused": [{"path": "state/handoffs/x.md", "reason": "stale successor"}],
                "error": "deliverable.cascade_terminal: 1 candidate(s) matched but every one was refused",
            }
        return {
            "exit_code": 1,
            "candidates_matched": 0,
            "advanced": [],
            "refused": [],
            "error": "deliverable.cascade_terminal: no live sizing carries this deliverable_id",
        }

    monkeypatch.setattr(cascade_mod, "_handler", _fake_handler)

    rc = pst._run_cascade(str(p), "dlv-refused-cascade-000")
    err = capsys.readouterr().err

    assert rc == 2
    assert "cascade resolved no downstream artifact for" in err
    assert "legitimately has none" not in err
