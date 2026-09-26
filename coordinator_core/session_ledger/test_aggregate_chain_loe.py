"""
coordinator_core.session_ledger.test_aggregate_chain_loe — CLI-entry-point
tests for aggregate_chain_loe.main(), the in-process entry point consumed by
the DoE-side CLI trampoline (coordinator/bin/aggregate-chain-loe.py).

The chain-walk/aggregate/format logic itself (aggregate(), parse_session_ledgers(),
resolve_handoff_path(), format_yaml_frontmatter/format_json) is covered
byte-for-byte against the retired bash oracle by the DoE-side test suite
(14 cases, run via the trampoline in-process). This file covers only
main()'s own CLI-parsing / exit-code / help-text surface, added for the
DOE-PORT trampoline.

Spec backlink: docs/plans/2026-06-29-handoff-lineage-dag-fan-in-fan-out.md § C2
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.session import harness_registry as hr
from coordinator_core.session_ledger.aggregate_chain_loe import main, resolve_state_root
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write_handoff(path: Path, created: str = "2026-05-05", predecessor: str = "null") -> None:
    path.write_text(
        f"""---
created: {created}
predecessor: {predecessor}
---

# Handoff

## Session Ledger

| Field | Value |
|-------|-------|
| session_id | sid-{path.stem} |
| agent_dispatches | 3 |
| opus_dispatches | 1 |
| em_tokens | 1000 |
""",
        encoding="utf-8",
    )


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / "archive" / "handoffs").mkdir(parents=True)
    (tmp_path / "coordinator" / "lib").mkdir(parents=True)
    return tmp_path


def test_help_exits_zero_and_prints_usage(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage: aggregate-chain-loe.sh --terminal-handoff <path>" in out
    assert "Exit codes:" in out


def test_missing_terminal_handoff_exits_one(capsys):
    rc = main([])
    assert rc == 1
    assert "Error: --terminal-handoff is required" in capsys.readouterr().err


def test_unknown_argument_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    assert "Error: unknown argument: --bogus" in capsys.readouterr().err


def test_not_inside_git_repo_exits_one(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/x.md"])
    assert rc == 1
    assert "not inside a git repo" in capsys.readouterr().err


def test_terminal_handoff_not_found_exits_one(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/missing.md"])
    assert rc == 1
    assert "terminal handoff not found" in capsys.readouterr().err


def test_single_session_chain_yaml_output(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "chain_loe:" in out
    assert "sessions: 1" in out
    assert "agent_dispatches: 3" in out


def test_single_session_chain_json_output(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md", "--format", "json"])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"chain_loe"' in out
    assert '"sessions": 1' in out


def test_unknown_format_exits_one_after_walk(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    h = tmp_path / "state" / "handoffs" / "term.md"
    _write_handoff(h)
    monkeypatch.chdir(tmp_path)
    rc = main(["--terminal-handoff", "state/handoffs/term.md", "--format", "xml"])
    assert rc == 1
    assert "unknown format 'xml'" in capsys.readouterr().err


def test_main_defaults_to_sys_argv(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["aggregate-chain-loe", "--help"])
    rc = main()
    assert rc == 0
    assert "Usage:" in capsys.readouterr().out


def test_resolve_state_root_is_scoped_to_passed_cwd_not_ambient_cwd(tmp_path, monkeypatch):
    repo_a = tmp_path / "repo_a"
    repo_b = tmp_path / "repo_b"
    repo_a.mkdir()
    repo_b.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo_a, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "init", "-q"], cwd=repo_b, check=True, **no_console_passthrough_kwargs())

    monkeypatch.chdir(repo_a)
    result = resolve_state_root(Path("unused"), repo_b)

    assert result == repo_b.resolve() / "state"
    assert result != repo_a.resolve() / "state"


# AC6: a REAL anchor/root divergence, constructed via CLAUDE_CONFIG_DIR +
# CLAUDE_PID overrides and a real registry file on disk — never by


def _epoch_to_filetime_ticks(epoch: float) -> int:
    return int((epoch + hr._FILETIME_EPOCH_OFFSET_SEC) * hr._FILETIME_TICKS_PER_SEC)


def _write_registry_record(sessions_dir, filename, session_id, pid, cwd, epoch=None):
    sessions_dir.mkdir(parents=True, exist_ok=True)
    if epoch is None:
        epoch = time.time() - 60
    payload = {
        "sessionId": session_id,
        "pid": pid,
        "procStart": _epoch_to_filetime_ticks(epoch),
        "cwd": str(cwd),
    }
    (sessions_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    return epoch


def test_main_refuses_on_real_repo_identity_mismatch(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    foreign_root = tmp_path / "foreign-repo"
    foreign_root.mkdir(parents=True)
    (foreign_root / ".git").mkdir()

    config_dir = tmp_path / "claude-config"
    sessions_dir = config_dir / "sessions"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_PID", "4242")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-mismatch")
    _write_registry_record(sessions_dir, "4242.json", "sess-mismatch", 4242, foreign_root)
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: ((4242, 0.0), "env-hit"),
    )
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "repo-identity" in err
    assert "MISMATCH" in err
    assert "sess-mismatch" in err


def test_main_does_not_refuse_on_repo_identity_match(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    config_dir = tmp_path / "claude-config"
    sessions_dir = config_dir / "sessions"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_PID", "5252")
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-match")
    _write_registry_record(sessions_dir, "5252.json", "sess-match", 5252, repo_root)
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: ((5252, 0.0), "env-hit"),
    )
    monkeypatch.setattr(
        "coordinator_core.pickup_assemble._session_core.stable_pid_alive",
        lambda pid, stored_start_epoch="": True,
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gates:" in out
    assert 'repo_identity: "MATCH"' in out


# check: does the EXISTING chain-walk (aggregate_chain_loe._EDGE_KINDS =
# Ledger row (the grammar this module's own comment above _ONELINE_RE
# declares): "YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary>".


def _write_fan_in_fixture(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"

    (handoffs / "A.md").write_text(
        """---
