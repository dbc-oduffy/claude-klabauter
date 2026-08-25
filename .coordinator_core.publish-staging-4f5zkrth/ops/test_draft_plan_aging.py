"""Tests for coordinator_core.ops.draft_plan_aging's sibling op,
`plan.list_stale_executing` — status:executing / git-log-mtime-age
predicate. Co-located per current house convention; the pre-existing
STALE-draft predicate's characterization tests live at
coordinator_core/tests/test_draft_plan_aging.py (untouched by this file).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

import coordinator_core.ipc as _ipc
import coordinator_core.ops.draft_plan_aging as _draft_plan_aging
from coordinator_core.ipc import get_op_handler
from coordinator_core.lifecycle_constants import PLAN_ORPHAN_TERMINAL_STATUS
from coordinator_core.ops.draft_plan_aging import (
    _CARRY_OBSERVABILITY_FIX_LANDED_ON,
    _git_commit_epoch,
    _is_census_local_sidecar,
    _list_dangling_baton_plan_references,
    _plan_list_orphaned,
    _plan_list_stale_executing,
    list_orphaned,
    list_stale_executing,
    resolve_plan_owner,
)

# Declared, not excused: this file spawns a real git process because
# `_git_commit_epoch` under test reads a plan file's real last-commit
# timestamp via `git log`, driving the staleness-age predicate -- no mock
# stands in for real commit history/timestamps. Tests build distinct commit
# histories per scenario (dangling baton refs, orphan detection, carry-
# observability-fix landed-on dates), so `_init_repo` is not hoisted to
# module scope -- per-test isolation. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")


def _write_and_commit_plan(
    repo: Path,
    name: str,
    status: str,
    commit_days_ago: int,
    today: date,
) -> Path:
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    path.write_text(
        "---\n"
        'title: "fixture"\n'
        f"status: {status}\n"
        "---\n\nbody\n",
        encoding="utf-8",
    )
    _run_git(repo, "add", str(path.relative_to(repo)))
    commit_date = today - timedelta(days=commit_days_ago)
    env_date = f"{commit_date.isoformat()}T12:00:00"
    subprocess.run(
        ["git", "commit", "-q", "-m", f"add {name}"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **_base_env(),
            "GIT_AUTHOR_DATE": env_date,
            "GIT_COMMITTER_DATE": env_date,
        },
    )
    return path


def _base_env() -> dict:
    import os

    return dict(os.environ)


def test_list_stale_executing_reports_old_executing_plan(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "old.md", "executing", commit_days_ago=10, today=today)

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert result == [{"path": "docs/plans/old.md", "age_days": 10}]


def test_list_stale_executing_excludes_below_threshold(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "fresh.md", "executing", commit_days_ago=1, today=today)

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert result == []


def test_list_stale_executing_ignores_non_executing_status(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "draft.md", "draft", commit_days_ago=30, today=today)

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert result == []


def test_list_stale_executing_excludes_checker_sidecar(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(
        tmp_path, "old.review.md", "executing", commit_days_ago=30, today=today
    )

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert result == []


def test_list_stale_executing_no_docs_plans_dir(tmp_path):
    _init_repo(tmp_path)

    result = list_stale_executing(tmp_path, threshold_days=3)

    assert result == []


def test_list_stale_executing_no_git_history_is_not_stale(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "uncommitted.md").write_text(
        "---\nstatus: executing\n---\n\nbody\n", encoding="utf-8"
    )

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert result == []


def test_git_commit_epoch_returns_none_for_unknown_path(tmp_path):
    _init_repo(tmp_path)
    assert _git_commit_epoch(tmp_path, "docs/plans/nope.md") is None


# ---------------------------------------------------------------------------
# C14 — `list_stale_executing` batches its per-candidate `git log -1
# --format=%ct` spawn into one `_batch_git_commit_epochs` walk. These tests
# pin the batched multi-plan shape and the absence-reconciliation contract
# (§ Anti-scope 25: a path absent from the walk's output must read as "no
# resolved timestamp", never silently defaulted).
# ---------------------------------------------------------------------------


def test_batch_git_commit_epochs_resolves_each_distinct_path_independently(tmp_path):
    from coordinator_core.ops.draft_plan_aging import _batch_git_commit_epochs

    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "old.md", "executing", commit_days_ago=10, today=today)
    _write_and_commit_plan(tmp_path, "fresh.md", "executing", commit_days_ago=1, today=today)

    result = _batch_git_commit_epochs(
        tmp_path, ["docs/plans/old.md", "docs/plans/fresh.md"]
    )

    assert set(result.keys()) == {"docs/plans/old.md", "docs/plans/fresh.md"}
    assert result["docs/plans/old.md"] < result["docs/plans/fresh.md"]


def test_batch_git_commit_epochs_omits_paths_with_no_touching_commit(tmp_path):
    """§ Anti-scope 25: a path with no commit reaching it is simply absent
    from the returned map — never coerced to a resolved epoch."""
    from coordinator_core.ops.draft_plan_aging import _batch_git_commit_epochs

    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "tracked.md", "executing", commit_days_ago=5, today=today)

    result = _batch_git_commit_epochs(
        tmp_path, ["docs/plans/tracked.md", "docs/plans/never-committed.md"]
    )

    assert "docs/plans/tracked.md" in result
    assert "docs/plans/never-committed.md" not in result


def test_batch_git_commit_epochs_empty_input_no_spawn():
    from coordinator_core.ops.draft_plan_aging import _batch_git_commit_epochs

    assert _batch_git_commit_epochs(Path("."), []) == {}


def _commit_at(repo: Path, message: str, commit_days_ago: int, today: date, *, allow_empty: bool = False) -> None:
    """Commit currently-staged changes with an explicit author/committer date,
    matching `_write_and_commit_plan`'s date-pinning idiom above."""
    commit_date = today - timedelta(days=commit_days_ago)
    env_date = f"{commit_date.isoformat()}T12:00:00"
    args = ["git", "commit", "-q", "-m", message]
    if allow_empty:
        args.append("--allow-empty")
    subprocess.run(
        args,
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        env={
            **_base_env(),
            "GIT_AUTHOR_DATE": env_date,
            "GIT_COMMITTER_DATE": env_date,
        },
    )


