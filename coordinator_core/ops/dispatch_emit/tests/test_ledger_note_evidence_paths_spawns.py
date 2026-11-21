"""A revise note naming a foreign plan path yields no write for it; the minted row still derives."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


_CODE = "coordinator_core/ops/dispatch_emit/ledger_remaining_paths.py"
_FOREIGN = "docs/plans/2026-10-23-pipelined-review-c2-c6.md"
_NOTE = f"Context only, see {_FOREIGN} for the DoE side."


def _git(root: Path, *args: str) -> str:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"git {args} exited {proc.returncode}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.mark.spawns_process
def test_a_committed_declared_file_still_differing_from_the_landed_sha_is_withheld(tmp_path, monkeypatch):
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
    from coordinator_core.ops.dispatch_emit.terminal_commit import _withheld_product_files
    from coordinator_core.ops.dispatch_emit.worktree_snapshot import Snapshot

    _git(tmp_path, "init", "-q")
    (tmp_path / "mod.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    (tmp_path / "whole.py").write_text("x\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "landed")
    sha = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "mod.py").write_text("a\nB\nc\nd\nE\n", encoding="utf-8")
    pre_commit = Snapshot(set(), [], {"mod.py", "whole.py"}, [])
    declared = {"mod.py", "whole.py"}

    assert _withheld_product_files(tmp_path, pre_commit, declared, declared, sha) == ["mod.py"]
    assert _withheld_product_files(tmp_path, pre_commit, declared, declared) == []
    _git(tmp_path, "commit", "-q", "-am", "peer moved HEAD")
    assert _withheld_product_files(tmp_path, pre_commit, declared, declared, sha) == ["mod.py"]
    assert _withheld_product_files(tmp_path, pre_commit, set(), set(), sha) == []
