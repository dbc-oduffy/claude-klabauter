"""
coordinator_core.ops.tests.test_plan_tasks_mutate

Tests for the plan.tasks.mutate op (add-task / stamp / resolve verbs).

Import guard: coordinator_core.ops.plan_tasks_mutate MUST be imported at
module load time so @register_op("plan.tasks.mutate") fires and populates
_REGISTRY.

Coverage (mapped to docs/plans/2026-07-10-pcli-need-1-plan-tasks-engine-plane.md
§ C4 and Acceptance Criteria AC5-AC10):
  - add-task happy path (row appended, schema-valid)
  - add-task duplicate-id fail-loud (AC5) — on-disk bytes unchanged after abort
  - add-task on a plan with NO ## Tasks heading (fresh section synthesized)
  - add-task on a plan with an existing empty ## Tasks heading and no
    adjacent fence (fence inserted under the EXISTING heading — F3)
  - stamp multi-id all-applied (AC6)
  - stamp atomicity: one bad id -> zero writes (AC6)
  - schema-invalid row rejected (AC7)
  - splice fidelity: frontmatter + surrounding body byte-identical OUTSIDE
    the fence-body span (AC7)
  - idempotent re-invocation (no write on identical add-task / no-op stamp)
  - path containment (AC10/F0): plan_path outside docs/plans/ rejected,
    exit_code=1, no write
  - sequential read-after-write (F4): two add-task calls in sequence land
    both rows in the final spine
  - CLI-seam end-to-end invocation (AC8)
  - resolve happy path: coded (no pm_approved needed), and closed
    disposition with a pre-stamped pm_approved: true (AC4)
  - resolve refuses a closed disposition lacking the PM's recorded assent —
    no write, and the refusal names no command that would satisfy the gate
    (AC4/D4, as amended by the 2026-07-29 grouping-approval contract). On a
    LEGACY plan that assent is the row's own pm_approved: true; on a
    GOVERNED plan it is the row's grouping reading status: approved with a
    digest matching a fresh recomputation over the membership the write
    produces. (This bullet previously described
    the deleted _PM_APPROVAL_OFFER — a gate that printed its own key, i.e.
    named the exact stamp command that would satisfy it — do not restore
    that wording.)
  - resolve id-not-found and required-field errors, no write
  - stamp refuses a whole batch carrying disposition/disposition_ref/
    disposition_detail — no write, refusal message offers --verb resolve
    (AC4/D4)

Spec backlink: coordinator_core/ops/plan_tasks_mutate.py
Plan: docs/plans/2026-07-10-pcli-need-1-plan-tasks-engine-plane.md § C4
    (add-task/stamp); docs/plans/2026-07-27-plan-line-item-resolution-model.md
    § C4 (resolve verb + stamp reserved-field refusal).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Import guard — MUST precede any test so @register_op fires first.
# ---------------------------------------------------------------------------
import coordinator_core.ops.plan_tasks_mutate as plan_tasks_mutate  # noqa: F401 — fires @register_op

from coordinator_core.ipc import _REGISTRY
from coordinator_core.ops.plan_tasks_mutate import _handler
from coordinator_core.win_portability import no_console_creationflags

# _invoke_cli spawns `python -m coordinator_core.invoke` as a real subprocess.
# That child inherits cwd but NOT pytest's rootdir sys.path insertion, so it
# can only resolve the coordinator_core package when cwd is (or is under) the
# repo root -- from any other cwd it dies with ModuleNotFoundError before it
# can write anything to stdout. Pinning cwd to the repo root derived from this
# file's own path makes the subprocess resolvable regardless of the invoking
# shell's cwd.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_OP_NAME = "plan.tasks.mutate"
assert _OP_NAME in _REGISTRY, (
    f"import guard failed: {_OP_NAME!r} not in _REGISTRY — "
    "coordinator_core.ops.plan_tasks_mutate @register_op did not fire"
)

# The CLI-seam end-to-end coverage (AC8) genuinely spawns
# `python -m coordinator_core.invoke` as a real subprocess to prove the wire
# path, not just the in-process handler; the path-containment fixture also
# needs a real git repo since plan_tasks_mutate resolves paths against actual
# repo state.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously — no pytest-asyncio needed."""
    return asyncio.run(coro)


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its root (main worktree).

    The caller passes repo_root / ".git" as ``repo_root`` to the handler
    (P9 WORKTREE DERIVATION: handler receives common_dir = <worktree>/.git).
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "plan-tasks-test@claude-klabauter.test")
    _git("config", "user.name", "Plan Tasks Test")
    _git("config", "commit.gpgsign", "false")

    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


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
```

## Trailer

Trailing prose after the tasks block.
"""

def _governed_plan(*, status: str = "approved", digest: str | None = None) -> str:
    """_PLAN_WITH_TASKS under the grouping-approval contract.

    The digest defaults to the one covering C1 AFTER it is closed as
    backlogged — i.e. the cut-set the PM would have been shown and approved,
    which is what resolve checks its prospective write against.

    Review: code-reviewer (Finding 2) -- docstring was stale, still naming
    spun_off, though the digest computation below already used backlogged.
    """
    from coordinator_core.frontmatter.schema_validate import compute_grouping_digest

    if digest is None:
        digest = compute_grouping_digest([{"id": "C1", "disposition": "backlogged"}], "defer")
    return f"""\
---
title: "Test Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  defer:
    status: {status}
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


_PLAN_EMPTY_TASKS_HEADING = """\
---
title: "Test Plan — empty heading"
status: draft
---

# Test Plan

Intro prose.

## Tasks

## Trailer

Trailing prose.
"""

_PLAN_NO_TASKS_HEADING = """\
---
title: "Test Plan — no heading"
status: draft
---

# Test Plan

Just prose, no Tasks section at all.
"""


def _seed_plan(repo: Path, name: str, content: str) -> Path:
    path = repo / "docs" / "plans" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _valid_task(task_id: str = "C2") -> dict:
    return {
        "id": task_id,
        "title": "Second chunk",
        "change_kind": "script-edit",
        "surface": "some/other.py",
        "queue_scope": "project",
        "deferred": False,
        "body": "Do the second thing.\n",
    }


# ---------------------------------------------------------------------------
# Registry assertion
# ---------------------------------------------------------------------------


def test_op_registered():
    """plan.tasks.mutate must appear in _REGISTRY (import guard validates at load)."""
    assert _OP_NAME in _REGISTRY


# ---------------------------------------------------------------------------
# add-task happy path
# ---------------------------------------------------------------------------


def test_add_task_happy_path(tmp_path):
    """add-task appends a schema-valid row; result is applied=True, row present."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "happy.md", _PLAN_WITH_TASKS)

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C2")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "message" in result

    text = plan.read_text(encoding="utf-8")
    assert "id: C2" in text
    assert "id: C1" in text, "original row must survive the append"
    # Trailer prose must survive byte-identical (outside the fence-body span).
    assert "## Trailer\n\nTrailing prose after the tasks block.\n" in text


# ---------------------------------------------------------------------------
# add-task duplicate-id fail-loud (AC5)
# ---------------------------------------------------------------------------


def test_add_task_duplicate_id_fails_loud_no_write(tmp_path):
    """Duplicate id aborts via MutateAbort — no write; on-disk bytes unchanged."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "dup.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    dup_task = _valid_task("C1")  # C1 already exists in the fixture
    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": dup_task},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "duplicate" in result.get("error", "").lower()

    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged after a duplicate-id abort"
    )


# ---------------------------------------------------------------------------
# add-task on a plan with NO ## Tasks heading at all
# ---------------------------------------------------------------------------


def test_add_task_no_tasks_heading_synthesizes_section(tmp_path):
    """A plan with no '## Tasks' heading gets a fresh section + fence synthesized."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "no-heading.md", _PLAN_NO_TASKS_HEADING)

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C1")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert text.count("## Tasks") == 1, (
        f"expected exactly one '## Tasks' heading to be synthesized; got:\n{text}"
    )
    assert "```yaml plan-tasks" in text
    assert "id: C1" in text
    # Prior prose must survive byte-identical (prefix preserved).
    assert text.startswith(_PLAN_NO_TASKS_HEADING.rstrip("\n"))


# ---------------------------------------------------------------------------
# add-task on a plan with an existing EMPTY ## Tasks heading, no adjacent fence
# ---------------------------------------------------------------------------


def test_add_task_empty_heading_inserts_fence_under_existing_heading(tmp_path):
    """A '## Tasks' heading with no adjacent fence gets the fence inserted under
    the EXISTING heading — no second heading is created (F3)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "empty-heading.md", _PLAN_EMPTY_TASKS_HEADING)

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C1")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert text.count("## Tasks") == 1, (
        f"no second '## Tasks' heading may be created; got:\n{text}"
    )
    assert "```yaml plan-tasks" in text
    assert "id: C1" in text
    # The '## Trailer' section (post-existing) must survive.
    assert "## Trailer" in text
    assert "Trailing prose." in text

    # The fence must appear AFTER the (single) '## Tasks' heading and BEFORE '## Trailer'.
    tasks_idx = text.index("## Tasks")
    fence_idx = text.index("```yaml plan-tasks")
    trailer_idx = text.index("## Trailer")
    assert tasks_idx < fence_idx < trailer_idx


# ---------------------------------------------------------------------------
# stamp multi-id all-applied (AC6)
# ---------------------------------------------------------------------------


_PLAN_TWO_ROWS = """\
---
title: "Test Plan — two rows"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Second.
```
"""


def test_stamp_multi_id_all_applied(tmp_path):
    """stamp updates fields on N ids in a single locked_rmw — all applied."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-ok.md", _PLAN_TWO_ROWS)

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "deferred": True, "pm_approved": True},
                {"id": "C2", "title": "Second chunk (renamed)"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "deferred: true" in text
    assert "pm_approved: true" in text
    assert "Second chunk (renamed)" in text


# ---------------------------------------------------------------------------
# stamp atomicity: one bad id -> zero writes (AC6)
# ---------------------------------------------------------------------------


def test_stamp_atomicity_bad_id_zero_writes(tmp_path):
    """One id-not-found in a multi-id stamp batch aborts the WHOLE batch — no
    writes, including the valid updates in the same batch."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-atomic.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "deferred": True, "pm_approved": True},
                {"id": "C-DOES-NOT-EXIST", "title": "ghost"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "not found" in result.get("error", "").lower()

    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged: the C1 update must NOT have been "
        "applied despite being valid — the batch is all-or-nothing"
    )


def test_stamp_duplicate_id_in_batch_fails_loud_no_write(tmp_path):
    """Review: code-reviewer (F2) — a duplicate id within one `updates` batch
    aborts the whole batch (fail-loud, mirrors add-task's dup discipline) —
    no writes, not last-write-wins."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-dup-batch.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "deferred": True},
                {"id": "C1", "pm_approved": True},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "duplicate" in result.get("error", "").lower()

    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged: a duplicate id in the batch must abort "
        "before any row mutation begins"
    )


# ---------------------------------------------------------------------------
# schema-invalid row rejected (AC7)
# ---------------------------------------------------------------------------


def test_add_task_schema_invalid_row_rejected(tmp_path):
    """A row missing a required field (e.g. 'surface') is rejected; no write."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "invalid-row.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    bad_task = {
        "id": "C-BAD",
        "title": "Missing surface + change_kind",
        # 'change_kind' and 'surface' are required by the schema — omitted.
    }

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": bad_task},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original


def test_stamp_schema_invalid_result_rejected(tmp_path):
    """A stamp that produces a schema-invalid resulting row (bad change_kind
    enum value) is rejected; no write."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-invalid.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "change_kind": "not-a-real-enum-value"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Splice fidelity (AC7): frontmatter + surrounding body byte-identical
# OUTSIDE the fence-body span.
# ---------------------------------------------------------------------------


def test_splice_fidelity_outside_span_byte_identical(tmp_path):
    """Frontmatter, intro prose, and trailing prose are byte-identical after
    a mutation; only the fence-body span content changes."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "splice.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C9")},
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result

    new_text = plan.read_text(encoding="utf-8")

    # Everything before the fence opener must be byte-identical.
    fence_opener = "```yaml plan-tasks\n"
    orig_prefix = original[: original.index(fence_opener) + len(fence_opener)]
    new_prefix = new_text[: new_text.index(fence_opener) + len(fence_opener)]
    assert orig_prefix == new_prefix, "content BEFORE the fence body must be byte-identical"

    # Everything after the fence closer ('\n```\n\n## Trailer...') must be
    # byte-identical (the fence closer marker itself, plus all trailing prose).
    fence_closer = "\n```\n\n## Trailer"
    orig_suffix = original[original.index(fence_closer):]
    new_suffix = new_text[new_text.index(fence_closer):]
    assert orig_suffix == new_suffix, "content AFTER the fence body must be byte-identical"


