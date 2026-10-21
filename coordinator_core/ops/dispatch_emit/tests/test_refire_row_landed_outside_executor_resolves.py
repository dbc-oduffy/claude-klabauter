"""A re-fire resolves an open row whose every declared write a commit after the plan's creation landed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.plan_row_coding import rows_landed_outside_executor

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plans/p.md"


def _plan(rows: dict) -> str:
    body = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {w[0]}\n  writes: {list(w)}\n"
        f"  disposition: open\n  deferred: false\n"
        for i, w in rows.items()
    )
    return f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(root: Path, rows: dict) -> Path:
    _git(root, "init", "-q")
    _commit(root, {"seed": "0", "old.py": "0"}, "seed")
    _commit(root, {_REL: _plan(rows)}, "plan")
    return root


def test_row_with_every_write_committed_after_the_plan_resolves(tmp_path):
    repo = _repo(tmp_path, {"C1": ("c1.py",), "C5": ("c5.py",)})
    landed = _commit(repo, {"c1.py": "1"}, "hand edit")
    _commit(repo, {"later": "1"}, "later")
    plan = (repo / _REL).read_text(encoding="utf-8")
    got = resumable_plan({"plan": _REL}, "state/sizings/p.yaml", repo)
    assert got.incomplete == ("C5",)
    assert got.landed_open == (("C1", landed),)
    assert "C1" not in got.incomplete and plan == (repo / _REL).read_text(encoding="utf-8")


def test_write_older_than_the_plan_does_not_count(tmp_path):
    repo = _repo(tmp_path, {"C1": ("old.py",), "C5": ("c5.py",)})
    assert rows_landed_outside_executor(
        repo, _REL, [{"id": "C1", "writes": ["old.py"]}, {"id": "C5", "writes": ["c5.py"]}]
    ) == {}
    assert resumable_plan({"plan": _REL}, "state/sizings/p.yaml", repo).incomplete == ("C1", "C5")


def test_partially_landed_row_stays_incomplete(tmp_path):
    repo = _repo(tmp_path, {"C1": ("c1.py", "c1b.py")})
    _commit(repo, {"c1.py": "1"}, "half")
    assert resumable_plan({"plan": _REL}, "state/sizings/p.yaml", repo).incomplete == ("C1",)


def test_the_walk_spawns_no_process(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"C1": ("c1.py",)})
    landed = _commit(repo, {"c1.py": "1"}, "hand edit")

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert rows_landed_outside_executor(repo, _REL, [{"id": "C1", "writes": ["c1.py"]}]) == {"C1": landed}
