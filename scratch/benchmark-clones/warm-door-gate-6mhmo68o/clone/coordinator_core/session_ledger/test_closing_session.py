"""
coordinator_core.session_ledger.test_closing_session

Covers the ``closing_session`` attribution added to ``aggregate_chain_loe``
per cross-repo/archive/2026-08-11-example-market-data-repo-em-chain-loe-ledger-
ordering-and-defeated-tell.md — the chain-terminal session heads no handoff
and appends its ``## Session Ledger`` row AFTER the completion-entry scaffold
calls this aggregator, so summing handoff rows alone always undercounts by
exactly that session's contribution, and the ``N of M`` tell that was supposed
to reveal it read ``"1 of 1"`` on a single-handoff chain because both halves
counted handoffs.

Fixture layout mirrors ``test_dispatch_fallback.py``.
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.session_ledger import aggregate_chain_loe as agg

_PRED_SID = "11111111-1111-4111-8111-111111111111"
_CLOSING_SID = "22222222-2222-4222-8222-222222222222"


def _init_repo(tmp_path: Path) -> Path:
    """Directory skeleton only — `aggregate()` takes every root explicitly and
    `dag.walk_forward` infers its own from the handoff path, so no `git init`
    spawn is owed here (unlike the CLI leg's `resolve_repo_root`)."""
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    return tmp_path


def _ledger_row(sid: str, ad: int, od: int) -> str:
    return f"""
## Session Ledger

| Field | Value |
|-------|-------|
| session_id | {sid} |
| agent_dispatches | {ad} |
| opus_dispatches | {od} |
| em_tokens | null |
"""


def _write_handoff(path: Path, body: str) -> None:
    path.write_text(
        "---\ncreated: 2026-08-11\npredecessor: null\n---\n\n# Handoff\n" + body,
        encoding="utf-8",
    )


def _aggregate(repo: Path, handoff: Path, closing=None):
    return agg.aggregate(
        terminal_handoff=str(handoff),
        repo_root=repo,
        handoffs_dir=repo / "state" / "handoffs",
        archive_dir=repo / "archive" / "handoffs",
        closing_session=closing,
    )


def test_closing_session_row_absent_is_attributed_and_told(tmp_path):
    """The reported defect: 8 dispatches rendered as the predecessor's 0d/1o."""
    repo = _init_repo(tmp_path)
    h = repo / "state" / "handoffs" / "term.md"
    _write_handoff(h, _ledger_row(_PRED_SID, 0, 1))

    result = _aggregate(
        repo,
        h,
        closing={"session_id": _CLOSING_SID, "agent_dispatches": 8, "opus_dispatches": 0},
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 8
    assert result["opus_dispatches"] == 1
    # The tell fires: one session accounted for by a row, two owe one.
    assert result["chain_sessions_with_ledger"] == "1 of 2"
    assert result["chain_session_total"] == 2


def test_closing_session_row_present_is_not_double_counted(tmp_path):
    """Idempotence: once the row lands, the caller-supplied tally is inert."""
    repo = _init_repo(tmp_path)
    h = repo / "state" / "handoffs" / "term.md"
    _write_handoff(h, _ledger_row(_PRED_SID, 0, 1) + _ledger_row(_CLOSING_SID, 8, 0))

    result = _aggregate(
        repo,
        h,
        closing={"session_id": _CLOSING_SID, "agent_dispatches": 8, "opus_dispatches": 0},
    )

    assert result["agent_dispatches"] == 8
    assert result["opus_dispatches"] == 1
    assert result["chain_sessions_with_ledger"] == "2 of 2"


def test_closing_session_matches_the_abbreviated_oneline_sid(tmp_path):
    """The LIVE grammar abbreviates the sid; a raw equality dedup would double-count."""
    repo = _init_repo(tmp_path)
    h = repo / "state" / "handoffs" / "term.md"
    _write_handoff(
        h,
        "\n## Session Ledger\n\n"
        f"2026-08-11 | {_PRED_SID[-6:]} | XS | 0d / 1o | predecessor close\n"
        f"2026-08-11 | {_CLOSING_SID[-6:]} | M | 8d / 0o | closing session\n",
    )

    result = _aggregate(
        repo,
        h,
        closing={"session_id": _CLOSING_SID, "agent_dispatches": 8, "opus_dispatches": 0},
    )

    assert result["agent_dispatches"] == 8, "closing tally attributed twice"
    assert result["opus_dispatches"] == 1
    assert result["chain_sessions_with_ledger"] == "2 of 2"


def test_closing_session_without_counts_still_fires_the_tell(tmp_path):
    """An unresolvable dispatched-agents.txt costs the attribution, not the tell."""
    repo = _init_repo(tmp_path)
    h = repo / "state" / "handoffs" / "term.md"
    _write_handoff(h, _ledger_row(_PRED_SID, 4, 2))

    result = _aggregate(repo, h, closing={"session_id": _CLOSING_SID})

    assert result["agent_dispatches"] == 4
    assert result["chain_sessions_with_ledger"] == "1 of 2"


def test_no_closing_session_preserves_handoff_based_output(tmp_path):
    """Negative-spec: the standalone leg stays byte-identical to the oracle."""
    repo = _init_repo(tmp_path)
    h = repo / "state" / "handoffs" / "term.md"
    _write_handoff(h, _ledger_row(_PRED_SID, 4, 2))

    result = _aggregate(repo, h)

    assert result["chain_sessions_with_ledger"] == "1 of 1"
    assert result["chain_session_total"] == result["chain_total"] == 1
    assert "sessions: 1" in agg.format_yaml_frontmatter(result)
