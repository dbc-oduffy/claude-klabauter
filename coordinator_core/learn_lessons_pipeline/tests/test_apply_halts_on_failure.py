"""Tests for `coordinator_core.learn_lessons_pipeline.apply` — AC1's halt
(§ D3): a non-zero exit from any dispatched directive stops every later
directive from dispatching at all.

Spec backlink: docs/plans/2026-09-11-the-lessons-pipeline-drains-without-a-ha.md § C4
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract import apply_base
from coordinator_core.learn_lessons_pipeline import CONSUMES_MANIFEST
from coordinator_core.learn_lessons_pipeline import apply as llp_apply

#: The six directives in C3's own emitted order/shape — id, cli-or-op, args,
#: depends_on. Mirrors `build_directives`'s output closely enough to drive
#: the real dispatch ladder without depending on `brief()`'s own I/O.
_DIRECTIVES: list[dict[str, Any]] = [
    {
        "id": "d-extract-lessons",
        "cli": "extract-lessons",
        "args": ["extract"],
        "depends_on": None,
    },
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


@pytest.fixture(autouse=True)
def _admit_stamp_op(monkeypatch):
    """`execute_directives` pre-validates EVERY directive (including the
    `op` one) before dispatching any of them — `resolve_op` calls
    `assert_dispatchable`, which default-denies until C5 registers
    `learn_lessons_pipeline` in `ASSEMBLER_DISPATCHABLE`. Admit it for
    these tests so the pre-validation pass itself is not what's under
    test here."""
    monkeypatch.setattr(
        apply_base,
        "ASSEMBLER_DISPATCHABLE",
        {"learn_lessons_pipeline": frozenset({"stamp-run-complete"})},
    )


class _FakeDispatch:
    """A monkeypatched dispatch table over the real ladder shape: each
    entry records `(cli_or_op_name, args)` into `calls` and raises for any
    call whose `args[0]` is in `fail_on`."""

    def __init__(self, fail_on: frozenset[str] = frozenset()) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.fail_on = fail_on

    def _handler(self, name: str, args: list[str], repo_root: Path) -> dict[str, Any]:
        self.calls.append((name, list(args)))
        if args and args[0] in self.fail_on:
            raise RuntimeError(f"{name} exited 1 (args={args!r}): simulated failure")
        return {"exit_code": 0}

    def table(self) -> dict[str, Any]:
        return {
            "extract-lessons": lambda args, repo_root: self._handler(
                "extract-lessons", args, repo_root
            ),
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


def _run(monkeypatch, tmp_path: Path, fake: _FakeDispatch) -> tuple[int, dict[str, Any]]:
    monkeypatch.setattr(llp_apply, "_DISPATCH_TABLE", fake.table())
    monkeypatch.setattr(
        llp_apply,
        "brief",
        lambda repo_root, *, roots=None: {
            "directives": _DIRECTIVES,
            "judgment_points": [],
        },
    )
    return llp_apply.apply(tmp_path)


def test_a_failing_verify_halts_before_drain_sweep_and_stamp(monkeypatch, tmp_path):
    fake = _FakeDispatch(fail_on=frozenset({"verify"}))
    exit_code, report = _run(monkeypatch, tmp_path, fake)

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report["failed_directive"] == "d-verify-extraction"
    assert report["landed"] == ["d-extract-lessons"]
    dispatched_names = [name for name, _args in fake.calls]
    assert dispatched_names == ["extract-lessons", "extract-lessons"]
    assert "lessons-outbox-drain" not in dispatched_names
    assert "age-sweep-lessons" not in dispatched_names
    assert "stamp-run-complete" not in dispatched_names


def test_b_all_green_run_lands_all_six_ids_in_emitted_order(monkeypatch, tmp_path):
    fake = _FakeDispatch(fail_on=frozenset())
    exit_code, report = _run(monkeypatch, tmp_path, fake)

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert report["landed"] == [d["id"] for d in _DIRECTIVES]


def test_c_failing_age_sweep_leaves_the_run_unstamped(monkeypatch, tmp_path):
    fake = _FakeDispatch(fail_on=frozenset({"sweep"}))
    exit_code, report = _run(monkeypatch, tmp_path, fake)

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report["failed_directive"] == "d-age-sweep"
    assert report["landed"] == [
        "d-extract-lessons",
        "d-verify-extraction",
        "d-drain-outbox",
        "d-assert-outbox-empty",
    ]
    dispatched_names = [name for name, _args in fake.calls]
    assert "stamp-run-complete" not in dispatched_names


def test_cli_dispatch_matches_consumes_manifest_and_run_stamp_is_in_neither():
    assert set(llp_apply._CLI_DISPATCH) == set(CONSUMES_MANIFEST)
    assert "run_stamp" not in llp_apply._CLI_DISPATCH
    assert "run_stamp" not in llp_apply._OP_DISPATCH
    assert "stamp-run-complete" not in llp_apply._CLI_DISPATCH
    assert set(CONSUMES_MANIFEST).isdisjoint(llp_apply._OP_DISPATCH)


def test_unknown_op_raises_rather_than_dispatching(monkeypatch):
    monkeypatch.setattr(
        apply_base,
        "ASSEMBLER_DISPATCHABLE",
        {"learn_lessons_pipeline": frozenset({"stamp-run-complete"})},
    )
    with pytest.raises(apply_base.UnrecognizedDirective):
        apply_base.resolve_op(
            llp_apply._DISPATCH_TABLE, "learn_lessons_pipeline", "not-a-real-op"
        )


def test_real_load_of_age_sweep_lessons_exercises_the_bin_import_path(tmp_path):
    """Loads `age-sweep-lessons.py` for real (not monkeypatched) and calls
    `main([])` in-process — proves `_ensure_import_path`'s bin-dir/`lib`
    eviction wiring actually runs for this CLI, per C4's body ("At least
    one C4 test loads age-sweep-lessons.py for real ... so this import
    path is actually exercised"). No `--before`/`--days`/`path` argv means
    `main` still reaches its own argparse rejection (exit 2) AFTER the
    `import lib` / `cc_invoke.ensure_engine_on_path` bootstrap has already
    run without raising — that bootstrap succeeding is what this test is
    for."""
    llp_apply._LOADED_MODULES.pop("age-sweep-lessons", None)
    with pytest.raises(RuntimeError, match=r"age-sweep-lessons exited"):
        llp_apply._dispatch_age_sweep_lessons([], tmp_path)
