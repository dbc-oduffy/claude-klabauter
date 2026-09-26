"""Regression tests for the 2026-09-24 example-game-repo-em lessons-grind memo's three
re-confirmed sub-claims (R00's 2026-09-26 reconfirm audit, item 23a/b/c):

    23a: a universal-scoped row must not be swept by a fixer before it has
         either drained through the outbox or the outbox is confirmed
         empty — `d-age-sweep` gates on `d-assert-outbox-empty`.
    23b: a dispatched fixer's WRITE-target path is refused when it resolves
         outside `--repo-root` (`apply._assert_write_targets_in_repo_root`).
    23c: `coordinator-lesson-promote` prints a repo-relative outbox path,
         never an absolute host path.

Spec backlink: docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md row R23.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract import apply_base
from coordinator_core.learn_lessons_pipeline import apply as llp_apply
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


# ---------------------------------------------------------------------------
# 23a — universal rows never reach the age-sweep fixer ahead of a confirmed-
# empty (or successfully drained) outbox.
# ---------------------------------------------------------------------------

_DIRECTIVES: list[dict[str, Any]] = [
    {"id": "d-extract-lessons", "cli": "extract-lessons", "args": ["extract"], "depends_on": None},
    {
        "id": "d-verify-extraction",
        "cli": "extract-lessons",
        "args": ["verify"],
        "depends_on": ["d-extract-lessons"],
    },
    {
        "id": "d-drain-outbox",
        "cli": "lessons-outbox-drain",
        "args": ["read"],
        "depends_on": ["d-verify-extraction"],
    },
    {
        "id": "d-assert-outbox-empty",
        "cli": "lessons-outbox-drain",
        "args": ["assert-empty"],
        "depends_on": ["d-drain-outbox"],
    },
    {
        "id": "d-age-sweep",
        "cli": "age-sweep-lessons",
        "args": ["sweep"],
        "depends_on": ["d-assert-outbox-empty"],
    },
    {
        "id": "d-stamp-run-complete",
        "op": "stamp-run-complete",
        "args": ["runs-dir", "2026-01-01"],
        "depends_on": ["d-age-sweep"],
    },
]


class _FakeDispatch:

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[str] = []
        self.fail_on = fail_on

    def _handler(self, name: str, args: list[str], repo_root: Path) -> dict[str, Any]:
        self.calls.append(name)
        if args and args[0] in self.fail_on:
            raise RuntimeError(f"{name} exited 1 (args={args!r}): simulated failure")
        return {"exit_code": 0}

    def table(self) -> dict[str, Any]:
        return {
            "extract-lessons": lambda args, repo_root: self._handler("extract-lessons", args, repo_root),
            "lessons-outbox-drain": lambda args, repo_root: self._handler(
                "lessons-outbox-drain", args, repo_root
            ),
            "age-sweep-lessons": lambda args, repo_root: self._handler(
                "age-sweep-lessons", args, repo_root
            ),
            "stamp-run-complete": lambda args, repo_root: self._handler(
                "stamp-run-complete", args, repo_root
            ),
        }


@pytest.fixture(autouse=True)
def _admit_stamp_op(monkeypatch):
    monkeypatch.setattr(
        apply_base,
        "ASSEMBLER_DISPATCHABLE",
        {"learn_lessons_pipeline": frozenset({"stamp-run-complete"})},
    )


def test_23a_unconfirmed_outbox_withholds_the_age_sweep_fixer(monkeypatch, tmp_path):
    """A universal-scoped row still sitting undrained in the outbox makes
    `assert-empty` fail (the CLI's own real refusal) — `d-age-sweep`, the one
    directive that mutates/archives lesson entries, must never dispatch past
    that point, and its downstream stamp must not either."""
    fake = _FakeDispatch(fail_on=frozenset({"assert-empty"}))
    monkeypatch.setattr(llp_apply, "_DISPATCH_TABLE", fake.table())
    monkeypatch.setattr(
        llp_apply, "brief", lambda repo_root, *, roots=None: {"directives": _DIRECTIVES, "judgment_points": []}
    )

    exit_code, report = llp_apply.apply(tmp_path)

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report["failed_directive"] == "d-assert-outbox-empty"
    assert "age-sweep-lessons" not in fake.calls
    assert "stamp-run-complete" not in fake.calls


# ---------------------------------------------------------------------------
# 23b — a dispatched fixer's write-target path is refused outside repo_root.
# ---------------------------------------------------------------------------


def test_23b_extract_write_target_outside_repo_root_is_refused(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sibling = tmp_path / "sibling-repo"
    sibling.mkdir()
    outside_extraction = sibling / "state" / "lessons" / "extracted.yaml"

    with pytest.raises(apply_base.OutOfRepoPath):
        llp_apply._assert_write_targets_in_repo_root(
            "extract-lessons",
            ["extract", str(repo_root / "state" / "lessons"), "-o", str(outside_extraction)],
            repo_root,
        )


def test_23b_age_sweep_write_target_outside_repo_root_is_refused(tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sibling_lessons = tmp_path / "sibling-repo" / "state" / "lessons"

    with pytest.raises(apply_base.OutOfRepoPath):
        llp_apply._assert_write_targets_in_repo_root(
            "age-sweep-lessons", [str(sibling_lessons), "--before", "2026-01-01", "--apply"], repo_root
        )


def test_23b_drain_read_roots_are_never_containment_checked(tmp_path):
    """`lessons-outbox-drain read <root>...` legitimately reads PEER repo
    roots outside `repo_root` — containment must not refuse the drain
    itself, only a WRITE target."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    peer_root = tmp_path / "peer-repo"
    peer_root.mkdir()

    llp_apply._assert_write_targets_in_repo_root(
        "lessons-outbox-drain", ["read", str(peer_root)], repo_root
    )


def test_23b_dispatch_of_an_out_of_root_write_target_halts_the_run(monkeypatch, tmp_path):
    """Real `_DISPATCH_TABLE` (real `_dispatch_age_sweep_lessons` ->
    `_run_cli`): the containment check fires and raises BEFORE
    `_run_cli` ever loads/invokes the real `age-sweep-lessons.py` script,
    so no monkeypatch of the dispatch table itself is needed here."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    sibling_lessons = tmp_path / "sibling-repo" / "state" / "lessons"

    directives = [
        {
            "id": "d-age-sweep",
            "cli": "age-sweep-lessons",
            "args": [str(sibling_lessons), "--before", "2026-01-01", "--apply"],
            "depends_on": None,
        },
    ]
    monkeypatch.setattr(
        llp_apply, "brief", lambda repo_root, *, roots=None: {"directives": directives, "judgment_points": []}
    )

    exit_code, report = llp_apply.apply(repo_root)

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report["failed_directive"] == "d-age-sweep"
    assert "outside repo root" in report["error"]


# ---------------------------------------------------------------------------
# 23c — coordinator-lesson-promote prints a repo-relative outbox path.
# ---------------------------------------------------------------------------


def _lesson_promote_script() -> str:
    here = Path(__file__).resolve()
    # coordinator_core/learn_lessons_pipeline/tests/ -> repo root -> coordinator/bin/
    repo_root = here.parents[3]
    return str(repo_root / "coordinator" / "bin" / "coordinator-lesson-promote.py")


def test_23c_lesson_promote_prints_a_repo_relative_outbox_path(tmp_path):
    doe_root = tmp_path / "doe-claude"
    (doe_root / "state" / "lessons-outbox").mkdir(parents=True)

    env = {**os.environ, "DOE_ROOT": str(doe_root)}
    env.pop("REPO_DOE_CLAUDE", None)

    result = subprocess.run(
        [
            sys.executable,
            _lesson_promote_script(),
            "--title",
            "23c repo-relative path test",
            "--body",
            "the printed path must be repo-relative, not an absolute host path",
            "--change-kind",
            "doctrine-edit",
            "--target-wiki",
            "unknown",
        ],
        env=env,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )

    assert result.returncode == 0, result.stderr
    written_line = next(
        line for line in result.stdout.splitlines() if line.startswith("Lesson outbox entry written:")
    )
    printed_path = written_line.split(":", 1)[1].strip()
    assert not os.path.isabs(printed_path), printed_path
    assert printed_path.startswith("state/lessons-outbox/"), printed_path
    assert str(doe_root) not in printed_path
