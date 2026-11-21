"""eviction_callers: removed definitions and the callers an eviction would strand."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit.eviction_callers import UNJUDGEABLE, stranded_callers

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE_HEAD = b"def shading_exempt():\n    return 1\n\n\ndef keep():\n    return 2\n"
BASE_RESET = b"def keep():\n    return 2\n"
MAIN_REL = b"from . import base\n\nprint(base.shading_exempt())\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _repo(tmp_path: Path, files: dict[str, bytes]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _fixture(tmp_path: Path, main: bytes = MAIN_REL) -> Path:
    return _repo(tmp_path, {
        "pkg/__init__.py": b"",
        "pkg/base.py": BASE_HEAD,
        "pkg/__main__.py": main,
        "pkg/other.py": b"shading_exempt = 3\n",
        "docs/note.py": b"# shading_exempt is mentioned here\n",
    })


REMOVED = {"pkg/base.py": {"shading_exempt"}}
AFTER = {"pkg/base.py": BASE_RESET}


def test_committed_caller_of_removed_symbol_is_returned(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    got = stranded_callers(root, REMOVED, AFTER)
    assert list(got) == ["pkg/base.py"]
    [(caller, why)] = got["pkg/base.py"]
    assert caller == "pkg/__main__.py"
    assert "shading_exempt" in why


@pytest.mark.parametrize("main", [
    b"from .base import shading_exempt\n\nshading_exempt()\n",
    b"from pkg import base\n\nbase.shading_exempt()\n",
    b"import pkg.base\n\npkg.base.shading_exempt()\n",
    b"from pkg.base import *\n\nshading_exempt()\n",
])
def test_import_shapes_are_resolved(tmp_path: Path, main: bytes) -> None:
    root = _fixture(tmp_path, main)
    got = stranded_callers(root, REMOVED, AFTER)
    assert [c for c, _ in got.get("pkg/base.py", [])] == ["pkg/__main__.py"]


def test_same_directory_bare_import_is_resolved(tmp_path: Path) -> None:
    root = _fixture(tmp_path, b"import base\n\nbase.shading_exempt()\n")
    got = stranded_callers(root, REMOVED, AFTER)
    assert [c for c, _ in got["pkg/base.py"]] == ["pkg/__main__.py"]


def test_name_mention_without_import_is_not_a_caller(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    got = stranded_callers(root, REMOVED, AFTER)
    assert [c for c, _ in got["pkg/base.py"]] == ["pkg/__main__.py"]  # other.py, note.py do not import base


def test_caller_that_does_not_use_the_removed_name_is_not_returned(tmp_path: Path) -> None:
    root = _fixture(tmp_path, b"from . import base\n\nprint(base.keep())\n")
    assert stranded_callers(root, REMOVED, AFTER) == {}


def test_dirty_caller_is_judged_on_its_tree_bytes(tmp_path: Path) -> None:
    root = _fixture(tmp_path, b"from . import base\n\nprint(base.keep())\n")
    (root / "pkg/__main__.py").write_bytes(MAIN_REL)
    got = stranded_callers(root, REMOVED, AFTER)
    assert [c for c, _ in got["pkg/base.py"]] == ["pkg/__main__.py"]


def test_planned_caller_is_judged_on_its_reset_bytes(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    reset_clean = b"from . import base\n\nprint(base.keep())\n"
    assert stranded_callers(root, REMOVED, {**AFTER, "pkg/__main__.py": reset_clean}) == {}
    still = stranded_callers(root, REMOVED, {**AFTER, "pkg/__main__.py": MAIN_REL})
    assert [c for c, _ in still["pkg/base.py"]] == ["pkg/__main__.py"]
    assert stranded_callers(root, REMOVED, {**AFTER, "pkg/__main__.py": None}) == {}


def test_planned_caller_is_a_candidate_even_if_grep_would_miss_its_reset(tmp_path: Path) -> None:
    root = _fixture(tmp_path, b"print('clean worktree')\n")
    got = stranded_callers(root, REMOVED, {**AFTER, "pkg/__main__.py": MAIN_REL})
    assert [c for c, _ in got["pkg/base.py"]] == ["pkg/__main__.py"]


def test_crlf_caller_is_judged(tmp_path: Path) -> None:
    root = _fixture(tmp_path, MAIN_REL.replace(b"\n", b"\r\n"))
    got = stranded_callers(root, REMOVED, AFTER)
    assert [c for c, _ in got["pkg/base.py"]] == ["pkg/__main__.py"]


def test_one_git_process_and_under_200ms_process_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path)
    calls: list[list[str]] = []
    real = git_run.run_git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    start = time.process_time()
    got = stranded_callers(root, REMOVED, AFTER)
    spent = time.process_time() - start
    assert got["pkg/base.py"]
    assert len(calls) == 1 and "grep" in calls[0]
    assert spent < 0.2


def test_unjudgeable_when_the_scan_fails_so_the_eviction_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _fixture(tmp_path)

    def broken(*a, **k):
        raise OSError("git missing")

    monkeypatch.setattr(git_run, "run_git", broken)
    got = stranded_callers(root, REMOVED, AFTER)
    assert got["pkg/base.py"][0][0] == UNJUDGEABLE
    assert "git missing" in got["pkg/base.py"][0][1]
