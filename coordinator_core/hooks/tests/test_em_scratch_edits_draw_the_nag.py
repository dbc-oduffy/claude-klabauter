"""A main-EM hand edit under `scratch/`, inside a nested-.git probe repo, or under the temp dir draws
the DIY nag and spends the XS write budget like any other product edit."""

from __future__ import annotations

import hashlib
import tempfile
import zlib
from pathlib import Path

import pytest

import coordinator_core.machine_profile as mp
from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import nudge_em_code_dispatch as mod
from coordinator_core.hooks import nudge_unrouted_sizing as sizing
from coordinator_core.session import xs_lane_budget as xb

SID = "sess-scratch-nag"
BODY = "v1 = f(1)\nv2 = f(2)\nv3 = f(3)\n"


def _obj(git: Path, kind: str, payload: bytes) -> str:
    raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
    sha = hashlib.sha1(raw).hexdigest()
    p = git / "objects" / sha[:2] / sha[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zlib.compress(raw))
    return sha


def _make_repo(root: Path) -> str:
    """Loose-object repo at `root` with one committed file; returns the HEAD sha."""
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "a.py").write_text("a = 1\n")
    body = b"100644 a.py\0" + bytes.fromhex(_obj(git, "blob", b"a = 1\n"))
    tree = _obj(git, "tree", body)
    sha = _obj(git, "commit", f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nm\n".encode())
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "refs" / "heads" / "main").write_text(sha + "\n")
    return sha


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    for target in (mod, leaf):
        monkeypatch.setattr(target, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
        monkeypatch.setattr(target, "_session_sizing_state", lambda payload, sid: (False, False))
    monkeypatch.setattr(mp, "guard_level", lambda name: "warn")
    monkeypatch.setattr(mod, "resolve_mode", lambda key, sid: False)


def _case(tmp_path: Path, name: str) -> tuple[Path, Path, str]:
    """(lane repo root, edited file, path of the file relative to the lane root) for one shape."""
    if name == "repo-scratch":
        root = tmp_path / "repo"
        rel = "scratch/x.py"
    elif name == "nested-git-probe":
        root = tmp_path / "repo"
        rel = "scratch/probe/x.py"
    else:
        root = tmp_path / "probe-repo"
        rel = "x.py"
    sha = _make_repo(root)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if name == "nested-git-probe":
        (target.parent / ".git").mkdir()
    return root, target, sha


CASES = ["repo-scratch", "nested-git-probe", "tmp-probe-repo"]


def _payload(root: Path, target: Path) -> dict:
    return {
        "tool_name": "Write",
        "session_id": SID,
        "cwd": str(root),
        "tool_input": {"file_path": str(target), "content": BODY},
    }


@pytest.mark.parametrize("name", CASES)
def test_scratch_edit_draws_the_advisory(tmp_path, monkeypatch, name):
    root, target, _ = _case(tmp_path, name)
    monkeypatch.setattr(sizing, "_repo_root", lambda payload: None)
    result = mod.op(_payload(root, target))
    assert result is not None
    out = result["hookSpecificOutput"]
    assert "No sizing" in out["additionalContext"]
    assert out.get("permissionDecision") in (None, "allow")


@pytest.mark.parametrize("name", CASES)
def test_scratch_edit_counts_against_the_xs_budget(tmp_path, name):
    root, target, sha = _case(tmp_path, name)
    assert xb.open_xs_lane(root, SID, sha)
    payload = {"session_id": SID, "cwd": str(root)}
    assert mod.shell_write_budget(payload, SID, str(target), BODY, None) is None
    rel = target.relative_to(root).as_posix()
    assert xb.read_lane(root, SID)["paths"] == [rel]


@pytest.mark.parametrize("name", CASES)
def test_scratch_edit_past_the_budget_is_refused(tmp_path, name):
    root, target, sha = _case(tmp_path, name)
    assert xb.open_xs_lane(root, SID, sha)
    payload = {"session_id": SID, "cwd": str(root)}
    result = mod.shell_write_budget(payload, SID, str(target), "".join(f"x{i}\n" for i in range(42)), None)
    assert result is not None
    assert "XS budget spent" in str(result)
    assert xb.read_lane(root, SID)["paths"] == []


@pytest.mark.parametrize("name", CASES)
def test_size_stage_spawn_scratch_write_is_treated_like_any_product_write(tmp_path, monkeypatch, name):
    from coordinator_core import warp_live
    from coordinator_core.ops.dispatch_emit import terminal_commit_params_channel as chan

    root, target, _ = _case(tmp_path, name)
    spawn = str(tmp_path / "wf_run" / "agent-a1b2c3d4e5f60718.jsonl")
    monkeypatch.setattr(chan, "spawn_transcript_path", lambda payload: spawn)
    monkeypatch.setattr(warp_live, "size_stage_run", lambda r, sid, run_dir: object())
    payload = {**_payload(root, target), "agent_id": "a1b2c3d4e5f60718"}

    result = mod.op(payload)
    assert result is not None
    assert "Size stage: change no file but the sizing" in result["hookSpecificOutput"]["additionalContext"]

    monkeypatch.setattr(warp_live, "size_stage_run", lambda r, sid, run_dir: None)
    assert mod.op(payload) is None
