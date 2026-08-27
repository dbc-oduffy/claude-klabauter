"""
coordinator_core.ops.tests.test_plan_tasks_grouping_digest

Tests for the plan.tasks.grouping_digest op — the read-only compute-and-print
surface over `compute_grouping_digest` (coordinator_core/ops/
plan_tasks_grouping_digest.py).

Import guard: coordinator_core.ops.plan_tasks_grouping_digest MUST be
imported at module load time so @register_op("plan.tasks.grouping_digest")
fires and populates _REGISTRY.

Coverage (mapped to this module's own dispatch brief, "Tests" section):
  - digest matches what plan_tasks_mutate's write-time `resolve` guard
    computes for the SAME prospective set — the test that actually matters;
    asserted against the guard's own code path (`plan_tasks_mutate._resolve`
    via the handler, driven end-to-end with a governed plan) never a
    hardcoded value or a hand-re-derived expectation fed back into
    `compute_grouping_digest` directly. Negative half: a digest computed
    for a DIFFERENT (narrower) cut-set is rejected by the same guard.
  - unchanged under row reorder and unrelated-field edits.
  - changes when a row enters/leaves the touched grouping; scoped to that
    grouping only (an unrelated grouping's digest is untouched).
  - fail-loud on: unknown grouping, unknown task id in `cut`, unknown
    disposition in `cut`, missing '## Tasks' spine.
  - CLI entrypoint parity with the handler for the same inputs.
  - read-only: the plan file is byte-identical before/after every call.

Spec backlink: coordinator_core/ops/plan_tasks_grouping_digest.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.plan_tasks_grouping_digest as plan_tasks_grouping_digest  # noqa: F401,E501 — fires @register_op

from coordinator_core.frontmatter.schema_validate import compute_grouping_digest
from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.plan_tasks_grouping_digest import (
    GroupingDigestError,
    _handler,
    compute_prospective_grouping_digest,
    main as cli_main,
)

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_OP_NAME = "plan.tasks.grouping_digest"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.plan_tasks_grouping_digest @register_op did not fire"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root (main worktree).

    Mirrors test_plan_tasks_mutate.py's own helper — the handler receives
    common_dir = <worktree>/.git (P9 worktree derivation).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "plan-tasks-digest-test@claude-klabauter.test")
    _git("config", "user.name", "Plan Tasks Digest Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


def _seed_plan(repo: Path, name: str, content: str) -> Path:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


_PLAN_WITH_TASKS = """\
---
title: "Test Plan"
status: draft
---

# Test Plan

Some intro prose.

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: some/other.py
  queue_scope: project
  deferred: false
  disposition: spun_off
  body: |
    Do the second thing.
- id: C3
  title: Third chunk
  change_kind: script-edit
  surface: some/third.py
  queue_scope: project
  deferred: false
  disposition: wont_do
  body: |
    Do the third thing.
```

## Trailer

Trailing prose after the tasks block.
"""

_PLAN_NO_TASKS_HEADING = """\
---
title: "Test Plan — no heading"
status: draft
---

# Test Plan

Just prose, no Tasks section at all.
"""


# ---------------------------------------------------------------------------
# Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# Matches the write-time guard's own computation (THE test that matters)
# ---------------------------------------------------------------------------


def test_digest_matches_resolve_guards_own_prospective_computation(tmp_path, monkeypatch):
    """A digest THIS MODULE computes for a prospective close of C1 into
    'defer' must be accepted by `plan_tasks_mutate._resolve`'s own guard for
    that same write — proved by driving the guard's real code path
    end-to-end (a governed plan + `_handler(verb=resolve, ...)`), not by
    hand-re-deriving the guard's expected value and feeding it back to
    `compute_grouping_digest` directly. The prior version of this test did
    exactly that: it imported `plan_tasks_mutate` under a `noqa: F401` and
    never called it, so a future divergence between this module's
    `_apply_cut` and `plan_tasks_mutate._resolve`'s own `_prospective_rows`
    closure would have gone uncaught — this test's whole reason to exist.

    Uses `backlogged` rather than `spun_off` (2026-08-05): DoE's ruling gave
    `spun_off` its own ungated grouping, fully exempt from
    `grouping_approvals` — a governed resolve to `spun_off` succeeds
    regardless of the digest on file (verified live: swapping the approved
    `defer` digest here for garbage does not change the outcome), so this
    test's central "digest matches the guard's own prospective computation"
    claim went untested. `backlogged` still maps to `defer` and is still
    checked against the approved digest, so it is the disposition that
    actually exercises the parity this test exists to prove. `spun_off`'s
    exemption already has its own named coverage elsewhere
    (`test_plan_tasks_mutate.py`'s spun_off resolve tests) — this test's
    name is specifically about digest-matching.
    """
    import asyncio

    import coordinator_core.ops.plan_tasks_mutate as plan_tasks_mutate

    def _run(coro):
        return asyncio.run(coro)

    repo = _make_git_repo(tmp_path)

    # Seed the plan first (ungoverned), get its digest via THIS module's
    # real @register_op handler (not the bare compute function) for the
    # SAME cut a governed resolve call below will apply — the value a PM
    # would have been shown and approved.
    pre_plan = _seed_plan(repo, "parity-pre.md", _PLAN_WITH_TASKS)
    cut = [{"id": "C1", "disposition": "backlogged"}]
    digest_result = _handler(
        {"plan_path": str(pre_plan), "grouping": "defer", "cut": cut},
        repo_root=repo / ".git",
    )
    assert digest_result["exit_code"] == 0, digest_result
    digest = digest_result["digest"]

    governed_plan_text = f"""\
---
title: "Test Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  defer:
    status: approved
    approver: pm
    approved_at: 2026-07-29
    pm_utterance: 'yes — C1 belongs in the backlog'
    digest: '{digest}'
---

# Test Plan

Some intro prose.

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
```

