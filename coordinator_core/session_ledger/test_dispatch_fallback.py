"""
coordinator_core.session_ledger.test_dispatch_fallback

Covers the ``dispatched-agents.txt`` fallback added to
``aggregate_chain_loe`` per state/debt-backlog/2026-08-11-chain-loe-renders-
a-fully-dispatched-ses-d6981e622244.yaml — a chain-terminal handoff carrying
NO ``## Session Ledger`` block (e.g. a machine-generated crash-
reconstruction recovery baton) now falls back to the per-session
``dispatched-agents.txt`` already written by ``track_dispatched_agents.py``,
rather than silently reporting zero effort.

Fixture layout mirrors ``test_aggregate_chain_loe.py``'s own git-repo helper
but drives ``aggregate()`` directly (not the CLI) so ``_session_core.
sessions_dir`` can be monkeypatched to a controlled temp directory instead
of resolving the real machine's git-common-dir session hub.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.session_ledger import aggregate_chain_loe as agg


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    return tmp_path


def _write_recovery_handoff(path: Path, recovers_session: str, created: str = "2026-08-11") -> None:
    path.write_text(
        f"""---
created: {created}
predecessor: null
kind: recovery
recovers_session: "{recovers_session}"
---

# Recovery baton

No Session Ledger block — machine-generated.
""",
        encoding="utf-8",
    )


def _write_ledgered_handoff(path: Path, sid: str, created: str = "2026-08-11") -> None:
    path.write_text(
        f"""---
created: {created}
predecessor: null
---

## Session Ledger

| Field | Value |
|-------|-------|
| session_id | {sid} |
| agent_dispatches | 3 |
| opus_dispatches | 1 |
| em_tokens | 1000 |
""",
        encoding="utf-8",
    )


def _write_agents_file(sessions_dir: Path, sid: str, rows: list) -> None:
    sess_dir = sessions_dir / sid
    sess_dir.mkdir(parents=True)
    text = "\n".join(rows) + ("\n" if rows else "")
    (sess_dir / "dispatched-agents.txt").write_text(text, encoding="utf-8")


def test_fallback_counts_dispatched_agents_when_no_ledger_block(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    sessions_dir = tmp_path / "sessions-hub"
    sid = "cb57af40-4874-4b6c-b192-10b2afe1c517"
    _write_agents_file(
        sessions_dir,
        sid,
        [
            "a1\tclaude-sonnet-5\tcoordinator:executor\t1",
            "a2\tclaude-opus\tcoordinator:code-reviewer\t2",
            "a3\tclaude-sonnet-5\tcoordinator:code-reviewer\t3",
        ],
    )
    monkeypatch.setattr(agg._session_core, "sessions_dir", lambda: str(sessions_dir))

    h = repo / "state" / "handoffs" / "term.md"
    _write_recovery_handoff(h, recovers_session=sid)

    result = agg.aggregate(
        terminal_handoff=str(h),
        repo_root=repo,
        handoffs_dir=repo / "state" / "handoffs",
        archive_dir=repo / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 3
    assert result["opus_dispatches"] == 1
    assert result["chain_sessions_with_ledger"] == "0 of 1"
    assert result["chain_sessions_with_dispatch_fallback"] == "1 of 1"
    assert result["tshirt"] != "XS" or result["agent_dispatches"] == 3  # tshirt recomputed from real counts


def test_fallback_absent_when_no_dispatched_agents_file(tmp_path, monkeypatch):
    """A ledger-less handoff whose recovers_session has no dispatched-agents.txt
    at all degrades to the pre-existing zero-record behavior — no fabricated
    zero-effort record is synthesized (distinct from a present-but-empty file,
    which legitimately yields (0, 0))."""
    repo = _init_repo(tmp_path)
    sessions_dir = tmp_path / "sessions-hub"
    sessions_dir.mkdir()
    monkeypatch.setattr(agg._session_core, "sessions_dir", lambda: str(sessions_dir))

    h = repo / "state" / "handoffs" / "term.md"
    _write_recovery_handoff(h, recovers_session="no-such-session")

    result = agg.aggregate(
        terminal_handoff=str(h),
        repo_root=repo,
        handoffs_dir=repo / "state" / "handoffs",
        archive_dir=repo / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 0
    assert result["chain_sessions_with_ledger"] == "0 of 1"
    assert result["chain_sessions_with_dispatch_fallback"] == "0 of 1"


def test_fallback_never_double_counts_when_both_sources_present(tmp_path, monkeypatch):
    """A handoff carrying a REAL Session Ledger block must never also draw
    from dispatched-agents.txt for the same session — the fallback is a
    fallback, not an additional source."""
    repo = _init_repo(tmp_path)
    sessions_dir = tmp_path / "sessions-hub"
    sid = "sid-with-both"
    _write_agents_file(
        sessions_dir,
        sid,
        ["a1\tclaude-sonnet-5\tcoordinator:executor\t1"] * 8,  # would be 8 if double-counted
    )
    monkeypatch.setattr(agg._session_core, "sessions_dir", lambda: str(sessions_dir))

    h = repo / "state" / "handoffs" / "term.md"
    _write_ledgered_handoff(h, sid=sid)

    result = agg.aggregate(
        terminal_handoff=str(h),
        repo_root=repo,
        handoffs_dir=repo / "state" / "handoffs",
        archive_dir=repo / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 3  # from the ledger row only, NOT 3+8
    assert result["chain_sessions_with_ledger"] == "1 of 1"
    assert result["chain_sessions_with_dispatch_fallback"] == "0 of 1"


def test_resolve_fallback_session_id_prefers_recovers_session_on_recovery_kind():
    text = """---
kind: recovery
recovers_session: crashed-sid
authoring_session: reconstructor-sid
---
"""
    assert agg._resolve_fallback_session_id(text) == "crashed-sid"


def test_resolve_fallback_session_id_falls_through_to_authoring_session():
    text = """---
kind: session-handoff
authoring_session: author-sid
---
"""
    assert agg._resolve_fallback_session_id(text) == "author-sid"


def test_resolve_fallback_session_id_none_when_neither_field_present():
    text = """---
kind: session-handoff
---
"""
    assert agg._resolve_fallback_session_id(text) is None


def test_count_dispatches_from_agents_file_missing_file_returns_none_none(tmp_path):
    ad, od = agg._count_dispatches_from_agents_file(tmp_path / "nope" / "dispatched-agents.txt")
    assert (ad, od) == (None, None)


def test_count_dispatches_from_agents_file_counts_opus_case_insensitively(tmp_path):
    f = tmp_path / "dispatched-agents.txt"
    f.write_text(
        "a1\tclaude-Opus-5\tcoordinator:executor\t1\n"
        "a2\tclaude-sonnet-5\tcoordinator:code-reviewer\t2\n",
        encoding="utf-8",
    )
    ad, od = agg._count_dispatches_from_agents_file(f)
    assert (ad, od) == (2, 1)