def test_batch_git_commit_epochs_resolves_conflict_resolution_merge_commit(tmp_path):
    """Regression for the merge-suppression trap: `git log --name-only`
    prints NO file-list line for a merge commit by default, even one that
    survives history simplification under a pathspec (i.e. genuinely
    touched the path via conflict resolution) — so without
    `--diff-merges=first-parent`, the batched matcher would skip past the
    merge's header (real, current `%ct`) straight to the next, OLDER commit
    that does print a name line, silently returning a stale timestamp. This
    pins that the merge commit's own epoch is returned instead.
    """
    from coordinator_core.ops.draft_plan_aging import _batch_git_commit_epochs

    today = date(2026, 7, 22)
    rel_path = "docs/plans/conflict.md"
    _init_repo(tmp_path)
    _run_git(tmp_path, "checkout", "-b", "main")

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plans_dir / "conflict.md"

    plan_path.write_text("base\n", encoding="utf-8")
    _run_git(tmp_path, "add", rel_path)
    _commit_at(tmp_path, "add base", commit_days_ago=20, today=today)

    _run_git(tmp_path, "checkout", "-b", "side")
    plan_path.write_text("side change\n", encoding="utf-8")
    _run_git(tmp_path, "add", rel_path)
    _commit_at(tmp_path, "side edit", commit_days_ago=15, today=today)

    _run_git(tmp_path, "checkout", "main")
    plan_path.write_text("trunk change\n", encoding="utf-8")
    _run_git(tmp_path, "add", rel_path)
    _commit_at(tmp_path, "trunk edit", commit_days_ago=14, today=today)

    merge = subprocess.run(
        ["git", "merge", "--no-ff", "--no-commit", "side"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert merge.returncode != 0, "expected a real conflict to set up this fixture"

    plan_path.write_text("resolved\n", encoding="utf-8")
    _run_git(tmp_path, "add", rel_path)
    _commit_at(tmp_path, "merge: resolve conflict", commit_days_ago=0, today=today)

    merge_epoch = int(
        subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )

    result = _batch_git_commit_epochs(tmp_path, [rel_path])

    assert rel_path in result
    assert result[rel_path] == merge_epoch


def test_list_stale_executing_multi_plan_uses_one_batched_git_log_call(tmp_path, monkeypatch):
    """Regression pin for C14: with N `status: executing` plans present,
    `list_stale_executing` must call `_batch_git_commit_epochs` exactly ONCE
    (a single git-log walk), never once per plan.
    """
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "old-a.md", "executing", commit_days_ago=10, today=today)
    _write_and_commit_plan(tmp_path, "old-b.md", "executing", commit_days_ago=20, today=today)
    _write_and_commit_plan(tmp_path, "old-c.md", "executing", commit_days_ago=30, today=today)

    call_count = 0
    real_batch = _draft_plan_aging._batch_git_commit_epochs

    def _counting_batch(repo_root, rel_paths):
        nonlocal call_count
        call_count += 1
        return real_batch(repo_root, rel_paths)

    monkeypatch.setattr(_draft_plan_aging, "_batch_git_commit_epochs", _counting_batch)

    result = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert call_count == 1
    assert {e["path"] for e in result} == {
        "docs/plans/old-a.md",
        "docs/plans/old-b.md",
        "docs/plans/old-c.md",
    }
    assert result == sorted(result, key=lambda e: e["path"])


def test_list_stale_executing_is_idempotent_across_repeated_invocation(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "old.md", "executing", commit_days_ago=10, today=today)

    first = list_stale_executing(tmp_path, threshold_days=3, today=today)
    second = list_stale_executing(tmp_path, threshold_days=3, today=today)

    assert first == second == [{"path": "docs/plans/old.md", "age_days": 10}]


def test_op_registered_under_contractual_key():
    handler = get_op_handler("plan.list_stale_executing")
    assert handler is _plan_list_stale_executing


def test_handler_fails_loud_when_repo_root_is_none():
    with pytest.raises(ValueError, match="repo_root is None"):
        _plan_list_stale_executing({"threshold_days": 3}, repo_root=None)


def test_handler_requires_integer_threshold_days(tmp_path):
    _init_repo(tmp_path)
    common_dir = tmp_path / ".git"
    with pytest.raises(ValueError, match="threshold_days"):
        _plan_list_stale_executing({}, repo_root=common_dir)


def test_handler_derives_worktree_root_from_common_dir(tmp_path):
    today = date(2026, 7, 22)
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "old.md", "executing", commit_days_ago=10, today=today)
    common_dir = tmp_path / ".git"

    result = _plan_list_stale_executing({"threshold_days": 3}, repo_root=common_dir)

    assert result == {"stale": [{"path": "docs/plans/old.md", "age_days": result["stale"][0]["age_days"]}]}
    assert result["stale"][0]["age_days"] >= 9


# ---------------------------------------------------------------------------
# resolve_plan_owner (AC1/AC2/AC14/AC16) — the C1 field-aware ownership
# resolver. Additive alongside _has_active_baton's whole-body-substring
# predicate above; these tests cover only the new function.
# ---------------------------------------------------------------------------


def _write_plan_with_deliverable(repo: Path, name: str, deliverable_id: "str | None" = None) -> Path:
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    fm = '---\ntitle: "fixture"\nstatus: draft\n'
    if deliverable_id is not None:
        fm += f"deliverable_id: {deliverable_id}\n"
    fm += "---\n\nbody\n"
    path.write_text(fm, encoding="utf-8")
    return path


def _write_handoff(
    repo: Path,
    name: str,
    status: str,
    deliverable_id: "str | None" = None,
    fm_plan: "str | None" = None,
    body: str = "",
) -> Path:
    handoffs_dir = repo / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    path = handoffs_dir / name
    fm = f"---\nstatus: {status}\n"
    if deliverable_id is not None:
        fm += f"deliverable_id: {deliverable_id}\n"
    if fm_plan is not None:
        fm += f"plan: {fm_plan}\n"
    fm += "---\n\n" + body + "\n"
    path.write_text(fm, encoding="utf-8")
    return path


def test_resolve_plan_owner_deliverable_id_match_against_open_handoff(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "owned-open.md", deliverable_id="dlv-abc123")
    _write_handoff(tmp_path, "h-open.md", "open", deliverable_id="dlv-abc123")

    assert resolve_plan_owner(plan, tmp_path) == "state/handoffs/h-open.md"


