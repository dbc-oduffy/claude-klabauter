"""
coordinator_core.ops.tests.test_merge_gate_receipt_contract

AC5, re-anchored (C2b, docs/plans/2026-09-11-the-merge-gate-proves-receipt-
coverage.md § C2b). Binds the reviewer sidecar WRITER
(`provision_report._splice_review_receipt`) to the review dimension READER
(`gate_dimension_review._review_dimension_check`) through the real
`receipt_credit.receipt_credited_shas`, not a mocked stand-in for it.

Why this file exists and `test_gate_dimension_review.py`'s own receipt-source
tests do not already cover it: every one of those monkeypatches
`receipt_credited_shas` itself, which pins this module's CALL SHAPE but
proves nothing about whether the block the real writer emits is the block the
real reader can actually parse and credit. A silent shape drift between the
two -- a renamed key, a re-ordered field, a splice point that stops landing
inside the frontmatter fence -- would sail through every existing test here
because none of them ever construct a sidecar with the writer and read it
back with the writer-independent reader in the same test.

Same rationale as `review_trail/tests/test_receipt_credit.py`'s module
docstring: the defect being guarded against is a STUCK NEGATIVE, not a stuck
positive. A suite that only proves "still FAILs" would pass identically
against a binding that reads nothing at all, which is why (1) below is the
one that actually exercises the credit path, with an explicit sidecar-removed
control proving the credit was not vacuous population-emptiness PASS.

Spawn-free (only `gate_dimension_review._run_git` is faked; the receipt path
below it does no subprocess work of its own -- `receipt_credit.py`'s own
`test_never_spawns_a_subprocess` already pins that at its unit) and
deliberately UNMARKED, matching `test_receipt_credit.py`'s own reasoning for
staying out of the `cadence` tier: the defect this guards went unnoticed for
486 commits precisely because nothing in the fast tier could see it.

Spec backlink: docs/plans/2026-09-11-the-merge-gate-proves-receipt-coverage.md § C2b
"""

from __future__ import annotations

from pathlib import Path

import coordinator_core.ops.gate_dimension_review as gate_dimension_review
from coordinator_core.ops.gate_validate_invocable import Verdict
from coordinator_core.session import machinery_paths
from coordinator_core.subagent_sandbox.provision_report import _splice_review_receipt

_SHA_A = "a" * 40
_SESSION = "11112222-3333-4444-5555-666677778888"
_PATH = "coordinator_core/coverage.py"

_HDR = gate_dimension_review._SHA_HEADER_PREFIX
_FSEP = gate_dimension_review._HEADER_FIELD_SEP


def _fake_git_log(committed_at: str, session_id: str = _SESSION) -> "tuple[int, str, str]":
    """One `\\x01`-prefixed header, fields in `_COMMIT_HEADER_FORMAT`'s own
    order (sha, committer date, Session-Id trailer), followed by one
    NUL/newline-separated wanted path line -- `_PATH`, which is both in
    `changed_files` below and not bookkeeping under
    `coverage._is_bookkeeping_path`, so the commit actually joins
    `commit_sha_set` rather than passing PASS vacuously on the
    empty-population branch."""
    header = f"{_HDR}{_SHA_A}{_FSEP}{committed_at}{_FSEP}{session_id}"
    return 0, f"{header}\n{_PATH}\0", ""


def _base_doc_text() -> str:
    """Minimal frontmatter + NON-BLANK body -- the same fence shape every
    `_build_*_doc_text` builder in `provision_report.py` produces, and the
    one `_splice_receipt_block`'s `"---\\n\\n"` marker requires."""
    return "---\nagent_type: 'code-reviewer'\n---\n\n## Findings\n\nOne real finding.\n"