# ---------------------------------------------------------------------------
# Idempotent re-invocation
# ---------------------------------------------------------------------------


def test_add_task_idempotent_no_write_on_identical_row(tmp_path):
    """Re-invoking add-task with the SAME row that is already present is a
    duplicate-id abort (fail-loud, no write) — distinct from a semantic no-op,
    per F6's note. This test asserts the file remains byte-unchanged either way."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "idem-add.md", _PLAN_WITH_TASKS)

    task = _valid_task("C-IDEM")
    first = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": task},
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True
    after_first = plan.read_text(encoding="utf-8")

    # Re-invoke with the identical row — id already present -> duplicate abort.
    second = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": task},
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 1, second
    assert second["applied"] is False

    after_second = plan.read_text(encoding="utf-8")
    assert after_second == after_first, (
        "second identical add-task must not have mutated the file"
    )


def test_stamp_no_op_no_write(tmp_path):
    """A stamp whose updates set fields to their CURRENT values re-serializes
    to byte-identical text (given the pinned deterministic dump options), so
    locked_rmw's byte-identity skip results in no actual write to disk — even
    though the handler's own `applied` bookkeeping (set inside the mutate
    closure before locked_rmw's write-skip decision) reports True either way.
    The load-bearing assertion is the on-disk mtime/content invariant, not the
    `applied` field (F6: idempotency is a property of locked_rmw's write path,
    not of this handler's return value)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-noop.md", _PLAN_TWO_ROWS)

    # First stamp: actually changes a field.
    first = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "deferred": True, "pm_approved": True}],
        },
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True
    after_first = plan.read_text(encoding="utf-8")
    mtime_after_first = plan.stat().st_mtime_ns

    # Second stamp: re-apply the SAME values — must reproduce byte-identical
    # new_text, so locked_rmw skips the write (content and mtime unchanged).
    second = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "deferred": True, "pm_approved": True}],
        },
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 0, second
    # Review: code-reviewer — make the docstring's "applied reports True
    # either way" claim executable rather than asserted-but-unchecked (F4).
    assert second["applied"] is True, second

    after_second = plan.read_text(encoding="utf-8")
    assert after_second == after_first
    assert plan.stat().st_mtime_ns == mtime_after_first, (
        "re-applying identical field values must not touch the file on disk "
        "(locked_rmw byte-identity skip) — mtime must be unchanged"
    )


def test_stamp_no_op_idempotent_for_multiline_body_with_blank_line(tmp_path):
    """Review: code-reviewer (F5) — _dump_rows re-serialization idempotency
    must also hold for a `body` value shaped to stress PyYAML's scalar-style
    selection: an embedded blank line plus trailing whitespace on a line.
    Extends the test_stamp_no_op_no_write pattern to a less "friendly" body
    string, to catch _dump_rows picking a different block/quote style on
    re-serialization (which would break the module's claimed idempotency
    guarantee, F6)."""
    repo = _make_git_repo(tmp_path)
    tricky_body = "First line with trailing spaces.   \n\nThird line after an embedded blank.\n"
    plan_content = f"""\
---
title: "Test Plan — tricky body"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    Do the first thing.
```
"""
    plan = _seed_plan(repo, "stamp-tricky-body.md", plan_content)

    # First stamp: set body to the tricky multi-line value (embedded blank
    # line + trailing whitespace on a line).
    first = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "body": tricky_body}],
        },
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True
    after_first = plan.read_text(encoding="utf-8")
    mtime_after_first = plan.stat().st_mtime_ns

    # Second stamp: re-apply the SAME tricky body value — must reproduce
    # byte-identical new_text, so locked_rmw skips the write.
    second = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "body": tricky_body}],
        },
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 0, second
    assert second["applied"] is True

    after_second = plan.read_text(encoding="utf-8")
    assert after_second == after_first, (
        "re-applying an identical multi-line body (embedded blank line + "
        "trailing whitespace) must round-trip byte-identically through "
        "_dump_rows — a scalar-style flip on re-serialization would break "
        "idempotency for this shape"
    )
    assert plan.stat().st_mtime_ns == mtime_after_first, (
        "re-applying identical field values must not touch the file on disk "
        "(locked_rmw byte-identity skip) — mtime must be unchanged"
    )


_PLAN_THREE_ROWS_MULTILINE_BODY = """\
---
title: "Test Plan — three rows, multiline bodies"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First paragraph of C1.

    Second paragraph of C1.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    First paragraph of C2.

    Second paragraph of C2.
- id: C3
  title: Third chunk
  change_kind: script-edit
  surface: c.py
  queue_scope: project
  deferred: false
  body: |
    First paragraph of C3.

    Second paragraph of C3.
```
"""