def test_resolve_plan_owner_deliverable_id_match_against_claimed_handoff(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "owned-claimed.md", deliverable_id="dlv-xyz789")
    _write_handoff(tmp_path, "h-claimed.md", "claimed", deliverable_id="dlv-xyz789")

    assert resolve_plan_owner(plan, tmp_path) == "state/handoffs/h-claimed.md"


def test_resolve_plan_owner_scope_entry_only_is_unowned(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "scope-only.md")
    _write_handoff(tmp_path, "h-scope.md", "open", body="scope:\n  - docs/plans/scope-only.md\n")

    assert resolve_plan_owner(plan, tmp_path) is None


def test_resolve_plan_owner_workstream_equality_only_is_unowned(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "workstream-only.md")
    # Give the plan a matching workstream too — the resolver never reads
    # `workstream` on either side, so this must still resolve UNOWNED.
    plan.write_text(plan.read_text(encoding="utf-8").replace("status: draft", "status: draft\nworkstream: shared-ws"))
    _write_handoff(tmp_path, "h-ws.md", "open", body="workstream: shared-ws\n")

    assert resolve_plan_owner(plan, tmp_path) is None


def test_resolve_plan_owner_origin_plan_id_only_is_unowned(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "origin-only.md")
    _write_handoff(tmp_path, "h-origin.md", "open", body="origin_plan_id: pln-origin-only\n")

    assert resolve_plan_owner(plan, tmp_path) is None


def test_resolve_plan_owner_path_line_pointer_retired_no_longer_confers_ownership(tmp_path):
    """C12/R2 (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md):
    the anchored `**Plan:**` body path-line secondary key is retired
    alongside the `plan:` frontmatter scalar — a plan with no
    `deliverable_id` now resolves UNOWNED regardless of what path-shaped
    pointer a handoff's body carries, rather than falling to the retired
    secondary key.
    """
    _init_repo(tmp_path)
    plan_no_id = _write_plan_with_deliverable(tmp_path, "path-line.md")
    _write_handoff(tmp_path, "h-path.md", "open", body="**Plan:** `docs/plans/path-line.md`\n")
    assert resolve_plan_owner(plan_no_id, tmp_path) is None

    plan_with_id = _write_plan_with_deliverable(tmp_path, "path-line-2.md", deliverable_id="dlv-has-one")
    _write_handoff(tmp_path, "h-path-2.md", "open", body="**Plan:** `docs/plans/path-line-2.md`\n")
    assert resolve_plan_owner(plan_with_id, tmp_path) is None


def test_resolve_plan_owner_frontmatter_plan_scalar_retired_no_longer_confers_ownership(tmp_path):
    """The retired `plan:` handoff frontmatter field (C12/R2) must not
    confer ownership even when the plan carries no `deliverable_id` at all
    — the field is gone as a join key, not merely deprioritized."""
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "fm-plan-scalar.md")
    _write_handoff(tmp_path, "h-fm-plan.md", "open", fm_plan="docs/plans/fm-plan-scalar.md")

    assert resolve_plan_owner(plan, tmp_path) is None


def test_dangling_baton_reference_plan_pointer_forms_produce_no_finding(tmp_path):
    """Retired-join-key regression: a handoff carrying only a path-shaped
    pointer (frontmatter `plan:` or a `**Plan:**` body line) and no
    `deliverable_id` is no longer consulted by the AC7 dangling-reference
    detector at all — "no deliverable_id, nothing to check", not a
    (correctly or incorrectly) resolved path reference."""
    _init_repo(tmp_path)
    _write_plan_with_deliverable(tmp_path, "resolves-dotslash.md")
    _write_handoff(tmp_path, "h-resolving-dotslash.md", "open", fm_plan="./docs/plans/resolves-dotslash.md")
    _write_handoff(tmp_path, "h-body-line.md", "open", body="**Plan:** `docs/plans/resolves-dotslash.md`\n")

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == []


def test_resolve_plan_owner_citation_shaped_prose_mention_is_unowned(tmp_path):
    """AC2's fourth negative case: a plan path mentioned in a 'see also' /
    prior-art-note prose context — NOT the anchored **Plan:**/plan: forms —
    must not confer ownership. This is exactly the whole-body-substring
    failure mode the new predicate exists to replace.
    """
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "citation-only.md")
    _write_handoff(
        tmp_path,
        "h-citation.md",
        "open",
        body="See also `docs/plans/citation-only.md` for prior art.\n",
    )

    assert resolve_plan_owner(plan, tmp_path) is None


def test_resolve_plan_owner_no_handoffs_dir_is_unowned(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "no-handoffs-dir.md", deliverable_id="dlv-none")

    assert resolve_plan_owner(plan, tmp_path) is None


def test_resolve_plan_owner_fails_loud_when_repo_root_is_none(tmp_path):
    _init_repo(tmp_path)
    plan = _write_plan_with_deliverable(tmp_path, "no-root.md", deliverable_id="dlv-abc")

    with pytest.raises(ValueError, match="repo_root"):
        resolve_plan_owner(plan, None)


# ---------------------------------------------------------------------------
# list_orphaned / plan.list_orphaned (C2) — the tiered orphan census. AC16:
# a real positive per populated tier, planted and observed before asserting.
# ---------------------------------------------------------------------------

_POST_CARRY_DATE = "2026-08-01"  # after _CARRY_OBSERVABILITY_FIX_LANDED_ON (2026-07-31)


def _write_census_plan(
    repo: Path,
    name: str,
    status: str = "draft",
    created: "str | None" = _POST_CARRY_DATE,
    execution_authorized_by: "str | None" = None,
    deliverable_id: "str | None" = None,
) -> Path:
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    path = plans_dir / name
    fm = f'---\ntitle: "fixture"\nstatus: {status}\n'
    if created is not None:
        fm += f"created: {created}\n"
    if execution_authorized_by is not None:
        fm += f"execution_authorized_by: {execution_authorized_by}\n"
    if deliverable_id is not None:
        fm += f"deliverable_id: {deliverable_id}\n"
    fm += "---\n\nbody\n"
    path.write_text(fm, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# _is_census_local_sidecar / list_orphaned population exclusion — the C4
# follow-on fix for the 47-file corpus leak (variant sidecar names the fixed
# `_is_sidecar_file` four-suffix denylist doesn't catch). Additive to
# `_is_sidecar_file`, never a replacement — see that predicate's docstring.
# ---------------------------------------------------------------------------


def test_is_census_local_sidecar_review_variant_excluded(tmp_path):
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "foo.md").write_text("---\nstatus: draft\n---\n\nbody\n", encoding="utf-8")
    sidecar = plans_dir / "foo.review-zoli.md"
    sidecar.write_text("body\n", encoding="utf-8")

    assert _is_census_local_sidecar(sidecar, plans_dir) is True


