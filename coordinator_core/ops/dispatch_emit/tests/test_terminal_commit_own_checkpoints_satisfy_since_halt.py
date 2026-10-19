"""A ledger resume whose declared product landed in the run's own post-halt checkpoints, worktree clean, is not unchanged since the halt."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit.ask_contract import (
    RUN_DIR_ROOT,
    UNCHANGED_SINCE_HALT_REASON,
    ManifestRow,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import (  # noqa: F401
    _REVIEWED,
    _SID,
    PLAN,
    RUN,
    RUN2,
    SCRIPT2,
    _second_run,
    halted,
)
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

MET = {"items": [{"id": "R1", "status": "met", "wired": True}]}


def _git(root, *a):
    out = subprocess.run(
        ["git", *a], cwd=str(root), check=True, capture_output=True, text=True, **no_console_creationflags()
    )
    return out.stdout.strip()


@pytest.fixture
def plain_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "halt")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest, _git(tmp_path, "rev-parse", "HEAD")


def _resume(halt, **extra):
    return {"run_id": "old", "halt_sha": halt, "blocking": ["R1"], "unlanded": ["C1"], **extra}


def _checkpoint(root, name, text):
    (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-q", "-m", f"checkpoint {name}")


def test_declared_path_committed_by_post_halt_checkpoint_with_clean_tree_passes(plain_repo):
    root, manifest, halt = plain_repo
    _checkpoint(root, "prod.py", "x = 2\n")
    assert _git(root, "status", "--porcelain", "--untracked-files=no") == ""
    assert tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest) == []


def test_post_halt_checkpoint_of_an_undeclared_path_still_refuses(plain_repo):
    root, manifest, halt = plain_repo
    _checkpoint(root, "other.py", "y = 2\n")
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


@pytest.fixture
def halted_repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.example")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "prod.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "other.py").write_text("y = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(
        tmp_path, "commit", "-q", "-m", "warp: record halted old run artifacts",
        "-m", "Halt-Plan: docs/plans/p.md\nHalted-By: ledger-check",
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"rows": [{"id": "C1", "writes": ["prod.py"]}]}), encoding="utf-8")
    return tmp_path, manifest


def _null_resume(**extra):
    return _resume(None, plan="docs/plans/p.md", **extra)


def test_null_halt_sha_resolves_halt_commit_and_post_halt_checkpoint_passes(halted_repo):
    root, manifest = halted_repo
    _checkpoint(root, "prod.py", "x = 2\n")
    assert tc._unchanged_since_halt_failures(root, _null_resume(), MET, manifest) == []


def test_null_halt_sha_without_change_since_halt_still_refuses(halted_repo):
    root, manifest = halted_repo
    got = tc._unchanged_since_halt_failures(root, _null_resume(), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_null_halt_sha_with_unresolvable_run_refuses(halted_repo):
    root, manifest = halted_repo
    _checkpoint(root, "prod.py", "x = 2\n")
    resume = {**_null_resume(), "run_id": "never-halted"}
    got = tc._unchanged_since_halt_failures(root, resume, MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def _declare_c1_writes_a_py(root) -> None:
    manifest = root / RUN_DIR_ROOT / RUN2 / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["rows"] = [
        ManifestRow(
            id="C1", agent_type="x", model="m", brief_path="b.md", writes=("a.py",), wave=1
        ).to_json()
    ]
    manifest.write_text(json.dumps(data), encoding="utf-8")


def _post_halt_checkpoint(root) -> None:
    (root / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(root, "add", "a.py")
    _git(
        root, "commit", "-q", "-m", "checkpoint(wave 2): 1 row — C1",
        "-m", f"Checkpoint-Plan: {PLAN}\nCheckpoint-Base: abc1234",
    )


def _terminal(root, token, resume):
    return tc._handler(
        {
            "script_path": SCRIPT2, "run_id": RUN2, "run_token": token, "incomplete_chunks": [],
            "inline_review": _REVIEWED, "session_id": _SID, "resume_ledger": resume,
            "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]},
        },
        repo_root=root / ".git",
    )


def _null_halt_resume(**extra):
    return {"run_id": RUN, "halt_sha": None, "plan": PLAN, "blocking": ["R1"], "unlanded": ["C1"], **extra}


def test_terminal_commit_null_halt_sha_accepts_own_post_halt_checkpoint(halted):
    token = _second_run(halted)
    _declare_c1_writes_a_py(halted)
    _post_halt_checkpoint(halted)
    out = _terminal(halted, token, _null_halt_resume())
    assert out["committed"] is True, out


def test_terminal_commit_null_halt_sha_without_run_id_refuses(halted):
    token = _second_run(halted)
    _declare_c1_writes_a_py(halted)
    _post_halt_checkpoint(halted)
    resume = {k: v for k, v in _null_halt_resume().items() if k != "run_id"}
    out = _terminal(halted, token, resume)
    assert out["committed"] is False and out["refused"] == "ledger-unmet", out
    assert out["ledger_failures"] == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]


def test_no_change_since_halt_still_refuses(plain_repo):
    root, manifest, halt = plain_repo
    got = tc._unchanged_since_halt_failures(root, _resume(halt), MET, manifest)
    assert got == [{"id": "R1", "reason": UNCHANGED_SINCE_HALT_REASON}]
