"""
coordinator_core.ops.tests.test_merge_gate_receipt_contract

C2b (docs/plans/2026-09-11-the-merge-gate-proves-receipt-coverage.md § C2b,
re-anchored AC5): binds the receipt WRITER
(`provision_report._splice_review_receipt`) to the review dimension's READER
(`gate_dimension_review._review_dimension_check`) with a real rendered
sidecar, never a hand-written fixture string.

WHY THIS EXISTS. Every existing receipt fixture (see
`review_trail/tests/test_receipt_credit.py::_write_sidecar`) hand-writes the
`review_receipt:` block rather than rendering it through the real splice
function. That proves the READER parses the shape correctly, but never
proves the WRITER's actual output is something the reader can credit -- a
rename of the key or field on either side, with no test exercising both,
would land silently. This module is the one place both meet.

Deliberately UNMARKED, for the same reason `test_receipt_credit.py` is: it
carries no `spawns_process`/`cadence` marker because `_run_git` is
monkeypatched below and the whole module spawns nothing, so parking it
behind a cadence gate would hide the exact defect class it exists to catch.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops import gate_dimension_review
from coordinator_core.session.machinery_paths import share_root
from coordinator_core.subagent_sandbox.provision_report import _splice_review_receipt

_SHA = "c" * 40
_SESSION = "aaaa1111-2222-3333-4444-555566667777"
_AGENT_ID = "agent-id-0001"
_CODE_PATH = "coordinator_core/foo.py"
_DIFF_BASE = "origin/main"


def _render_receipt_doc(*, session_id: str, agent_type: str, stamped_at: str) -> str:
    doc_text = (
        "---\n"
        f"agent_type: '{agent_type}'\n"
        "---\n\n"
        "## Findings\n\nOne real finding.\n"
    )
    return _splice_review_receipt(doc_text, session_id, _AGENT_ID, agent_type, stamped_at)


def _write_sidecar(repo_root: Path, doc_text: str, *, name: str = "receipt.md") -> Path:
    session_dir = Path(share_root(str(repo_root))) / _SESSION
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / name
    path.write_text(doc_text, encoding="utf-8")
    return path


def _fake_git_log_output(*, sha: str, committed_at: str, session_id: str) -> str:
    """One `\\x01`-prefixed header, field order per `_COMMIT_HEADER_FORMAT`
    (sha, committer date, Session-Id trailer), followed by one wanted,
    non-bookkeeping path line -- so the commit actually joins
    `commit_sha_set` rather than passing PASS on the empty-population
    branch (staff-eng review: a headers-only fixture proves nothing about
    the receipt binding)."""
    sep = gate_dimension_review._HEADER_FIELD_SEP
    prefix = gate_dimension_review._SHA_HEADER_PREFIX
    return f"{prefix}{sha}{sep}{committed_at}{sep}{session_id}\n{_CODE_PATH}"


def _check(monkeypatch, repo_root: Path, *, sha: str, committed_at: str, session_id: str):
    monkeypatch.setattr(
        gate_dimension_review,
        "_run_git",
        lambda args, cwd: (0, _fake_git_log_output(sha=sha, committed_at=committed_at, session_id=session_id), ""),
    )
    monkeypatch.setattr(gate_dimension_review, "read_reviewed_set", lambda repo_root_str: frozenset())
    return gate_dimension_review._review_dimension_check(
        [_CODE_PATH], _DIFF_BASE, repo_root
    )


def test_credits_a_commit_the_real_writer_stamped_before_it(tmp_path, monkeypatch) -> None:
    """THE POSITIVE CONTROL (1). A commit dated before `stamped_at`, whose
    Session-Id matches, is credited by a receipt rendered through the real
    writer -- PASS in the receipt-credited form, not the empty-population
    one."""
    doc_text = _render_receipt_doc(
        session_id=_SESSION, agent_type="code-reviewer", stamped_at="2026-09-20T12:00:00+00:00"
    )
    _write_sidecar(tmp_path, doc_text)

    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T11:00:00+00:00", session_id=_SESSION
    )

    assert result.verdict == gate_dimension_review.Verdict.PASS
    assert "covered: all 1 commit(s)" in result.detail


def test_removing_the_sidecar_fails_the_same_fixture(tmp_path, monkeypatch) -> None:
    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T11:00:00+00:00", session_id=_SESSION
    )

    assert result.verdict == gate_dimension_review.Verdict.FAIL


def test_does_not_credit_a_commit_authored_after_the_receipt(tmp_path, monkeypatch) -> None:
    doc_text = _render_receipt_doc(
        session_id=_SESSION, agent_type="code-reviewer", stamped_at="2026-09-20T12:00:00+00:00"
    )
    _write_sidecar(tmp_path, doc_text)

    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T13:00:00+00:00", session_id=_SESSION
    )

    assert result.verdict == gate_dimension_review.Verdict.FAIL


def test_a_blank_body_sidecar_from_the_real_writer_does_not_credit(tmp_path, monkeypatch) -> None:
    doc_text = (
        "---\n"
        "agent_type: 'code-reviewer'\n"
        "---\n\n"
        "   \n"
    )
    doc_text = _splice_review_receipt(
        doc_text, _SESSION, _AGENT_ID, "code-reviewer", "2026-09-20T12:00:00+00:00"
    )
    _write_sidecar(tmp_path, doc_text)

    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T11:00:00+00:00", session_id=_SESSION
    )

    assert result.verdict == gate_dimension_review.Verdict.FAIL


def test_a_non_reviewer_receipt_does_not_credit(tmp_path, monkeypatch) -> None:
    """(4): a receipt stamped for agent_type `executor` gives FAIL -- an
    executor applies work, it does not render a review verdict, and is not
    a `DELEGATE_REVIEWERS` member."""
    doc_text = _render_receipt_doc(
        session_id=_SESSION, agent_type="executor", stamped_at="2026-09-20T12:00:00+00:00"
    )
    _write_sidecar(tmp_path, doc_text)

    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T11:00:00+00:00", session_id=_SESSION
    )

    assert result.verdict == gate_dimension_review.Verdict.FAIL


def test_never_spawns_a_subprocess(tmp_path, monkeypatch) -> None:
    import subprocess

    def explode(*args, **kwargs):
        raise AssertionError("test_merge_gate_receipt_contract must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)

    doc_text = _render_receipt_doc(
        session_id=_SESSION, agent_type="code-reviewer", stamped_at="2026-09-20T12:00:00+00:00"
    )
    _write_sidecar(tmp_path, doc_text)

    result = _check(
        monkeypatch, tmp_path, sha=_SHA, committed_at="2026-09-20T11:00:00+00:00", session_id=_SESSION
    )
    assert result.verdict == gate_dimension_review.Verdict.PASS