def test_list_orphaned_excludes_review_variant_sidecar_from_population(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "foo.md", created=_POST_CARRY_DATE)
    (tmp_path / "docs" / "plans" / "foo.review-zoli.md").write_text(
        "---\nstatus: draft\ncreated: 2026-08-01\n---\n\nbody\n", encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    # Only foo.md itself enters the population; the sidecar variant is excluded.
    assert result["population_count"] == 1


def test_list_orphaned_excludes_timestamped_plan_coverage_check_sidecar(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "foo.md", created=_POST_CARRY_DATE)
    (
        tmp_path
        / "docs"
        / "plans"
        / "foo.plan-coverage-check.2026-07-01T08-31-09Z.md"
    ).write_text(
        "---\nstatus: draft\ncreated: 2026-08-01\n---\n\nbody\n", encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["population_count"] == 1


def test_list_orphaned_excludes_node_map_and_phase0_sidecar_variants(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "foo.md", created=_POST_CARRY_DATE)
    plans_dir = tmp_path / "docs" / "plans"
    (plans_dir / "foo.node-map.md").write_text(
        "---\nstatus: draft\ncreated: 2026-08-01\n---\n\nbody\n", encoding="utf-8"
    )
    (plans_dir / "foo.phase0.md").write_text(
        "---\nstatus: draft\ncreated: 2026-08-01\n---\n\nbody\n", encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["population_count"] == 1


def test_list_orphaned_anti_over_exclusion_dotted_name_without_real_parent_stays_in(tmp_path):
    """The guard against the fix silently shrinking the orphan count: a
    dotted plan filename whose prefix is NOT a real file on disk (no parent
    plan to be a sidecar of) must stay IN the population.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    # "2026-07-05-strang-03.md" with no "2026-07-05.md" sibling on disk.
    _write_census_plan(tmp_path, "2026-07-05-strang-03.md", created=_POST_CARRY_DATE)

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["population_count"] == 1


def test_list_orphaned_existing_sidecar_suffixes_still_excluded_no_regression(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "foo.md", created=_POST_CARRY_DATE)
    plans_dir = tmp_path / "docs" / "plans"
    for suffix in (".prior-art-check.md", ".review.md", ".docs-check.md", ".plan-coverage-check.md"):
        (plans_dir / f"foo{suffix}").write_text(
            "---\nstatus: draft\ncreated: 2026-08-01\n---\n\nbody\n", encoding="utf-8"
        )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["population_count"] == 1


def test_is_census_local_sidecar_documented_coincidental_prefix_limitation(tmp_path):
    """Review: code-reviewer — pins the documented residual limitation: a
    dotted, legitimately-real plan filename whose prefix happens to match an
    unrelated real plan file on disk is misclassified as that file's
    sidecar, even though the two files have no actual parent/sidecar
    relationship. Accepted as a known, documented limitation (not narrowed)
    — see `_is_census_local_sidecar`'s docstring.
    """
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "v1.md").write_text("---\nstatus: draft\n---\n\nbody\n", encoding="utf-8")
    coincidental = plans_dir / "v1.2-notes.md"
    coincidental.write_text("---\nstatus: draft\n---\n\nbody\n", encoding="utf-8")

    assert _is_census_local_sidecar(coincidental, plans_dir) is True


def test_list_orphaned_p1_authorized_orphan_no_age_gate(tmp_path):
    today = date(2026, 8, 1)
    _init_repo(tmp_path)
    _write_census_plan(
        tmp_path, "authorized.md", created=today.isoformat(), execution_authorized_by="PM"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    # Observe before asserting (AC16): planted today, no owning handoff, real
    # authorization — must be reported despite zero age.
    assert result["authorized_orphan"] == [
        {"path": "docs/plans/authorized.md", "execution_authorized_by": "PM"}
    ]
    assert result["parked_count"] == 0
    assert result["chain_gap"] == []


def test_list_orphaned_execution_authorized_by_null_lands_in_p3_never_p1(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(
        tmp_path,
        "explicit-null.md",
        created=_POST_CARRY_DATE,
        execution_authorized_by="null",
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["authorized_orphan"] == []
    assert result["parked_count"] == 1


def test_list_orphaned_p3_parked_counted_only_past_threshold(tmp_path):
    today = date(2026, 8, 20)  # 19 days after _POST_CARRY_DATE
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "old-unauthorized.md", created=_POST_CARRY_DATE)
    _write_census_plan(
        tmp_path, "fresh-unauthorized.md", created=(today - timedelta(days=1)).isoformat()
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["parked_count"] == 1
    # The fresh (below-threshold) plan is not dropped — it lands in the
    # separate parked_below_threshold_count bucket, never merged into the
    # aged parked_count tier (regression guard for the silent-drop defect).
    assert result["parked_below_threshold_count"] == 1
    assert result["authorized_orphan"] == []
    assert result["chain_gap"] == []


def test_list_orphaned_young_unowned_plan_counted_below_threshold_not_dropped(tmp_path):
    """An unowned, unauthorized, non-legacy plan created TODAY (age 0) must
    land in parked_below_threshold_count, NOT parked_count, and must not be
    dropped from the census entirely — the original silent-drop defect this
    test guards against.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "young.md", created=today.isoformat())

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["parked_below_threshold_count"] == 1
    assert result["parked_count"] == 0
    assert result["authorized_orphan"] == []
    assert result["legacy_unjoinable_count"] == 0
    assert result["population_count"] == 1


def test_list_orphaned_same_plan_aged_past_threshold_lands_in_parked_count(tmp_path):
    """The same shape of plan (unowned, unauthorized, non-legacy), once its
    age crosses threshold_days, must land in parked_count, not the new
    below-threshold bucket — guards against the fix inverting the gate.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "aged.md", created=_POST_CARRY_DATE)  # 19 days before today

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["parked_count"] == 1
    assert result["parked_below_threshold_count"] == 0
    assert result["population_count"] == 1


def test_list_orphaned_partition_is_total_across_mixed_corpus(tmp_path):
    """Totality invariant (AC5/AC6): for any corpus, population_count must
    equal the sum of every tier/bucket — owned + legacy + P1 + chain_gap +
    aged-P3 + young-P3. This is the regression guard for the whole class of
    silent-drop defect: it would have caught the original hole (a young,
    unowned, unauthorized, non-legacy plan counted nowhere).
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)

    # owned
    plan = _write_census_plan(tmp_path, "owned.md", created=_POST_CARRY_DATE, deliverable_id="d-owned")
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "hf.md").write_text(
        "---\nstatus: open\ndeliverable_id: d-owned\n---\n\nbody\n", encoding="utf-8"
    )
    # legacy_unjoinable
    _write_census_plan(tmp_path, "legacy.md", created="2026-01-01")
    # P1 authorized_orphan
    _write_census_plan(
        tmp_path, "authorized.md", created=_POST_CARRY_DATE, execution_authorized_by="PM"
    )
    # aged P3 (parked_count)
    _write_census_plan(tmp_path, "aged-p3.md", created=_POST_CARRY_DATE)
    # young P3 (parked_below_threshold_count)
    _write_census_plan(tmp_path, "young-p3.md", created=today.isoformat())

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    total = (
        result["owned_count"]
        + result["legacy_unjoinable_count"]
        + len(result["authorized_orphan"])
        + len(result["chain_gap"])
        + result["parked_count"]
        + result["parked_below_threshold_count"]
    )
    assert total == result["population_count"]
    assert result["population_count"] == 5
    assert result["owned_count"] == 1
    assert result["legacy_unjoinable_count"] == 1
    assert len(result["authorized_orphan"]) == 1
    assert result["parked_count"] == 1
    assert result["parked_below_threshold_count"] == 1
    assert plan.is_file()


