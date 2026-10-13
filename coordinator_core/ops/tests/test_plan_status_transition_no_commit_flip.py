"""stamp-implemented --no-commit: writes the plan, runs every refusal, commits nothing."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.archive_stamp import cs_stamp_plan_implemented
from coordinator_core.ops.plan_status_transition import main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_PLAN = """---
title: T
status: executing
owner: x
---

# Plan

## Tasks

```yaml plan-tasks
- id: C1
  title: only row
  change_kind: code-edit
  surface: a.py
  writes:
    - a.py
  disposition: {disposition}
  body: |
    do it
```
"""


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, env=_ENV, timeout=30,
        **no_console_creationflags(),
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


def _repo(root: Path, disposition: str) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    plan = root / "p.md"
    plan.write_text(_PLAN.format(disposition="open"), encoding="utf-8")
    _git(root, "add", "--", "p.md")
    _git(root, "commit", "-q", "-m", "initial")
    # Rows coded on disk but not yet committed: the state the terminal commit calls the flip in.
    plan.write_text(_PLAN.format(disposition=disposition), encoding="utf-8")
    return plan


def test_no_commit_leaves_plan_modified_and_head_unmoved(tmp_path):
    plan = _repo(tmp_path / "r", "coded")
    head = _git(plan.parent, "rev-parse", "HEAD")

    rc = main(["stamp-implemented", "--plan", str(plan), "--no-commit"])

    assert rc == 0
    assert "status: implemented" in plan.read_text(encoding="utf-8")
    assert _git(plan.parent, "rev-parse", "HEAD") == head
    assert _git(plan.parent, "status", "--porcelain", "--", "p.md").startswith(" M")


def test_frontmatter_identical_to_committing_path(tmp_path):
    a = _repo(tmp_path / "a", "coded")
    b = _repo(tmp_path / "b", "coded")

    assert main(["stamp-implemented", "--plan", str(a), "--no-commit"]) == 0
    assert main(["stamp-implemented", "--plan", str(b)]) == 0

    assert a.read_text(encoding="utf-8") == b.read_text(encoding="utf-8")
    assert _git(b.parent, "status", "--porcelain", "--", "p.md") == ""


def test_open_spine_rows_refuse_identically(tmp_path, capsys):
    a = _repo(tmp_path / "a", "open")
    b = _repo(tmp_path / "b", "open")

    rc_no = cs_stamp_plan_implemented(str(a), refuse_open_spine_rows=True, commit=False)
    err_no = capsys.readouterr().err.replace(str(a), "<plan>")
    rc_yes = cs_stamp_plan_implemented(str(b), refuse_open_spine_rows=True)
    err_yes = capsys.readouterr().err.replace(str(b), "<plan>")

    assert rc_no == rc_yes == 1
    assert err_no == err_yes
    assert "status: executing" in a.read_text(encoding="utf-8")


def test_status_refusal_fires_with_no_commit(tmp_path, capsys):
    plan = _repo(tmp_path / "r", "coded")
    text = plan.read_text(encoding="utf-8").replace("status: executing", "status: blocked")
    plan.write_text(text, encoding="utf-8")

    assert main(["stamp-implemented", "--plan", str(plan), "--no-commit"]) == 1
    assert plan.read_text(encoding="utf-8") == text


def test_no_commit_is_rejected_on_other_verbs(tmp_path, capsys):
    plan = _repo(tmp_path / "r", "coded")

    assert main(["stamp-superseded", "--plan", str(plan), "--by", "x.md", "--no-commit"]) == 1
    assert "--no-commit" in capsys.readouterr().err