created: 2026-08-17
predecessor: none
---

# Handoff A (root)

## Session Ledger

2026-08-17 | 444444 | S | 4d / 2o | root session A
""",
        encoding="utf-8",
    )

    (handoffs / "P1.md").write_text(
        """---
created: 2026-08-18
predecessor: A.md
---

# Handoff P1

## Session Ledger

2026-08-18 | 222222 | S | 2d / 0o | parent session P1
""",
        encoding="utf-8",
    )

    (handoffs / "P2.md").write_text(
        """---
created: 2026-08-18
predecessor: A.md
---

# Handoff P2

## Session Ledger

2026-08-18 | 333333 | S | 3d / 1o | parent session P2
""",
        encoding="utf-8",
    )

    (handoffs / "S.md").write_text(
        """---
created: 2026-08-19
predecessor: none
additional_predecessors: [P1.md, P2.md]
---

# Handoff S (fan-in successor, terminal)

## Session Ledger

2026-08-19 | 111111 | S | 1d / 0o | successor session S
""",
        encoding="utf-8",
    )

    return tmp_path


def test_fan_in_successor_aggregate_dedups_diamond_ancestor_no_double_count(monkeypatch, tmp_path):
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _write_fan_in_fixture(tmp_path)
    monkeypatch.chdir(repo_root)

    result = aggregate(
        terminal_handoff="state/handoffs/S.md",
        repo_root=repo_root,
        handoffs_dir=repo_root / "state" / "handoffs",
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["chain_total"] == 4
    assert result["agent_dispatches"] == 10
    assert result["opus_dispatches"] == 3


def test_fan_in_successor_aggregate_reaches_parents_not_zero_rendered(monkeypatch, tmp_path):
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _write_fan_in_fixture(tmp_path)
    monkeypatch.chdir(repo_root)

    result = aggregate(
        terminal_handoff="state/handoffs/S.md",
        repo_root=repo_root,
        handoffs_dir=repo_root / "state" / "handoffs",
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 10
    assert result["agent_dispatches"] != 1
    assert result["opus_dispatches"] == 3


def test_walk_forward_predecessor_only_would_zero_render_the_fan_in_parents(tmp_path):
    """Direct demonstration of the failure mode the two tests above guard
    against: walk_forward restricted to edge_kinds={'predecessor'} alone
    (i.e. NOT following additional_predecessors, as aggregate_chain_loe's
    real _EDGE_KINDS does) never reaches P1/P2/A from S, because S's own
    `predecessor` is `none`. This is NOT a call into aggregate_chain_loe --
    it directly exercises dag.walk_forward with a deliberately-wrong
    edge-kind set, to show what the quieter zero-render failure would look
    like if _EDGE_KINDS ever regressed to `{'predecessor'}` alone."""
    from coordinator_core.dag import walk_forward

    repo_root = _write_fan_in_fixture(tmp_path)
    s_path = repo_root / "state" / "handoffs" / "S.md"

    walk = walk_forward(str(s_path), edge_kinds={"predecessor"})

    assert walk["orderedPaths"] == [str(s_path.resolve())] or [
        Path(p).name for p in walk["orderedPaths"]
    ] == ["S.md"]


def test_main_no_registry_record_is_unresolved_never_refuses(tmp_path, monkeypatch, capsys):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_root = _init_repo(repo_root)
    h = repo_root / "state" / "handoffs" / "term.md"
    _write_handoff(h)

    config_dir = tmp_path / "claude-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-unresolved")
    monkeypatch.setattr(
        "coordinator_core.session.core._resolve_claude_pid_from_env",
        lambda: (None, "env-miss:absent"),
    )

    monkeypatch.chdir(repo_root)
    rc = main(["--terminal-handoff", "state/handoffs/term.md"])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'repo_identity: "UNRESOLVED"' in out


def _write_ledgerless_handoff(path: Path, created: str, predecessor: str) -> None:
    path.write_text(
        f"""---
