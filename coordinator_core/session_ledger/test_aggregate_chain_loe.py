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

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
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
    """Review: code-reviewer (F1) — resolve_state_root(coordinator_root, cwd)
    must resolve against *cwd*, not the process's ambient os.getcwd(). Two
    distinct (non-meta) repos: chdir the process into repo_a, then resolve
    against repo_b explicitly — the result must be scoped to repo_b."""
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


# ---------------------------------------------------------------------------
# C4 (docs/plans/2026-08-11-ceremony-closes-against-a-foreign-repo.md) —
# main()'s wiring of coordinator_core.pickup_assemble.compute_repo_identity_gate.
# AC6: a REAL anchor/root divergence, constructed via CLAUDE_CONFIG_DIR +
# CLAUDE_PID overrides and a real registry file on disk — never by
# monkeypatching compute_repo_identity_gate's own return value. Reuses the
# fixture-construction pattern from
# coordinator_core/pickup_assemble/tests/test_repo_identity_gate.py (the one
# leg monkeypatched there, too, is `_resolve_claude_pid_from_env`'s
# psutil-name-match check — inherently OS-process-identity bound and
# unconstructible as a real fixture inside a test process not named
# "claude"; every other input is real files on disk).
# ---------------------------------------------------------------------------


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
    # The registry's own real record names foreign_root as the session's
    # anchor cwd — a real divergence from repo_root, the ceremony's
    # `--terminal-handoff`-resolved root below.
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
    # The registry's real record anchors the session inside repo_root itself
    # — a genuine, on-disk MATCH.
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


# ---------------------------------------------------------------------------
# C2 (docs/plans/2026-08-19-batons-unify-into-one-successor.md) — premise
# check: does the EXISTING chain-walk (aggregate_chain_loe._EDGE_KINDS =
# {"predecessor", "additional_predecessors"}, walked via dag.walk_forward)
# already handle a two-parent (fan-in) merged successor without double-
# counting a common ancestor reached by both parents, and without
# zero-rendering the parents entirely because the successor's own
# `predecessor` field is absent?
#
# Fixture (hand-built frontmatter, NOT generated by any unification code
# path — a diamond fan-in):
#
#     S (terminal, predecessor: none, additional_predecessors: [P1.md, P2.md])
#     |-- P1 (predecessor: A.md)
#     |-- P2 (predecessor: A.md)
#     `-- both P1 and P2 converge on A (predecessor: none) -- the diamond
#
# Every node carries exactly one hand-written one-line-append Session
# Ledger row (the grammar this module's own comment above _ONELINE_RE
# declares): "YYYY-MM-DD | <sid6> | <tshirt> | <Nd / No> | <summary>".
# ---------------------------------------------------------------------------


def _write_fan_in_fixture(tmp_path: Path) -> Path:
    """Build the diamond fan-in fixture by hand under tmp_path/state/handoffs.

    S --additional_predecessors--> {P1, P2} --predecessor--> A (common root).
    Returns tmp_path (the repo root), after ``_init_repo``.
    """
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
    """Double-count guard: A is reachable from S via BOTH P1 and P2 (the
    diamond). aggregate() must sum A's row exactly once — 1+2+3+4 = 10
    agent_dispatches / 0+0+1+2 = 3 opus_dispatches — never 14/5 (A counted
    twice, once per incoming path)."""
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
    # All four nodes (S, P1, P2, A) visited exactly once each -- not 5
    # (which double-counting A would produce).
    assert result["chain_total"] == 4
    assert result["agent_dispatches"] == 10
    assert result["opus_dispatches"] == 3


def test_fan_in_successor_aggregate_reaches_parents_not_zero_rendered(monkeypatch, tmp_path):
    """Zero-render guard: S's own `predecessor` field is `none` (absence is
    the fan-in successor's actual on-disk shape) -- both parents are ONLY
    reachable via `additional_predecessors`. If the walk only followed
    `predecessor`, P1/P2/A would be unreachable and the aggregate would
    reflect only S's own row (1d/0o), not the full chain (10d/3o). Assert
    the aggregate is non-zero-beyond-S and equals the hand-summed total --
    the quieter, more damaging failure mode per the plan chunk."""
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
    # S's own row alone would be 1d/0o -- the fixture proves the walk
    # actually reached P1/P2/A (additional_predecessors), not just S.
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


# ---------------------------------------------------------------------------
# AC4/AC5 (docs/plans/2026-08-20-the-ledger-check-follows-the-body-not-ju.md):
# a chain that attributed NO LoE from any of the three sources renders
# `agent_dispatches: null` / `tshirt: null` instead of a measured-looking
# `0`/`"XS"`. See aggregate()'s `attributed_nothing` comment for the exact
# discriminator and its `chain_total > 1` narrowing.
# ---------------------------------------------------------------------------


def _write_ledgerless_handoff(path: Path, created: str, predecessor: str) -> None:
    """A handoff with no `## Session Ledger` heading at all and no fields that
    would make it a dispatch-fallback candidate (no `kind: recovery`, no
    `authoring_session`) -- genuinely no source to attribute from."""
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
    """The genuine AC4 target: a two-handoff chain where NEITHER handoff
    carries a Session Ledger row, dispatch-fallback data, or a closing_session
    tally -- the chain attributed literally nothing, so `agent_dispatches`/
    `opus_dispatches`/`tshirt` must all render null together rather than the
    measured-looking 0/0/"XS"."""
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
    """Positive control (AC5): a two-handoff chain where ONE handoff carries a
    real ledger row must keep rendering real numbers, not null -- the fix
    must not blank out the common case. Also proves a genuinely-attributed
    `opus_dispatches: 0` (from the ledger row's own "0o") stays a real int,
    not null -- attributed-zero and unmeasured are different things."""
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
    """The one arm of `attributed_nothing` the reviewer flagged as untested
    (review-c2-findings.md §6): a multi-handoff chain with NO ledger rows and
    NO dispatch-fallback data anywhere, but a `closing_session` that supplies
    a genuine, on-disk-real `agent_dispatches: 0` / `opus_dispatches: 0`
    tally. This is the exact case where nulling the chain would silently
    erase a real (zero) measurement rather than correctly flag an unmeasured
    one -- `closing_row_missing` must fire and keep the result at real 0s,
    NOT null."""
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
    """A real (non-null) 0 -- e.g. an ad-only ledger row -- must render as the
    bare integer `0`, not the "null" sentinel: attributed-zero and unmeasured
    are different states and must not collapse to the same output."""
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