def test_stamp_one_row_preserves_literal_block_style_on_other_rows(tmp_path):
    """Regression (2026-08-21, row-body-flattening defect): stamping ONE row
    of a multi-row spine must not flatten every OTHER row's multi-line
    `body:` field from a literal block scalar (`body: |`) into a
    double-quoted single line with embedded `\\n` escapes.

    Asserts on the emitted TEXT, not the parsed structure — the parsed
    structure was always correct (safe_load round-trips either style to the
    same string), which is exactly why no prior test caught this: the old
    serializer only ever picked the WRONG scalar style, never the wrong
    content."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-preserves-literal-style.md", _PLAN_THREE_ROWS_MULTILINE_BODY)

    before_text = plan.read_text(encoding="utf-8")
    literal_count_before = before_text.count("body: |")
    assert literal_count_before == 3, "fixture sanity: all three rows start as literal blocks"

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C2", "deferred": True, "pm_approved": True}],
        },
        repo_root=repo / ".git",
    ))
    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    after_text = plan.read_text(encoding="utf-8")
    literal_count_after = after_text.count("body: |")
    assert literal_count_after == 3, (
        "stamping one row must not change how many rows emit their body as a "
        f"literal block scalar — before={literal_count_before} "
        f"after={literal_count_after}, text:\n{after_text}"
    )
    # The two rows this stamp did NOT touch keep their body's literal-block
    # source text verbatim (not merely "some literal block somewhere").
    assert "First paragraph of C1.\n\n    Second paragraph of C1." in after_text
    assert "First paragraph of C3.\n\n    Second paragraph of C3." in after_text
    # No row's body regressed to a quoted single-line-with-\\n-escapes form.
    assert "\\n" not in after_text


@pytest.mark.parametrize(
    "label,body",
    [
        ("lf-trailing-newline", "para one.\npara two.\n"),
        ("crlf", "para one.\r\npara two.\r\n"),
        ("no-trailing-newline", "para one.\npara two."),
        ("two-trailing-newlines", "para one.\npara two.\n\n"),
        ("whitespace-only-line", "para one.\n   \npara two.\n"),
        ("trailing-whitespace-on-line", "para one.   \npara two.\n"),
    ],
)
def test_dump_rows_round_trips_every_body_shape_byte_for_byte(label, body):
    """`_dump_rows` must return the body it was given, for every newline and
    whitespace shape — whichever scalar style it picks to get there.

    The style fallback is the point. `_plan_tasks_str_representer` can only
    use a literal block (`|`) for bodies YAML can represent that way; a body
    carrying CRLF, a whitespace-only line, or trailing whitespace on a line
    falls back to the quoted form, because a literal block would not survive
    the round trip. That fallback is CONTENT-SAFE and this test is what says
    so — it was measured, not assumed, and it is pinned here because the two
    plausible failure modes are invisible in review: PyYAML choosing a
    chomping indicator that drops or adds a trailing newline, and a CRLF body
    normalizing to LF. Both would be silent content mutation of prose in rows
    the caller never named, which is the defect class the literal-style fix
    exists to close.

    Style is deliberately NOT asserted. Which shapes earn a block scalar is
    PyYAML's call and may shift across versions; that they come back byte-
    identical is the contract.
    """
    import yaml

    rows = [{"id": "C1", "body": body}]

    restored = yaml.safe_load(plan_tasks_mutate._dump_rows(rows))[0]["body"]

    assert restored == body, (
        f"{label}: body did not survive the dump/load round trip — "
        f"expected {body!r}, got {restored!r}"
    )


# ---------------------------------------------------------------------------
# Path containment (AC10 / F0)
# ---------------------------------------------------------------------------


def test_rejects_out_of_tree_absolute_path(tmp_path):
    """An absolute plan_path outside docs/plans/ is rejected; target untouched."""
    repo = _make_git_repo(tmp_path)
    outside = tmp_path / "outside" / "secret.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(_PLAN_WITH_TASKS, encoding="utf-8")
    original = outside.read_text(encoding="utf-8")

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(outside), "task": _valid_task("C2")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert outside.read_text(encoding="utf-8") == original


def test_rejects_traversal_path(tmp_path):
    """A '../' traversal plan_path escaping docs/plans/ is rejected; target
    (if it exists) is untouched.

    Review: code-reviewer — containment here relies entirely on `_resolve_path`
    joining the relative path onto worktree and then `Path.resolve()`
    collapsing the `..` segments before `contained_path` checks containment;
    there is no explicit parse-time `..` string reject (contrast with the JS
    oracle's `memo-transition.js` explicit dotdot-separator regex reject). This
    op relies solely on post-resolve containment (F7) — a legitimate but
    different design choice worth noting for a reader comparing security
    posture against memo_transition.py.
    """
    repo = _make_git_repo(tmp_path)
    secret = repo / "secret.md"
    secret.write_text(_PLAN_WITH_TASKS, encoding="utf-8")
    original = secret.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "add-task",
            "plan_path": "docs/plans/../../secret.md",
            "task": _valid_task("C2"),
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert secret.read_text(encoding="utf-8") == original


def test_rejects_out_of_tree_path_nonexistent_target(tmp_path):
    """An out-of-tree plan_path pointing at a NON-existent file is still
    rejected on containment grounds (never reaches the FileNotFoundError path),
    and no file is created at the target."""
    repo = _make_git_repo(tmp_path)
    outside = tmp_path / "outside" / "does-not-exist.md"

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(outside), "task": _valid_task("C2")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert not outside.exists(), "containment-rejected path must not be created"


# ---------------------------------------------------------------------------
# Sequential read-after-write (F4)
# ---------------------------------------------------------------------------


def test_sequential_add_task_calls_both_land_in_final_spine(tmp_path):
    """Two add-task calls in sequence, distinct ids, both land in the final
    spine — proves the second locked_rmw invocation reads the first's
    committed bytes, not a stale snapshot."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "sequential.md", _PLAN_WITH_TASKS)

    first = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C-SEQ-A")},
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True

    second = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C-SEQ-B")},
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 0, second
    assert second["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "id: C1" in text
    assert "id: C-SEQ-A" in text
    assert "id: C-SEQ-B" in text


# ---------------------------------------------------------------------------
# CLI seam end-to-end (AC8)
# ---------------------------------------------------------------------------


def _invoke_cli(op: str, params: dict, repo: Path) -> subprocess.CompletedProcess:
    """Run ``python -m coordinator_core.invoke <op> '<json-params>' --repo <repo>``."""
    env = {**os.environ}
    return subprocess.run(
        [
            sys.executable,
            "-m", "coordinator_core.invoke",
            op,
            json.dumps(params),
            "--repo", str(repo),
        ],
        capture_output=True,
        text=True,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )


def test_cli_seam_add_task_end_to_end(tmp_path):
    """plan.tasks.mutate add-task via the real invoke path: exit 0, valid
    JSON-RPC result, and the plan file is mutated on disk (AC8)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "cli-seam.md", _PLAN_WITH_TASKS)

    params = {
        "verb": "add-task",
        "plan_path": str(plan),
        "task": _valid_task("C-CLI"),
    }

    result = _invoke_cli("plan.tasks.mutate", params, repo)
    assert result.returncode == 0, (
        f"invoke exited {result.returncode} (expected 0).\n"
        f"stdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )

    response = json.loads(result.stdout)
    assert "error" not in response, f"unexpected JSON-RPC error: {response}"
    assert "result" in response, f"expected 'result' key in response: {response}"

    op_result = response["result"]
    assert op_result.get("exit_code") == 0, op_result
    assert op_result.get("applied") is True, op_result

    text = plan.read_text(encoding="utf-8")
    assert "id: C-CLI" in text


# ---------------------------------------------------------------------------
# resolve verb (AC4, D4) — plan_tasks_mutate.py resolve/stamp-refusal
# ---------------------------------------------------------------------------


def test_resolve_coded_no_pm_approved_needed(tmp_path):
    """resolve --disposition coded writes disposition + ref + detail with no
    pm_approved required — D3: coded is evidence of work done, not a scope
    decision."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-coded.md", _PLAN_WITH_TASKS)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "coded",
            "disposition_ref": "abc1234",
            "disposition_detail": "shipped in abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert "disposition_ref: abc1234" in text
    assert "disposition_detail: shipped in abc1234" in text


def test_resolve_governed_admits_on_approved_grouping(tmp_path):
    """On a governed plan the per-row pm_approved boolean is absent by
    design, and an approved grouping whose digest covers this write is what
    authorizes it.

    C12: spun_off's disposition_ref is a computed/verified producer, so the
    referenced spinoff artifact must exist on disk before this call."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-governed-ok.md", _governed_plan())
    _seed_plan(repo, "2026-07-27-spinoff.md", "# Spinoff\n")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/2026-07-27-spinoff.md",
            "disposition_detail": "moved to spinoff plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert "disposition: spun_off" in plan.read_text(encoding="utf-8")


def test_add_task_governed_admits_row_without_pm_approved(tmp_path):
    """add-task must resolve `governed` from the same plan frontmatter
    `resolve` does, so a governed plan's rows are validated against the
    governed schema (pm_approved not required) rather than the legacy one.

    Regression for cross-repo/archive/2026-08-13-doe-claude-em-plan-tasks-
    mutate-governed-flag-asymmetry.md: prior to the fix, add-task always
    validated with governed=False, so a backlogged/wont_do row with no
    pm_approved field failed the legacy schema's required-field check on a
    plan `resolve` would have accepted."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "add-task-governed-ok.md", _governed_plan())

    result = _run(_handler(
        {
            "verb": "add-task",
            "plan_path": str(plan),
            "task": {
                "id": "C2",
                "title": "Second chunk",
                "change_kind": "script-edit",
                "surface": "some/other.py",
                "queue_scope": "project",
                "deferred": False,
                "body": "Do the second thing.\n",
                "disposition": "backlogged",
                "disposition_ref": "state/backlog/example.yaml",
                "disposition_detail": "deferred by the same PM ruling",
                "case_against": "deferred by the same PM ruling",
            },
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert "id: C2" in plan.read_text(encoding="utf-8")


def test_stamp_governed_uses_governed_schema(tmp_path):
    """stamp must resolve `governed` the same way add-task/resolve do —
    stamping a non-reserved field on a governed-plan row that already
    carries `disposition: backlogged` (and no pm_approved) must not be
    revalidated against the legacy pm_approved-required schema.

    Regression for cross-repo/archive/2026-08-13-doe-claude-em-plan-tasks-
    mutate-governed-flag-asymmetry.md."""
    repo = _make_git_repo(tmp_path)
    plan_text = _governed_plan().replace(
        "  body: |\n    Do the first thing.\n",
        "  body: |\n    Do the first thing.\n  disposition: backlogged\n"
        "  disposition_ref: state/backlog/example.yaml\n"
        "  disposition_detail: deferred by the same PM ruling\n"
        "  case_against: deferred by the same PM ruling\n",
    )
    plan = _seed_plan(repo, "stamp-governed-ok.md", plan_text)

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "title": "First chunk (renamed)"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert "title: First chunk (renamed)" in plan.read_text(encoding="utf-8")


def test_resolve_governed_refuses_pending_grouping(tmp_path):
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-governed-pending.md", _governed_plan(status="pending"))
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "moved to the backlog",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    error = result.get("error", "")
    assert "pending" in error.lower()
    assert "--verb stamp" not in error, "refusal must not print a command"
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_governed_refuses_stale_digest(tmp_path):
    """A digest approved over a different cut-set than this write produces.

    This is the widened-cut-set case: the PM approved something, then the
    membership changed. Because the digest covers ONLY (row id, disposition)
    pairs, the mismatch is always substantive — there is no reformat that
    could have caused it and nothing to re-stamp past.
    """
    repo = _make_git_repo(tmp_path)
    stale = "sha256:" + "0" * 64
    plan = _seed_plan(repo, "resolve-governed-stale.md", _governed_plan(digest=stale))
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "moved to the backlog",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    error = result.get("error", "")
    assert "cut-set" in error.lower()
    assert "--verb stamp" not in error, "refusal must not print a command"
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_closed_disposition_refuses_without_pm_approved(tmp_path):
    """resolve --disposition backlogged on a LEGACY plan whose row has no
    pm_approved: true is refused (MutateAbort) — no write, and the refusal
    directs the author to the PM WITHOUT naming a command that would satisfy
    the gate (D4, as amended by the 2026-07-29 grouping-approval contract).

    Uses `backlogged` rather than `spun_off` (2026-08-05): DoE's ruling
    relaxed `spun_off`'s pm_approved requirement unconditionally, so it no
    longer exercises this gate in either legacy or governed mode — see
    `_PLAN_TASKS_PM_APPROVAL_GATED_DISPOSITIONS`. `backlogged` remains
    PM-gated and is the disposition this test's assertions actually protect.

    This assertion was inverted on 2026-07-29. It previously required the
    refusal to name the stamp-first path — "refusal must name the concrete
    unblock: stamp pm_approved: true first". That was the defect, not the
    feature: the field being checked was one the same agent could set one
    command earlier, so the gate printed its own key and the test held it
    there. DoE's contract makes the requirement explicit — whatever refuses
    must direct the author to ask the PM, and must NOT print a stamp
    command, a CLI invocation, or any other means of satisfying the field
    without one.
    """
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-refuse.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "moved to the backlog",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "pm_approved" in error.lower() or "ratif" in error.lower(), error
    assert "resolve" in error.lower()
    assert "pm" in error.lower(), "refusal must direct the author to the PM"
    for forbidden in ("--verb stamp", "--updates", "stamp the row"):
        assert forbidden not in error, (
            f"refusal offers a command-shaped remediation ({forbidden!r}) — that is "
            "the gate-prints-its-own-key defect the grouping-approval contract removes"
        )

    # Review: code-reviewer Finding 4 — the legacy refusal previously reused
    # _GROUPING_APPROVAL_HINT verbatim, describing a "grouping" and a
    # pm_utterance field that don't exist anywhere on a LEGACY plan's schema.
    # A legacy plan has no groupings and nowhere to record pm_utterance, so
    # this vocabulary must not appear in its refusal.
    for governed_only_term in ("grouping", "pm_utterance"):
        assert governed_only_term not in error.lower(), (
            f"legacy refusal names {governed_only_term!r}, a governed-plan-only "
            "concept absent from a legacy plan's schema"
        )

    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged after an ungated closed-disposition refusal"
    )


def test_resolve_governed_spun_off_needs_no_grouping_approval(tmp_path):
    """A GOVERNED plan can resolve a spun_off row with NO grouping approval
    present for it AT ALL -- not pending, not absent-but-checked, simply
    never consulted (2026-08-05 EM/PM ruling on the C1/C3 interaction,
    ratifying the predecessor executor's flagged deviation).

    `grouping_approvals` here carries only a 'ruled_out' block (itself
    pending, i.e. would refuse if it were ever checked for this row) and NO
    'spun_off' key -- which the schema could never carry anyway
    (`grouping_approvals.properties` is do/defer/ruled_out only,
    additionalProperties: false, so a 'spun_off' key can never exist). The
    resolve must succeed regardless: `_PLAN_TASKS_PM_APPROVAL_GATED_
    DISPOSITIONS` excludes spun_off in BOTH legacy and governed mode, so it
    never enters `closed_by_grouping` bookkeeping and no grouping lookup is
    attempted for it."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-governed-spun-off-no-approval.md", f"""\
---
title: "Test Plan"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  ruled_out:
    status: pending
    approver: pm
    approved_at: 2026-07-29
    pm_utterance: 'not yet -- still deciding'
    digest: 'sha256:{"0" * 64}'
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
```
""")
    _seed_plan(repo, "2026-07-27-spinoff.md", "# Spinoff\n")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/2026-07-27-spinoff.md",
            "disposition_detail": "moved to spinoff plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "disposition: spun_off" in plan.read_text(encoding="utf-8")


def test_resolve_closed_disposition_succeeds_with_pm_approved(tmp_path):
    """resolve --disposition spun_off succeeds once the row already carries
    pm_approved: true (set via a prior stamp call, since pm_approved is not a
    reserved field) (AC4/D4).

    Uses spun_off rather than backlogged deliberately: backlogged now
    delegates row-routing to coordinator-harvest-deferrals (C5, AC5) and
    computes its own disposition_ref — see the dedicated
    test_resolve_backlogged_* tests below for that path. spun_off exercises
    the SAME generic pm_approved gate with a caller-supplied disposition_ref,
    unaffected by C5's delegation.

    C12 (AC17): spun_off's disposition_ref is now a computed/verified
    producer (`_dispatch_spun_off`) rather than a hand-typed pass-through —
    it must resolve to a real file, so the referenced spinoff artifact is
    seeded on disk before this call (see test_resolve_spun_off_* below for
    the dedicated producer coverage).
    """
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-approved.md", _PLAN_WITH_TASKS)
    spinoff = _seed_plan(repo, "2026-07-27-spinoff.md", "# Spinoff\n")

    stamp_result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "pm_approved": True}],
        },
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/2026-07-27-spinoff.md",
            "disposition_detail": "moved to spinoff plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "disposition: spun_off" in text
    assert "pm_approved: true" in text
    assert spinoff.is_file()


def test_resolve_task_id_not_found_no_write(tmp_path):
    """resolve on a non-existent id aborts — no write."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-missing.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C-DOES-NOT-EXIST",
            "disposition": "coded",
            "disposition_ref": "abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "not found" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_missing_disposition_errors_no_write(tmp_path):
    """resolve without a 'disposition' param is rejected before any lock is
    taken — no write."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-no-disp.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {"verb": "resolve", "plan_path": str(plan), "id": "C1"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original


def test_stamp_refuses_batch_carrying_disposition_field_no_write(tmp_path):
    """stamp refuses the WHOLE batch when any update entry names disposition/
    disposition_ref/disposition_detail — the offer names --verb resolve, and
    no writes are applied (including the otherwise-valid sibling entry in the
    same batch) (AC4/D4)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-reserved.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "title": "harmless rename"},
                {"id": "C2", "disposition": "wont_do"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "").lower()
    assert "resolve" in error
    assert "disposition" in error

    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged: the whole batch is refused, including "
        "the harmless sibling update entry"
    )


def test_stamp_refuses_disposition_ref_field_alone(tmp_path):
    """The reserved-field refusal fires on disposition_ref/disposition_detail
    individually too, not only on 'disposition' itself."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "stamp-reserved-ref.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "disposition_ref": "abc1234"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "resolve" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# resolve --backlogged delegation to coordinator-harvest-deferrals (C5, AC5)
# ---------------------------------------------------------------------------
#
# These tests fully fake the loaded coordinator-harvest-deferrals module (via
# monkeypatching plan_tasks_mutate._load_harvest_module) rather than exercise
# the real CLI subprocess/CLAUDE_KLABAUTER_ROOT resolution — that CLI has its own test
# suite (coordinator/bin/tests/test_plan_tasks_spine_and_harvest.py et al.).
# What's under test HERE is plan_tasks_mutate.py's OWN delegation logic:
# the pm_approved-gate-before-dispatch ordering, the idempotency-key reuse,
# the change_kind-split reuse, disposition_ref computation, and MutateAbort
# on every failure mode. The fake exposes the exact attribute surface
# _dispatch_backlogged actually calls (_parse_plan_id, _harvest_key,
# _candidate_search_dirs, _already_harvested, _run_queue_append,
# _run_lesson_promote, _QUEUE_ELIGIBLE_CHANGE_KINDS,
# _LESSON_PROMOTE_CHANGE_KINDS) and implements a REAL evidence-key scan over
# tmp_path-scoped directories, so idempotency (second call = zero writes) is
# exercised for real rather than asserted by fiat.

_PLAN_WITH_DOCTRINE_TASK = """\
---
title: "Test Plan — doctrine row"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: Universal doctrine row
  change_kind: doctrine-edit
  surface: docs/wiki/some-wiki.md
  queue_scope: project
  deferred: false
  body: |
    Some doctrine-edit body.
```
"""


def _make_fake_harvest_module(repo: Path, plan_id: str = "test-plan-id"):
    """A minimal stand-in for the loaded coordinator-harvest-deferrals
    module, exposing only the attributes _dispatch_backlogged calls.
    _run_queue_append / _run_lesson_promote actually write a small
    `evidence:`-carrying *.yaml file into a directory INSIDE `repo` (the
    worktree root every caller already built via `_make_git_repo`), and
    _already_harvested does a real scan over those directories — so a
    second dispatch call for the same key is genuinely a no-op, not merely
    stubbed to look like one.

    Inside `repo`, not a bare `tmp_path`-scoped sibling directory (fixed
    2026-07-29, DR-103 validator-wiring defect fix): `_to_repo_relative`
    only renders a repo-relative `disposition_ref` when the found evidence
    path is actually UNDER the plan's own worktree — a sibling-of-`repo`
    tmp directory fell OUTSIDE it, so `_to_repo_relative` fell back to the
    raw absolute path, which `_cf_plan_tasks_disposition_shape`'s
    `_is_single_repo_relative_path` (DR-096: no leading slash) now
    correctly rejects now that `_validate_row` actually applies that rule
    (previously it had zero callers, so this shape defect in the fixture
    went unnoticed). Real `coordinator-harvest-deferrals` search dirs
    resolve to real in-worktree paths (`state/improvement-queue/`, etc.) —
    this fixture now mirrors that shape instead of an out-of-tree one.
    """
    queue_dir = repo / "harvest-queue"
    lessons_dir = repo / "harvest-lessons"
    queue_dir.mkdir(parents=True, exist_ok=True)
    lessons_dir.mkdir(parents=True, exist_ok=True)

    write_calls: list = []

    def _scan(key: str, dirs: list) -> str | None:
        for d in dirs:
            for p in Path(d).glob("*.yaml"):
                text = p.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if line.strip().startswith("evidence:") and key in line:
                        return str(p)
        return None

    module = types.SimpleNamespace()
    module._write_calls = write_calls
    module._parse_plan_id = lambda text: plan_id
    module._harvest_key = lambda pid, row_id: f"harvest-key: {pid}:{row_id}"
    module._candidate_search_dirs = lambda row: [str(queue_dir), str(lessons_dir)]
    module._already_harvested = lambda key, dirs: _scan(key, dirs) is not None
    module._QUEUE_ELIGIBLE_CHANGE_KINDS = frozenset(
        {
            "script-edit", "skill-edit", "wiki-append", "wiki-new", "hook-edit",
            "agent-prompt-edit", "doc-edit", "test-edit", "code-edit",
        }
    )
    module._LESSON_PROMOTE_CHANGE_KINDS = frozenset({"doctrine-edit", "snippet-sync-update"})

    def _run_queue_append(row, key, dry_run):
        write_calls.append(("queue", row["id"], key))
        target = queue_dir / f"{row['id']}.yaml"
        target.write_text(f"title: {row['title']}\nevidence: {key}\n", encoding="utf-8")
        return True

    def _run_lesson_promote(row, key, dry_run):
        write_calls.append(("lesson", row["id"], key))
        target = lessons_dir / f"{row['id']}.yaml"
        target.write_text(f"title: {row['title']}\nevidence: {key}\n", encoding="utf-8")
        return True

    module._run_queue_append = _run_queue_append
    module._run_lesson_promote = _run_lesson_promote
    return module


def test_resolve_backlogged_delegates_to_harvest_row_routing(tmp_path, monkeypatch):
    """resolve --disposition backlogged on a queue-eligible change_kind
    delegates to coordinator-harvest-deferrals' own queue route
    (coordinator-queue-append shape), one operation from the caller's point
    of view: the disposition write and the routed entry both land, and the
    routed filename is recorded verbatim in disposition_ref (AC5)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert fake_harvest._write_calls == [("queue", "C1", "harvest-key: test-plan-id:C1")]

    text = plan.read_text(encoding="utf-8")
    assert "disposition: backlogged" in text
    assert "disposition_detail: deferred to backlog" in text
    m = re.search(r"disposition_ref:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m, text
    ref_value = m.group(1).strip('"')
    assert ref_value.endswith("C1.yaml"), ref_value


def test_resolve_backlogged_ignores_caller_supplied_disposition_ref(tmp_path, monkeypatch):
    """A caller-supplied `disposition_ref` for a `backlogged` disposition is
    NOT a pass-through — it is silently overridden by the path
    `_dispatch_backlogged` derives from the harvest CLI's own row-routing
    (see `_dispatch_backlogged` docstring and the `effective_ref` comment
    directly above the write in `_resolve`). This test exists because that
    contract had zero regression coverage: the assertion above only proves
    the derived ref is written; it does not prove a caller's ref is
    disregarded rather than merely never having been supplied. Pinning this
    stops a future refactor from silently turning `_dispatch_backlogged`'s
    derivation back into a pass-through of the caller's value."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-ref-ignored.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "disposition_ref": "state/some-other-queue/BOGUS-CALLER-SUPPLIED-REF.yaml",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "BOGUS-CALLER-SUPPLIED-REF" not in text, (
        "caller-supplied disposition_ref leaked through for a backlogged row: " + text
    )
    m = re.search(r"disposition_ref:\s*(\S+)\s*$", text, re.MULTILINE)
    assert m, text
    ref_value = m.group(1).strip('"')
    assert ref_value.endswith("C1.yaml"), ref_value


_PLAN_WITH_TWO_OPEN_TASKS = """\
---
title: "Test Plan — two open rows"
status: draft
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
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: some/other.py
  queue_scope: project
  deferred: false
  body: |
    Do the second thing.
```
"""


def test_resolve_backlogged_repositions_rather_than_d5_refusing(tmp_path, monkeypatch):
    """Closing C1 (position 1) to `backlogged` while C2 stays `open` in `do`
    at position 2 used to violate D5 (a `defer` row above a `do` row) and
    refuse outright. `_reposition_rows_for_d5` (2026-08-06, D5 ordering-
    deadlock fix) now repositions the whole spine into D5's required order
    as part of the same write instead of refusing -- this test replaces the
    former `test_resolve_d5_refusal_fires_before_any_harvest_dispatch`,
    which asserted the now-superseded refusal behaviour; the harvest
    dispatch this test also exercises still fires exactly once, on the
    correctly-repositioned row."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-d5-reposition.md", _PLAN_WITH_TWO_OPEN_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert fake_harvest._write_calls == [("queue", "C1", "harvest-key: test-plan-id:C1")]

    text = plan.read_text(encoding="utf-8")
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_ordering

    assert check_plan_tasks_ordering(text) is None
    assert _spine_id_order(text) == ["C2", "C1"], _spine_id_order(text)


# The exact D5 ordering-deadlock repro (queue: state/bug-backlog/2026-08-06-
# plan-tasks-mutate-d5-ordering-deadlocks-c223a7208a5a.yaml): C1 and C2 are
# ALREADY coded (reached by ordinary forward-order coding — this is not a
# corrupted spine, it is what an in-progress plan looks like), and C3 is the
# one row still open, sitting LAST — after both coded rows. That is already
# a do-suborder violation (open must sort above coded) under the OLD
# pre-write check, which refused every resolve call on this spine outright,
# regardless of what the call was doing. `disposition_ref` is present on
# both coded rows because `_cf_plan_tasks_disposition_shape` requires one
# for every `coded` row.
_PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL = """\
---
title: "Test Plan — coded prefix, open tail"
status: draft
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
  disposition: coded
  disposition_ref: abc1111
  disposition_detail: "shipped in an earlier session"
  body: |
    Do the first thing.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: some/other.py
  queue_scope: project
  deferred: false
  disposition: coded
  disposition_ref: abc2222
  disposition_detail: "shipped in an earlier session"
  body: |
    Do the second thing.
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

_PLAN_WITH_THREE_OPEN_TASKS = """\
---
title: "Test Plan — three open rows"
status: draft
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
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: some/other.py
  queue_scope: project
  deferred: false
  body: |
    Do the second thing.
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


def _spine_id_order(text: str) -> list:
    """The task spine's row ids, in on-disk fence order — read straight off
    the rendered YAML (`- id: <id>` is always the first line of each row's
    mapping, since `_dump_rows` pins `sort_keys=False` and every row dict is
    built with `id` first), so this checks the ACTUAL written order rather
    than re-deriving it from a parsed structure that could paper over a
    dump-order regression."""
    return re.findall(r"^- id:\s*(\S+)", text, re.MULTILINE)


def test_resolve_last_row_wont_do_after_earlier_coded_repositions_and_succeeds(tmp_path):
    """THE bug repro: closing a spine's last (still-open) row to `wont_do`
    once every earlier row is already `coded` used to deadlock — refused on
    the PRE-write check (the do-suborder rule already called C3-after-C1/C2
    invalid), and refused on the POST-write check if the caller hand-edited
    C3 to the top to dodge that. Removing the now-unsatisfiable pre-write
    precondition and repositioning automatically fixes it: the call now
    succeeds outright, with no hand-edit and no un-resolve verb needed."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-last-row-wont-do-deadlock.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C3", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C3",
            "disposition": "wont_do",
            "disposition_detail": "superseded by C1/C2's landed work",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "disposition: wont_do" in text

    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_ordering

    assert check_plan_tasks_ordering(text) is None, (
        "resulting spine must satisfy D5 (do, then spun_off, then defer, "
        "then ruled_out; open above coded within do)"
    )
    assert _spine_id_order(text) == ["C1", "C2", "C3"], _spine_id_order(text)


def test_resolve_last_row_backlogged_after_earlier_coded_dispatches_and_succeeds(tmp_path, monkeypatch):
    """The same deadlock scenario, closed to `backlogged` instead of
    `wont_do` — the harvest-delegation dispatch must still fire correctly
    once the pre-write deadlock is removed."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-last-row-backlogged-deadlock.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C3", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C3",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert fake_harvest._write_calls == [("queue", "C3", "harvest-key: test-plan-id:C3")]

    text = plan.read_text(encoding="utf-8")
    assert "disposition: backlogged" in text

    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_ordering

    assert check_plan_tasks_ordering(text) is None
    assert _spine_id_order(text) == ["C1", "C2", "C3"], _spine_id_order(text)


def test_resolve_middle_row_wont_do_repositions_preserving_sibling_order(tmp_path):
    """Closing a MIDDLE row (C2 of three open rows) to `wont_do` must move
    it past its still-open siblings into the `ruled_out` grouping, WITHOUT
    shuffling C1 and C3 relative to each other — the stable sort's own
    narrowness guarantee."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-middle-row-wont-do.md", _PLAN_WITH_THREE_OPEN_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C2", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C2",
            "disposition": "wont_do",
            "disposition_detail": "superseded, no longer needed",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_ordering

    assert check_plan_tasks_ordering(text) is None
    assert _spine_id_order(text) == ["C1", "C3", "C2"], (
        "C2 must move to the end (ruled_out sorts after do), and C1/C3 "
        f"(both untouched, both still 'open') must keep their relative "
        f"order: {_spine_id_order(text)}"
    )


def test_resolve_backlogged_doctrine_edit_routes_to_lesson_promote(tmp_path, monkeypatch):
    """A change_kind in {doctrine-edit, snippet-sync-update} routes through
    _run_lesson_promote rather than _run_queue_append — the improvement-queue
    schema rejects those two kinds at project scope (AC5)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-doctrine.md", _PLAN_WITH_DOCTRINE_TASK)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert fake_harvest._write_calls == [("lesson", "C1", "harvest-key: test-plan-id:C1")]


def test_resolve_backlogged_is_idempotent_no_double_write(tmp_path, monkeypatch):
    """Re-running resolve --backlogged on an already-routed row does not
    double-write the queue entry — the (plan_id, row id) idempotency key is
    reused verbatim from coordinator-harvest-deferrals, so the second call's
    dedup scan finds the first call's entry and skips dispatch."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-idem.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    r1 = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))
    assert r1["exit_code"] == 0, r1

    r2 = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))
    assert r2["exit_code"] == 0, r2

    assert len(fake_harvest._write_calls) == 1, (
        "second resolve --backlogged must not re-dispatch to coordinator-"
        "queue-append/coordinator-lesson-promote — idempotency key dedup"
    )

    text = plan.read_text(encoding="utf-8")
    assert text.count("disposition_ref:") == 1


def test_resolve_backlogged_missing_detail_aborts_no_dispatch(tmp_path, monkeypatch):
    """resolve --disposition backlogged with no disposition_detail refuses
    BEFORE dispatching to coordinator-harvest-deferrals (D4 Defect 2 fix) —
    a synthesised detail would only restate disposition_ref, so the caller
    must supply real PM-reasoning prose. No queue/lesson write, no spine
    write, and the refusal is worded as an offer, mirroring the pm_approved
    gate's voice."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-nodetail.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {"verb": "resolve", "plan_path": str(plan), "id": "C1", "disposition": "backlogged"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "disposition_detail" in result.get("error", "").lower()
    assert fake_harvest._write_calls == []
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_backlogged_missing_plan_id_aborts_no_write(tmp_path, monkeypatch):
    """A plan whose frontmatter carries no plan_id cannot form the
    (plan_id, row id) idempotency key — resolve --backlogged aborts before
    any write (spine untouched, no queue/lesson dispatch attempted)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-noplanid.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    fake_harvest._parse_plan_id = lambda text: None
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "plan_id" in result.get("error", "").lower()
    assert fake_harvest._write_calls == []
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_backlogged_unroutable_change_kind_aborts_no_write(tmp_path, monkeypatch):
    """A change_kind absent from BOTH of the harvest module's routing sets
    aborts with no write, rather than silently defaulting to one route."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-unroutable.md", _PLAN_WITH_TASKS)

    fake_harvest = _make_fake_harvest_module(repo)
    fake_harvest._QUEUE_ELIGIBLE_CHANGE_KINDS = frozenset()
    fake_harvest._LESSON_PROMOTE_CHANGE_KINDS = frozenset()
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert "unroutable" in result.get("error", "").lower()
    assert fake_harvest._write_calls == []
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_backlogged_dispatch_failure_aborts_no_write(tmp_path, monkeypatch):
    """A False return from the underlying _run_queue_append (e.g. the
    subprocess failed) aborts resolve — no disposition write against a
    queue write that didn't actually land."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-dispatchfail.md", _PLAN_WITH_TASKS)
    original = plan.read_text(encoding="utf-8")

    fake_harvest = _make_fake_harvest_module(repo)
    fake_harvest._run_queue_append = lambda row, key, dry_run: False
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    after_stamp = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == after_stamp, "no write on dispatch failure"


# ---------------------------------------------------------------------------
# resolve --disposition wont_do requires disposition_detail (C2, 2026-08-05)
# ---------------------------------------------------------------------------
#
# BREAK-CLASS regression coverage: `_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS`
# used to omit `wont_do` while the vendored JSON schema (branch 4, present
# since before 1.3.0) has always required `disposition_detail` for `wont_do`
# and forbidden `disposition_ref` — so a `wont_do` resolve with no detail
# passed this write path and produced a row that was schema-invalid on the
# very next validation pass. See `_PLAN_TASKS_DETAIL_REQUIRED_DISPOSITIONS`'s
# own docstring in plan_tasks_mutate.py for the full defect writeup.


def test_resolve_wont_do_missing_detail_refuses_no_write(tmp_path):
    """resolve --disposition wont_do with no disposition_detail refuses
    (MutateAbort) before any write, mirroring the pm_approved/backlogged
    detail gate's offer-shaped voice."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-wont-do-nodetail.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {"verb": "resolve", "plan_path": str(plan), "id": "C1", "disposition": "wont_do"},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "disposition_detail" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_wont_do_blank_detail_refuses_no_write(tmp_path):
    """A whitespace-only disposition_detail is treated as absent -- the gate
    strips before checking, so a caller cannot satisfy it with blank
    padding."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-wont-do-blankdetail.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "wont_do",
            "disposition_detail": "   ",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "disposition_detail" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# resolve --disposition backlogged/wont_do requires case_against (leg 1,
# 2026-08-06, plan docs/plans/2026-08-06-deferrals-carry-both-sides.md)
# ---------------------------------------------------------------------------
#
# The vendored schema (1.6.0) makes case_against REQUIRED via an allOf
# conditional on the same trigger set, but is presence-only / non-hard-
# failing at that layer -- this op is the hard-rejection enforcement leg,
# mirroring the disposition_detail gate's own coverage shape above.


def test_resolve_backlogged_missing_case_against_refuses_no_write(tmp_path):
    """resolve --disposition backlogged with disposition_detail but no
    case_against refuses (MutateAbort) before any write, in this op's own
    offer-shaped voice -- not the vendored schema's raw 'required field
    missing'."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-nocaseagainst.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "case_against" in error.lower(), error
    assert "required field missing" not in error, (
        "refusal must be in this op's own voice, not the raw schema error: " + error
    )
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_backlogged_blank_case_against_refuses_no_write(tmp_path):
    """A whitespace-only case_against is treated as absent -- the gate
    strips before checking, mirroring the disposition_detail gate."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-backlogged-blankcaseagainst.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "   ",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "case_against" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_wont_do_missing_case_against_refuses_no_write(tmp_path):
    """resolve --disposition wont_do with disposition_detail but no
    case_against refuses before any write -- same trigger set as
    backlogged."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-wontdo-nocaseagainst.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    before = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "wont_do",
            "disposition_detail": "not worth doing",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "case_against" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == before


def test_resolve_spun_off_does_not_require_case_against(tmp_path):
    """spun_off is deliberately excluded from the case_against trigger set
    -- nothing leaves the corpus on a spinoff, so there is no scope cut to
    argue against. Mirrors the pm_approved gate's own spun_off exemption."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-spunoff-nocaseagainst.md", _PLAN_WITH_TASKS)
    _seed_plan(repo, "2026-08-06-nocaseagainst-spinoff.md", "# Spinoff\n")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/2026-08-06-nocaseagainst-spinoff.md",
            "disposition_detail": "moved to spinoff plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = plan.read_text(encoding="utf-8")
    assert "disposition: spun_off" in text
    assert "case_against" not in text


def test_resolve_coded_does_not_require_case_against(tmp_path):
    """coded is not a scope-cut disposition at all -- unaffected by the
    case_against gate."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-coded-nocaseagainst.md", _PLAN_WITH_TASKS)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "coded",
            "disposition_ref": "abc1234",
            "disposition_detail": "shipped in abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert "case_against" not in text


def test_resolve_wont_do_with_detail_writes_and_validates_clean(tmp_path):
    """resolve --disposition wont_do WITH disposition_detail writes
    successfully and the resulting plan source validates clean end-to-end
    (ordering + grouping-approval + per-row JSON-Schema/cross-field rules,
    via check_plan_tasks_source -- the same door the write guards and this
    op's own row validation route through)."""
    from coordinator_core.frontmatter.schema_validate import check_plan_tasks_source

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-wont-do-withdetail.md", _PLAN_WITH_TASKS)

    _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "wont_do",
            "disposition_detail": "declined -- out of scope",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "disposition: wont_do" in text
    assert "disposition_detail: declined -- out of scope" in text
    assert "disposition_ref:" not in text, "wont_do forbids disposition_ref"

    assert check_plan_tasks_source(text) is None, (
        "a wont_do row with detail must validate clean end-to-end"
    )


# ---------------------------------------------------------------------------
# resolve --spun_off computed producer (C12, AC16/AC17)
# ---------------------------------------------------------------------------


def test_resolve_spun_off_round_trip_ref_points_at_real_file(tmp_path):
    """Round-trip (AC16): resolve --disposition spun_off on a row whose
    disposition_ref names a spinoff artifact that actually exists on disk
    succeeds, and the recorded disposition_ref is the artifact's own
    repo-relative path — a real, existing file, not a hand-typed literal
    that merely looks like one."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-spinoff-roundtrip.md", _PLAN_WITH_TASKS)
    spinoff = _seed_plan(repo, "2026-07-29-roundtrip-spinoff.md", "# Roundtrip Spinoff\n")

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/2026-07-29-roundtrip-spinoff.md",
            "disposition_detail": "accepted deferral, forked into its own plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    match = re.search(r"disposition_ref:\s*(\S+)", text)
    assert match, f"disposition_ref not found in spine:\n{text}"
    recorded_ref = match.group(1).strip("'\"")

    # The recorded ref must point at a file that actually exists on disk —
    # the failure mode this producer exists to prevent (a ref pointing at
    # nothing).
    assert (repo / recorded_ref).is_file(), (
        f"disposition_ref {recorded_ref!r} does not resolve to a real file under {repo}"
    )
    assert recorded_ref == "docs/plans/2026-07-29-roundtrip-spinoff.md"
    assert spinoff.is_file()


def test_resolve_spun_off_missing_ref_aborts_no_write(tmp_path):
    """resolve --disposition spun_off with no disposition_ref at all is
    refused (MutateAbort) — /spinoff must create the artifact and pass its
    path; resolve does not synthesize one."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-spinoff-noref.md", _PLAN_WITH_TASKS)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result
    after_stamp = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_detail": "accepted deferral, forked into its own plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "disposition_ref" in result.get("error", "")
    assert plan.read_text(encoding="utf-8") == after_stamp, "no write when disposition_ref is missing"


def test_resolve_spun_off_nonexistent_ref_aborts_no_write(tmp_path):
    """resolve --disposition spun_off whose disposition_ref does not resolve
    to a real file is refused (MutateAbort) — a ref pointing at nothing is
    the failure mode this producer exists to prevent (AC16/AC17)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-spinoff-badref.md", _PLAN_WITH_TASKS)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result
    after_stamp = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "spun_off",
            "disposition_ref": "docs/plans/does-not-exist-anywhere.md",
            "disposition_detail": "accepted deferral, forked into its own plan",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "does not exist anywhere" in error or "does-not-exist-anywhere" in error
    assert "does not point to a file that exists" in error
    assert plan.read_text(encoding="utf-8") == after_stamp, "no write when disposition_ref is a dangling path"


# ---------------------------------------------------------------------------
# resolve batch (C13, 2026-07-30) -- N rows closed atomically in one write
# ---------------------------------------------------------------------------


def _governed_two_row_defer_cutset(*, status: str = "approved", digest: str | None = None) -> str:
    """A GOVERNED plan whose 'defer' grouping is approved over the CUT-SET
    {C1 -> backlogged, C2 -> backlogged} -- both rows, not one. This is the
    exact shape that was empirically unreachable before batch resolve: a
    single-row `resolve` call could only ever produce a one-row prospective
    membership, which never matches a digest approved over two rows.

    Review: code-reviewer (Finding 1) -- C3 remapped 'spun_off' out of the
    'defer' grouping into its own grouping, so a digest computed over
    spun_off rows for grouping 'defer' hashed an empty member set. Rows here
    now use 'backlogged', which is still mapped to 'defer', to keep this a
    real two-row cut-set.
    """
    from coordinator_core.frontmatter.schema_validate import compute_grouping_digest

    if digest is None:
        digest = compute_grouping_digest(
            [
                {"id": "C1", "disposition": "backlogged"},
                {"id": "C2", "disposition": "backlogged"},
            ],
            "defer",
        )
    return f"""\
---
title: "Test Plan -- two-row cut-set"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  defer:
    status: {status}
    approver: pm
    approved_at: 2026-07-30
    pm_utterance: 'yes -- close both C1 and C2 together'
    digest: '{digest}'
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Second.
```
"""


def _governed_two_groupings_plan(*, defer_digest: str | None = None, ruled_out_digest: str | None = None) -> str:
    """A GOVERNED plan with TWO independently-approved groupings: 'defer'
    covers {C1 -> backlogged} and 'ruled_out' covers {C2 -> wont_do} -- for a
    batch that spans groupings in one call (AC: "each affected grouping is
    checked against its own approval block and its own prospective
    membership").

    Review: code-reviewer (Finding 1) -- the 'defer' leg's digest was
    computed over a spun_off row, which is no longer in the 'defer'
    grouping post-C3 and hashed an empty set. Uses 'backlogged' instead so
    the approval is over a real one-row cut-set for 'defer'.
    """
    from coordinator_core.frontmatter.schema_validate import compute_grouping_digest

    if defer_digest is None:
        defer_digest = compute_grouping_digest([{"id": "C1", "disposition": "backlogged"}], "defer")
    if ruled_out_digest is None:
        ruled_out_digest = compute_grouping_digest([{"id": "C2", "disposition": "wont_do"}], "ruled_out")
    return f"""\
---
title: "Test Plan -- spans groupings"
status: draft
schema_version: '1.2.0'
grouping_approvals:
  defer:
    status: approved
    approver: pm
    approved_at: 2026-07-30
    pm_utterance: 'yes -- spin off C1'
    digest: '{defer_digest}'
  ruled_out:
    status: approved
    approver: pm
    approved_at: 2026-07-30
    pm_utterance: 'yes -- wont_do C2'
    digest: '{ruled_out_digest}'
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: C2
  title: Second chunk
  change_kind: script-edit
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Second.
```
"""


def test_resolve_batch_two_row_cutset_lands(tmp_path, monkeypatch):
    """THE composition test that did not exist before C13: a two-row
    'defer' cut-set is approved as a SET, applied as a BATCH, and lands.
    Before batch resolve this scenario was structurally unreachable --
    closing C1 alone produced a one-row prospective membership that could
    never match a digest approved over {C1, C2}, and closing C2 afterward
    never ran because the C1 call always aborted first.

    Review: code-reviewer (Finding 1) -- repointed from `spun_off` to
    `backlogged`. `spun_off` is now its own ungated grouping (C3), so
    resolving both rows to spun_off never reached the grouping-digest gate
    at all; `backlogged` is still in the PM-gated 'defer' grouping this test
    exists to exercise.
    """
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-cutset.md", _governed_two_row_defer_cutset())

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "backlogged",
                    "disposition_detail": "closed as part of the approved two-row cut",
                    "case_against": "waiting costs little; nothing depends on this landing now",
                },
                {
                    "id": "C2",
                    "disposition": "backlogged",
                    "disposition_detail": "closed as part of the approved two-row cut",
                    "case_against": "waiting costs little; nothing depends on this landing now",
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert text.count("disposition: backlogged") == 2


def test_resolve_batch_spans_two_groupings_each_checked_independently(tmp_path, monkeypatch):
    """A single batch closes C1 -> backlogged ('defer') and C2 -> wont_do
    ('ruled_out') -- each grouping's approval is checked against its OWN
    block and its OWN prospective membership.

    Review: code-reviewer (Finding 1) -- C1 repointed from `spun_off` to
    `backlogged`. `spun_off` is now ungated (its own grouping, C3), so
    resolving C1 to spun_off never exercised the 'defer' grouping's digest
    check this test's docstring claims to cover; `backlogged` still does.
    """
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-spans.md", _governed_two_groupings_plan())

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "backlogged",
                    "disposition_detail": "backlogged",
                    "case_against": "waiting costs little; nothing depends on this landing now",
                },
                {
                    "id": "C2",
                    "disposition": "wont_do",
                    "disposition_detail": "not worth doing",
                    "case_against": "waiting costs little; nothing depends on this landing now",
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    text = plan.read_text(encoding="utf-8")
    assert "disposition: backlogged" in text
    assert "disposition: wont_do" in text


def test_resolve_batch_one_grouping_unapproved_refuses_whole_batch(tmp_path):
    """Batch spans two groupings; 'ruled_out' is still pending. The WHOLE
    batch is refused (including the otherwise-approved C1/'defer' half) --
    no partial application, and the file is byte-unchanged.

    Review: code-reviewer (Finding 1) -- C1 repointed from `spun_off` to
    `backlogged` so the approved 'defer' half is actually gated (and
    therefore a real thing to be "otherwise-approved") rather than exempt.
    """
    repo = _make_git_repo(tmp_path)
    plan_text = _governed_two_groupings_plan()
    plan_text = plan_text.replace(
        "ruled_out:\n    status: approved", "ruled_out:\n    status: pending"
    )
    plan = _seed_plan(repo, "resolve-batch-one-pending.md", plan_text)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "backlogged",
                    "disposition_detail": "backlogged",
                },
                {
                    "id": "C2",
                    "disposition": "wont_do",
                    "disposition_detail": "not worth doing",
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "ruled_out" in error
    assert "pending" in error.lower()
    assert plan.read_text(encoding="utf-8") == original, (
        "whole batch must be refused byte-unchanged, including the approved half"
    )


def test_resolve_batch_atomicity_last_row_fails_leaves_bytes_unchanged(tmp_path):
    """Atomicity: a batch where the LAST row fails (missing
    disposition_detail) leaves the spine byte-identical to before the
    call -- asserted on the raw bytes, not just on the exception."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-atomic.md", _PLAN_TWO_ROWS)

    stamp_result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "pm_approved": True},
                {"id": "C2", "pm_approved": True},
            ],
        },
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result
    before_bytes = plan.read_bytes()

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "wont_do",
                    "disposition_detail": "not worth doing",
                },
                {
                    "id": "C2",
                    "disposition": "wont_do",
                    # disposition_detail deliberately omitted -- this is the
                    # LAST row in the batch, and it is the one that fails.
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "disposition_detail" in result.get("error", "")
    after_bytes = plan.read_bytes()
    assert after_bytes == before_bytes, (
        "a batch where only the LAST row fails must leave the spine byte-identical -- "
        "C1's otherwise-valid wont_do must NOT have been written"
    )


def test_resolve_batch_of_one_matches_single_row_behaviour(tmp_path):
    """A `resolves` list containing exactly one entry behaves identically to
    the single-row id/disposition param shape -- same write, same message
    shape (no 'N task(s)' plural wording for a batch of one)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-of-one.md", _PLAN_WITH_TASKS)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "coded",
                    "disposition_ref": "abc1234",
                    "disposition_detail": "shipped in abc1234",
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "resolved to 'coded'" in result["message"]
    assert "task(s)" not in result["message"]

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert "disposition_ref: abc1234" in text


def test_resolve_batch_prospective_set_mismatch_refuses(tmp_path):
    """A batch whose prospective (post-write) membership does not match the
    approved digest is refused -- the approval was for a DIFFERENT cut-set
    than this batch would produce (e.g. approved {C1, C2}, but only C1 is
    named in this batch)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-narrower.md", _governed_two_row_defer_cutset())
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {
                    "id": "C1",
                    "disposition": "backlogged",
                    "disposition_detail": "closing only C1",
                },
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "cut-set" in error.lower()
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_single_row_against_multirow_approved_cut_still_refused(tmp_path):
    """The pre-existing, CORRECT refusal: a single-row (non-batch) resolve
    call against a grouping approved over a two-row cut-set is refused --
    this is not a regression introduced by batch resolve, it is the exact
    defect batch resolve exists to make solvable via the batch path
    instead."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-single-vs-batch-approval.md", _governed_two_row_defer_cutset())
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "closing only C1",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "cut-set" in error.lower()
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_batch_unknown_task_id_aborts_whole_call(tmp_path):
    """An unknown id anywhere in the batch aborts the WHOLE call -- C1's
    otherwise-valid resolution must not land just because it was checked
    first."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-unknown-id.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {"id": "C1", "disposition": "coded", "disposition_detail": "done"},
                {"id": "C-DOES-NOT-EXIST", "disposition": "coded", "disposition_detail": "done"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "not found" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_batch_duplicate_id_aborts_no_write(tmp_path):
    """A duplicate id within one `resolves` batch aborts before the lock is
    even taken -- no write."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-dup-id.md", _PLAN_TWO_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {"id": "C1", "disposition": "coded", "disposition_detail": "done"},
                {"id": "C1", "disposition": "wont_do", "disposition_detail": "changed my mind"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert "duplicate" in result.get("error", "").lower()
    assert plan.read_text(encoding="utf-8") == original


def test_resolve_batch_legacy_plan_each_row_checked_independently(tmp_path):
    """On a LEGACY plan (no grouping_approvals) a batch checks each row's
    own pm_approved independently -- one approved + one not-yet-approved in
    the same batch refuses the whole call."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-batch-legacy-mixed.md", _PLAN_TWO_ROWS)

    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result
    after_stamp = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "resolves": [
                {"id": "C1", "disposition": "wont_do", "disposition_detail": "not worth doing"},
                {"id": "C2", "disposition": "wont_do", "disposition_detail": "also not worth doing"},
            ],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "C2" in error
    assert plan.read_text(encoding="utf-8") == after_stamp, (
        "C1's otherwise-approved wont_do must not land while C2 is ungated"
    )


# ---------------------------------------------------------------------------
# A4 -- `_to_repo_relative` posix output (2026-07-28)
# ---------------------------------------------------------------------------


def test_to_repo_relative_emits_posix_form(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import _to_repo_relative

    worktree = tmp_path
    nested = worktree / "state" / "lessons" / "x.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text("x", encoding="utf-8")

    result = _to_repo_relative(str(nested), worktree)

    assert result == "state/lessons/x.yaml"
    assert "\\" not in result


def test_to_repo_relative_windows_style_input_still_posix(tmp_path, monkeypatch):
    # A4 fix: `_to_repo_relative` now routes through `rel_id`
    # (`path.relative_to(root).as_posix()`), which is ALWAYS forward-slash
    # regardless of the producing host's OS -- unlike the prior
    # `str(...relative_to(...))`, which rendered `os.sep`
    # (`state\lessons\x.yaml` on Windows) straight into the tracked
    # plan-tasks spine's `disposition_ref` field.
    from coordinator_core.wire_paths import rel_id

    worktree = tmp_path
    nested = worktree / "state" / "lessons" / "x.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text("x", encoding="utf-8")

    # Simulate the Windows-separator hazard directly against the shared
    # canonical helper the fix now routes through -- `rel_id` must emit
    # posix form even when `os.sep` is `\` (monkeypatched), since
    # `Path.as_posix()` is separator-independent by construction.
    monkeypatch.setattr(os, "sep", "\\")
    result = rel_id(nested.resolve(), worktree.resolve())
    assert result == "state/lessons/x.yaml"
    assert "\\" not in result


def test_to_repo_relative_outside_worktree_falls_back_unchanged(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import _to_repo_relative

    worktree = tmp_path / "repo"
    worktree.mkdir()
    outside = tmp_path / "elsewhere" / "x.yaml"
    outside.parent.mkdir(parents=True)
    outside.write_text("x", encoding="utf-8")

    result = _to_repo_relative(str(outside), worktree)

    assert result == str(outside)


# ---------------------------------------------------------------------------
# C1 (2026-08-14) -- resolve stamps `landed` when no row is left open, via
# the EXISTING sole writer (execute_plan_assemble.close_out_and_stamp.
# _stamp_plan_landed). See docs/plans/2026-08-14-landed-fires-at-spine-
# resolution-and-clo.md § C1 / AC1, AC2, AC4, AC5.
# ---------------------------------------------------------------------------


def _read_status(plan: Path) -> str:
    m = re.search(r'^status:\s*"?([A-Za-z_]+)"?\s*$', plan.read_text(encoding="utf-8"), re.MULTILINE)
    assert m, plan.read_text(encoding="utf-8")
    return m.group(1)


def test_resolve_all_rows_resolved_stamps_landed_no_execute_plan_involved(tmp_path):
    """AC1: resolving the LAST still-open row of an otherwise-fully-resolved
    spine acquires `status: landed` from the `resolve` path alone -- no
    operator action, no `/execute-plan` run. `_PLAN_WITH_CODED_PREFIX_AND_
    OPEN_TAIL` already has C1/C2 `coded`; only C3 is open."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(
        repo, "resolve-landed-all-resolved.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL
    )
    assert _read_status(plan) == "draft"

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C3",
            "disposition": "coded",
            "disposition_ref": "abc3333",
            "disposition_detail": "shipped in this session",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result.get("landed_stamp") == "ok", result

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert _read_status(plan) == "landed"


def test_resolve_landed_stamp_exception_does_not_fail_the_resolve(tmp_path, monkeypatch):
    """Review: code-reviewer (P2 #2) -- the `try/except Exception` around
    `_stamp_plan_landed` (C1) is plan-specified as a derived side effect
    that "must never fail resolve", but that contract had zero test
    coverage of its failure branch. Force the stamp call itself to RAISE
    and assert BOTH halves of the contract: the row resolution the caller
    actually asked for still applied to disk with exit_code == 0, AND
    `landed_stamp` reports the error rather than silently omitting it."""
    import coordinator_core.execute_plan_assemble.close_out_and_stamp as close_out_and_stamp

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated stamp failure")

    monkeypatch.setattr(close_out_and_stamp, "_stamp_plan_landed", _boom)

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(
        repo, "resolve-landed-stamp-raises.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL
    )

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C3",
            "disposition": "coded",
            "disposition_ref": "abc3333",
            "disposition_detail": "shipped in this session",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result.get("landed_stamp", "").startswith("error"), result
    assert "simulated stamp failure" in result["landed_stamp"]

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    # The row resolution itself applied even though the derived stamp
    # attempt raised -- but the stamp never ran, so status is untouched.
    assert _read_status(plan) == "draft"


def test_resolve_landed_stamp_nonzero_rc_does_not_fail_the_resolve(tmp_path, monkeypatch):
    """Same contract as above, exercised via the OTHER failure shape the
    swallow's own body distinguishes: `_stamp_plan_landed` returning a
    non-zero rc (an ordinary business-logic failure, no exception) rather
    than raising. `landed_stamp` must report "error" here too, and the
    row resolution must still have applied."""
    import coordinator_core.execute_plan_assemble.close_out_and_stamp as close_out_and_stamp

    monkeypatch.setattr(close_out_and_stamp, "_stamp_plan_landed", lambda *a, **k: 1)

    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(
        repo, "resolve-landed-stamp-nonzero-rc.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL
    )

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C3",
            "disposition": "coded",
            "disposition_ref": "abc3333",
            "disposition_detail": "shipped in this session",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert result.get("landed_stamp") == "error", result

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert _read_status(plan) == "draft"


def test_resolve_one_row_still_open_does_not_acquire_landed(tmp_path):
    """AC2: a plan with at least one `open` row does NOT acquire `landed`
    via the resolve path -- resolving one of two rows leaves the other
    `open`, so status is left exactly as it was."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-landed-one-open.md", _PLAN_TWO_ROWS)
    assert _read_status(plan) == "draft"

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "coded",
            "disposition_ref": "abc1111",
            "disposition_detail": "shipped",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "landed_stamp" not in result, result

    assert _read_status(plan) == "draft"
    text = plan.read_text(encoding="utf-8")
    assert "id: C2" in text
    assert "disposition: coded" not in text.split("id: C2", 1)[1].split("- id:", 1)[0], (
        "C2 must remain unresolved (still 'open' by omission)"
    )


def test_resolve_all_wont_do_backlogged_reaches_landed_never_implemented(tmp_path, monkeypatch):
    """AC4: a plan whose rows resolve entirely as `wont_do`/`backlogged`
    (i.e. NOTHING shipped) still reaches `landed`, and never `implemented`
    -- landed derivation is a separate oracle from the ACs/shipped-evidence
    gate that guards `implemented`."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-landed-wont-do-backlogged.md", _PLAN_TWO_ROWS)

    fake_harvest = _make_fake_harvest_module(repo)
    monkeypatch.setattr(plan_tasks_mutate, "_load_harvest_module", lambda: fake_harvest)

    stamp_result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [
                {"id": "C1", "pm_approved": True},
                {"id": "C2", "pm_approved": True},
            ],
        },
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    backlogged_result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "backlogged",
            "disposition_detail": "deferred to backlog",
            "case_against": "waiting costs little; nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))
    assert backlogged_result["exit_code"] == 0, backlogged_result
    assert "landed_stamp" not in backlogged_result, backlogged_result
    assert _read_status(plan) == "draft"

    wont_do_result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C2",
            "disposition": "wont_do",
            "disposition_detail": "no longer worth doing",
            "case_against": "nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))

    assert wont_do_result["exit_code"] == 0, wont_do_result
    assert wont_do_result.get("landed_stamp") == "ok", wont_do_result

    assert _read_status(plan) == "landed"
    text = plan.read_text(encoding="utf-8")
    assert "disposition: backlogged" in text
    assert "disposition: wont_do" in text


def test_resolve_path_never_reaches_a_terminal_status(tmp_path):
    """AC5: no path exists from spine-resolution to a terminal status.

    Distinct from `test_landed_transition_does_not_reach_the_cascade_
    entrypoint` (execute_plan_assemble's own test, over the CLOSE-OUT
    path): this asserts directly against the RESOLVE path in this module
    -- across a full-resolution-to-coded scenario and a full-resolution-
    to-wont_do/backlogged scenario, the resulting `status:` is never one
    of `_FROZEN_STATUSES` (implemented/superseded/abandoned/deferred), and
    is never `implemented` -- landed is the only status resolve can ever
    produce."""
    from coordinator_core.ops.plan_status_transition import _FROZEN_STATUSES

    repo = _make_git_repo(tmp_path)

    coded_plan = _seed_plan(
        repo, "resolve-terminal-check-coded.md", _PLAN_WITH_CODED_PREFIX_AND_OPEN_TAIL
    )
    coded_result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(coded_plan),
            "id": "C3",
            "disposition": "coded",
            "disposition_ref": "abc3333",
            "disposition_detail": "shipped in this session",
        },
        repo_root=repo / ".git",
    ))
    assert coded_result["exit_code"] == 0, coded_result
    coded_status = _read_status(coded_plan)
    assert coded_status not in _FROZEN_STATUSES, coded_status
    assert coded_status != "implemented", coded_status
    assert coded_status == "landed", coded_status

    wont_do_plan = _seed_plan(repo, "resolve-terminal-check-wont-do.md", _PLAN_WITH_TASKS)
    stamp_result = _run(_handler(
        {"verb": "stamp", "plan_path": str(wont_do_plan), "updates": [{"id": "C1", "pm_approved": True}]},
        repo_root=repo / ".git",
    ))
    assert stamp_result["exit_code"] == 0, stamp_result

    wont_do_result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(wont_do_plan),
            "id": "C1",
            "disposition": "wont_do",
            "disposition_detail": "not worth doing",
            "case_against": "nothing depends on this landing now",
        },
        repo_root=repo / ".git",
    ))
    assert wont_do_result["exit_code"] == 0, wont_do_result
    wont_do_status = _read_status(wont_do_plan)
    assert wont_do_status not in _FROZEN_STATUSES, wont_do_status
    assert wont_do_status != "implemented", wont_do_status
    assert wont_do_status == "landed", wont_do_status


# ---------------------------------------------------------------------------
# Untouched-invalid-row deadlock fix (2026-08-16): a pre-existing
# schema-invalid row a mutation does NOT touch must not veto that mutation.
# Reproduces the live deadlock (two invalid rows, each blocking the other's
# repair) and asserts the write path this fix restores, plus the invariant
# it must NOT relax (a mutation may never WRITE a newly-invalid row).
# ---------------------------------------------------------------------------

_PLAN_TWO_INVALID_ROWS = """\
---
title: "Test Plan — two pre-existing invalid rows"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: B1
  title: First bad row
  change_kind: not-a-real-enum-value
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: B2
  title: Second bad row
  change_kind: not-a-real-enum-value
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Second.
```
"""


def test_stamp_repairs_one_invalid_row_in_one_call(tmp_path):
    """A spine with ONE pre-existing schema-invalid row can be repaired to
    valid via a single stamp call that touches only that row."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "one-invalid-row.md", _PLAN_TWO_ROWS)
    # Corrupt C1 in place so it starts invalid, on disk, before any op runs.
    text = plan.read_text(encoding="utf-8")
    text = text.replace(
        "  change_kind: script-edit\n  surface: a.py",
        "  change_kind: not-a-real-enum-value\n  surface: a.py",
    )
    plan.write_text(text, encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "C1", "change_kind": "script-edit"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    assert "change_kind: script-edit" in plan.read_text(encoding="utf-8")


def test_stamp_two_invalid_rows_repaired_in_two_calls_no_deadlock(tmp_path):
    """The exact deadlock this fix exists for: two rows in the SAME spine
    are each pre-existing schema-invalid, and neither call touches the
    other row. Repairing B1 alone must succeed despite B2 still being
    invalid on disk, and repairing B2 alone (in a second call) must then
    also succeed — the two rows must not be able to block each other."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "two-invalid-rows.md", _PLAN_TWO_INVALID_ROWS)

    first = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "B1", "change_kind": "script-edit"}],
        },
        repo_root=repo / ".git",
    ))
    assert first["exit_code"] == 0, first
    assert first["applied"] is True
    text_after_first = plan.read_text(encoding="utf-8")
    assert "id: B1" in text_after_first
    b1_block = text_after_first.split("id: B2")[0]
    assert "change_kind: script-edit" in b1_block
    assert "not-a-real-enum-value" in text_after_first, (
        "B2 must still be present and still invalid — the first call must "
        "not have touched it"
    )

    second = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "B2", "change_kind": "script-edit"}],
        },
        repo_root=repo / ".git",
    ))
    assert second["exit_code"] == 0, second
    assert second["applied"] is True
    final_text = plan.read_text(encoding="utf-8")
    assert "not-a-real-enum-value" not in final_text
    assert final_text.count("change_kind: script-edit") == 2


def test_stamp_mutation_writing_a_newly_invalid_row_still_refused(tmp_path):
    """Invariant (1) must not regress: a mutation that would WRITE a row
    into an invalid state is still refused with no write — even when the
    spine ALREADY carries an untouched pre-existing invalid row. The
    untouched-invalid-row diagnostic exists to surface pre-existing breakage
    without vetoing an unrelated write; it must never widen into 'anything
    goes because the spine was already broken.'"""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "still-refuses.md", _PLAN_TWO_INVALID_ROWS)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            # B1 starts invalid (bad change_kind); this update does not fix
            # it — it renames the row instead, so B1 is STILL invalid after
            # this write. B1 is touched, so it must still be refused.
            "updates": [{"id": "B1", "title": "renamed but still bad"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original, (
        "no write must land: the touched row (B1) is still invalid after "
        "this mutation, so it must be refused exactly as before — the "
        "untouched-invalid-row diagnostic must never let a mutation write "
        "a row that remains invalid"
    )


def test_stamp_untouched_invalid_rows_diagnostic_names_the_ids(tmp_path):
    """A successful stamp that repairs one row while a sibling row remains
    pre-existing-invalid surfaces a `warnings` diagnostic naming the
    untouched row's id — a repair must not silently normalize a broken
    spine into looking fully clean (requirement 4 of the 2026-08-16 fix)."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "diagnostic-names-ids.md", _PLAN_TWO_INVALID_ROWS)

    result = _run(_handler(
        {
            "verb": "stamp",
            "plan_path": str(plan),
            "updates": [{"id": "B1", "change_kind": "script-edit"}],
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True
    warnings = result.get("warnings") or []
    assert warnings, "expected a non-empty warnings list naming the untouched-invalid row"
    assert any("B2" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# add-task / resolve untouched_ids regression coverage (2026-08-16 fix,
# e4a1ffe9a000) — the four stamp-only tests above left add-task and resolve,
# which received the identical `touched_ids` treatment, with zero dedicated
# coverage. Review: code-reviewer (P2) — cover both obligations (still
# refuses a newly-invalid touched write; surfaces untouched-invalid ids as
# warnings) for both verbs, prioritising resolve's Phase 1/Phase 2 split as
# the highest-risk shape in the file.
# ---------------------------------------------------------------------------

_PLAN_ONE_VALID_ONE_INVALID = """\
---
title: "Test Plan — one valid, one pre-existing invalid sibling"
status: draft
---

# Test Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: First chunk
  change_kind: script-edit
  surface: a.py
  queue_scope: project
  deferred: false
  body: |
    First.
- id: BX
  title: Pre-existing bad row
  change_kind: not-a-real-enum-value
  surface: b.py
  queue_scope: project
  deferred: false
  body: |
    Bad.
```
"""


def test_add_task_succeeds_with_untouched_invalid_row_and_warns(tmp_path):
    """add-task on a spine carrying a pre-existing, untouched schema-invalid
    row (BX) still succeeds and appends the new valid row — the untouched
    row must not veto an unrelated write — and the reply names BX in
    `warnings`."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "add-task-sibling-invalid.md", _PLAN_ONE_VALID_ONE_INVALID)

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": _valid_task("C2")},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "id: C2" in text
    assert "not-a-real-enum-value" in text, "BX must survive untouched"

    warnings = result.get("warnings") or []
    assert warnings, "expected a non-empty warnings list naming the untouched-invalid row"
    assert any("BX" in w for w in warnings), warnings


def test_add_task_writing_a_newly_invalid_row_still_refused(tmp_path):
    """Invariant (1) must not regress for add-task: a new row that is itself
    schema-invalid is still refused with no write, even on a spine that
    already carries an untouched pre-existing invalid row (BX) — the
    untouched-invalid-row diagnostic must never widen into 'anything goes
    because the spine was already broken.'"""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "add-task-sibling-invalid-refused.md", _PLAN_ONE_VALID_ONE_INVALID)
    original = plan.read_text(encoding="utf-8")

    bad_task = {
        "id": "C-BAD",
        "title": "Missing required fields",
        # 'change_kind' and 'surface' are required by the schema — omitted.
    }

    result = _run(_handler(
        {"verb": "add-task", "plan_path": str(plan), "task": bad_task},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original, (
        "no write must land: the newly-added row (C-BAD) is invalid, so it "
        "must be refused exactly as before — an untouched sibling-invalid "
        "row must not relax this"
    )


def test_resolve_succeeds_with_untouched_invalid_row_and_warns_untouched_ids(tmp_path):
    """resolve repairs/closes a valid row (C1 -> coded) on a spine that also
    carries a pre-existing, untouched schema-invalid row (BX) — Phase 1
    writes disposition fields, Phase 2 builds `resolved_ids` around
    dispatch calls, and neither phase may be vetoed by BX's unrelated
    breakage. The reply must still name BX in `warnings`."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-sibling-invalid.md", _PLAN_ONE_VALID_ONE_INVALID)

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            "id": "C1",
            "disposition": "coded",
            "disposition_ref": "abc1234",
            "disposition_detail": "shipped in abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 0, result
    assert result["applied"] is True

    text = plan.read_text(encoding="utf-8")
    assert "disposition: coded" in text
    assert "not-a-real-enum-value" in text, "BX must survive untouched"

    warnings = result.get("warnings") or []
    assert warnings, "expected a non-empty warnings list naming the untouched-invalid row"
    assert any("BX" in w for w in warnings), warnings


def test_resolve_writing_a_newly_invalid_touched_row_still_refused(tmp_path):
    """Invariant (1) must not regress for resolve: resolving a row that is
    ITSELF schema-invalid (bad change_kind) still leaves it invalid after
    Phase 1 writes only the disposition fields — resolve never touches
    change_kind — so the touched row must still be refused with no write,
    even on a spine that also carries a second, untouched invalid row.
    Asserts byte-identical file content (not just exit_code) to rule out a
    silent partial write from Phase 1's in-place mutation before the
    Phase 2 dispatch loop and the post-loop `_validate_all` gate."""
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "resolve-newly-invalid-refused.md", _PLAN_ONE_VALID_ONE_INVALID)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {
            "verb": "resolve",
            "plan_path": str(plan),
            # BX starts invalid (bad change_kind); resolve only ever writes
            # disposition/disposition_ref/disposition_detail, so BX is still
            # invalid after this write. BX is the touched row here, so it
            # must still be refused.
            "id": "BX",
            "disposition": "coded",
            "disposition_ref": "abc1234",
            "disposition_detail": "shipped in abc1234",
        },
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    assert plan.read_text(encoding="utf-8") == original, (
        "no write must land: the touched row (BX) is still invalid after "
        "this mutation (resolve does not touch change_kind), so it must be "
        "refused exactly as before — Phase 1's in-place row mutation before "
        "the post-loop _validate_all gate must never produce a silent "
        "partial write"
    )


# ---------------------------------------------------------------------------
# Write-time parse refusal — an unparseable, LOCATED spine body
# ---------------------------------------------------------------------------

_PLAN_UNPARSEABLE_SPINE = _PLAN_WITH_TASKS.replace(
    "    Do the first thing.\n",
    "    Do the first thing.\n<!-- an annotation written INSIDE the fence -->\n",
)


@pytest.mark.parametrize(
    "params",
    [
        {"verb": "add-task", "task": _valid_task("C2")},
        {"verb": "stamp", "updates": [{"id": "C1", "fields": {"surface": "other/path.py"}}]},
    ],
    ids=["add-task", "stamp"],
)
def test_unparseable_spine_refuses_the_write_naming_the_line(tmp_path, params):
    """A fence that locates but does not parse aborts cleanly, naming the line.

    `locate_fenced_block` blanks HTML comments for its own scan but slices
    `body` from the original source, so this spine reaches the verb as
    LOCATED and only fails at `yaml.safe_load`. Before the refusal it raised
    an uncaught `yaml.YAMLError` through `locked_rmw`; the contract now is a
    normal exit_code=1 abort, byte-unchanged on disk.
    """
    repo = _make_git_repo(tmp_path)
    plan = _seed_plan(repo, "unparseable.md", _PLAN_UNPARSEABLE_SPINE)
    original = plan.read_text(encoding="utf-8")

    result = _run(_handler(
        {"plan_path": str(plan), **params},
        repo_root=repo / ".git",
    ))

    assert result["exit_code"] == 1, result
    assert result["applied"] is False
    error = result.get("error", "")
    assert "does not parse as YAML" in error, error
    assert "line" in error, "the abort must name the offending line in the fenced block"
    assert plan.read_text(encoding="utf-8") == original, (
        "file must be byte-unchanged after a parse abort"
    )