created: {created}
predecessor: {predecessor}
---

# Handoff

No Session Ledger block.
""",
        encoding="utf-8",
    )


def test_aggregate_nulls_out_for_a_fully_degenerate_multi_handoff_chain(monkeypatch, tmp_path):
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _init_repo(tmp_path)
    handoffs = repo_root / "state" / "handoffs"
    root = handoffs / "root.md"
    term = handoffs / "term.md"
    _write_ledgerless_handoff(root, created="2026-08-18", predecessor="null")
    _write_ledgerless_handoff(term, created="2026-08-19", predecessor="root.md")

    result = aggregate(
        terminal_handoff=str(term),
        repo_root=repo_root,
        handoffs_dir=handoffs,
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["chain_total"] == 2
    assert result["agent_dispatches"] is None
    assert result["opus_dispatches"] is None
    assert result["tshirt"] is None


def test_aggregate_keeps_numbers_when_any_handoff_attributes_something(monkeypatch, tmp_path):
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _init_repo(tmp_path)
    handoffs = repo_root / "state" / "handoffs"
    root = handoffs / "root.md"
    term = handoffs / "term.md"
    root.write_text(
        """---
created: 2026-08-18
predecessor: null
---

## Session Ledger

2026-08-18 | 111111 | S | 2d / 0o | root session
""",
        encoding="utf-8",
    )
    _write_ledgerless_handoff(term, created="2026-08-19", predecessor="root.md")

    result = aggregate(
        terminal_handoff=str(term),
        repo_root=repo_root,
        handoffs_dir=handoffs,
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 2
    assert result["opus_dispatches"] == 0
    assert result["opus_dispatches"] is not None
    assert result["tshirt"] is not None


def test_aggregate_keeps_real_zero_when_closing_session_supplies_it(monkeypatch, tmp_path):
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _init_repo(tmp_path)
    handoffs = repo_root / "state" / "handoffs"
    root = handoffs / "root.md"
    term = handoffs / "term.md"
    _write_ledgerless_handoff(root, created="2026-08-18", predecessor="null")
    _write_ledgerless_handoff(term, created="2026-08-19", predecessor="root.md")

    result = aggregate(
        terminal_handoff=str(term),
        repo_root=repo_root,
        handoffs_dir=handoffs,
        archive_dir=repo_root / "archive" / "handoffs",
        closing_session={
            "session_id": "closing-sess-000000",
            "agent_dispatches": 0,
            "opus_dispatches": 0,
        },
    )

    assert result["exit_code"] == 0
    assert result["chain_total"] == 2
    assert result["agent_dispatches"] == 0
    assert result["agent_dispatches"] is not None
    assert result["opus_dispatches"] == 0
    assert result["opus_dispatches"] is not None
    assert result["tshirt"] is not None


def _minimal_result(**overrides) -> dict:
    base = {
        "chain_total": 2,
        "chain_session_total": 2,
        "agent_dispatches": None,
        "opus_dispatches": None,
        "em_tokens": None,
        "tshirt": None,
        "commits": [],
        "chain_sessions_with_ledger": "0 of 2",
        "chain_sessions_with_dispatch_fallback": "0 of 2",
        "chain_span_days": 1,
        "chain_starting_handoff": "state/handoffs/root.md",
        "chain_walk_terminated_early": "",
    }
    base.update(overrides)
    return base


def test_format_yaml_frontmatter_renders_null_not_the_string_none():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_yaml_frontmatter

    out = format_yaml_frontmatter(_minimal_result())
    assert "  agent_dispatches: null" in out
    assert "  opus_dispatches: null" in out
    assert "  tshirt: null" in out
    assert "None" not in out


def test_format_json_renders_json_null_for_degenerate_result():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_json

    out = format_json(_minimal_result())
    obj = json.loads(out)
    assert obj["chain_loe"]["agent_dispatches"] is None
    assert obj["chain_loe"]["opus_dispatches"] is None
    assert obj["chain_loe"]["tshirt"] is None


def test_format_yaml_frontmatter_still_renders_numbers_when_attributed():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_yaml_frontmatter

    out = format_yaml_frontmatter(
        _minimal_result(
            agent_dispatches=5, opus_dispatches=1, tshirt="S", chain_sessions_with_ledger="2 of 2"
        )
    )
    assert "  agent_dispatches: 5" in out
    assert "  opus_dispatches: 1" in out
    assert '  tshirt: "S"' in out


def test_format_yaml_frontmatter_renders_a_real_attributed_zero_opus_dispatches():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_yaml_frontmatter

    out = format_yaml_frontmatter(
        _minimal_result(
            agent_dispatches=5, opus_dispatches=0, tshirt="S", chain_sessions_with_ledger="2 of 2"
        )
    )
    assert "  opus_dispatches: 0" in out
    assert "  opus_dispatches: null" not in out


def test_format_json_still_renders_numbers_when_attributed():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_json

    out = format_json(
        _minimal_result(
            agent_dispatches=5, opus_dispatches=1, tshirt="S", chain_sessions_with_ledger="2 of 2"
        )
    )
    obj = json.loads(out)
    assert obj["chain_loe"]["agent_dispatches"] == 5
    assert obj["chain_loe"]["opus_dispatches"] == 1
    assert obj["chain_loe"]["tshirt"] == "S"


def test_format_oneline_row_abbreviates_session_id_leading_six():
    from coordinator_core.session_ledger.aggregate_chain_loe import format_oneline_row

    row = format_oneline_row(
        "2026-08-14",
        "a0df95e6-7369-4211-9ebd-c1eaa8466848",
        "S",
        3,
        1,
        "did a thing",
    )
    assert row == "2026-08-14 | a0df95 | S | 3d / 1o | did a thing"


# B15b regression fixture — vendored inline from DoE-claude
# `coordinator/tests/fixtures/session-ledger-two-ceremony.md` (DoE-claude
# @a037746a, read at authoring for @b4e1b034 per the plan's
# `external_reads_ungated` entry). Copied here as literal row text rather
# than read from that sibling tree at run time, per the plan's anti-scope
# ("do not make a test read DoE-claude's tree at run time").
_TWO_CEREMONY_ROWS = (
    "2026-05-21 | 7bffa2 | XS | 0d / 0o | Reconciled the pending list "
    "(pickup-reconciliation close)\n"
    "2026-05-21 | 7bffa2 | L | 26d / 4o | Executed plan + mandatory "
    "four-slice partitioned review (workstream-complete close)\n"
)


def test_two_ceremony_session_aggregates_both_rows_not_just_the_first(monkeypatch, tmp_path):
    """The defect this row fixes: the old sid-only dedup collapsed a
    same-session two-ceremony chain to its FIRST row (the XS 0d/0o pickup
    close), losing the second row's L 26d/4o `/workstream-complete` close
    entirely. The row-identity fix (session_id, summary) counts both."""
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _init_repo(tmp_path)
    handoffs = repo_root / "state" / "handoffs"
    term = handoffs / "term.md"
    term.write_text(
        f"""---