def _write_sidecar(
    tmp_path: Path,
    *,
    session_id: str = _SESSION,
    agent_type: str = "code-reviewer",
    stamped_at: str,
    body_doc_text: "str | None" = None,
    name: str = "coordinatorcode-reviewer.abc123.md",
) -> Path:
    """Builds the sidecar with the REAL writer
    (`provision_report._splice_review_receipt`), never a hand-written block,
    under the share root `machinery_paths.share_roots` resolves."""
    doc_text = body_doc_text if body_doc_text is not None else _base_doc_text()
    spliced = _splice_review_receipt(
        doc_text, session_id, "a0c4e2ed8b92a39d4", agent_type, stamped_at
    )
    share_root = Path(machinery_paths.share_roots(str(tmp_path))[0])
    session_dir = share_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(spliced, encoding="utf-8")
    return path


def test_writer_written_receipt_credits_a_commit_predating_it(
    monkeypatch, tmp_path: Path
) -> None:
    """(1) THE POSITIVE. A commit dated before `stamped_at`, Session-Id
    matching, gives PASS in the receipt-credited form -- not the empty-
    population form, proving the commit actually reached the credit check."""
    _write_sidecar(tmp_path, stamped_at="2026-08-28T12:00:00+00:00")
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _fake_git_log("2026-08-28T11:00:00+00:00"),
    )
    result = gate_dimension_review._review_dimension_check(
        [_PATH], "abc..HEAD", tmp_path
    )
    assert result.verdict is Verdict.PASS
    assert "covered: all 1 commit(s)" in result.detail


def test_control_same_fixture_with_sidecar_removed_gives_fail(
    monkeypatch, tmp_path: Path
) -> None:
    """(1)'s control: same commit, same range, no sidecar written at all --
    must FAIL. Without this, a reader that credits everything regardless of
    receipt state would still pass the positive above."""
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _fake_git_log("2026-08-28T11:00:00+00:00"),
    )
    result = gate_dimension_review._review_dimension_check(
        [_PATH], "abc..HEAD", tmp_path
    )
    assert result.verdict is Verdict.FAIL


def test_writer_written_receipt_does_not_credit_a_commit_authored_after_it(
    monkeypatch, tmp_path: Path
) -> None:
    """(2) The ordering rule survives the real writer: a commit dated AFTER
    `stamped_at` is not credited -- a reviewer cannot have read a commit that
    did not yet exist."""
    _write_sidecar(tmp_path, stamped_at="2026-08-27T16:13:31+00:00")
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _fake_git_log("2026-08-28T11:32:24+00:00"),
    )
    result = gate_dimension_review._review_dimension_check(
        [_PATH], "abc..HEAD", tmp_path
    )
    assert result.verdict is Verdict.FAIL


def test_writer_written_blank_body_sidecar_does_not_credit(
    monkeypatch, tmp_path: Path
) -> None:
    """(3) A receipt is stamped at DISPATCH, before the reviewer writes
    anything -- a blank body means the review aborted, not that it passed,
    even though the receipt block itself is well-formed and well-timed."""
    blank_doc = "---\nagent_type: 'code-reviewer'\n---\n\n   \n"
    _write_sidecar(
        tmp_path, stamped_at="2026-08-28T12:00:00+00:00", body_doc_text=blank_doc
    )
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _fake_git_log("2026-08-28T11:00:00+00:00"),
    )
    result = gate_dimension_review._review_dimension_check(
        [_PATH], "abc..HEAD", tmp_path
    )
    assert result.verdict is Verdict.FAIL


def test_writer_written_receipt_for_review_integrator_does_not_credit(
    monkeypatch, tmp_path: Path
) -> None:
    """(4) `review-integrator` is not a `DELEGATE_REVIEWERS` member -- a
    receipt stamped under that agent_type is not a delegate review and must
    not credit the commit, even though the block is otherwise well-formed and
    well-timed."""
    _write_sidecar(
        tmp_path,
        stamped_at="2026-08-28T12:00:00+00:00",
        agent_type="review-integrator",
    )
    monkeypatch.setattr(
        gate_dimension_review, "read_reviewed_set", lambda repo_root: set()
    )
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: _fake_git_log("2026-08-28T11:00:00+00:00"),
    )
    result = gate_dimension_review._review_dimension_check(
        [_PATH], "abc..HEAD", tmp_path
    )
    assert result.verdict is Verdict.FAIL
