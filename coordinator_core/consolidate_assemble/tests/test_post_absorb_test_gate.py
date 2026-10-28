"""The brief's terminal test-gate: depends on every absorb directive, scopes to
one `diff --name-only <pre-absorb HEAD>..HEAD`, and is never an apply directive."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core import consolidate_assemble as ca
from coordinator_core.consolidate_assemble import apply as apply_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

NOW = 2_000_000_000.0


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _repo(tmp_path: Path, with_branch: bool) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "seed")
    if with_branch:
        _git(root, "checkout", "-q", "-b", "work/peer")
        (root / "b.txt").write_text("peer\n", encoding="utf-8", newline="\n")
        _git(root, "add", "b.txt")
        _git(root, "commit", "-q", "-m", "peer work")
        _git(root, "checkout", "-q", "main")
    return root


def _brief(root: Path) -> dict:
    return ca.brief(repo_root=root, run_git=ca.default_run_git, now=NOW)


def test_gate_depends_on_every_absorb_and_names_pre_absorb_head(tmp_path: Path) -> None:
    root = _repo(tmp_path, with_branch=True)
    obj = _brief(root)
    gate = obj["gates"]["test_gate"]
    absorb_ids = [d["id"] for d in obj["directives"] if d["cli"] in ("merge-only", "merge-and-delete", "merge-ours")]
    assert absorb_ids == ["d-absorb-work/peer"]
    assert gate["depends_on"] == absorb_ids
    assert gate["pre_absorb_head"] == _git(root, "rev-parse", "HEAD")
    assert gate["changed_files_command"] == ["git", "diff", "--name-only", f"{gate['pre_absorb_head']}..HEAD"]
    assert "broad suite" in gate["scope"]


def test_gate_is_not_an_apply_directive(tmp_path: Path) -> None:
    obj = _brief(_repo(tmp_path, with_branch=True))
    assert all(d["id"] != "d-test-gate" for d in obj["directives"])
    assert all(d["cli"] in apply_mod._CLI_DISPATCH for d in obj["directives"])


def test_no_absorb_means_no_gate(tmp_path: Path) -> None:
    obj = _brief(_repo(tmp_path, with_branch=False))
    assert "test_gate" not in obj["gates"]
