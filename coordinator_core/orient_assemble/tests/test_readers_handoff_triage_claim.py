"""
Tests for coordinator_core.orient_assemble.readers_handoff_triage's AC11
ledger-first filter — `_suppress_live_ledger_claims` / `_read_ready`.

Spec backlink: docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
§ Tasks, chunk C7 (AC11), sub-dispatch C7a.

THE INCIDENT THIS COVERS: `_read_ready` lists `deployment_state=ready_to_fire
AND status=open` off the tracked-frontmatter mirror alone. A handoff whose
branch-independent claim ledger still holds a LIVE claim (the mirror having
reverted to `open` on a branch switch — this plan's own Problem section
incident) must not be advertised as pickup-ready. A DEAD-holder ledger claim
must NOT suppress the listing — that baton is genuinely available.

Negative-spec:
    - Does NOT exercise `_cmd_ready` / the `records.query` engine op — this
      is a unit test of the post-hoc line filter
      (`_suppress_live_ledger_claims`), matching the module's own
      "post-hoc line filtering, not a query-time predicate" docstring; the
      ported CLI's query logic is out of scope for this chunk.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coordinator_core import claim_state as claim_state_module
from coordinator_core.orient_assemble import readers_handoff_triage as rht


def _write_claim_dir(common_dir: Path, handoff_name: str, session_id: str, claimed_at: str = "2026-08-07T10:00:00Z") -> Path:
    claim_dir = common_dir / "coordinator-sessions" / "handoff-claims" / handoff_name
    claim_dir.mkdir(parents=True, exist_ok=True)
    (claim_dir / "session_id").write_text(session_id, encoding="utf-8")
    (claim_dir / "claimed_at").write_text(claimed_at, encoding="utf-8")
    return claim_dir


def _write_handoff(path: Path, *, status: str = "open") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nstatus: {status}\n---\n\n# body\n", encoding="utf-8")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    repo_root = tmp_path
    common_dir = repo_root / "gitdir"
    common_dir.mkdir()
    monkeypatch.setattr(rht, "_REPO_ROOT", repo_root)
    return repo_root, common_dir


def test_live_ledger_claim_suppresses_the_line(workspace):
    """AC11: a ledger-claimed (live holder) + mirror-open handoff is ABSENT
    from the filtered listing, even though the mirror alone would have
    advertised it (deployment_state=ready_to_fire AND status=open)."""
    repo_root, common_dir = workspace
    link_path = "state/handoffs/2026-08-07-worked.md"
    _write_handoff(repo_root / link_path, status="open")
    _write_claim_dir(common_dir, "2026-08-07-worked.md", "sess-live")

    text = f"- [worked](state/handoffs/2026-08-07-worked.md) — open\n"

    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True):
        filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert "worked" not in filtered


def test_dead_ledger_holder_still_advertised(workspace):
    """The control case: a ledger claim whose holder is DEAD degrades to "no
    ledger claim" inside resolve_claim_state — the line must survive the
    filter (the baton is genuinely available for pickup)."""
    repo_root, common_dir = workspace
    link_path = "state/handoffs/2026-08-07-abandoned.md"
    _write_handoff(repo_root / link_path, status="open")
    _write_claim_dir(common_dir, "2026-08-07-abandoned.md", "sess-dead")

    text = "- [abandoned](state/handoffs/2026-08-07-abandoned.md) — open\n"

    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=False):
        filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert "abandoned" in filtered


def test_no_ledger_claim_at_all_still_advertised(workspace):
    """No ledger claim dir exists at all — resolve_claim_state's source is
    "none" (or "mirror" if the frontmatter itself carried a claim, which a
    ready_to_fire/open handoff never does) — the line must survive."""
    repo_root, common_dir = workspace
    link_path = "state/handoffs/2026-08-07-untouched.md"
    _write_handoff(repo_root / link_path, status="open")

    text = "- [untouched](state/handoffs/2026-08-07-untouched.md) — open\n"

    filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert "untouched" in filtered


def test_mixed_listing_keeps_only_the_unclaimed_and_dead_lines(workspace):
    """A multi-line listing: one live-claimed line drops, the dead-claimed
    and unclaimed lines survive — exercises the per-line filter against a
    realistic multi-handoff markdown-list body."""
    repo_root, common_dir = workspace
    _write_handoff(repo_root / "state/handoffs/2026-08-07-worked.md", status="open")
    _write_claim_dir(common_dir, "2026-08-07-worked.md", "sess-live")
    _write_handoff(repo_root / "state/handoffs/2026-08-07-abandoned.md", status="open")
    _write_claim_dir(common_dir, "2026-08-07-abandoned.md", "sess-dead")
    _write_handoff(repo_root / "state/handoffs/2026-08-07-untouched.md", status="open")

    text = (
        "- [worked](state/handoffs/2026-08-07-worked.md) — open\n"
        "- [abandoned](state/handoffs/2026-08-07-abandoned.md) — open\n"
        "- [untouched](state/handoffs/2026-08-07-untouched.md) — open\n"
    )

    def _fake_live(claim_dir_str: str) -> bool:
        return "worked" in claim_dir_str

    with mock.patch.object(claim_state_module, "cs_claim_holder_live", side_effect=_fake_live):
        filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert "worked" not in filtered
    assert "abandoned" in filtered
    assert "untouched" in filtered


def test_title_containing_link_syntax_still_resolves_the_real_link_path(workspace):
    """Reviewer Finding 0 (P3, EM-overturned to FIX): a handoff `title` that
    itself contains the literal sequence `](` must not fool the extractor
    into reading garbage as `link_path` off the FIRST match — the real link
    is the RIGHTMOST `](...)` on the line. A live ledger claim on this
    handoff must still be correctly resolved and the line suppressed."""
    repo_root, common_dir = workspace
    link_path = "state/handoffs/2026-08-07-worked.md"
    _write_handoff(repo_root / link_path, status="open")
    _write_claim_dir(common_dir, "2026-08-07-worked.md", "sess-live")

    text = "- [Fix ](broken) link](state/handoffs/2026-08-07-worked.md) — open\n"

    with mock.patch.object(claim_state_module, "cs_claim_holder_live", return_value=True):
        filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert filtered == ""


def test_title_containing_unbalanced_brackets_still_advertised_when_unclaimed(workspace):
    """Companion control case: a title with unbalanced brackets/parens and
    NO live ledger claim must still survive the filter — proves the
    rightmost-match fix doesn't over-suppress lines that were never claimed."""
    repo_root, common_dir = workspace
    link_path = "state/handoffs/2026-08-07-untouched.md"
    _write_handoff(repo_root / link_path, status="open")

    text = "- [odd ](title) with )( stray chars](state/handoffs/2026-08-07-untouched.md) — open\n"

    filtered = rht._suppress_live_ledger_claims(text, common_dir=common_dir)

    assert "untouched" in filtered
