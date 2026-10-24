"""Another plan's checkpoint or another run's halt record landing a shared file is not this row's landing."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.plan_row_coding import (
    _is_foreign_executor_commit,
    outside_landings,
    rows_landed_outside_executor,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plans/p.md"
_OTHER = "docs/plans/other.md"
_ROW = [{"id": "C1", "writes": ["c1.py"]}]


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


def _repo(root: Path) -> Path:
    _git(root, "init", "-q")
    _commit(root, {"seed": "0", "c1.py": "0"}, "seed")
    _commit(root, {_REL: _plan({"C1": ("c1.py",)})}, "plan")
    return root


def _foreign_checkpoint(root: Path, text: str) -> str:
    return _commit(
        root, {"c1.py": text}, f"checkpoint(wave 1): 1 rows — X\n\nCheckpoint-Plan: {_OTHER}\nCheckpoint-Base: abc1234",
    )


def test_foreign_plan_checkpoint_does_not_resolve_the_row(tmp_path):
    repo = _repo(tmp_path)
    _foreign_checkpoint(repo, "1")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_foreign_halt_record_does_not_resolve_the_row(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, halted_record_message("warp-other-run", {"halted": "ledger-check", "plan": _OTHER}))
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {}


def test_this_plans_own_halt_record_still_resolves_the_row(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, halted_record_message("warp-own-run", {"halted": "ledger-check", "plan": _REL}))
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": landed}


def test_a_hand_commit_after_the_foreign_one_resolves_with_its_own_sha(tmp_path):
    repo = _repo(tmp_path)
    _foreign_checkpoint(repo, "1")
    hand = _commit(repo, {"c1.py": "2"}, "hand edit")
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": hand}


def test_default_walk_still_counts_the_foreign_checkpoint(tmp_path):
    repo = _repo(tmp_path)
    foreign = _foreign_checkpoint(repo, "1")
    head = _git(repo, "rev-parse", "HEAD")
    chunks = [SimpleNamespace(id="C1", paths=["c1.py"])]
    kw = dict(include_start=True, after_creation=True)
    assert outside_landings(repo, head, _REL, chunks, **kw) == {"C1": [foreign]}
    assert outside_landings(repo, head, _REL, chunks, skip_foreign_runs=True, **kw) == {}


def test_foreign_predicate_both_sides():
    assert _is_foreign_executor_commit(f"checkpoint(wave 1): 1 rows — X\n\nCheckpoint-Plan: {_OTHER}", _REL)
    assert not _is_foreign_executor_commit(f"checkpoint(wave 1): 1 rows — X\n\nCheckpoint-Plan: {_REL}", _REL)
    assert not _is_foreign_executor_commit("warp: record halted r1 run artifacts\n\nHalted-By: x", _REL)
    assert _is_foreign_executor_commit("warp: record halted r1 run artifacts (process files)\n\nHalt-Plan: " + _OTHER, _REL)
    assert not _is_foreign_executor_commit("hand edit\n\nHalt-Plan: " + _OTHER, _REL)


def test_the_walk_spawns_no_process(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _foreign_checkpoint(repo, "1")
    hand = _commit(repo, {"c1.py": "2"}, "hand edit")

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert rows_landed_outside_executor(repo, _REL, _ROW) == {"C1": hand}
