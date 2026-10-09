"""`approved_body_sha` ignores the spine bookkeeping `dispatch.terminal_commit` writes
(open -> coded, disposition_ref, D5 re-sort) and still refuses a real plan edit."""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.primitives import (
    APPROVED_BODY_CHANGED,
    APPROVED_BODY_OK,
    canonical_body_sha,
    check_approved_body,
    replace_fm_field,
    rebuild,
    split_frontmatter,
    stamp_approved_body_sha,
)

_ROWS = """- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
- id: C4
  title: t4
  change_kind: code-edit
  surface: a.py
  deferred: false
  body: |
    disposition: coded
    multi-line
- id: C5
  title: t5
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
"""


def _plan(rows: str = _ROWS, prose: str = "Prose.") -> str:
    return (
        "---\ntitle: p\nstatus: approved\n---\n\n# Plan\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{rows.rstrip()}\n```\n\n{prose}\n"
    )


def _approved(**kw) -> str:
    return stamp_approved_body_sha(_plan(**kw))


def test_terminal_commit_flip_keeps_the_stamp_valid():
    from coordinator_core.ops.dispatch_emit.terminal_commit import _flip_rows_coded

    approved = _approved()
    flipped, ids = _flip_rows_coded(approved, {"C3", "C4"}, "a" * 40)
    assert ids == ["C3", "C4"]
    assert "disposition_ref" in flipped and flipped != approved
    assert flipped.index("id: C3") < flipped.index("id: C5")  # coding a row never moves it
    assert check_approved_body(flipped)[0] == APPROVED_BODY_OK


def test_terminal_commit_noop_detail_keeps_the_stamp_valid():
    from coordinator_core.ops.dispatch_emit.terminal_commit import _NOOP_DETAIL, _flip_rows_coded

    approved = _approved()
    flipped, ids = _flip_rows_coded(approved, {"C3"}, "a" * 40, {"C3": _NOOP_DETAIL})
    assert ids == ["C3"] and "disposition_detail" in flipped
    assert check_approved_body(flipped)[0] == APPROVED_BODY_OK
    assert check_approved_body(flipped.replace("t5", "t5 edited"))[0] == APPROVED_BODY_CHANGED


def test_review_stamp_and_frontmatter_bookkeeping_is_invisible():
    approved = _approved()
    split = split_frontmatter(approved)
    fm = replace_fm_field(split.fm_text, "status", "executing")
    assert check_approved_body(rebuild(split, fm))[0] == APPROVED_BODY_OK


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t.replace("Prose.", "Different prose."),
        lambda t: t.replace("title: t3", "title: t3 edited"),
        lambda t: t.replace("  surface: a.py\n  disposition: open", "  surface: a.py\n  extra: 1\n  disposition: open", 1),
        lambda t: t.replace("```\n\nProse", "- id: C6\n  title: t6\n  change_kind: code-edit\n  surface: a.py\n```\n\nProse"),
        lambda t: t.replace("- id: C5\n  title: t5\n  change_kind: code-edit\n  surface: a.py\n  disposition: open\n  deferred: false\n", ""),
        lambda t: t.replace("disposition: open\n  deferred: false\n- id: C4", "disposition: backlogged\n  deferred: false\n- id: C4"),
    ],
    ids=["prose", "row-field", "row-extra-key", "task-added", "task-removed", "closed-disposition"],
)
def test_a_real_body_edit_still_refuses(mutate):
    approved = _approved()
    edited = mutate(approved)
    assert edited != approved
    assert check_approved_body(edited)[0] == APPROVED_BODY_CHANGED


def test_a_block_scalar_line_that_looks_like_a_disposition_is_content():
    approved = _approved()
    edited = approved.replace("    disposition: coded\n", "    multi-line\n    again\n", 1)
    assert check_approved_body(edited)[0] == APPROVED_BODY_CHANGED


def test_legacy_stamp_over_canonical_body_sha_is_accepted():
    plan = _plan()
    split = split_frontmatter(plan)
    legacy = rebuild(
        split, replace_fm_field(split.fm_text + "approved_body_sha: x\n", "approved_body_sha", canonical_body_sha(plan), numeric_quoting=True)
    )
    assert check_approved_body(legacy)[0] == APPROVED_BODY_OK
    assert check_approved_body(legacy.replace("Prose.", "Other."))[0] == APPROVED_BODY_CHANGED


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_terminal_commit_then_only_incomplete_emit_is_not_refused(tmp_path):
    """Real terminal_commit on an incomplete approved plan, then the emit gate."""
    import subprocess

    from coordinator_core.ops.dispatch_emit import terminal_commit
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.op import _refuse_unapproved_body
    from coordinator_core.win_portability import no_console_creationflags

    def git(*a):
        subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    plan = tmp_path / "docs" / "plan.md"
    plan.write_text(_approved(), encoding="utf-8")
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (tmp_path / "a.py").write_text("a2\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C5", title="t5", paths=("c.py",)),
        ),
        plan_path="docs/plan.md",
    )
    (tmp_path / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8")

    out = terminal_commit._handler(
        {
            "script_path": "run.mjs",
            "incomplete_chunks": ["C5"],
            "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0},
        },
        repo_root=tmp_path / ".git",
    )

    assert out["rows_coded"] == {"docs/plan.md": ["C3"]}
    after = plan.read_text(encoding="utf-8")
    assert "disposition_ref" in after
    _refuse_unapproved_body(str(plan))  # raises ValueError on "plan body changed since approval"


_GATED_ROWS = """- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  deferred: false
  external_gate:
  - owner_repo: other-repo
    condition: land the thing
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_clearing_an_external_gate_keeps_the_stamp_valid_and_real_edit_refuses(tmp_path):
    from coordinator_core.ops.plan_tasks_mutate import _clear_gate

    import subprocess
    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan = tmp_path / "docs" / "plans" / "plan.md"
    approved = _approved(rows=_GATED_ROWS)
    plan.write_text(approved, encoding="utf-8")
    res = _clear_gate("docs/plans/plan.md", "C3", "other-repo", "landed in abc123", tmp_path, tmp_path)
    assert res.get("applied") is True, res
    cleared = plan.read_text(encoding="utf-8")
    assert "cleared: true" in cleared and cleared != approved
    assert check_approved_body(cleared)[0] == APPROVED_BODY_OK
    assert check_approved_body(cleared.replace("land the thing", "land another"))[0] == APPROVED_BODY_CHANGED
