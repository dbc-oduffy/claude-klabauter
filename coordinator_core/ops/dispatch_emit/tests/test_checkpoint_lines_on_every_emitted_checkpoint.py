"""A script that composes `checkpoint(wave N)` commits carries the `Checkpoint-Plan:` and
`Checkpoint-Base:` lines `git.checkpoint_guard` requires; an emit that cannot carry them is refused."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.checkpoint_guard import checkpoint_refusal
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.win_portability import no_console_creationflags

from .conftest import REVIEW_KW

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN_REL = "docs/plans/p.md"
_PLAN = (
    '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n## Tasks\n\n'
    "```yaml plan-tasks\n"
    "- id: C1\n  title: t1\n  change_kind: doc-edit\n  surface: docs/x/C1.md\n"
    "  writes:\n    - docs/x/C1.md\n  queue_scope: project\n  disposition: open\n"
    "  body: |\n    Do C1.\n```\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _init(tmp_path: Path, *, commit: bool) -> Path:
    r = tmp_path / "repo"
    (r / "docs" / "plans").mkdir(parents=True)
    (r / _PLAN_REL).write_text(_PLAN, encoding="utf-8")
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    if commit:
        _git(r, "add", "--", _PLAN_REL)
        _git(r, "commit", "-q", "-m", "add plan")
    return r


@pytest.mark.parametrize("with_out", [False, True], ids=["default-path", "explicit-out"])
def test_every_checkpoint_script_carries_both_lines(tmp_path, with_out):
    repo = _init(tmp_path, commit=True)
    kw = {"script_path": (repo / "state" / "warp" / "fresh.workflow.mjs").as_posix()} if with_out else {}

    script = emit_script(repo / _PLAN_REL, repo_root=repo, **kw, **REVIEW_KW)

    assert "checkpoint(wave" in script
    assert f"Checkpoint-Plan: {_PLAN_REL}" in script
    assert "Checkpoint-Base: " in script


def test_template_message_passes_the_checkpoint_guard(tmp_path):
    repo = _init(tmp_path, commit=True)
    head = _git(repo, "rev-parse", "HEAD")
    script = emit_script(repo / _PLAN_REL, repo_root=repo, **REVIEW_KW)

    plan_line = re.search(r"Checkpoint-Plan: ([^\\\n']+)", script)
    assert plan_line is not None
    message = (
        "checkpoint(wave 1): 1 rows — C1"
        f"\n\nCheckpoint-Plan: {plan_line.group(1)}\nCheckpoint-Base: {head}"
    )

    assert checkpoint_refusal(repo, repo / ".git", head, message, ["docs/x/C1.md"]) is None


def test_a_head_resolving_to_no_commit_is_refused_before_any_script_exists(tmp_path):
    repo = _init(tmp_path, commit=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")

    with pytest.raises(ValueError, match="checkpoint_guard"):
        emit_script(repo / _PLAN_REL, repo_root=repo, **REVIEW_KW)
