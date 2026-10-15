"""A percolate round never promotes `main`: a clean round on a `candidate`-branch
dest leaves the origin's `main` where it was, and the round module loads no
klabauter-promote sibling (DR-314 as amended 2026-10-15).
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from . import test_percolate_round as base

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_mod = base._mod
# The round fixtures patch `subprocess.run` process-wide; real git reads must bypass the spy.
_REAL_RUN = subprocess.run


def _git(cwd: Path, *args: str, hours_ago: int = 0) -> str:
    env = dict(
        os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL=os.devnull,
    )
    if hours_ago:
        stamp = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()
        env["GIT_COMMITTER_DATE"] = stamp
        env["GIT_AUTHOR_DATE"] = stamp
    result = _REAL_RUN(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _candidate_rig(tmp_path: Path) -> tuple[Path, Path]:
    origin, dest = tmp_path / "origin.git", tmp_path / "dest"
    origin.mkdir()
    dest.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    _git(dest, "init", "-b", "main")
    _git(dest, "remote", "add", "origin", str(origin))
    (dest / "a.txt").write_text("a")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "base", hours_ago=200)
    _git(dest, "push", "origin", "main", hours_ago=200)
    _git(dest, "checkout", "-b", "candidate")
    (dest / "b.txt").write_text("b")
    _git(dest, "add", ".")
    _git(dest, "commit", "-m", "candidate work", hours_ago=100)
    _git(dest, "push", "-u", "origin", "candidate", hours_ago=100)
    return origin, dest


def _clean_noop_round(tmp_path: Path, monkeypatch, dest: Path) -> int:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    root = tmp_path / "percolate-root"
    (root / "setup").mkdir(parents=True)
    spy = base._SubprocessSpy(
        dryrun_stdout="", real_stdout="", parse2_stdout=base._parse2_stdout(),
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    base._install_manifest_stub(monkeypatch, spy)
    base._install_sibling_cli_stub(monkeypatch, spy)
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda t, r: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda t, r: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)
    monkeypatch.setattr(_mod, "_field7_preflight", lambda _target: None)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(root), "--yes"])
    with contextlib.redirect_stdout(io.StringIO()):
        return _mod._cmd_round(args)


def test_clean_round_on_a_candidate_dest_leaves_origin_main_unchanged(tmp_path, monkeypatch):
    origin, dest = _candidate_rig(tmp_path)
    main_before = _git(origin, "rev-parse", "refs/heads/main")
    assert _git(dest, "reflog", "show", "origin/candidate")
    assert _clean_noop_round(tmp_path, monkeypatch, dest) == 0
    assert _git(origin, "rev-parse", "refs/heads/main") == main_before


def test_round_source_loads_no_promote_sibling():
    source = Path(_mod.__file__).read_text(encoding="utf-8")
    assert "promote_candidate" not in source
    assert "_KLABAUTER_PROMOTE" not in source
    assert '"klabauter-promote.py"' not in source
    assert not hasattr(_mod, "_promote_if_channel")
