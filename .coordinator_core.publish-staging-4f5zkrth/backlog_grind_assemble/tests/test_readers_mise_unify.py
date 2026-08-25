"""
coordinator_core.backlog_grind_assemble.tests.test_readers_mise_unify —
C8 (`docs/plans/2026-08-19-batons-unify-into-one-successor.md`): `/mise`
inherits execution batons by the `baton_role` axis, with the path-shape
heuristic as a COUNTED fallback where the axis is absent, and unifies its
inheritable set into ONE directive per run — never one per item.

Exercises `readers_mise._read_baton_unification` (and `collect()`'s own
wiring of it) directly, the same idiom `test_backlog_grind_assemble.py`
already uses for `_read_phase_6_review_scale` — a real on-disk
`state/mise-inventory/<run-id>.md` record, `_resolve_state_root`
monkeypatched to the fixture root, no other reader stubbed.

Spec backlink: docs/plans/2026-08-19-batons-unify-into-one-successor.md,
chunk C8.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from coordinator_core.backlog_grind_assemble import readers_mise

_START_SHA = "a" * 40
_RUN_ID = "run-unify"


def _write_inventory_record(
    state_root: Path,
    run_id: str,
    rows: list[str],
    *,
    start_sha: str = _START_SHA,
) -> Path:
    inventory_dir = state_root / "mise-inventory"
    inventory_dir.mkdir(parents=True, exist_ok=True)
    record = inventory_dir / f"{run_id}.md"
    body = "\n".join(rows)
    record.write_text(
        "---\n"
        f"run_id: {run_id}\n"
        f"start_sha: {start_sha}\n"
        "---\n\n"
        "| identifier | spec path | disposition |\n"
        "| --- | --- | --- |\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return record


def _write_handoff(repo_root: Path, rel_path: str, *, baton_role: Optional[str] = None) -> None:
    full = repo_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    fm_role = f"baton_role: {baton_role}\n" if baton_role is not None else ""
    full.write_text(
        "---\n"
        "title: fixture\n"
        f"{fm_role}"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )


def _write_todo(repo_root: Path, rel_path: str) -> None:
    full = repo_root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text("# todo\n\n- [ ] item\n", encoding="utf-8")


class TestUnifiesOncePerRun:
    def test_three_inheritable_batons_yield_one_directive_with_all_as_fan_in_legs(
        self, tmp_path, monkeypatch
    ):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_handoff(tmp_path, "docs/plans/2026-08-19-a.md", baton_role="work")
        _write_handoff(tmp_path, "state/handoffs/2026-08-19-b.md", baton_role="work")
        _write_todo(tmp_path, "tasks/c/todo.md")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=[
                "| A | docs/plans/2026-08-19-a.md | in_progress |",
                "| B | state/handoffs/2026-08-19-b.md | in_progress |",
                "| C | tasks/c/todo.md | in_progress |",
            ],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert len(result.directives) == 1
        directive = result.directives[0]
        assert directive["id"] == readers_mise._MISE_UNIFY_DIRECTIVE_ID
        assert sorted(directive["additional_predecessors"]) == sorted(
            [
                "docs/plans/2026-08-19-a.md",
                "state/handoffs/2026-08-19-b.md",
                "tasks/c/todo.md",
            ]
        )

    def test_non_inheritable_artifact_is_not_inherited(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_handoff(tmp_path, "docs/plans/2026-08-19-work.md", baton_role="work")
        _write_handoff(tmp_path, "state/handoffs/2026-08-19-record.md", baton_role="record")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=[
                "| A | docs/plans/2026-08-19-work.md | in_progress |",
                "| B | state/handoffs/2026-08-19-record.md | in_progress |",
            ],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert len(result.directives) == 1
        directive = result.directives[0]
        assert directive["additional_predecessors"] == ["docs/plans/2026-08-19-work.md"]

    def test_run_unifies_exactly_once_never_per_item(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_handoff(tmp_path, "docs/plans/2026-08-19-x.md", baton_role="work")
        _write_handoff(tmp_path, "docs/plans/2026-08-19-y.md", baton_role="work")
        _write_handoff(tmp_path, "docs/plans/2026-08-19-z.md", baton_role="work")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=[
                "| A | docs/plans/2026-08-19-x.md | in_progress |",
                "| B | docs/plans/2026-08-19-y.md | in_progress |",
                "| C | docs/plans/2026-08-19-z.md | in_progress |",
            ],
        )

        result = readers_mise.collect("mise-en-place", run_id=_RUN_ID)

        unify_directives = [
            d for d in result.directives
            if d["id"] == readers_mise._MISE_UNIFY_DIRECTIVE_ID
        ]
        assert len(unify_directives) == 1
        assert len(unify_directives[0]["additional_predecessors"]) == 3

    def test_no_inheritable_batons_emits_no_directive(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_handoff(tmp_path, "docs/plans/2026-08-19-only-record.md", baton_role="record")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=["| A | docs/plans/2026-08-19-only-record.md | in_progress |"],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert result.directives == []


class TestRoleAxisFallbackCounted:
    def test_absent_axis_falls_back_to_heuristic_and_is_counted(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        # No `baton_role` at all -- legacy record, absence is unknown.
        _write_handoff(tmp_path, "docs/plans/2026-08-19-legacy.md", baton_role=None)
        _write_handoff(tmp_path, "state/handoffs/2026-08-19-stamped.md", baton_role="work")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=[
                "| A | docs/plans/2026-08-19-legacy.md | in_progress |",
                "| B | state/handoffs/2026-08-19-stamped.md | in_progress |",
            ],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert len(result.directives) == 1
        directive = result.directives[0]
        # Both are still inheritable: the legacy leg via the counted
        # fallback, the stamped leg via the axis.
        assert sorted(directive["additional_predecessors"]) == sorted(
            [
                "docs/plans/2026-08-19-legacy.md",
                "state/handoffs/2026-08-19-stamped.md",
            ]
        )
        assert directive["role_axis_fallback_count"] == 1

    def test_todo_leg_permanently_on_the_fallback_but_not_counted(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_todo(tmp_path, "tasks/perm/todo.md")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=["| A | tasks/perm/todo.md | in_progress |"],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert len(result.directives) == 1
        directive = result.directives[0]
        assert directive["additional_predecessors"] == ["tasks/perm/todo.md"]
        # Frontmatter-less legs are excluded from the fallback count's
        # denominator (the plan's resolved frontmatter-less-leg note) --
        # the retirement gate is scoped to frontmatter-bearing artifacts.
        assert directive["role_axis_fallback_count"] == 0

    def test_zero_fallback_when_every_leg_is_stamped(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        _write_handoff(tmp_path, "docs/plans/2026-08-19-stamped-1.md", baton_role="work")
        _write_handoff(tmp_path, "docs/plans/2026-08-19-stamped-2.md", baton_role="work")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=[
                "| A | docs/plans/2026-08-19-stamped-1.md | in_progress |",
                "| B | docs/plans/2026-08-19-stamped-2.md | in_progress |",
            ],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        directive = result.directives[0]
        assert directive["role_axis_fallback_count"] == 0

    def test_unrecognised_role_value_is_treated_as_unknown_not_as_work(
        self, tmp_path, monkeypatch
    ):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        # `execution` was ruled against explicitly -- must never be read as
        # a positive match for either enum member; falls to the counted
        # heuristic fallback instead.
        _write_handoff(tmp_path, "docs/plans/2026-08-19-weird.md", baton_role="execution")

        _write_inventory_record(
            state_root,
            _RUN_ID,
            rows=["| A | docs/plans/2026-08-19-weird.md | in_progress |"],
        )

        result = readers_mise._read_baton_unification(_RUN_ID)

        directive = result.directives[0]
        assert directive["additional_predecessors"] == ["docs/plans/2026-08-19-weird.md"]
        assert directive["role_axis_fallback_count"] == 1


class TestGating:
    def test_absent_run_id_emits_no_directive(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))
        (state_root / "mise-inventory").mkdir(parents=True)

        result = readers_mise._read_baton_unification(None)

        assert result.directives == []

    def test_phase_0_absent_inventory_dir_emits_no_directive(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))

        result = readers_mise._read_baton_unification(_RUN_ID)

        assert result.directives == []

    def test_unresolvable_record_emits_no_directive(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        monkeypatch.setattr(readers_mise, "_resolve_state_root", lambda: str(state_root))
        (state_root / "mise-inventory").mkdir(parents=True)

        result = readers_mise._read_baton_unification("run-missing")

        assert result.directives == []
