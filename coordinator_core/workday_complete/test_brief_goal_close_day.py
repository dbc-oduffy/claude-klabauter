"""
coordinator_core.workday_complete.test_brief_goal_close_day — conformance
suite for the C4 day-goal close-out wiring into the `workday-complete-
assemble` computed-skill engine (`jp_day_goal_closeout` + `d_goal_close_day`).

Scope (docs/plans/2026-07-25-day-goal-close-out-lifecycle.md § C4): the
judgment point + its gated directive are emitted ONLY when C2's
`collect_open_day_goals` reports at least one open row (today or stale),
absent otherwise; the directive never fires on an unresolved judgment
point (halt contract, per `workday_complete.apply`); and
`workday-complete-apply` resolves the new `goal-close-day` CLI name through
`_CLI_DISPATCH` without raising `UnrecognizedDirective` (AC10).

Run scoped only:
    python3 -m pytest coordinator_core/workday_complete/test_brief_goal_close_day.py -q
Spec backlink: pln-day-scoped-goal-close-out-life-69a25c § C4
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path

from coordinator_core.workday_complete import apply as wc_apply
from coordinator_core.workday_complete import brief as wc_brief

_EMPTY = {"today": [], "stale": [], "unreadable_error": None}


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_real_repo(tmp_path: Path) -> Path:
    """A throwaway real git repo (tmp_path-based), mirroring
    `test_cockpit_contract_freshness._init_real_repo`'s pattern — used by the
    Finding-1-regression tests below to exercise the real
    `_resolve_repo_common_dir_for_ceremony` -> `main_worktree_root` ->
    `resolve_context` wiring end to end, rather than the
    `_compute_open_day_goals` monkeypatch every other test in this suite
    uses (Finding 2)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q"], cwd=repo)
    _run_git(["config", "user.email", "test@example.com"], cwd=repo)
    _run_git(["config", "user.name", "Test"], cwd=repo)
    (repo / "README.md").write_text("placeholder\n")
    _run_git(["add", "README.md"], cwd=repo)
    _run_git(["commit", "-q", "-m", "init"], cwd=repo)
    return repo.resolve()


def _rows(*, today: list[dict] | None = None, stale: list[dict] | None = None) -> dict:
    return {"today": today or [], "stale": stale or [], "unreadable_error": None}


def _row(goal_id: str, text: str = "some priority") -> dict:
    return {"goal_id": goal_id, "text": text}


def _stub_operator_config(monkeypatch) -> None:
    """`brief()` calls `resolve_operator_config` before anything else — a
    concern this suite is not exercising (that resolution machinery has its
    own conformance suite), and the autouse HOME-quarantine fixture
    (`coordinator_core/conftest.py`) means it fails to resolve a real
    settings-home/claude-klabauter-root/doe-root under a bare `tmp_path`. Stub it to a
    successful no-op, same posture `test_workstream_complete.py` uses for the
    unrelated `compute_session_shape_gate` seam."""
    monkeypatch.setattr(
        wc_brief,
        "resolve_operator_config",
        lambda env=None: {
            "settings_home": "",
            "claude_klabauter_bin": "",
            "claude_klabauter_root": "",
            "doe_root": "",
        },
    )


# ---------------------------------------------------------------------------
# jp_day_goal_closeout / d_goal_close_day presence — conditional on open rows
# ---------------------------------------------------------------------------


def test_judgment_point_and_directive_absent_when_no_open_day_rows(monkeypatch):
    _stub_operator_config(monkeypatch)
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: _EMPTY)
    exit_code, envelope = wc_brief.brief(decisions={})
    assert exit_code == 0
    jp_ids = {jp["id"] for jp in envelope["judgment_points"]}
    directive_ids = {d["id"] for d in envelope["directives"]}
    assert "jp_day_goal_closeout" not in jp_ids
    assert "d_goal_close_day" not in directive_ids