def test_list_orphaned_p1_created_today_no_age_gate_regression(tmp_path):
    """AC4 regression guard: a P1 (authorized) plan created TODAY still
    reports as P1 with no age gate applied — the age gate governs P3 only.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(
        tmp_path, "authorized-today.md", created=today.isoformat(), execution_authorized_by="PM"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["authorized_orphan"] == [
        {"path": "docs/plans/authorized-today.md", "execution_authorized_by": "PM"}
    ]
    assert result["parked_count"] == 0
    assert result["parked_below_threshold_count"] == 0


def test_list_orphaned_chain_gap_never_populated(tmp_path):
    _init_repo(tmp_path)
    today = date(2026, 8, 20)
    # A wide spread of unowned fixtures — authorized, unauthorized, legacy —
    # none of them may ever land in chain_gap; the P2 tier stays parked/empty
    # until a validated chain-membership mechanism exists.
    _write_census_plan(tmp_path, "a.md", created=_POST_CARRY_DATE, execution_authorized_by="PM")
    _write_census_plan(tmp_path, "b.md", created=_POST_CARRY_DATE)
    _write_census_plan(tmp_path, "c.md", created="2026-01-01")

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["chain_gap"] == []


def test_list_orphaned_legacy_unjoinable_excluded_from_tiers(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "pre-fix.md", created="2026-01-01")

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    # Observe: predates the fix-landing cutoff, unowned, unauthorized — must
    # be counted as legacy_unjoinable and NOT bleed into P1 or P3.
    assert result["legacy_unjoinable_count"] == 1
    assert result["parked_count"] == 0
    assert result["authorized_orphan"] == []
    assert result["population_count"] == 1


def test_list_orphaned_approved_and_reviewed_not_unrecognized(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "approved.md", status="approved", created=_POST_CARRY_DATE)
    _write_census_plan(tmp_path, "reviewed.md", status="reviewed", created=_POST_CARRY_DATE)

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    # "approved" and "reviewed" are recognized non-terminal plan statuses
    # (schema SSOT: coordinator_core/frontmatter/schemas/plan.schema.json
    # properties.status.enum) — neither belongs in unrecognized_status, but
    # both are still non-terminal so both land in the live population.
    assert result["unrecognized_status"] == []
    assert result["population_count"] == 2
    assert result["parked_count"] == 2


def test_list_orphaned_unrecognized_status_in_bucket_and_population(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "bogus.md", status="bogus", created=_POST_CARRY_DATE)

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["unrecognized_status"] == [
        {"path": "docs/plans/bogus.md", "status": "bogus"}
    ]
    # A genuinely unrecognized status is non-terminal (not in
    # PLAN_ORPHAN_TERMINAL_STATUS) so it is also part of the orphan-eligible
    # population, not excluded by virtue of being unrecognized.
    assert result["population_count"] == 1
    assert result["parked_count"] == 1


def test_list_orphaned_owned_plan_excluded_from_all_tiers(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    plan = _write_census_plan(
        tmp_path, "owned.md", created=_POST_CARRY_DATE, deliverable_id="dlv-owned"
    )
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "h.md").write_text(
        "---\nstatus: open\ndeliverable_id: dlv-owned\n---\n\nbody\n", encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["owned_count"] == 1
    assert result["population_count"] == 1
    assert result["authorized_orphan"] == []
    assert result["parked_count"] == 0
    assert result["legacy_unjoinable_count"] == 0


def test_list_orphaned_terminal_status_excluded_from_population(tmp_path):
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "done.md", status="implemented", created="2026-01-01")

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["population_count"] == 0
    assert result["legacy_unjoinable_count"] == 0
    # Frontmatter-bearing but terminal-status: scanned, but neither in the
    # population nor the non-plan-excluded bucket — see terminal_count.
    assert result["terminal_count"] == 1
    assert result["scanned_count"] == 1
    assert result["non_plan_excluded_count"] == 0


# ---------------------------------------------------------------------------
# Non-plan population exclusion (cross-repo memo
# 2026-08-03-doe-claude-em-two-rulings-plan-orphan-population-and-dr088-antiscope.md
# § 1): frontmatter presence, structurally, is the discriminator — NOT a
# filename denylist and NOT a probe for a particular key like status:.
# ---------------------------------------------------------------------------


def test_list_orphaned_frontmatterless_file_excluded_and_counted(tmp_path):
    """A file with no YAML frontmatter block at all (INDEX.md, README.md)
    leaves the population entirely: absent from population_count, and never
    appears in unrecognized_status or any orphan tier — it lands solely in
    the counted-never-alarmed non_plan_excluded_count bucket.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "INDEX.md").write_text(
        "# Plan Index\n\nSee below for the current plan roster.\n", encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["non_plan_excluded_count"] == 1
    assert result["population_count"] == 0
    assert result["unrecognized_status"] == []
    assert result["authorized_orphan"] == []
    assert result["chain_gap"] == []
    assert result["parked_count"] == 0
    assert result["parked_below_threshold_count"] == 0
    assert result["legacy_unjoinable_count"] == 0
    assert result["scanned_count"] == 1
    assert result["terminal_count"] == 0


def test_list_orphaned_plan_with_frontmatter_but_no_status_key_still_in_population(tmp_path):
    """The discriminator-pinning test: a REAL plan with a frontmatter block
    but no status: key at all must still count in the population and still
    reach unrecognized_status — frontmatter PRESENCE, not status presence,
    is what distinguishes a plan from a non-plan file. Guards against the
    discriminator being silently re-implemented as a status probe.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "no-status.md").write_text(
        f'---\ntitle: "fixture"\ncreated: {_POST_CARRY_DATE}\n---\n\nbody\n', encoding="utf-8"
    )

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["non_plan_excluded_count"] == 0
    assert result["population_count"] == 1
    assert result["unrecognized_status"] == [
        {"path": "docs/plans/no-status.md", "status": None}
    ]


def test_list_orphaned_accounting_invariant_across_mixed_fixture(tmp_path):
    """scanned_count == population_count + non_plan_excluded_count +
    terminal_count across a mixed fixture: owned, parked, young-parked,
    legacy, terminal, and non-plan files all present in one directory.
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)

    # owned
    _write_census_plan(tmp_path, "owned.md", created=_POST_CARRY_DATE, deliverable_id="d-owned")
    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True, exist_ok=True)
    (handoffs_dir / "hf.md").write_text(
        "---\nstatus: open\ndeliverable_id: d-owned\n---\n\nbody\n", encoding="utf-8"
    )
    # aged P3 (parked_count)
    _write_census_plan(tmp_path, "aged-p3.md", created=_POST_CARRY_DATE)
    # young P3 (parked_below_threshold_count)
    _write_census_plan(tmp_path, "young-p3.md", created=today.isoformat())
    # legacy_unjoinable
    _write_census_plan(tmp_path, "legacy.md", created="2026-01-01")
    # terminal status
    _write_census_plan(tmp_path, "done.md", status="implemented", created="2026-01-01")
    # non-plan files
    plans_dir = tmp_path / "docs" / "plans"
    (plans_dir / "INDEX.md").write_text("# Plan Index\n", encoding="utf-8")
    (plans_dir / "README.md").write_text("# README\n", encoding="utf-8")

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert result["scanned_count"] == (
        result["population_count"] + result["non_plan_excluded_count"] + result["terminal_count"]
    )
    assert result["scanned_count"] == 7
    assert result["non_plan_excluded_count"] == 2
    assert result["terminal_count"] == 1
    assert result["population_count"] == 4
    assert result["owned_count"] == 1
    assert result["parked_count"] == 1
    assert result["parked_below_threshold_count"] == 1
    assert result["legacy_unjoinable_count"] == 1