## Trailer

Trailing prose after the tasks block.
"""
    plan = _seed_plan(repo, "parity.md", governed_plan_text)

    # `backlogged` delegates to coordinator-harvest-deferrals for its
    # queue/lesson routing (unlike `spun_off`, which this test previously
    # exercised and which writes only the disposition itself) — fake that
    # module out exactly as test_plan_tasks_mutate.py's own backlogged
    # coverage does, since what's under test here is the digest/grouping
    # gate, not the harvest CLI's routing (which has its own suite).
    import types

    queue_dir = repo / "harvest-queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    fake_harvest = types.SimpleNamespace(
        _parse_plan_id=lambda text: "test-plan-id",
        _harvest_key=lambda pid, row_id: f"harvest-key: {pid}:{row_id}",
        _candidate_search_dirs=lambda row: [str(queue_dir)],
        _already_harvested=lambda key, dirs: False,
        _QUEUE_ELIGIBLE_CHANGE_KINDS=frozenset({"script-edit"}),
        _LESSON_PROMOTE_CHANGE_KINDS=frozenset(),
    )

    def _run_queue_append(row, key, dry_run):
        target = queue_dir / f"{row['id']}.yaml"
        target.write_text(f"title: {row['title']}\nevidence: {key}\n", encoding="utf-8")
        return True

    fake_harvest._run_queue_append = _run_queue_append
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    # Drive the guard's REAL code path — plan_tasks_mutate._resolve via its
    # public _handler — with the digest this module produced. If the two
    # constructions ever diverge, this raises (MutateAbort -> exit_code 1)
    # instead of silently passing.
    resolve_result = _run(
        plan_tasks_mutate._handler(
            {
                "verb": "resolve",
                "plan_path": str(plan),
                "id": "C1",
                "disposition": "backlogged",
                "disposition_detail": "moved to the backlog",
                "case_against": "waiting costs little; nothing depends on this landing now",
            },
            repo_root=repo / ".git",
        )
    )
    assert resolve_result["exit_code"] == 0, resolve_result
    assert "disposition: backlogged" in plan.read_text(encoding="utf-8")


def test_digest_computed_for_wrong_cut_set_is_rejected_by_resolve(tmp_path):
    """The negative half: a digest computed for a DIFFERENT cut-set than the
    write actually produces must be refused by `plan_tasks_mutate._resolve`
    — a parity test that can only pass proves little."""
    import asyncio

    import coordinator_core.ops.plan_tasks_mutate as plan_tasks_mutate

    def _run(coro):
        return asyncio.run(coro)

    from coordinator_core.ops.plan_tasks_grouping_digest import (
        compute_prospective_grouping_digest,
    )

    repo = _make_git_repo(tmp_path)

    # Digest approved for closing ONLY C1 — a narrower cut than the write
    # below (which also closes C3) will actually produce. Uses `backlogged`
    # rather than `spun_off` (2026-08-05): DoE's ruling gave `spun_off` its
    # own ungated grouping, so a `defer`-grouping digest computed over
    # `spun_off` rows would hash an empty set — a degenerate, not a
    # narrower, cut-set. `backlogged` remains gated into `defer` and is the
    # disposition this test's "wrong cut-set" assertion actually exercises.
    wrong_cut = [{"id": "C1", "disposition": "backlogged"}]
    stale_digest = compute_prospective_grouping_digest(
        _PLAN_WITH_TASKS, "defer", wrong_cut
    )

    governed_plan_text = f"""\