def test_judgment_point_and_directive_present_when_only_today_rows_open(monkeypatch):
    _stub_operator_config(monkeypatch)
    monkeypatch.setattr(
        wc_brief, "_compute_open_day_goals", lambda: _rows(today=[_row("today-1")])
    )
    exit_code, envelope = wc_brief.brief(decisions={})
    assert exit_code == 0
    jp_ids = {jp["id"] for jp in envelope["judgment_points"]}
    directive_ids = {d["id"] for d in envelope["directives"]}
    assert "jp_day_goal_closeout" in jp_ids
    assert "d_goal_close_day" in directive_ids


def test_judgment_point_and_directive_present_when_only_stale_rows_open(monkeypatch):
    _stub_operator_config(monkeypatch)
    monkeypatch.setattr(
        wc_brief, "_compute_open_day_goals", lambda: _rows(stale=[_row("stale-1")])
    )
    exit_code, envelope = wc_brief.brief(decisions={})
    assert exit_code == 0
    jp_ids = {jp["id"] for jp in envelope["judgment_points"]}
    directive_ids = {d["id"] for d in envelope["directives"]}
    assert "jp_day_goal_closeout" in jp_ids
    assert "d_goal_close_day" in directive_ids


def test_judgment_point_question_and_evidence_present_today_and_stale_distinctly(monkeypatch):
    _stub_operator_config(monkeypatch)
    open_day_goals = _rows(
        today=[_row("today-1", "ship the thing")],
        stale=[_row("stale-1", "an older priority")],
    )
    monkeypatch.setattr(wc_brief, "_compute_open_day_goals", lambda: open_day_goals)
    exit_code, envelope = wc_brief.brief(decisions={})
    assert exit_code == 0
    [jp] = [j for j in envelope["judgment_points"] if j["id"] == "jp_day_goal_closeout"]
    assert "1 day goal(s) due today" in jp["question"]
    assert "1 stale day goal(s)" in jp["question"]
    assert "today-1" in jp["evidence"]
    assert "stale-1" in jp["evidence"]
    assert jp["recommendation"] is None
    assert {d["value"] for d in jp["dispositions"]} == {"record", "skip"}


def test_directive_carries_depends_on_and_decisions_are_threaded_into_args(monkeypatch):
    _stub_operator_config(monkeypatch)
    monkeypatch.setattr(
        wc_brief, "_compute_open_day_goals", lambda: _rows(today=[_row("today-1")])
    )
    decisions = {"day_goal_closeout": {"today-1": "done"}}
    exit_code, envelope = wc_brief.brief(decisions=decisions)
    assert exit_code == 0
    [directive] = [d for d in envelope["directives"] if d["id"] == "d_goal_close_day"]
    assert directive["cli"] == "goal-close-day"
    assert directive["depends_on"] == "jp_day_goal_closeout"
    assert directive["args"] == ["--decisions", '{"today-1": "done"}']


# ---------------------------------------------------------------------------
# Halt contract — the directive never fires on an unresolved judgment point.
# ---------------------------------------------------------------------------


def test_directive_blocked_when_judgment_point_unresolved():
    jp = wc_brief._build_day_goal_closeout_judgment_point(_rows(today=[_row("today-1")]))
    directive = {
        "id": "d_goal_close_day",
        "cli": "goal-close-day",
        "args": [],
        "depends_on": "jp_day_goal_closeout",
        "already_satisfied": False,
    }
    jp_by_id = wc_apply._judgment_points_by_id([jp])
    assert wc_apply._directive_gate_open(directive, jp_by_id, {}) is False


def test_directive_fires_once_judgment_point_resolves_to_record():
    jp = wc_brief._build_day_goal_closeout_judgment_point(_rows(today=[_row("today-1")]))
    directive = {
        "id": "d_goal_close_day",
        "cli": "goal-close-day",
        "args": [],
        "depends_on": "jp_day_goal_closeout",
        "already_satisfied": False,
    }
    jp_by_id = wc_apply._judgment_points_by_id([jp])
    decisions = {"jp_day_goal_closeout": {"disposition": "record"}}
    assert wc_apply._directive_gate_open(directive, jp_by_id, decisions) is True


