
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract import apply_base
from coordinator_core.learn_lessons_pipeline import CONSUMES_MANIFEST
from coordinator_core.learn_lessons_pipeline import apply as llp_apply
from coordinator_core.ops import post_run_finish
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

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
    llp_apply._LOADED_MODULES.pop("age-sweep-lessons", None)
    with pytest.raises(RuntimeError, match=r"age-sweep-lessons exited"):
        llp_apply._dispatch_age_sweep_lessons([], tmp_path)


_SID = "00000000-0000-4000-8000-000000000000"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout


def _seed_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


class _WritingDispatch(_FakeDispatch):
    """Fake dispatch whose stamp step writes one wiki page and one evidence file, declaring both."""

    def __init__(self, root: Path) -> None:
        super().__init__()
        self.root = root

    def table(self) -> dict[str, Any]:
        table = super().table()

        def _stamp(args: list[str], repo_root: Path) -> dict[str, Any]:
            self.calls.append(("stamp-run-complete", list(args)))
            page = self.root / "docs" / "wiki" / "lessons" / "page.md"
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text("---\ntitle: Page\n---\n# Wrong\n\nBody.\n", encoding="utf-8")
            evidence = self.root / "state" / "lessons-run" / "COMPLETE"
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("done\n", encoding="utf-8")
            declare_write(page)
            declare_write(evidence)
            return {"exit_code": 0}

        table["stamp-run-complete"] = _stamp
        return table


def _record_finish(monkeypatch, tail: dict[str, Any]) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    def _fake(params: dict[str, Any], repo_root: Path | None = None) -> dict[str, Any]:
        seen.append({"params": params, "repo_root": repo_root})
        return tail

    monkeypatch.setattr(post_run_finish, "_handler", _fake)
    return seen


def test_default_param_apply_ends_in_post_run_finish_with_a_clean_run_scope(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _seed_repo(tmp_path)
    fake = _WritingDispatch(tmp_path)
    exit_code, report = _run(monkeypatch, tmp_path, fake)

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert report["post_run"]["exit_code"] == 0 and report["post_run"]["pipeline"] == "lessons"
    assert report["post_run"]["run_id"] == "learn-lessons-2026-01-01"
    assert (tmp_path / "docs/wiki/lessons/page.md").read_text(encoding="utf-8").startswith(
        "---\ntitle: Page\n---\n# Page\n"
    )
    tracked = _git(tmp_path, "ls-files").splitlines()
    assert {"docs/wiki/lessons/page.md", "docs/wiki/lessons/_index.md", "state/lessons-run/COMPLETE"} <= set(tracked)
    porcelain = [
        ln[3:] for ln in _git(tmp_path, "status", "--porcelain", "-uall").splitlines()
        if not ln[3:].startswith(".coordinator-local/")
    ]
    assert porcelain == []


def test_the_tail_takes_wiki_pages_as_touched_and_other_declared_writes_as_evidence(monkeypatch, tmp_path):
    seen = _record_finish(monkeypatch, {"exit_code": 0})
    exit_code, _report = _run(monkeypatch, tmp_path, _WritingDispatch(tmp_path))

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert len(seen) == 1
    assert seen[0]["repo_root"] == tmp_path
    assert seen[0]["params"] == {
        "pipeline": "lessons",
        "run_id": "learn-lessons-2026-01-01",
        "touched": ["docs/wiki/lessons/page.md"],
        "evidence": ["state/lessons-run/COMPLETE"],
    }


def test_a_non_zero_apply_exit_skips_the_tail(monkeypatch, tmp_path):
    seen = _record_finish(monkeypatch, {"exit_code": 0})
    exit_code, report = _run(monkeypatch, tmp_path, _FakeDispatch(fail_on=frozenset({"sweep"})))

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert seen == [] and "post_run" not in report


def test_a_tail_that_does_not_finish_clean_is_a_partial_mutation(monkeypatch, tmp_path):
    _record_finish(monkeypatch, {"exit_code": 1, "error": "commit refused"})
    exit_code, report = _run(monkeypatch, tmp_path, _FakeDispatch())

    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report["failed_directive"] == "post_run.finish"
    assert report["post_run"]["error"] == "commit refused"