created: 2026-05-21
predecessor: null
---

## Session Ledger

{_TWO_CEREMONY_ROWS}""",
        encoding="utf-8",
    )

    result = aggregate(
        terminal_handoff=str(term),
        repo_root=repo_root,
        handoffs_dir=handoffs,
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["agent_dispatches"] == 26
    assert result["opus_dispatches"] == 4
    assert result["tshirt"] is not None


def test_two_ceremony_row_repeated_across_chain_batons_counts_once(monkeypatch, tmp_path):
    """A byte-identical row (same session_id, same summary) copied onto a
    second baton in the chain must still count once — the row-identity fix
    dedups on (session_id, summary), not merely "any row seen for this sid"."""
    from coordinator_core.session_ledger.aggregate_chain_loe import aggregate

    repo_root = _init_repo(tmp_path)
    handoffs = repo_root / "state" / "handoffs"
    root = handoffs / "root.md"
    term = handoffs / "term.md"
    root.write_text(
        f"""---
created: 2026-05-21
predecessor: null
---

## Session Ledger

{_TWO_CEREMONY_ROWS}""",
        encoding="utf-8",
    )
    term.write_text(
        f"""---
created: 2026-05-21
predecessor: root.md
---

## Session Ledger

{_TWO_CEREMONY_ROWS}""",
        encoding="utf-8",
    )

    result = aggregate(
        terminal_handoff=str(term),
        repo_root=repo_root,
        handoffs_dir=handoffs,
        archive_dir=repo_root / "archive" / "handoffs",
    )

    assert result["exit_code"] == 0
    assert result["chain_total"] == 2
    # Both ceremony rows counted once each, not twice (root and term carry
    # byte-identical copies of the same two rows).
    assert result["agent_dispatches"] == 26
    assert result["opus_dispatches"] == 4