def test_directive_stays_blocked_when_judgment_point_resolves_to_skip():
    jp = wc_brief._build_day_goal_closeout_judgment_point(_rows(today=[_row("today-1")]))
    directive = {
        "id": "d_goal_close_day",
        "cli": "goal-close-day",
        "args": [],
        "depends_on": "jp_day_goal_closeout",
        "already_satisfied": False,
    }
    jp_by_id = wc_apply._judgment_points_by_id([jp])
    decisions = {"jp_day_goal_closeout": {"disposition": "skip"}}
    assert wc_apply._directive_gate_open(directive, jp_by_id, decisions) is False


# ---------------------------------------------------------------------------
# AC10 — workday-complete-apply resolves goal-close-day through _CLI_DISPATCH
# without raising UnrecognizedDirective.
# ---------------------------------------------------------------------------


def test_goal_close_day_is_a_consumes_manifest_member():
    assert "goal-close-day" in wc_brief.CONSUMES_MANIFEST


def test_apply_resolves_goal_close_day_cli_without_raising():
    resolved = wc_apply._resolve_cli("goal-close-day")
    assert resolved.name == "goal-close-day.py"
    assert resolved.is_file()


# ---------------------------------------------------------------------------
# Finding 1 regression (code-reviewer, P1) — real repo-root resolution must
# route through main_worktree_root, never the current worktree's own root.
# Finding 2 (code-reviewer, P2) — every test above monkeypatches
# _compute_open_day_goals directly, so this wiring was previously
# unexercised by any test in this suite.
# ---------------------------------------------------------------------------


def test_resolve_repo_common_dir_for_ceremony_returns_git_common_dir(tmp_path):
    repo = _init_real_repo(tmp_path)
    common_dir = wc_brief._resolve_repo_common_dir_for_ceremony(start=repo)
    assert common_dir is not None
    assert Path(common_dir).resolve() == (repo / ".git").resolve()


def test_compute_open_day_goals_resolves_context_at_main_worktree_root(
    tmp_path, monkeypatch
):
    """Baseline (non-worktree) case: `_compute_open_day_goals()` unmocked
    resolves `resolve_context` at the repo root derived from the real
    `git rev-parse --git-common-dir` -> `main_worktree_root` chain."""
    repo = _init_real_repo(tmp_path)
    monkeypatch.chdir(repo)

    captured_roots: list[Path] = []

    def _spy_resolve_context(repo_root):
        captured_roots.append(Path(repo_root).resolve())
        return types.SimpleNamespace(
            central_state_root=tmp_path / "state", repo_name="test-org/test-repo"
        )

    monkeypatch.setattr(wc_brief, "resolve_context", _spy_resolve_context)
    monkeypatch.setattr(wc_brief, "collect_open_day_goals", lambda *a, **k: dict(_EMPTY))

    result = wc_brief._compute_open_day_goals()

    assert result == _EMPTY
    assert captured_roots == [repo]


def test_compute_open_day_goals_scopes_to_main_worktree_from_linked_worktree(
    tmp_path, monkeypatch
):
    """Finding 1 regression: invoked from a LINKED worktree (a first-class,
    documented layout per this repo's CLAUDE.md), `_compute_open_day_goals`
    must still resolve `resolve_context` at the MAIN worktree root — never
    the linked worktree's own root, which `git rev-parse --show-toplevel`
    (the pre-fix behavior) would have returned instead."""
    repo = _init_real_repo(tmp_path)
    linked = tmp_path / "linked-worktree"
    _run_git(["worktree", "add", str(linked), "-b", "linked-branch"], cwd=repo)
    linked = linked.resolve()

    monkeypatch.chdir(linked)

    captured_roots: list[Path] = []

    def _spy_resolve_context(repo_root):
        captured_roots.append(Path(repo_root).resolve())
        return types.SimpleNamespace(
            central_state_root=tmp_path / "state", repo_name="test-org/test-repo"
        )

    monkeypatch.setattr(wc_brief, "resolve_context", _spy_resolve_context)
    monkeypatch.setattr(wc_brief, "collect_open_day_goals", lambda *a, **k: dict(_EMPTY))

    result = wc_brief._compute_open_day_goals()

    assert result == _EMPTY
    assert captured_roots == [repo], (
        f"must resolve context at the MAIN worktree root {repo}, not the "
        f"linked worktree {linked}; got {captured_roots}"
    )
    assert captured_roots[0] != linked
