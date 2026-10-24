"""terminal_commit's integrity gate drops halted holds the open merge brings in, and
`_merge_landing_plan` delegates to merge_landing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import merge_landing, peer_hunk_gate, terminal_commit
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

H = "ask-20200101T000000-abc123"
P = "src/held.py"
Q = "src/other.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path, theirs_message: str, theirs_rel: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _write(root, "state/sizings/s.yaml", "deliverable_id: deliv-xyz\n")
    _write(root, f"state/warp/{H}/halted.json", json.dumps({"status": "halted"}))
    _write(root, f"state/warp/{H}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))
    _write(root, P, "base\n")
    _write(root, Q, "base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    _git(root, "checkout", "-q", "-b", "theirs", "main")
    _write(root, theirs_rel, "theirs\n")
    _git(root, "add", theirs_rel)
    _git(root, "commit", "-q", "-m", theirs_message)
    _git(root, "checkout", "-q", "main")
    _write(root, "main_side.txt", "x\n")
    _git(root, "add", "main_side.txt")
    _git(root, "commit", "-q", "-m", "main side")
    _git(root, "merge", "--no-ff", "--no-commit", "-q", "theirs")
    assert (root / ".git" / "MERGE_HEAD").exists()
    return root


def _gate_findings(root: Path, monkeypatch) -> list:
    monkeypatch.setattr(
        peer_hunk_gate, "co_declared_peer_holds", lambda *a, **k: [(P, H)],
    )
    out = terminal_commit._landing_integrity_gate(
        root, [P], [], None, {}, "ask-20200101T000009-self00", merge_open=True,
    )
    return [f for f in out.get("integrity", []) if f["reason"] == peer_hunk_gate.PEER_HUNKS_CO_DECLARED]


def test_halted_hold_on_merge_head_ancestry_is_dropped(tmp_path, monkeypatch):
    root = _repo(tmp_path, f"warp: record halted {H} run artifacts", P)
    assert _gate_findings(root, monkeypatch) == []


def test_halted_hold_outside_merge_head_ancestry_still_refuses(tmp_path, monkeypatch):
    root = _repo(tmp_path, "unrelated change", P)
    findings = _gate_findings(root, monkeypatch)
    assert [f["path"] for f in findings] == [P]


def test_merge_landing_plan_delegates_with_no_owner(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(worktree_root, all_paths, deleted_paths, *, product, owner_run_id=None):
        seen.update(args=(worktree_root, all_paths, deleted_paths), product=product, owner=owner_run_id)
        return {"sentinel": True}

    monkeypatch.setattr(merge_landing, "landing_plan", fake)
    out = terminal_commit._merge_landing_plan(tmp_path, ["a"], ["b"], product={"a"})
    assert out == {"sentinel": True}
    assert seen["args"] == (tmp_path, ["a"], ["b"]) and seen["product"] == {"a"} and seen["owner"] is None