def test_list_orphaned_no_docs_plans_dir(tmp_path):
    _init_repo(tmp_path)

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["population_count"] == 0
    assert result["authorized_orphan"] == []
    assert result["non_plan_excluded_count"] == 0
    assert result["scanned_count"] == 0
    assert result["terminal_count"] == 0


def test_list_orphaned_fails_loud_when_repo_root_is_none():
    with pytest.raises(ValueError, match="repo_root"):
        list_orphaned(None, threshold_days=14)


def test_op_registered_under_plan_list_orphaned_key():
    handler = get_op_handler("plan.list_orphaned")
    assert handler is _plan_list_orphaned


def test_plan_list_orphaned_handler_fails_loud_when_repo_root_is_none():
    with pytest.raises(ValueError, match="repo_root is None"):
        _plan_list_orphaned({"threshold_days": 14}, repo_root=None)


def test_plan_list_orphaned_handler_requires_integer_threshold_days(tmp_path):
    _init_repo(tmp_path)
    common_dir = tmp_path / ".git"
    with pytest.raises(ValueError, match="threshold_days"):
        _plan_list_orphaned({}, repo_root=common_dir)


def test_plan_list_orphaned_handler_derives_worktree_root_from_common_dir(tmp_path):
    today_marker = _CARRY_OBSERVABILITY_FIX_LANDED_ON + timedelta(days=1)
    _init_repo(tmp_path)
    _write_census_plan(
        tmp_path, "authorized.md", created=today_marker.isoformat(), execution_authorized_by="PM"
    )
    common_dir = tmp_path / ".git"

    result = _plan_list_orphaned({"threshold_days": 14}, repo_root=common_dir)

    assert result["authorized_orphan"] == [
        {"path": "docs/plans/authorized.md", "execution_authorized_by": "PM"}
    ]


# ---------------------------------------------------------------------------
# Reverse direction (C3, AC7/AC16) — baton -> plan dangling-pointer detector.
# Real positive planted and observed BEFORE the assertion is written, per
# AC16, for the deliverable_id form — the sole join key since 2026-08-04's
# C12/R2 retirement of the path-pointer secondary key (see the tests below
# asserting a path-line pointer alone produces no finding at all).
# ---------------------------------------------------------------------------


def test_dangling_baton_reference_path_pointer_alone_is_not_reported(tmp_path):
    """C12/R2: a body-line path pointer with no `deliverable_id` is no
    longer a join key the AC7 detector consults at all — retired alongside
    `resolve_plan_owner`'s secondary key, not merely made non-blocking."""
    _init_repo(tmp_path)
    _write_handoff(
        tmp_path,
        "h-dangling-path.md",
        "open",
        body="**Plan:** `docs/plans/never-existed.md`\n",
    )

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == []


def test_dangling_baton_reference_deliverable_id_does_not_resolve(tmp_path):
    _init_repo(tmp_path)
    _write_handoff(tmp_path, "h-dangling-dlv.md", "open", deliverable_id="dlv-no-such-plan")

    # No plan anywhere carries this deliverable_id.
    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == [
        {
            "handoff": "state/handoffs/h-dangling-dlv.md",
            "reference": "dlv-no-such-plan",
            "reference_kind": "deliverable_id",
        }
    ]


def test_dangling_baton_reference_deliverable_id_resolves_against_terminal_plan(tmp_path):
    """A dangling positive needs a NEGATIVE too: a deliverable_id that
    resolves against a plan file regardless of that plan's status (even
    terminal/'implemented') must NOT be reported — the reverse check reads
    the deliverable_id index built from ALL plan files, not just the
    orphan-eligible non-terminal population.
    """
    _init_repo(tmp_path)
    _write_census_plan(
        tmp_path, "shipped.md", status="implemented", deliverable_id="dlv-shipped"
    )
    _write_handoff(tmp_path, "h-shipped.md", "open", deliverable_id="dlv-shipped")

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == []


def test_dangling_baton_reference_handoff_with_no_plan_reference_is_not_reported(tmp_path):
    _init_repo(tmp_path)
    _write_handoff(tmp_path, "h-no-ref.md", "open", body="just some notes, no plan pointer\n")

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == []


def test_dangling_baton_reference_deliverable_id_still_checked_alongside_retired_path_line(tmp_path):
    """Retired-secondary-key regression: `deliverable_id` remains the sole
    join key. A handoff carrying an unresolvable `deliverable_id` AND a
    path-line that would itself resolve still reports on the
    `deliverable_id`, since the path-line is no longer consulted at all."""
    _init_repo(tmp_path)
    _write_plan_with_deliverable(tmp_path, "would-resolve.md")
    _write_handoff(
        tmp_path,
        "h-precedence.md",
        "open",
        deliverable_id="dlv-unresolvable",
        body="**Plan:** `docs/plans/would-resolve.md`\n",
    )

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == [
        {
            "handoff": "state/handoffs/h-precedence.md",
            "reference": "dlv-unresolvable",
            "reference_kind": "deliverable_id",
        }
    ]


def test_dangling_baton_reference_no_handoffs_dir(tmp_path):
    _init_repo(tmp_path)

    result = list_orphaned(tmp_path, threshold_days=14)

    assert result["dangling_baton_references"] == []


def test_list_orphaned_skips_undecodable_handoff_and_plan_files(tmp_path):
    """Review: code-reviewer (regraded break-class by EM) — UnicodeDecodeError
    subclasses ValueError, not OSError, so the pre-existing `except OSError`
    guards around read_text did not catch it. A single corrupt/non-UTF-8
    file in state/handoffs/ or docs/plans/ must not crash list_orphaned
    (reached by _read_orphaned_plans() inside brief(cadence) — advisory-only,
    never blocks).
    """
    today = date(2026, 8, 20)
    _init_repo(tmp_path)

    handoffs_dir = tmp_path / "state" / "handoffs"
    handoffs_dir.mkdir(parents=True)
    (handoffs_dir / "bad-handoff.md").write_bytes(b"\xff\xfe---\nstatus: open\n---\n\nbad bytes\n")

    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "bad-plan.md").write_bytes(b"---\nstatus: draft\ncreated: 2026-08-01\n---\n\n\xff\xfe bad bytes\n")
    _write_census_plan(tmp_path, "good.md", created=_POST_CARRY_DATE)

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    # Completes without raising; the corrupt plan is simply omitted from the
    # population (skip-and-continue, same shape as the OSError path), the
    # good plan is still counted, and the corrupt handoff is skipped rather
    # than crashing the owner-resolution / dangling-reference walks.
    assert result["population_count"] == 1
    assert result["dangling_baton_references"] == []


def test_list_dangling_baton_plan_references_direct_unit(tmp_path):
    _init_repo(tmp_path)
    _write_handoff(
        tmp_path,
        "h-direct.md",
        "open",
        deliverable_id="dlv-direct-missing",
    )

    findings = _list_dangling_baton_plan_references(tmp_path, {})

    assert findings == [
        {
            "handoff": "state/handoffs/h-direct.md",
            "reference": "dlv-direct-missing",
            "reference_kind": "deliverable_id",
        }
    ]


# ---------------------------------------------------------------------------
# AC10 — backward-compatibility parity: scan()'s (lines, rc) contract must be
# IDENTICAL before and after C1-C3. Deliberately NOT a captured-baseline-vs-
# live-rerun diff (see plan chunk C5a brief): `_has_recent_real_work_commit`
# reads git log over each plan's own `scope:` paths and `_has_active_baton`
# substring-matches the live state/handoffs/ corpus -- both drift between
# chunks of THIS session for reasons unrelated to the resolver change, so a
# two-different-times comparison is corpus-churn-sensitive by construction
# and would false-fail or false-pass. Instead: load HEAD's module and the
# working-tree module side by side in ONE process and run scan() on the SAME
# docs/plans snapshot, at the SAME instant, under the SAME git state -- immune
# to churn between the two calls because there is no time for anything to
# churn.
# ---------------------------------------------------------------------------

_AC10_MODULE_REL_PATH = "coordinator_core/ops/draft_plan_aging.py"


