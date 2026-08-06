"""
coordinator_core.tests.test_coverage_dag_silent_fallback_guards — regression tests
for the two coverage.py DAG-fixpoint sites that used to swallow exceptions silently
(no note, no log) instead of matching the Guard-2 pattern already used at
coverage.py:856/:864 (capture `type(exc).__name__: {exc}` into result.notes and set
indeterminate=True).

Sites closed:
    1. resolve_live_session_ids() raising inside _derive_dag_chain_set's live_sids
       hoist (formerly coverage.py:833) — was `except Exception: live_sids =
       frozenset()`, which is NOT fail-closed: continuing the fixpoint on a false-
       empty live-session set can make _handoff_session_live report an actually-live
       blocker as non-live, flipping an ancestor to coverable that a correct lookup
       would have kept blocked. Now: capture + indeterminate=True + early return.
    2. _get_handoff_consumed_by's underlying frontmatter read/parse raising inside
       _handoff_session_live (formerly coverage.py:726, `except Exception: return
       None`) — indistinguishable from a legitimately-unconsumed handoff. Now:
       _handoff_session_live surfaces a Guard-2-shaped note that the fixpoint loop
       folds into result.notes + indeterminate=True.

    _get_handoff_consumed_by itself keeps its original Optional[str] contract (it is
    imported directly by 6 modules outside coverage.py that compare its return value
    with `is None` / `== sid`); its failure path is a stderr diagnostic, not a
    result.notes capture — see the Tier 2 comment block at its definition.

Routed from state/bug-backlog/2026-07-22-coverage-gate-silent-fallbacks-design-call.yaml.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a git command in cwd; raise on non-zero exit."""
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """dag._FRONTMATTER_CACHE is module-level; clear it so a stale parse from a
    prior test's tmp_path never masks a fresh fixture's frontmatter.
    """
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


