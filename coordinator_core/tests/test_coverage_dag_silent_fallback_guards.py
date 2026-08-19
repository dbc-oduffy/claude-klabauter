"""
coordinator_core.tests.test_coverage_dag_silent_fallback_guards — regression tests
for coverage.py sites that used to swallow handoff-frontmatter read/parse
exceptions silently (no note, no log) instead of surfacing them.

Routed from state/bug-backlog/2026-07-22-coverage-gate-silent-fallbacks-design-call.yaml.

K-001 note (state/kill-ledger.md): `run_coverage_gate` and its verdict were
removed under kill-ledger entry K-001. The one test asserting on
`run_coverage_gate`'s public result surface (VERDICT=INDETERMINATE,
exit_code=2) was deleted as dead code at that time.

2026-08-19 note (state/kill-ledger.md, DAG-fixpoint cut orphaned by K-007):
the DAG-mode fixpoint itself (`_derive_dag_chain_set`) is now removed, so the
two tests that pinned ITS Guard-2 behaviour directly —
`test_resolve_live_session_ids_failure_is_indeterminate_with_note` (the
`resolve_live_session_ids()`-raises site) and
`test_fixpoint_propagates_handoff_read_failure_to_indeterminate` (the
end-to-end blocker-read-failure site) — retire with it, along with their
now-unused `_make_closing_only_repo`/`_git`/`_init_repo` fixture scaffolding.
The remaining tests below pin `_handoff_session_live` and
`_get_handoff_consumed_by`/`_parse_handoff_consumed_by` directly — those
helpers have live production consumers outside this module (see
`_get_handoff_consumed_by`'s own docstring) and are unaffected by the cut.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import coverage as cov
from coordinator_core import dag


@pytest.fixture(autouse=True)
def clear_frontmatter_cache():
    """dag._FRONTMATTER_CACHE is module-level; clear it so a stale parse from a
    prior test's tmp_path never masks a fresh fixture's frontmatter.
    """
    dag._FRONTMATTER_CACHE.clear()
    yield
    dag._FRONTMATTER_CACHE.clear()


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
