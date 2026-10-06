"""The commit route refuses an execute-run checkpoint that would re-land a
row the EM closed on the spine or reverted outside the run."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_PLAN = """---
title: p
---
# P

## Tasks

```yaml plan-tasks
- id: A1
  title: a
  status: {a1}
  writes: [src/a.py]
- id: B1
  title: b
  writes: [src/b.py]
```
"""


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), capture_output=True, check=True, **_NOWIN)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "r"
    (r / "src").mkdir(parents=True)
    (r / "docs").mkdir()
    _git(r, "init", "-q", "-b", "work/z")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    (r / "src/a.py").write_text("v0\n", encoding="utf-8", newline="\n")
    (r / "docs/p.md").write_text(_PLAN.format(a1="open"), encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def _message(base, rows="A1"):
    n = len(rows.split(", "))
    return (
        f"checkpoint(wave 1): {n} rows — {rows}\n\n"
        f"Checkpoint-Plan: docs/p.md\nCheckpoint-Base: {base}"
    )


def _write(repo, path, text):
    (repo / path).write_text(text, encoding="utf-8", newline="\n")


def test_a_clean_checkpoint_lands(repo):
    base = gcommit.head_sha(repo)
    _write(repo, "src/a.py", "v1\n")
    assert gcommit.commit_paths(repo, ["src/a.py"], _message(base))


def test_a_row_closed_on_the_spine_is_refused(repo):
    base = gcommit.head_sha(repo)
    _write(repo, "docs/p.md", _PLAN.format(a1="wont_do"))
    gcommit.commit_paths(repo, ["docs/p.md"], "plan: A1 wont_do")
    _write(repo, "src/a.py", "v1\n")
    with pytest.raises(CommitRefused, match="closed on the spine: A1"):
        gcommit.commit_paths(repo, ["src/a.py"], _message(base))


def test_writes_reverted_outside_the_run_are_refused(repo):
    base = gcommit.head_sha(repo)
    _write(repo, "src/a.py", "v1\n")
    gcommit.commit_paths(repo, ["src/a.py"], _message(base))
    _write(repo, "src/a.py", "v0\n")
    gcommit.commit_paths(repo, ["src/a.py"], "revert A1")
    _write(repo, "src/a.py", "v1\n")
    with pytest.raises(CommitRefused, match="changed outside this run: src/a.py"):
        gcommit.commit_paths(repo, ["src/a.py"], _message(base))


def test_a_checkpoint_without_the_guard_lines_is_refused(repo):
    _write(repo, "src/a.py", "v1\n")
    with pytest.raises(CommitRefused, match="re-emit"):
        gcommit.commit_paths(repo, ["src/a.py"], "checkpoint(wave 1): 1 rows — A1")


def test_an_ordinary_commit_is_untouched(repo):
    _write(repo, "src/a.py", "v1\n")
    assert gcommit.commit_paths(repo, ["src/a.py"], "fix a")