def _repo_root_for_ac10() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_module_from_file(module_name: str, file_path: Path):
    """Load *file_path* as module *module_name* via spec_from_file_location.

    A distinct module_name (never "coordinator_core.ops.draft_plan_aging")
    keeps this load from clobbering the real module's sys.modules entry --
    the file has no relative imports, so its own absolute imports
    (coordinator_core.ipc etc.) resolve normally regardless of the name it's
    loaded under.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def test_ac10_scan_parity_head_vs_working_tree(tmp_path):
    """AC10: draft_plan_aging.scan()'s exit-code contract (0 clean / 1 stale /
    2 internal error) AND its reported STALE plan-set are identical before
    and after C1-C3.

    Loads HEAD's version of draft_plan_aging.py and the working-tree version
    side by side in this one process, then runs scan() on the real repo's
    docs/plans/ directory through both, back to back, with no intervening
    git-state or wall-clock change between the two calls -- see section
    docstring above for why a captured-baseline-vs-live-rerun shape would be
    corpus-churn-sensitive instead.

    Degenerate case: once this chunk's diff is committed, HEAD IS the
    working tree and there is nothing left to differentially compare --
    this test then skips (trivially satisfied) rather than erroring, so it
    keeps passing green forever after landing instead of needing removal.
    """
    repo_root = _repo_root_for_ac10()
    plans_dir = repo_root / "docs" / "plans"
    assert plans_dir.is_dir()

    worktree_file = repo_root / Path(_AC10_MODULE_REL_PATH)
    worktree_source = worktree_file.read_text(encoding="utf-8")

    head_show = subprocess.run(
        ["git", "show", f"HEAD:{_AC10_MODULE_REL_PATH}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if head_show.returncode != 0:
        pytest.skip(
            f"git show HEAD:{_AC10_MODULE_REL_PATH} failed ({head_show.returncode}): "
            f"{head_show.stderr.strip()} -- cannot establish a pre-C1-C3 baseline to diff against."
        )
    head_source = head_show.stdout

    if head_source == worktree_source:
        pytest.skip(
            "HEAD and the working tree are byte-identical for draft_plan_aging.py "
            "(C1-C3 already committed) -- AC10 parity is vacuously satisfied, "
            "nothing left to differentially compare."
        )

    head_file = tmp_path / "draft_plan_aging_ac10_head.py"
    head_file.write_text(head_source, encoding="utf-8")

    registry_snapshot = dict(_ipc._REGISTRY)
    old_cwd = os.getcwd()
    try:
        head_module = _load_module_from_file("draft_plan_aging_ac10_head", head_file)
        worktree_module = _load_module_from_file(
            "draft_plan_aging_ac10_worktree", worktree_file
        )

        today = date.today()
        os.chdir(repo_root)
        # Back-to-back in the same process, same cwd, same instant, same
        # docs/plans snapshot -- the whole point is there is no window
        # between these two calls for the corpus to move under us.
        head_result = head_module.scan(str(plans_dir), today=today)
        worktree_result = worktree_module.scan(str(plans_dir), today=today)
    finally:
        os.chdir(old_cwd)
        _ipc._REGISTRY.clear()
        _ipc._REGISTRY.update(registry_snapshot)

    assert worktree_result == head_result, (
        f"AC10 violated: scan() diverged between HEAD and working tree.\n"
        f"HEAD:       {head_result!r}\n"
        f"working tree: {worktree_result!r}"
    )


def test_ac10_cli_end_to_end_exit_code_sanity(tmp_path):
    """Single CLI-level sanity check (not the sole AC10 evidence -- see the
    in-process parity test above): the trampoline still returns one of its
    contractual 0/1/2 codes on a real invocation against a synthetic
    directory, and never the reserved transport code 3.
    """
    _init_repo(tmp_path)
    _write_and_commit_plan(tmp_path, "cli-sanity.md", "draft", commit_days_ago=1, today=date.today())

    repo_root = _repo_root_for_ac10()
    result = subprocess.run(
        [sys.executable, str(repo_root / "coordinator" / "bin" / "draft-plan-aging.py"), str(tmp_path / "docs" / "plans")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={**os.environ, "MAKIMA_ROOT": str(repo_root)},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    assert result.returncode in (0, 1, 2), (
        f"unexpected exit code {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Status-partition totality and disjointness. DoE ruling 80b0b29fb struck
# "landed" from PLAN_ORPHAN_TERMINAL_STATUS; a value that is neither terminal
# NOR listed in _KNOWN_NON_TERMINAL_PLAN_STATUSES falls into the
# unrecognized_status bucket, which is the false-report class that made every
# "approved" plan read as unrecognized. The first test below asserts the
# partition stays total over the schema enum, so striking or adding a status
# cannot silently reopen the unrecognized-status defect. Totality alone does
# not catch a status *moved* between the two sets while remaining a member of
# both (e.g. "landed" re-added to PLAN_ORPHAN_TERMINAL_STATUS while still
# listed in _KNOWN_NON_TERMINAL_PLAN_STATUSES) -- that value is still in "at
# least one" bucket, so it would pass a totality-only check. The second
# assertion below closes that gap by asserting the two sets are disjoint; the
# regression test that follows is the load-bearing guard against re-adding
# "landed" specifically.
# ---------------------------------------------------------------------------


def test_every_schema_status_is_terminal_or_known_non_terminal():
    import json

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "frontmatter"
            / "schemas"
            / "plan.schema.json"
        ).read_text(encoding="utf-8")
    )
    enum = schema["properties"]["status"]["enum"]

    unpartitioned = [
        s
        for s in enum
        if s not in PLAN_ORPHAN_TERMINAL_STATUS
        and s not in _draft_plan_aging._KNOWN_NON_TERMINAL_PLAN_STATUSES
    ]

    assert unpartitioned == [], (
        "schema status value(s) in neither bucket -- they would be reported as "
        f"unrecognized_status: {unpartitioned}"
    )

    double_partitioned = sorted(
        PLAN_ORPHAN_TERMINAL_STATUS
        & _draft_plan_aging._KNOWN_NON_TERMINAL_PLAN_STATUSES
    )

    assert double_partitioned == [], (
        "schema status value(s) in BOTH buckets -- ambiguously terminal and "
        f"non-terminal at once: {double_partitioned}"
    )


def test_list_orphaned_landed_plan_is_population_not_terminal_not_unrecognized(tmp_path):
    """`landed` is non-terminal per plan.schema.json (chunk code on the branch,
    spine rows still open), so its plan stays in the orphan population -- and
    it is a recognized value, so it never reaches the diagnostic bucket."""
    today = date(2026, 8, 20)
    _init_repo(tmp_path)
    _write_census_plan(tmp_path, "landed.md", status="landed", created=_POST_CARRY_DATE)

    result = list_orphaned(tmp_path, threshold_days=14, today=today)

    assert "landed" not in PLAN_ORPHAN_TERMINAL_STATUS
    assert result["unrecognized_status"] == []
    assert result["population_count"] == 1
    assert result["terminal_count"] == 0
