"""Characterization tests for coordinator_core.ops.draft_plan_aging.

Ported (parity target) from coordinator/tests/draft-plan-aging.bats (DoE-claude,
20 cases) — each bats `t_*` case has a corresponding test here, plus unit-level
coverage for the individual helper predicates.

Spec backlink: DoE-claude:pln-continuity-artifact-staleness--bec61c § Design Fix #2, § Chunks C1
Port of: draft-plan-aging.sh (DoE b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import os
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest

from coordinator_core.ops.draft_plan_aging import (
    _has_active_baton,
    _has_real_work_or_completion_match,
    _load_completed_deliverable_ids,
    _is_sidecar_file,
    _normalize_prefix,
    _read_scope_paths,
    check_one,
    main,
    scan,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

# Declared, not excused: `_has_real_work_or_completion_match` reads real git log/commit
# timestamps to decide plan staleness -- the property under test is that real-commit
# recency signal, which no mock reproduces. Tests build/mutate their own repo per test
# (distinct commit histories per aging scenario), so the fixture is not hoisted to
# module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_TODAY = date(2026, 7, 16)


def _write_plan(path: Path, created: str, status: str = "draft", scope_lines: "list[str] | None" = None) -> Path:
    body = "---\n"
    body += 'title: "Aging fixture"\n'
    body += f"status: {status}\n"
    body += f"created: {created}\n"
    if scope_lines:
        body += "scope:\n"
        for line in scope_lines:
            body += f"  - {line}\n"
    body += "---\n\n# Body\nSome plan.\n"
    path.write_text(body, encoding="utf-8")
    return path


def _init_git_repo(d: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=d, check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=d, check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True, timeout=30, **no_console_passthrough_kwargs())


def _commit(d: Path, message: str, days_ago: "int | None" = None) -> None:
    env = os.environ.copy()
    if days_ago is not None:
        git_date = (date.today() - timedelta(days=days_ago)).isoformat() + "T12:00:00"
        env["GIT_AUTHOR_DATE"] = git_date
        env["GIT_COMMITTER_DATE"] = git_date
    subprocess.run(["git", "add", "-A"], cwd=d, check=True, timeout=30, **no_console_passthrough_kwargs())
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=d, check=True, timeout=30, env=env, **no_console_passthrough_kwargs())


# ---------------------------------------------------------------------------
# _is_sidecar_file / _normalize_prefix / _read_scope_paths — pure helpers
# ---------------------------------------------------------------------------


def test_is_sidecar_file_matches_all_suffixes():
    assert _is_sidecar_file("x.prior-art-check.md")
    assert _is_sidecar_file("x.review.md")
    assert _is_sidecar_file("x.docs-check.md")
    assert _is_sidecar_file("x.plan-coverage-check.md")
    assert not _is_sidecar_file("real-plan.md")


def test_normalize_prefix_strips_published_prefix():
    assert _normalize_prefix("plugins/coordinator-claude/coordinator/bin/x.sh") == "coordinator/bin/x.sh"
    assert _normalize_prefix("coordinator/bin/x.sh") == "coordinator/bin/x.sh"


def test_read_scope_paths_extracts_dash_lines():
    text = "---\nscope:\n  - a/b.md\n  - c/d.md\n---\n"
    assert _read_scope_paths(text) == ["a/b.md", "c/d.md"]


def test_read_scope_paths_empty_when_no_scope_block():
    assert _read_scope_paths("---\nstatus: draft\n---\n") == []


# ---------------------------------------------------------------------------
# check_one — predicate boundary cases (bats cases 1, 2, 9)
# ---------------------------------------------------------------------------


def test_13d_under_boundary_silent(tmp_path):
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=13)).isoformat())
    line, rc = check_one(str(p), _TODAY, completed_deliverable_ids=frozenset())
    assert (line, rc) == ("", 0)


def test_status_implemented_never_stale(tmp_path):
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=40)).isoformat(), status="implemented")
    line, rc = check_one(str(p), _TODAY, completed_deliverable_ids=frozenset())
    assert (line, rc) == ("", 0)


def test_status_reviewed_never_stale(tmp_path):
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=40)).isoformat(), status="reviewed")
    line, rc = check_one(str(p), _TODAY, completed_deliverable_ids=frozenset())
    assert (line, rc) == ("", 0)


def test_exit_2_missing_created_field(tmp_path):
    p = tmp_path / "plan.md"
    p.write_text('---\ntitle: "Missing created"\nstatus: draft\n---\n\n# Body\n', encoding="utf-8")
    line, rc = check_one(str(p), _TODAY, completed_deliverable_ids=frozenset())
    assert rc == 2
    assert line == ""


def test_unparseable_created_date_exits_2(tmp_path):
    p = _write_plan(tmp_path / "plan.md", "not-a-date")
    line, rc = check_one(str(p), _TODAY, completed_deliverable_ids=frozenset())
    assert rc == 2


# ---------------------------------------------------------------------------
# _has_real_work_or_completion_match — F0 mechanical-denylist + real-work (bats 3, 4, 5)
# ---------------------------------------------------------------------------


def test_mechanical_commit_does_not_suppress_reclaim(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("original\n", encoding="utf-8")
    _commit(tmp_path, "initial commit", days_ago=40)
    (scope_dir / "file.md").write_text("changed\n", encoding="utf-8")
    _commit(tmp_path, "reclaim(docs): sweep touch")

    _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat(), scope_lines=["scope-touched/file.md"])
    monkeypatch.chdir(tmp_path)
    has_work, err = _has_real_work_or_completion_match("plan.md", frozenset())
    assert err is None
    assert has_work is False


def test_mechanical_commit_pickup_frontmatter_does_not_suppress(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("original\n", encoding="utf-8")
    _commit(tmp_path, "initial commit", days_ago=40)
    (scope_dir / "file.md").write_text("changed\n", encoding="utf-8")
    _commit(tmp_path, "pickup: some-slug — frontmatter mutation")

    _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat(), scope_lines=["scope-touched/file.md"])
    monkeypatch.chdir(tmp_path)
    has_work, err = _has_real_work_or_completion_match("plan.md", frozenset())
    assert err is None
    assert has_work is False


def test_real_work_commit_suppresses(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("original\n", encoding="utf-8")
    _commit(tmp_path, "initial commit", days_ago=40)
    (scope_dir / "file.md").write_text("changed\n", encoding="utf-8")
    _commit(tmp_path, "fix: tighten the boundary check")

    _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat(), scope_lines=["scope-touched/file.md"])
    monkeypatch.chdir(tmp_path)
    has_work, err = _has_real_work_or_completion_match("plan.md", frozenset())
    assert err is None
    assert has_work is True


def test_no_scope_block_returns_false_no_git_call(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat())
    has_work, err = _has_real_work_or_completion_match(str(p), frozenset())
    assert (has_work, err) == (False, None)


def test_git_log_failure_returns_diagnostic(tmp_path, monkeypatch):
    # Not a git repo -> git log fails -> (None, diagnostic), not (False, None).
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("x\n", encoding="utf-8")
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat(), scope_lines=["scope-touched/file.md"])

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    has_work, err = _has_real_work_or_completion_match("plan.md", frozenset())
    assert has_work is None
    assert err is not None
    assert "git log failed" in err


def test_prefix_normalization_resolves_scope_path(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    (tmp_path / "coordinator" / "bin").mkdir(parents=True)
    scope_file = tmp_path / "coordinator" / "bin" / "tool.sh"
    scope_file.write_text("original\n", encoding="utf-8")
    _commit(tmp_path, "initial commit", days_ago=40)
    scope_file.write_text("changed\n", encoding="utf-8")
    _commit(tmp_path, "feat: add tool behavior")

    monkeypatch.chdir(tmp_path)
    _write_plan(
        tmp_path / "plan.md",
        (_TODAY - timedelta(days=20)).isoformat(),
        scope_lines=["plugins/coordinator-claude/coordinator/bin/tool.sh"],
    )
    has_work, err = _has_real_work_or_completion_match("plan.md", frozenset())
    assert err is None
    assert has_work is True


def test_completion_entry_deliverable_id_match_suppresses_no_scope_touch(tmp_path, monkeypatch):
    # Defect 1: a plan delivered entirely through commits that never touch a
    # scope-cited path still reads as real work when a completion entry
    # under archive/completed/ carries the same deliverable_id.
    _init_git_repo(tmp_path)
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("original\n", encoding="utf-8")
    _commit(tmp_path, "initial commit", days_ago=40)

    completed = tmp_path / "archive" / "completed" / "2026-07"
    completed.mkdir(parents=True)
    (completed / "2026-07-20-shipped-elsewhere.md").write_text(
        '---\ntitle: "Shipped"\ndeliverable_id: dlv-shared-work-abc123\nchain: "x"\nstatus: pending-release\n---\n\nBody.\n',
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    _write_plan(
        tmp_path / "plan.md",
        (_TODAY - timedelta(days=20)).isoformat(),
        scope_lines=["scope-touched/file.md"],
    )
    (tmp_path / "plan.md").write_text(
        (tmp_path / "plan.md").read_text(encoding="utf-8").replace(
            "status: draft\n", "status: draft\ndeliverable_id: dlv-shared-work-abc123\n"
        ),
        encoding="utf-8",
    )

    has_work, err = _has_real_work_or_completion_match("plan.md", _load_completed_deliverable_ids())
    assert err is None
    assert has_work is True


def test_no_completion_entry_match_still_uses_scope_arm(tmp_path, monkeypatch):
    # No archive/completed/ directory at all: degrades to empty set, scope-
    # path arm alone still governs (no crash, no false suppress).
    monkeypatch.chdir(tmp_path)
    p = _write_plan(tmp_path / "plan.md", (_TODAY - timedelta(days=20)).isoformat())
    has_work, err = _has_real_work_or_completion_match(str(p), frozenset())
    assert (has_work, err) == (False, None)


def test_completion_no_match_falls_through_to_git_log_failure(tmp_path, monkeypatch):
    # Review: code-reviewer (Finding 3a) — a completion-entry NON-match must
    # not swallow the pre-existing git-log-failure (None, diagnostic) path;
    # the early-return only fires on an actual match.
    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("x\n", encoding="utf-8")
    _write_plan(
        tmp_path / "plan.md",
        (_TODAY - timedelta(days=20)).isoformat(),
        scope_lines=["scope-touched/file.md"],
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    has_work, err = _has_real_work_or_completion_match(
        "plan.md", frozenset({"dlv-unrelated-other-plan"})
    )
    assert has_work is None
    assert err is not None
    assert "git log failed" in err


def test_completion_match_skips_git_log_call_entirely(tmp_path, monkeypatch):
    # Review: code-reviewer (Finding 3b) — a completion-entry match returns
    # early and must never invoke `git log` at all; also guards the
    # brightline claim that a completion hit costs zero spawns.
    def _forbidden_run(*args, **kwargs):
        raise AssertionError("git log must not be invoked on a completion-entry match")

    monkeypatch.setattr(subprocess, "run", _forbidden_run)

    scope_dir = tmp_path / "scope-touched"
    scope_dir.mkdir()
    (scope_dir / "file.md").write_text("x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    p = _write_plan(
        tmp_path / "plan.md",
        (_TODAY - timedelta(days=20)).isoformat(),
        scope_lines=["scope-touched/file.md"],
    )
    p.write_text(
        p.read_text(encoding="utf-8").replace(
            "status: draft\n", "status: draft\ndeliverable_id: dlv-shared-work-abc123\n"
        ),
        encoding="utf-8",
    )

    has_work, err = _has_real_work_or_completion_match(
        "plan.md", frozenset({"dlv-shared-work-abc123"})
    )
    assert (has_work, err) == (True, None)


# ---------------------------------------------------------------------------
# _has_active_baton (bats 6)
# ---------------------------------------------------------------------------


def test_active_baton_suppresses(tmp_path, monkeypatch):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "baton.md").write_text(
        '---\ntitle: "Baton"\nstatus: open\ncreated: 2026-07-15\n---\n\n'
        "Continuing work on plan.md, see [plan](plan.md).\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("plan.md")
    assert (has_baton, err) == (True, None)


def test_claimed_baton_does_not_suppress(tmp_path, monkeypatch):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "claimed.md").write_text(
        '---\ntitle: "Claimed baton"\nstatus: claimed\ncreated: 2026-06-17\n---\n\n'
        "Old work on plan.md.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("plan.md")
    assert (has_baton, err) == (False, None)


def test_consumed_baton_old_vocabulary_tolerated(tmp_path, monkeypatch):
    # Writer tolerates the pre-DR-084 'consumed' status value alongside 'claimed'.
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "consumed.md").write_text(
        '---\ntitle: "Consumed baton"\nstatus: consumed\ncreated: 2026-06-17\n---\n\n'
        "Old work on plan.md.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("plan.md")
    assert (has_baton, err) == (False, None)


def test_no_handoffs_dir_returns_false(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("plan.md")
    assert (has_baton, err) == (False, None)


def test_scope_frontmatter_only_reference_suppresses(tmp_path, monkeypatch):
    # Defect 2: a plan named only in a baton's structured scope: frontmatter
    # list, never in narrative prose, must still be found.
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "baton.md").write_text(
        '---\ntitle: "Baton"\nstatus: open\ncreated: 2026-07-15\n'
        "scope:\n  - plan.md\n  - other/file.md\n---\n\n"
        "No narrative mention of the plan at all.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("plan.md")
    assert (has_baton, err) == (True, None)


def test_scope_frontmatter_prefix_variant_normalized(tmp_path, monkeypatch):
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "baton.md").write_text(
        '---\ntitle: "Baton"\nstatus: open\ncreated: 2026-07-15\n'
        "scope:\n  - plugins/coordinator-claude/docs/plans/plan.md\n---\n\n"
        "No narrative mention.\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    has_baton, err = _has_active_baton("docs/plans/plan.md")
    assert (has_baton, err) == (True, None)


# ---------------------------------------------------------------------------
# scan / main — directory scan, sidecar exclusion, exit-code contract (bats 7, 8, 10, 11)
# ---------------------------------------------------------------------------


def test_directory_scan_lists_only_stale(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path / "stale.md", (_TODAY - timedelta(days=20)).isoformat())
    _write_plan(tmp_path / "fresh.md", (_TODAY - timedelta(days=5)).isoformat())
    _write_plan(tmp_path / "not-draft.md", (_TODAY - timedelta(days=20)).isoformat(), status="implemented")

    monkeypatch.chdir(tmp_path)
    lines, rc = scan(str(tmp_path), _TODAY)
    assert rc == 1
    assert len(lines) == 1
    assert "stale.md" in lines[0]


def test_sidecar_excluded_from_directory_scan(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    _write_plan(tmp_path / "real-plan.md", (_TODAY - timedelta(days=20)).isoformat())
    _write_plan(tmp_path / "some-plan.prior-art-check.md", (_TODAY - timedelta(days=20)).isoformat())
    _write_plan(tmp_path / "other-plan.review.md", (_TODAY - timedelta(days=20)).isoformat())

    monkeypatch.chdir(tmp_path)
    lines, rc = scan(str(tmp_path), _TODAY)
    assert rc == 1
    assert len(lines) == 1
    assert "real-plan.md" in lines[0]


def test_sidecar_not_stale_in_single_file_mode(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    p = _write_plan(tmp_path / "some-plan.prior-art-check.md", (_TODAY - timedelta(days=20)).isoformat())
    monkeypatch.chdir(tmp_path)
    lines, rc = scan(str(p), _TODAY)
    assert (lines, rc) == ([], 0)


def test_main_missing_arg_exits_2(capsys):
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "draft-plan-aging.sh: missing argument" in captured.err


def test_main_path_not_found_exits_2(tmp_path, capsys):
    missing = str(tmp_path / "does-not-exist.md")
    rc = main([missing])
    captured = capsys.readouterr()
    assert rc == 2
    assert "draft-plan-aging.sh: path not found" in captured.err


def test_main_stale_exits_1_and_prints_line(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    old_enough = (date.today() - timedelta(days=20)).isoformat()
    p = _write_plan(tmp_path / "plan.md", old_enough)
    monkeypatch.chdir(tmp_path)
    rc = main([str(p)])
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.startswith("STALE:")


def test_main_not_stale_exits_0(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    too_recent = (date.today() - timedelta(days=1)).isoformat()
    p = _write_plan(tmp_path / "plan.md", too_recent)
    monkeypatch.chdir(tmp_path)
    rc = main([str(p)])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == ""
