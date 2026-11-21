
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract import apply_base
from coordinator_core.learn_lessons_pipeline import apply as llp_apply
from coordinator_core.session.declared_writes import declare_write
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_LESSON_PAGE = "docs" + "/wiki/lessons/page.md"

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
        "id": "d-mechanical-close",
        "op": "mechanical-close",
        "args": [],
        "depends_on": ["d-assert-outbox-empty"],
    },
    {
        "id": "d-age-sweep",
        "cli": "age-sweep-lessons",
        "args": ["sweep"],
        "depends_on": ["d-mechanical-close"],
    },
    {
        "id": "d-stamp-run-complete",
        "op": "stamp-run-complete",
        "args": ["runs-dir", "2026-01-01"],
        "depends_on": ["d-age-sweep"],
    },
]


@pytest.fixture(autouse=True)
def _admit_stamp_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """`execute_directives` pre-validates EVERY directive (including the
    `op` one) before dispatching any of them — `resolve_op` calls
    `assert_dispatchable`, which default-denies until C5 registers
    `learn_lessons_pipeline` in `ASSEMBLER_DISPATCHABLE`. Admit it for
    these tests so the pre-validation pass itself is not what's under
    test here."""
    monkeypatch.setattr(
        apply_base,
        "ASSEMBLER_DISPATCHABLE",
        {"learn_lessons_pipeline": frozenset({"stamp-run-complete", "mechanical-close"})},
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
            "mechanical-close": lambda args, repo_root: self._handler(
                "mechanical-close", args, repo_root
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


def test_default_param_apply_ends_in_post_run_finish_with_a_clean_run_scope(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _seed_repo(tmp_path)
    fake = _WritingDispatch(tmp_path)
    exit_code, report = _run(monkeypatch, tmp_path, fake)

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert report["post_run"]["exit_code"] == 0 and report["post_run"]["pipeline"] == "lessons"
    assert report["post_run"]["run_id"] == "learn-lessons-2026-01-01"
    assert (tmp_path / _LESSON_PAGE).read_text(encoding="utf-8").startswith(
        "---\ntitle: Page\n---\n# Page\n"
    )
    tracked = _git(tmp_path, "ls-files").splitlines()
    assert {_LESSON_PAGE, "docs" + "/wiki/lessons/_index.md", "state/lessons-run/COMPLETE"} <= set(tracked)
    porcelain = [
        ln[3:] for ln in _git(tmp_path, "status", "--porcelain", "-uall").splitlines()
        if not ln[3:].startswith(".coordinator-local/")
    ]
    assert porcelain == []
