"""
coordinator_core.plan_assemble.predicates.test_concurrent_preflight — unit
tests for `concurrent_preflight`, row `:83`'s Layer 0 leaf reader.

Purpose: exercise both legs (today-dated-plan collision, `source_memo:`
collision) against a fixture plan directory and a fixture git history —
never the live tree, per this chunk's Test surface note.

Negative-spec:
  - Does NOT touch the live `docs/plans/` directory or the real repo's git
    history — every fixture is built under a `tmp_path`-rooted repo.
  - Does NOT assert a disposition/verdict field exists — the module never
    emits one; tests assert raw evidence shape only.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C10
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext, undetermined
from coordinator_core.plan_assemble.predicates.concurrent_preflight import (
    concurrent_preflight,
)

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _init_repo(repo_root: Path) -> None:
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo_root, check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_root,
        check=True,
        creationflags=_NO_WINDOW,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo_root,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _commit_all(repo_root: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo_root, check=True, creationflags=_NO_WINDOW)
    subprocess.run(
        ["git", "commit", "-q", "-m", message],
        cwd=repo_root,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _context(
    repo_root: Path,
    plan_path: Path | None = None,
    plan_frontmatter: dict | None = None,
) -> PredicateContext:
    return PredicateContext(
        repo_root=repo_root,
        plan_path=plan_path,
        plan_frontmatter=plan_frontmatter,
        plan_body=None,
        sizing_object_path=None,
        sizing_frontmatter=None,
        resolved_route="plan",
        caller_flags={},
    )


def test_no_docs_plans_dir_is_undetermined(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = concurrent_preflight(_context(tmp_path))
    assert result["today_dated_plan"] == undetermined(
        reason="docs/plans/ is not a directory under repo_root"
    )


def test_today_dated_plan_file_hit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    import datetime

    today = datetime.date.today().isoformat()
    (plans_dir / f"{today}-some-other-plan.md").write_text("---\n---\nbody\n")
    _commit_all(tmp_path, "seed")

    result = concurrent_preflight(_context(tmp_path))
    leg_a = result["today_dated_plan"]
    assert leg_a["today_dated_plan_hit"] is True
    assert f"{today}-some-other-plan.md" in leg_a["today_dated_plan_files"]


def test_today_commit_hit_without_today_dated_file(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "2020-01-01-old-plan.md").write_text("---\n---\nbody\n")
    _commit_all(tmp_path, "touch docs/plans")

    result = concurrent_preflight(_context(tmp_path))
    leg_a = result["today_dated_plan"]
    assert leg_a["today_dated_plan_hit"] is False
    assert leg_a["today_commit_hit"] is True
    assert len(leg_a["today_commit_lines"]) >= 1


def test_no_collision_clean_history(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "README.md").write_text("hello\n")
    _commit_all(tmp_path, "unrelated")

    result = concurrent_preflight(_context(tmp_path))
    leg_a = result["today_dated_plan"]
    assert leg_a["today_dated_plan_hit"] is False
    assert leg_a["today_commit_hit"] is False


def test_source_memo_absent_is_undetermined(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    result = concurrent_preflight(_context(tmp_path))
    assert result["source_memo_collision"] == undetermined(
        reason="plan frontmatter carries no source_memo: — leg (b) does not apply"
    )


def test_source_memo_frontmatter_collision(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    own_plan = plans_dir / "2026-08-13-my-plan.md"
    own_plan.write_text(
        "---\nsource_memo: cross-repo/inbox/2026-08-13-some-memo.md\n---\nbody\n"
    )
    other_plan = plans_dir / "2026-08-12-other-plan.md"
    other_plan.write_text(
        "---\nsource_memo: cross-repo/inbox/2026-08-13-some-memo.md\n---\nbody\n"
    )
    _commit_all(tmp_path, "seed")

    ctx = _context(
        tmp_path,
        plan_path=own_plan,
        plan_frontmatter={
            "source_memo": "cross-repo/inbox/2026-08-13-some-memo.md"
        },
    )
    result = concurrent_preflight(ctx)
    leg_b = result["source_memo_collision"]
    assert leg_b["frontmatter_hit"] is True
    assert "2026-08-12-other-plan.md" in leg_b["frontmatter_hits"]
    assert own_plan.name not in leg_b["frontmatter_hits"]


def test_source_memo_body_mention_fallback(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    own_plan = plans_dir / "2026-08-13-my-plan.md"
    own_plan.write_text(
        "---\nsource_memo: cross-repo/inbox/2026-08-13-some-memo.md\n---\nbody\n"
    )
    mentioning_plan = plans_dir / "2026-08-12-mentions-it.md"
    mentioning_plan.write_text(
        "---\n---\nSee cross-repo/inbox/2026-08-13-some-memo.md for context.\n"
    )
    _commit_all(tmp_path, "seed")

    ctx = _context(
        tmp_path,
        plan_path=own_plan,
        plan_frontmatter={
            "source_memo": "cross-repo/inbox/2026-08-13-some-memo.md"
        },
    )
    result = concurrent_preflight(ctx)
    leg_b = result["source_memo_collision"]
    assert leg_b["frontmatter_hit"] is False
    assert leg_b["body_mention_hit"] is True
    assert "2026-08-12-mentions-it.md" in leg_b["body_mention_hits"]


def test_source_memo_body_only_line_is_not_a_frontmatter_hit(tmp_path: Path) -> None:
    """Regression: a plan whose BODY prose quotes `source_memo: <same
    basename>` verbatim (e.g. documenting another plan's frontmatter in
    prose) must not be picked up by the frontmatter-hit path — only the
    body-mention fallback bucket, since the line never appears inside the
    real `---`-delimited frontmatter block."""
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    own_plan = plans_dir / "2026-08-13-my-plan.md"
    own_plan.write_text(
        "---\nsource_memo: cross-repo/inbox/2026-08-13-some-memo.md\n---\nbody\n"
    )
    quoting_plan = plans_dir / "2026-08-12-quotes-it.md"
    quoting_plan.write_text(
        "---\n---\n"
        "The other plan's frontmatter reads:\n"
        "source_memo: cross-repo/inbox/2026-08-13-some-memo.md\n"
        "(quoted verbatim in prose, not real frontmatter here)\n"
    )
    _commit_all(tmp_path, "seed")

    ctx = _context(
        tmp_path,
        plan_path=own_plan,
        plan_frontmatter={
            "source_memo": "cross-repo/inbox/2026-08-13-some-memo.md"
        },
    )
    result = concurrent_preflight(ctx)
    leg_b = result["source_memo_collision"]
    assert leg_b["frontmatter_hit"] is False
    assert quoting_plan.name not in leg_b["frontmatter_hits"]
    assert leg_b["body_mention_hit"] is True
    assert quoting_plan.name in leg_b["body_mention_hits"]


def test_source_memo_no_collision(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)

    own_plan = plans_dir / "2026-08-13-my-plan.md"
    own_plan.write_text(
        "---\nsource_memo: cross-repo/inbox/2026-08-13-some-memo.md\n---\nbody\n"
    )
    unrelated_plan = plans_dir / "2026-08-12-unrelated.md"
    unrelated_plan.write_text("---\n---\nnothing here\n")
    _commit_all(tmp_path, "seed")

    ctx = _context(
        tmp_path,
        plan_path=own_plan,
        plan_frontmatter={
            "source_memo": "cross-repo/inbox/2026-08-13-some-memo.md"
        },
    )
    result = concurrent_preflight(ctx)
    leg_b = result["source_memo_collision"]
    assert leg_b["frontmatter_hit"] is False
    assert leg_b["body_mention_hit"] is False
