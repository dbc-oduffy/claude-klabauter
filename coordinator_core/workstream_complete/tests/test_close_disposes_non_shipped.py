"""test_close_disposes_non_shipped — C1 of
state/dispatch-briefs/2026-08-31-a-close-disposes-the-baton-it-closed/C1.md.

Covers the seam this chunk built: `directives_commit_tail.
resolve_close_stamp_candidates`/`apply_close_stamps`/`revert_close_stamps` —
the sibling disposal path for `closed`/`abandoned`/`continued` dispositions
that `resolve_ship_stamp_candidates`/`apply_ship_stamps` (the predecessor
plan's own C2) never covered, since that resolver only ever selects
`disposition == "shipped"`.

Negative-spec (mirrors `test_close_ships_its_batons.py`'s own): none of these
tests drive a real `handoff.transition` op end-to-end through the JSON-RPC
registry or a real claim ledger directory tree beyond what `tmp_path` itself
provides — `_held_handoff_basenames` and the claim-release call
(`session.claims.release_artifact`) are monkeypatched at the seam this
module calls through, so what is under test is the DISPOSAL ORCHESTRATION
(positive membership, the closed_reason refusal, the continued assertion,
stamp-then-release ordering, revert-on-failed-commit), not `handoff.
transition close`'s own already-tested frontmatter mutation.

Run: python -m pytest coordinator_core/workstream_complete/tests/test_close_disposes_non_shipped.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.workstream_complete import directives_commit_tail


def _write_handoff(tmp_path: Path, basename: str, deployment_state: str = "in_flight") -> Path:
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / basename
    path.write_text(
        f"---\nstatus: claimed\ndeployment_state: {deployment_state}\n---\nbody\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# resolve_close_stamp_candidates — positive-membership rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("disposition", ["closed", "abandoned", "continued"])
def test_non_shipped_disposition_is_a_candidate(tmp_path, monkeypatch, disposition):
    _write_handoff(tmp_path, "foo.md")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    entry = {"disposition": disposition}
    if disposition != "continued":
        entry["closed_reason"] = "cancelled"
    decisions = {"handoff_dispositions": {"foo.md": entry}}

    candidates = directives_commit_tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions)

    expected_reason = "cancelled" if disposition != "continued" else None
    assert candidates == [("state/handoffs/foo.md", disposition, expected_reason)]


def test_shipped_disposition_is_not_a_close_stamp_candidate(tmp_path, monkeypatch):
    # Regression guard on the predecessor plan: `shipped` still belongs
    # exclusively to `resolve_ship_stamp_candidates` — this sibling resolver
    # must never also pick it up.
    _write_handoff(tmp_path, "foo.md")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "shipped", "shipped_in": "deadbeef"}}}

    candidates = directives_commit_tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_held_claim_this_close_never_touched_is_not_a_candidate(tmp_path, monkeypatch):
    _write_handoff(tmp_path, "bar.md")
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["bar.md"]
    )
    decisions = {"handoff_dispositions": {}}

    candidates = directives_commit_tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


def test_already_archived_baton_is_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(
        directives_commit_tail, "_held_handoff_basenames", lambda *_a, **_k: ["foo.md"]
    )
    decisions = {"handoff_dispositions": {"foo.md": {"disposition": "closed", "closed_reason": "stale"}}}

    candidates = directives_commit_tail.resolve_close_stamp_candidates(tmp_path, "sid", decisions)

    assert candidates == []


# ---------------------------------------------------------------------------
# apply_close_stamps — closed/abandoned: terminal stamp + release
# ---------------------------------------------------------------------------


def _stub_transition_ok(monkeypatch):
    async def _ok(params, repo_root=None):
        return {"exit_code": 0, "applied": True, "message": "closed"}

    monkeypatch.setattr(
        "coordinator_core.ops.handoff_transition._handler", _ok
    )


@pytest.mark.parametrize("disposition", ["closed", "abandoned"])
def test_closed_and_abandoned_stamp_terminal_and_release_claim(tmp_path, monkeypatch, disposition):
    _write_handoff(tmp_path, "foo.md")
    _stub_transition_ok(monkeypatch)
    released = []
    monkeypatch.setattr(
        directives_commit_tail,
        "_release_handoff_claim",
        lambda root, basename: released.append(basename),
    )

    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", disposition, "cancelled")]
    )

    assert outcome.disposed_paths == ("state/handoffs/foo.md",)
    assert outcome.skipped_paths == ()
    assert outcome.attempted == 1
    assert backups == {"state/handoffs/foo.md": (tmp_path / "state/handoffs/foo.md").read_text(encoding="utf-8")}
    assert released == ["foo.md"]


def test_missing_closed_reason_is_a_caller_error_never_guessed(tmp_path, monkeypatch):
    _write_handoff(tmp_path, "foo.md")
    _stub_transition_ok(monkeypatch)
    released = []
    monkeypatch.setattr(
        directives_commit_tail,
        "_release_handoff_claim",
        lambda root, basename: released.append(basename),
    )

    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", "closed", None)]
    )

    assert outcome.disposed_paths == ()
    assert outcome.skipped_paths == ("state/handoffs/foo.md",)
    assert "closed_reason" in outcome.diagnostics[0]
    assert backups == {}
    assert released == [], "no write, no release — a fabricated reason is never invented"


def test_invalid_closed_reason_is_refused(tmp_path, monkeypatch):
    _write_handoff(tmp_path, "foo.md")
    _stub_transition_ok(monkeypatch)
    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", "closed", "not-a-real-reason")]
    )

    assert outcome.disposed_paths == ()
    assert outcome.skipped_paths == ("state/handoffs/foo.md",)
    assert backups == {}


def test_close_verb_failure_skips_without_releasing(tmp_path, monkeypatch):
    _write_handoff(tmp_path, "foo.md")

    async def _fail(params, repo_root=None):
        return {"exit_code": 1, "applied": False, "error": "close refused"}

    monkeypatch.setattr("coordinator_core.ops.handoff_transition._handler", _fail)
    released = []
    monkeypatch.setattr(
        directives_commit_tail,
        "_release_handoff_claim",
        lambda root, basename: released.append(basename),
    )

    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", "closed", "stale")]
    )

    assert outcome.disposed_paths == ()
    assert outcome.skipped_paths == ("state/handoffs/foo.md",)
    assert backups == {}
    assert released == [], "a write that never landed must never release the claim"


# ---------------------------------------------------------------------------
# apply_close_stamps — continued: assertion only, no re-stamp
# ---------------------------------------------------------------------------


def test_continued_disposition_asserts_and_releases_without_a_write(tmp_path, monkeypatch):
    handoff = _write_handoff(tmp_path, "foo.md", deployment_state="continued")
    before = handoff.read_text(encoding="utf-8")
    released = []
    monkeypatch.setattr(
        directives_commit_tail,
        "_release_handoff_claim",
        lambda root, basename: released.append(basename),
    )

    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", "continued", None)]
    )

    assert outcome.disposed_paths == ("state/handoffs/foo.md",)
    assert handoff.read_text(encoding="utf-8") == before, "continued is already terminal — no re-stamp"
    assert backups == {}, "nothing was written, so nothing to revert"
    assert released == ["foo.md"]


def test_continued_disposition_assertion_failure_skips_and_never_releases(tmp_path, monkeypatch):
    # On-disk deployment_state does NOT read continued -- the assertion this
    # chunk's item (c) requires must refuse rather than guess or overwrite.
    _write_handoff(tmp_path, "foo.md", deployment_state="in_flight")
    released = []
    monkeypatch.setattr(
        directives_commit_tail,
        "_release_handoff_claim",
        lambda root, basename: released.append(basename),
    )

    outcome, backups = directives_commit_tail.apply_close_stamps(
        tmp_path, [("state/handoffs/foo.md", "continued", None)]
    )

    assert outcome.disposed_paths == ()
    assert outcome.skipped_paths == ("state/handoffs/foo.md",)
    assert released == []


# ---------------------------------------------------------------------------
# revert_close_stamps — mirrors revert_ship_stamps' own contract
# ---------------------------------------------------------------------------


def test_revert_close_stamps_restores_original_bytes(tmp_path):
    handoff = _write_handoff(tmp_path, "foo.md")
    original = handoff.read_text(encoding="utf-8")
    handoff.write_text("mutated\n", encoding="utf-8")

    directives_commit_tail.revert_close_stamps(
        tmp_path, ["state/handoffs/foo.md"], {"state/handoffs/foo.md": original}
    )

    assert handoff.read_text(encoding="utf-8") == original


def test_revert_close_stamps_skips_relpaths_with_no_backup(tmp_path):
    handoff = _write_handoff(tmp_path, "foo.md")
    original = handoff.read_text(encoding="utf-8")

    # No exception, no write, for a relpath (e.g. a "continued" candidate)
    # absent from backups.
    directives_commit_tail.revert_close_stamps(tmp_path, ["state/handoffs/foo.md"], {})

    assert handoff.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# EMPTY_CLOSE_STAMP_OUTCOME sentinel — "ran, found nothing" reader
# ---------------------------------------------------------------------------


def test_empty_close_stamp_outcome_sentinel_shape():
    assert directives_commit_tail.EMPTY_CLOSE_STAMP_OUTCOME == directives_commit_tail.CloseStampOutcome(
        disposed_paths=(), skipped_paths=(), attempted=0, diagnostics=()
    )
