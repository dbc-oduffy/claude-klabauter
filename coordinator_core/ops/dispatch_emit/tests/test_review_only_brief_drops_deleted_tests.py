"""A review-only brief never hands the test runner a test file the reviewed range deleted."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.dispatch_emit.emit import compose_script, deleted_in_range
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit import _wave_row

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_GONE = "pkg/tests/test_gone.py"
_KEPT = "pkg/tests/test_kept.py"


def _git(root: Path, *args: str) -> str:
    out = run_git(["-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=str(root))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def _repo(tmp_path: Path) -> tuple[str, str]:
    _git(tmp_path, "init", "-q")
    (tmp_path / "pkg" / "tests").mkdir(parents=True)
    for rel in (_GONE, _KEPT, "pkg/mod.py"):
        (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / _GONE).unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "delete a test")
    return base, _git(tmp_path, "rev-parse", "HEAD")


def _test_prompt(script: str) -> str:
    start = script.index("phase('Scoped test run')")
    return script[start : script.index("test:terminal", start)]


def test_deleted_in_range_names_only_the_paths_the_range_deleted(tmp_path):
    base, head = _repo(tmp_path)

    assert deleted_in_range([_GONE, _KEPT], base, head, tmp_path) == {_GONE}


def test_deleted_in_range_is_empty_when_git_cannot_answer(tmp_path):
    assert deleted_in_range([_GONE], "a" * 40, "b" * 40, tmp_path) == set()


def test_review_only_brief_omits_a_test_file_the_range_deleted(tmp_path):
    base, head = _repo(tmp_path)
    waves = [[_wave_row("C1", [_GONE, _KEPT, "pkg/mod.py"])]]

    script = compose_script(
        waves,
        name="wf",
        description="d",
        repo_root=tmp_path,
        run_base_sha=base,
        review_only=True,
        review_terminal_sha=head,
        plan_path="docs/plans/p.md",
        **REVIEW_KW,
    )
    prompt = _test_prompt(script)

    assert "test_kept.py" in prompt
    assert "test_gone.py" not in prompt
