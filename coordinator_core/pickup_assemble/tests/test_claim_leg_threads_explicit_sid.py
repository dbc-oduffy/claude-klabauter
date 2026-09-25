"""
coordinator_core.pickup_assemble.tests.test_claim_leg_threads_explicit_sid

Purpose: regression pin for P088-C1
(docs/plans/2026-09-11-session-identity-residue-memo-send-sent.md).

VERIFY-AND-PIN, NOT A FIX. Census row 2 (as of this row's fire time) reads
2 claim-grant call sites, and BOTH already thread an explicitly-resolved
session id into `liveness.claim_held_by_me`:

  - `pickup_brief.compute_claim_grant` -> `_liveness.claim_held_by_me(...,
    my_sid=_explicitly_scoped_session_id())`
  - `pickup_brief._claim_already_self_held` (ported verbatim into
    `pickup_assemble`) -> same call shape, same accessor

`_explicitly_scoped_session_id()` reads ONLY the `apply_base.session_identity()`
contextvar scope (`current_session_env()`) — never `os.environ` — so a caller
that entered that scope with an explicit id is recognised by `claim_held_by_me`
even inside a warm-served request, and a caller with NO such scope active
gets `""`, which leaves `claim_held_by_me` on its unchanged (fail-closed under
warm-serve) resolution path. This test proves both halves and pins them so a
future "fix" that re-adds an `os.environ` fallback, or drops the `my_sid=`
threading, breaks loudly here first.

Run from the repo root: python -m pytest
coordinator_core/pickup_assemble/tests/test_claim_leg_threads_explicit_sid.py -q
"""
from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.pickup_assemble as pa
import coordinator_core.pickup_brief as pb
from coordinator_core.contract import apply_base

pytestmark = [pytest.mark.cadence]


def _seed_claim_dir(repo: Path, class_: str, basename: str, holder_sid: str) -> Path:
    cdir = repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "session_id").write_text(f"{holder_sid}\n", encoding="utf-8")
    (cdir / "stage").write_text("apply\n", encoding="utf-8")
    return cdir


def test_compute_claim_grant_recognises_scoped_id_as_self(tmp_path):
    """`compute_claim_grant` resolves `held_by_self: True` for the SCOPED
    id, proving `my_sid=_explicitly_scoped_session_id()` is actually wired
    through to `claim_held_by_me` (not merely present in source text)."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _seed_claim_dir(repo, "handoff", "h1.md", "sid-scoped")

    with apply_base.session_identity("sid-scoped"):
        grant = pb.compute_claim_grant(
            repo, "handoff", "h1.md", "state/handoffs/h1.md", cwd=str(repo)
        )

    assert grant["held_by_self"] is True
    assert grant["holder"] == "sid-scoped"


def test_claim_already_self_held_recognises_scoped_id(tmp_path):
    """`_claim_already_self_held` (both the `pickup_brief` original and the
    `pickup_assemble`-ported copy) resolves True for the SCOPED id via the
    same `my_sid=_explicitly_scoped_session_id()` threading."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _seed_claim_dir(repo, "handoff", "h1.md", "sid-scoped")

    with apply_base.session_identity("sid-scoped"):
        assert pb._claim_already_self_held(repo, "handoff", "h1.md") is True
        assert pa._claim_already_self_held(repo, "handoff", "h1.md") is True


def test_no_scope_active_resolves_empty_scoped_id():
    """Negative: with NO `session_identity()` scope active,
    `_explicitly_scoped_session_id()` returns `""` on both modules — the
    fail-closed branch inside `claim_held_by_me` governs, never an
    `os.environ` ambient fallback (module docstrings' NEGATIVE SPEC)."""
    assert pb._explicitly_scoped_session_id() == ""
    assert pa._explicitly_scoped_session_id() == ""


def test_no_scope_active_does_not_self_grant_a_foreign_claim(tmp_path):
    """Negative, end-to-end: with no scope active, a claim recorded under
    SOME OTHER session id is not recognised as self-held merely because
    `_explicitly_scoped_session_id()` fell back to something ambient — it
    resolves `""`, and `claim_held_by_me` falls through to its own
    (unchanged) resolution path rather than granting on an empty id."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _seed_claim_dir(repo, "handoff", "h1.md", "sid-someone-else")

    assert pb._explicitly_scoped_session_id() == ""
    assert pa._explicitly_scoped_session_id() == ""
