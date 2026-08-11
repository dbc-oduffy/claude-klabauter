"""
coordinator_core.ops.fleet.tests.test_archive_actioned_memos

Purpose: sweep-eligibility coverage for the `superseded` terminal status added
to `fleet.archive_actioned_memos` — receiver-side supersession pair (C3 of
docs/plans/2026-08-11-receiver-side-supersession-pair-a-writab.md).

Re-runs the plan's Problem-section probe (three fixture memos through
`_is_terminal`, superseded-pair/actioned-control/actioned-no-disposition) as
AC4's own test, plus a parallel `superseded-no-disposition` fail-closed-to-keep
case and a `_DISPOSITION_FIELDS`/`_has_disposition` regression for
`superseded_by`. Fixtures are tmp-dir files, never hand-edited real memos.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.archive_actioned_memos import (
    _DISPOSITION_FIELDS,
    _REASON_ACTIONED_NO_DISPOSITION,
    _REASON_SUPERSEDED_NO_DISPOSITION,
    _has_disposition,
    _is_terminal,
)


def _run(coro):
    """Run an async coroutine synchronously — house convention (pytest-asyncio
    is deliberately absent tree-wide, see pyproject.toml)."""
    return asyncio.run(coro)


def _write_memo(path: Path, fm_body: str) -> Path:
    path.write_text(f"---\n{fm_body}---\n\nBody.\n", encoding="utf-8")
    return path


@pytest.fixture
def common_dir(tmp_path: Path) -> Path:
    """A `.git`-shaped dir with no memo-claims subtree — every memo in this
    file's tests is claim-free, so `_is_terminal`'s liveness check never finds
    a claim dir and always falls through to the terminal return."""
    d = tmp_path / ".git"
    d.mkdir()
    return d


class TestIsTerminalSupersededStatus:
    """AC4 — the plan's Problem-section probe, re-run as the acceptance test."""

    def test_c3_probe_three_row_result(self, tmp_path: Path, common_dir: Path):
        superseded_pair = _write_memo(
            tmp_path / "superseded-pair.md",
            "status: superseded\nsuperseded_by: some-other-memo.md\n",
        )
        actioned_control = _write_memo(
            tmp_path / "actioned-control.md",
            "status: actioned\ndecision: accepted\n",
        )
        actioned_no_disposition = _write_memo(
            tmp_path / "actioned-no-disposition.md",
            "status: actioned\n",
        )

        sp_terminal, sp_reason = _run(_is_terminal(superseded_pair, common_dir))
        ac_terminal, ac_reason = _run(_is_terminal(actioned_control, common_dir))
        and_terminal, and_reason = _run(_is_terminal(actioned_no_disposition, common_dir))

        # superseded-pair flips False -> True (this plan's whole point).
        assert sp_terminal is True
        assert sp_reason == "actioned; no live claim"

        # Control rows unchanged.
        assert ac_terminal is True
        assert ac_reason == "actioned; no live claim"
        assert and_terminal is False
        assert and_reason == _REASON_ACTIONED_NO_DISPOSITION

    def test_superseded_no_disposition_stays_fail_closed_to_keep(
        self, tmp_path: Path, common_dir: Path
    ):
        """Sibling of actioned-no-disposition: a hand-written `status:
        superseded` with no superseded_by (or other disposition field) is NOT
        terminal, and reports the distinct third reason string — the
        blur this predicate's docstring says must not happen."""
        memo = _write_memo(tmp_path / "superseded-no-disposition.md", "status: superseded\n")
        is_terminal, reason = _run(_is_terminal(memo, common_dir))
        assert is_terminal is False
        assert reason == _REASON_SUPERSEDED_NO_DISPOSITION
        assert reason != _REASON_ACTIONED_NO_DISPOSITION

    def test_open_status_still_not_terminal(self, tmp_path: Path, common_dir: Path):
        """Regression: an ordinary open memo is unaffected by the widened
        status set."""
        memo = _write_memo(tmp_path / "open.md", "status: open\n")
        is_terminal, reason = _run(_is_terminal(memo, common_dir))
        assert is_terminal is False
        assert reason == "status='open' (not actioned)"


class TestHasDispositionSupersededBy:
    """superseded_by is recognised as a disposition field (_DISPOSITION_FIELDS)."""

    def test_superseded_by_in_disposition_fields(self):
        assert "superseded_by" in _DISPOSITION_FIELDS

    def test_has_disposition_true_for_superseded_by_only(self, tmp_path: Path):
        memo = _write_memo(
            tmp_path / "m.md", "status: superseded\nsuperseded_by: other.md\n"
        )
        assert _has_disposition(memo) is True

    def test_has_disposition_false_with_no_fields(self, tmp_path: Path):
        memo = _write_memo(tmp_path / "m.md", "status: superseded\n")
        assert _has_disposition(memo) is False
