"""
coordinator_core.plan_assemble.predicates.test_supersedes_index — unit
coverage for `supersedes_index.supersedes_plan`, chunk C7's row `:164`
producer.

Purpose: verify the reverse-index build/lookup against fixture plan
directories (never the live `docs/plans/`), and pin the ruling this chunk
must not re-litigate: no `supersedes:` key anywhere in the read/write path.

Negative-spec:
  - Does NOT read or write `coordinator_core/frontmatter/schemas/plan.schema.json`.
  - Does NOT exercise the live repo's `docs/plans/` directory — every case
    constructs its own fixture directory under `tmp_path`.

Spec backlink: pln-plan-assemble-wave-2-the-predi-fad89b, chunk C7
"""
from __future__ import annotations

from pathlib import Path

from coordinator_core.plan_assemble.predicates import PredicateContext
from coordinator_core.plan_assemble.predicates.supersedes_index import supersedes_plan


def _write_plan(plans_dir: Path, filename: str, frontmatter_lines: list[str]) -> Path:
    plans_dir.mkdir(parents=True, exist_ok=True)
    text = "---\n" + "\n".join(frontmatter_lines) + "\n---\nbody\n"
    fpath = plans_dir / filename
    fpath.write_text(text, encoding="utf-8")
    return fpath


def _ctx(repo_root: Path, plan_path: Path) -> PredicateContext:
    return PredicateContext.from_paths(
        repo_root=repo_root,
        plan_path=plan_path,
        sizing_object_path=None,
        resolved_route="plan",
    )


def test_present_true_when_a_successor_declares_superseded_by(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    predecessor = _write_plan(
        plans_dir, "old-plan.md",
        ["title: Old plan", "created: 2026-01-01", "author: a", "status: superseded"],
    )
    _write_plan(
        plans_dir, "new-plan.md",
        [
            "title: New plan",
            "created: 2026-02-01",
            "author: a",
            "status: draft",
            "superseded_by_placeholder: ignore-me",
        ],
    )
    _write_plan(
        plans_dir, "actual-successor.md",
        [
            "title: Actual successor",
            "created: 2026-02-02",
            "author: a",
            "status: draft",
            "superseded_by: old-plan",
        ],
    )

    ctx = _ctx(tmp_path, predecessor)
    result = supersedes_plan(ctx)

    assert result == {"present": True, "target": "actual-successor"}


def test_present_false_when_nothing_declares_this_plan_as_predecessor(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    lone = _write_plan(
        plans_dir, "lone-plan.md",
        ["title: Lone plan", "created: 2026-01-01", "author: a", "status: draft"],
    )

    ctx = _ctx(tmp_path, lone)
    result = supersedes_plan(ctx)

    assert result == {"present": False, "target": None}


def test_lookup_matches_by_plan_id_frontmatter_not_just_filename_stem(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    predecessor = _write_plan(
        plans_dir, "predecessor-file.md",
        [
            "title: Predecessor",
            "created: 2026-01-01",
            "author: a",
            "status: superseded",
            "plan_id: predecessor-slug",
        ],
    )
    _write_plan(
        plans_dir, "successor-file.md",
        [
            "title: Successor",
            "created: 2026-01-02",
            "author: a",
            "status: draft",
            "superseded_by: predecessor-slug",
        ],
    )

    ctx = _ctx(tmp_path, predecessor)
    result = supersedes_plan(ctx)

    assert result["present"] is True
    assert result["target"] == "successor-file"


def test_undetermined_when_no_plan_supplied(tmp_path: Path) -> None:
    ctx = PredicateContext.from_paths(
        repo_root=tmp_path,
        plan_path=None,
        sizing_object_path=None,
        resolved_route="plan",
    )

    result = supersedes_plan(ctx)

    assert result["undetermined"] is True
    assert "no --plan" in result["reason"]


def test_graceful_absent_plans_dir(tmp_path: Path) -> None:
    plan_path = tmp_path / "docs" / "plans" / "solo.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\ntitle: Solo\ncreated: 2026-01-01\nauthor: a\nstatus: draft\n---\nbody\n",
        encoding="utf-8",
    )
    other_repo_root = tmp_path / "no-plans-dir-here"
    other_repo_root.mkdir()

    ctx = PredicateContext.from_paths(
        repo_root=other_repo_root,
        plan_path=plan_path,
        sizing_object_path=None,
        resolved_route="plan",
    )

    result = supersedes_plan(ctx)

    assert result == {"present": False, "target": None}


def test_malformed_sibling_plan_is_quarantined_not_raised(tmp_path: Path) -> None:
    plans_dir = tmp_path / "docs" / "plans"
    plans_dir.mkdir(parents=True)
    (plans_dir / "broken.md").write_text("not frontmatter at all\n", encoding="utf-8")
    predecessor = _write_plan(
        plans_dir, "ok-plan.md",
        ["title: OK plan", "created: 2026-01-01", "author: a", "status: draft"],
    )

    ctx = _ctx(tmp_path, predecessor)
    result = supersedes_plan(ctx)

    assert result == {"present": False, "target": None}