---
title: "Test Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  defer:
    status: approved
    approver: pm
    approved_at: 2026-07-29
    pm_utterance: 'yes — C1 belongs in its own plan'
    digest: '{stale_digest}'
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: some/path.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
- id: C3
  title: Third chunk
  change_kind: script-edit
  surface: some/third.py
  queue_scope: project
  deferred: false
  body: |
    Do the third thing.
```
"""
    plan = _seed_plan(repo, "wrong-cut.md", governed_plan_text)
    original = plan.read_text(encoding="utf-8")
    backlog_ref = _seed_plan(repo, "2026-07-27-backlog.md", "# Backlog\n")

    # This resolve batch closes BOTH C1 and C3 into 'defer' — a wider
    # membership than the approved digest covers.
    resolve_result = _run(
        plan_tasks_mutate._handler(
            {
                "verb": "resolve",
                "plan_path": str(plan),
                "resolves": [
                    {
                        "id": "C1",
                        "disposition": "backlogged",
                        "disposition_ref": str(backlog_ref.relative_to(repo)),
                        "disposition_detail": "moved to the backlog",
                    },
                    {
                        "id": "C3",
                        "disposition": "backlogged",
                        "disposition_ref": str(backlog_ref.relative_to(repo)),
                        "disposition_detail": "moved to the backlog",
                    },
                ],
            },
            repo_root=repo / ".git",
        )
    )
    assert resolve_result["exit_code"] == 1, resolve_result
    assert "different cut-set" in resolve_result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Stability under reorder / unrelated-field edits
# ---------------------------------------------------------------------------


def test_digest_unchanged_under_row_reorder(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "reorder.md", _PLAN_WITH_TASKS)

    result_a = _handler(
        {"plan_path": str(plan), "grouping": "defer", "cut": []}, repo_root=repo / ".git"
    )

    reordered_source = plan.read_text(encoding="utf-8").replace(
        "- id: C1", "- id: __PLACEHOLDER__"
    )
    # Swap C1 and C2's fence order by round-tripping through the same rows,
    # reversed for the two 'do'/'defer' rows only.
    from coordinator_core.frontmatter.body_blocks import locate_fenced_block
    import yaml

    located = locate_fenced_block(plan.read_text(encoding="utf-8"))
    rows = yaml.safe_load(located.body)
    rows[0], rows[1] = rows[1], rows[0]
    new_body = yaml.safe_dump(rows, sort_keys=False, default_flow_style=False, allow_unicode=True)
    text = plan.read_text(encoding="utf-8")
    text = text.replace(located.body, new_body)
    plan.write_text(text, encoding="utf-8")

    result_b = _handler(
        {"plan_path": str(plan), "grouping": "defer", "cut": []}, repo_root=repo / ".git"
    )

    assert result_a["exit_code"] == 0 and result_b["exit_code"] == 0
    assert result_a["digest"] == result_b["digest"]


def test_digest_unchanged_under_unrelated_field_edit(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "unrelated.md", _PLAN_WITH_TASKS)

    before = _handler(
        {"plan_path": str(plan), "grouping": "defer", "cut": []}, repo_root=repo / ".git"
    )

    text = plan.read_text(encoding="utf-8")
    text = text.replace("Do the second thing.", "Do the second thing, revised.")
    plan.write_text(text, encoding="utf-8")

    after = _handler(
        {"plan_path": str(plan), "grouping": "defer", "cut": []}, repo_root=repo / ".git"
    )

    assert before["exit_code"] == 0 and after["exit_code"] == 0
    assert before["digest"] == after["digest"]


# ---------------------------------------------------------------------------
# Changes on membership change; scoped to the touched grouping only
# ---------------------------------------------------------------------------


def test_digest_changes_when_row_enters_grouping_scoped_to_that_grouping():
    """Cutting C1 into 'defer' changes 'defer'’s digest but leaves 'do' and
    'ruled_out' untouched.

    Uses `backlogged` rather than `spun_off` (2026-08-05): DoE's ruling gave
    `spun_off` its own ungated grouping (`_PLAN_TASKS_GROUPING_BY_DISPOSITION`
    maps it to `'spun_off'`, not `'defer'`), so cutting a row to `spun_off`
    no longer touches `defer`'s membership at all. `backlogged` is the
    disposition that still maps to `defer` and so is the one this test's
    "entering the grouping" assertion can actually exercise.
    """
    before_rows = [
        {"id": "C1", "disposition": "open"},
        {"id": "C2", "disposition": "spun_off"},
        {"id": "C3", "disposition": "wont_do"},
    ]
    after_rows = [
        {"id": "C1", "disposition": "backlogged"},
        {"id": "C2", "disposition": "spun_off"},
        {"id": "C3", "disposition": "wont_do"},
    ]

    assert compute_grouping_digest(before_rows, "defer") != compute_grouping_digest(
        after_rows, "defer"
    )
    assert compute_grouping_digest(before_rows, "do") != compute_grouping_digest(
        after_rows, "do"
    )  # C1 leaves 'do' too — grouping membership is derived, not independent
    assert compute_grouping_digest(before_rows, "ruled_out") == compute_grouping_digest(
        after_rows, "ruled_out"
    )


def test_module_digest_scoped_to_touched_grouping_only(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "scoped.md", _PLAN_WITH_TASKS)

    before_ruled_out = _handler(
        {"plan_path": str(plan), "grouping": "ruled_out", "cut": []}, repo_root=repo / ".git"
    )
    after_ruled_out = _handler(
        {
            "plan_path": str(plan),
            "grouping": "ruled_out",
            "cut": [{"id": "C1", "disposition": "spun_off"}],
        },
        repo_root=repo / ".git",
    )

    assert before_ruled_out["exit_code"] == 0 and after_ruled_out["exit_code"] == 0
    assert before_ruled_out["digest"] == after_ruled_out["digest"], (
        "closing C1 into 'defer' must not perturb 'ruled_out''s digest"
    )


# ---------------------------------------------------------------------------
# Fail-loud
# ---------------------------------------------------------------------------


def test_fail_loud_unknown_grouping(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "badgroup.md", _PLAN_WITH_TASKS)

    result = _handler(
        {"plan_path": str(plan), "grouping": "not-a-grouping", "cut": []},
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "error" in result


def test_fail_loud_unknown_task_id_in_cut(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "badid.md", _PLAN_WITH_TASKS)

    result = _handler(
        {
            "plan_path": str(plan),
            "grouping": "defer",
            "cut": [{"id": "DOES-NOT-EXIST", "disposition": "spun_off"}],
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "DOES-NOT-EXIST" in result["error"]


def test_fail_loud_unknown_disposition_in_cut(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "baddisp.md", _PLAN_WITH_TASKS)

    result = _handler(
        {
            "plan_path": str(plan),
            "grouping": "defer",
            "cut": [{"id": "C1", "disposition": "not-a-real-disposition"}],
        },
        repo_root=repo / ".git",
    )
    assert result["exit_code"] == 1
    assert "not-a-real-disposition" in result["error"]


def test_fail_loud_missing_spine(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "nospine.md", _PLAN_NO_TASKS_HEADING)

    result = _handler(
        {"plan_path": str(plan), "grouping": "defer", "cut": []}, repo_root=repo / ".git"
    )
    assert result["exit_code"] == 1
    assert "error" in result


def test_direct_call_raises_for_unrecognized_id_or_disposition():
    with pytest.raises(GroupingDigestError):
        compute_prospective_grouping_digest(
            _PLAN_WITH_TASKS, "defer", [{"id": "NOPE", "disposition": "spun_off"}]
        )
    with pytest.raises(GroupingDigestError):
        compute_prospective_grouping_digest(
            _PLAN_WITH_TASKS, "defer", [{"id": "C1", "disposition": "bogus"}]
        )
    with pytest.raises(GroupingDigestError):
        compute_prospective_grouping_digest(_PLAN_NO_TASKS_HEADING, "defer", [])


# ---------------------------------------------------------------------------
# Never writes the plan
# ---------------------------------------------------------------------------


def test_never_writes_the_plan(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "readonly.md", _PLAN_WITH_TASKS)
    before = plan.read_bytes()

    _handler(
        {
            "plan_path": str(plan),
            "grouping": "defer",
            "cut": [{"id": "C1", "disposition": "spun_off"}],
        },
        repo_root=repo / ".git",
    )

    assert plan.read_bytes() == before


# ---------------------------------------------------------------------------
# CLI parity
# ---------------------------------------------------------------------------


def test_cli_matches_handler_digest(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "cli.md", _PLAN_WITH_TASKS)

    handler_result = _handler(
        {
            "plan_path": "docs/plans/cli.md",
            "grouping": "defer",
            "cut": [{"id": "C1", "disposition": "spun_off"}],
        },
        repo_root=repo / ".git",
    )
    assert handler_result["exit_code"] == 0

    rc = cli_main(
        [
            "--plan",
            "docs/plans/cli.md",
            "--grouping",
            "defer",
            "--cut",
            "C1:spun_off",
            "--root",
            str(repo),
        ]
    )
    out = capsys.readouterr().out.strip()

    assert rc == 0
    assert out == handler_result["digest"]


def test_cli_path_containment_rejects_escape(tmp_path, capsys):
    repo = _make_git_repo(tmp_path)
    rc = cli_main(
        ["--plan", "../../etc/passwd", "--grouping", "defer", "--root", str(repo)]
    )
    assert rc == 2
