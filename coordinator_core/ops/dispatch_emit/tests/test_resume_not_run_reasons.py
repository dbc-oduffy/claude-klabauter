"""resumable_plan names the evidence for each spine row the resume does not stage, and the gate snapshot carries it."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_gate, resume_snapshot
from coordinator_core.ops.dispatch_emit.sizing_fire import resumable_plan
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL, _plan  # noqa: F401

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plans/p.md"
_SIZING = "state/sizings/p.yaml"


def _plan_text(rows: dict) -> str:
    body = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {w[0]}\n  writes: {list(w)}\n"
        f"  disposition: {disp}\n  deferred: false\n"
        for i, (w, disp) in rows.items()
    )
    return f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}```\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict, *message: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    args = [a for m in (message or ("c",)) for a in ("-m", m)]
    _git(root, "commit", "-q", *args)
    return _git(root, "rev-parse", "HEAD")


def _repo(root: Path, rows: dict) -> Path:
    _git(root, "init", "-q")
    _commit(root, {"seed": "0"}, "seed")
    _commit(root, {_REL: _plan_text(rows)}, "plan")
    return root


def _reasons(plan) -> dict:
    return dict(plan.not_run_reasons)


def test_coded_row_reads_its_disposition(tmp_path):
    repo_ = _repo(tmp_path, {"A": (("a.py",), "coded"), "B": (("b.py",), "open")})
    got = resumable_plan({"plan": _REL}, _SIZING, repo_)
    assert got.incomplete == ("B",)
    assert _reasons(got) == {"A": "disposition coded"}


def test_hand_commit_row_reads_landed_outside_the_executor(tmp_path):
    repo_ = _repo(tmp_path, {"A": (("a.py",), "open"), "B": (("b.py",), "open")})
    sha = _commit(repo_, {"a.py": "1"}, "hand edit")
    got = resumable_plan({"plan": _REL}, _SIZING, repo_)
    assert got.incomplete == ("B",)
    assert _reasons(got) == {"A": f"landed outside the executor at {sha}"}


def test_checkpoint_row_reads_landed_at(tmp_path):
    repo_ = _repo(tmp_path, {"A": (("a.py",), "open"), "B": (("b.py",), "open")})
    _commit(repo_, {"a.py": "1"}, "checkpoint(wave 1): 1 row — A", f"Checkpoint-Plan: {_REL}\nCheckpoint-Base: abc1234")
    got = resumable_plan({"plan": _REL}, _SIZING, repo_)
    assert got.incomplete == ("B",)
    assert _reasons(got)["A"].startswith("landed at ")


def test_every_id_is_incomplete_xor_reasoned(tmp_path):
    repo_ = _repo(
        tmp_path,
        {
            "A": (("a.py",), "coded"),
            "B": (("b.py",), "open"),
            "C": (("c.py",), "open"),
            "D": (("d.py",), "open"),
        },
    )
    _commit(repo_, {"c.py": "1"}, "hand edit")
    got = resumable_plan({"plan": _REL}, _SIZING, repo_)
    reasoned = set(_reasons(got))
    assert set(got.incomplete) == {"B", "D"}
    assert reasoned == {"A", "C"}
    assert not reasoned & set(got.incomplete)


def test_gate_snapshot_carries_a_reason_for_every_not_to_run_id(repo, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    run = "ask-20261017T000000-aaaaaa"
    _put(repo, tshirt="S", route="spec-dispatch")
    _plan(repo, rows=[("A1", "coded"), ("A2", "open"), ("A3", "delivered_elsewhere")])
    ask_gate._handler({"sizing_path": REL, "run_id": run}, repo_root=repo / ".git")
    snap = resume_snapshot.read(repo, run, PLAN_REL)
    assert snap.not_to_run == frozenset({"A1", "A3"})
    assert set(snap.reasons) == set(snap.not_to_run)
    assert snap.reasons["A1"] == "disposition coded"