def _make_closing_only_repo(tmp_path: Path) -> Path:
    """A minimal repo with a single closing handoff and no predecessor — enough to
    reach the live_sids hoist / fixpoint entry without needing ancestors.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    closing = handoffs / "closing.md"
    closing.write_text("---\nsession_id: s1\n---\nClosing body.\n")
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        [
            "commit", "-m",
            "add closing handoff\n\n"
            "Session-Id: 33333333-3333-3333-3333-333333333333",
        ],
        repo,
    )
    return repo


def test_resolve_live_session_ids_failure_is_indeterminate_with_note(
    tmp_path: Path, monkeypatch
) -> None:
    """resolve_live_session_ids() raising must classify INDETERMINATE with a note
    naming the exception — not silently fall back to an empty live_sids set (which
    is fail-OPEN in effect: a truly-live blocker would read as non-live).
    """
    repo = _make_closing_only_repo(tmp_path)
    closing = repo / "state" / "handoffs" / "closing.md"

    def _boom():
        raise RuntimeError("liveness backend unavailable")

    monkeypatch.setattr(cov, "resolve_live_session_ids", _boom)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        f"resolve_live_session_ids() raising must classify INDETERMINATE, not "
        f"silently continue on an empty live_sids set; notes={result.notes!r}"
    )
    assert any(
        "resolve_live_session_ids raised RuntimeError" in note for note in result.notes
    ), f"expected a note naming the exception type; got {result.notes!r}"


def test_run_coverage_gate_reports_indeterminate_on_live_sids_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """Caller-visible surface: run_coverage_gate must return VERDICT=INDETERMINATE
    (exit_code=2) when resolve_live_session_ids() raises during the DAG fixpoint —
    asserting the public result surface, not just the internal dataclass.
    """
    repo = _make_closing_only_repo(tmp_path)
    closing = repo / "state" / "handoffs" / "closing.md"

    def _boom():
        raise RuntimeError("liveness backend unavailable")

    monkeypatch.setattr(cov, "resolve_live_session_ids", _boom)

    result = cov.run_coverage_gate(
        repo_root=str(repo), from_handoff=str(closing.resolve())
    )

    assert result.verdict == "INDETERMINATE"
    assert result.exit_code == 2
    assert any(
        "resolve_live_session_ids raised RuntimeError" in note for note in result.notes
    ), f"expected the raised-exception note on the caller-visible result; got {result.notes!r}"


def test_handoff_session_live_surfaces_note_on_read_failure(tmp_path: Path) -> None:
    """_handoff_session_live must return (True, note) — still conservative-live, but
    with a Guard-2-shaped note — when the underlying frontmatter read/parse raises,
    rather than the old (True, None) that was indistinguishable from a legitimately-
    unconsumed handoff.
    """
    missing_path = str(tmp_path / "does-not-exist.md")

    is_live, note = cov._handoff_session_live(missing_path, frozenset())

    assert is_live is True, "must stay conservative-live on read failure"
    assert note is not None, "must surface a note distinguishing this from a clean None"
    assert "_get_handoff_consumed_by raised" in note
    assert "FileNotFoundError" in note


def test_get_handoff_consumed_by_contract_unchanged_on_read_failure(
    tmp_path: Path, capsys
) -> None:
    """_get_handoff_consumed_by keeps its original Optional[str] contract (external
    callers compare with `is None` / `== sid`) — a read failure still returns bare
    None, not a tuple, but now emits a stderr diagnostic instead of vanishing
    silently.
    """
    missing_path = str(tmp_path / "does-not-exist.md")

    val = cov._get_handoff_consumed_by(missing_path)

    assert val is None
    captured = capsys.readouterr()
    assert "_get_handoff_consumed_by" in captured.err
    assert "FileNotFoundError" in captured.err


def test_parse_handoff_consumed_by_reads_claimed_by(tmp_path: Path) -> None:
    """New-vocabulary frontmatter: ``claimed_by:`` alone is read."""
    handoff = tmp_path / "claimed.md"
    handoff.write_text("---\nclaimed_by: session-new\n---\nBody.\n")

    assert cov._parse_handoff_consumed_by(str(handoff)) == "session-new"


def test_parse_handoff_consumed_by_reads_consumed_by(tmp_path: Path) -> None:
    """Old-vocabulary frontmatter: ``consumed_by:`` alone is still tolerated —
    DR-084 transitional ingest tolerance for not-yet-migrated consumer-repo
    corpora (example-retrieval-repo, example-cockpit-repo), see the function's docstring.
    """
    handoff = tmp_path / "consumed.md"
    handoff.write_text("---\nconsumed_by: session-old\n---\nBody.\n")

    assert cov._parse_handoff_consumed_by(str(handoff)) == "session-old"


def test_parse_handoff_consumed_by_prefers_claimed_by_when_both_present(
    tmp_path: Path,
) -> None:
    """When a record carries both field names, ``claimed_by`` must win
    regardless of which line comes first in the file — this pins the
    dedicated claimed_by-then-consumed_by search order over relying on
    regex-alternation position, which would instead match whichever name
    appears earliest in the text.
    """
    consumed_first = tmp_path / "consumed-first.md"
    consumed_first.write_text(
        "---\nconsumed_by: session-old\nclaimed_by: session-new\n---\nBody.\n"
    )
    assert cov._parse_handoff_consumed_by(str(consumed_first)) == "session-new"

    claimed_first = tmp_path / "claimed-first.md"
    claimed_first.write_text(
        "---\nclaimed_by: session-new\nconsumed_by: session-old\n---\nBody.\n"
    )
    assert cov._parse_handoff_consumed_by(str(claimed_first)) == "session-new"


def test_get_handoff_consumed_by_reads_both_vocabularies(tmp_path: Path) -> None:
    """Public accessor mirrors the dual-vocabulary read for both field names."""
    claimed = tmp_path / "claimed.md"
    claimed.write_text("---\nclaimed_by: session-new\n---\nBody.\n")
    consumed = tmp_path / "consumed.md"
    consumed.write_text("---\nconsumed_by: session-old\n---\nBody.\n")

    assert cov._get_handoff_consumed_by(str(claimed)) == "session-new"
    assert cov._get_handoff_consumed_by(str(consumed)) == "session-old"


def test_fixpoint_propagates_handoff_read_failure_to_indeterminate(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end: a blocker handoff whose frontmatter read raises must flip the
    whole DAG fixpoint to INDETERMINATE with a note, not silently resolve the
    ancestor's coverability off a wrongly-conservative-but-unflagged default.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    handoffs = repo / "state" / "handoffs"
    handoffs.mkdir(parents=True)

    ancestor = handoffs / "ancestor.md"
    ancestor.write_text("---\nsession_id: s2\n---\nAncestor body.\n")
    _git(["add", "state/handoffs/ancestor.md"], repo)
    _git(["commit", "-m", "add ancestor handoff"], repo)

    closing = handoffs / "closing.md"
    closing.write_text(
        "---\nsession_id: s1\npredecessor: ancestor.md\n---\nClosing body.\n"
    )
    _git(["add", "state/handoffs/closing.md"], repo)
    _git(
        [
            "commit", "-m",
            "add closing handoff\n\n"
            "Session-Id: 33333333-3333-3333-3333-333333333333",
        ],
        repo,
    )

    # Force reverse_membership to report the ancestor as blocked by some other
    # (non-closing-set) handoff, so the fixpoint must consult _handoff_session_live
    # on that blocker — and force that consult to raise.
    monkeypatch.setattr(
        cov, "reverse_membership", lambda node, dag_index, **kwargs: frozenset({"blocker.md"})
    )

    def _boom_parse(path):
        raise OSError("disk read error")

    monkeypatch.setattr(cov, "_parse_handoff_consumed_by", _boom_parse)

    result = cov._derive_dag_chain_set(
        str(closing.resolve()), str(repo), closing_session_id=""
    )

    assert result.indeterminate is True, (
        f"a blocker whose consumed_by cannot be read must classify INDETERMINATE; "
        f"notes={result.notes!r}"
    )
    assert any("_get_handoff_consumed_by raised OSError" in note for note in result.notes), (
        f"expected an OSError-naming note; got {result.notes!r}"
    )
