"""An emitted workflow script is committed, so it carries no host path: the
repo root is read at run time from `args.repoRoot`."""

from __future__ import annotations

import os
import re
import sys

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.commit_request import parse_marker

from .conftest import REVIEW_KW
from .test_emit_script_opens_plan_once import _write_plan

_HOST_PATH = re.compile(r"(?:^|[\s'\"`(=:])(?:[A-Za-z]:[\\/]|/(?:home|Users|root|opt|tmp|var|mnt)/)")


def _emit(tmp_path, monkeypatch):
    claude_dir = tmp_path / "offpath"
    claude_dir.mkdir()
    # shutil.which resolves an extensionless file on POSIX only; Windows needs a PATHEXT suffix.
    binary = claude_dir / ("claude.cmd" if sys.platform == "win32" else "claude")
    binary.write_text("@echo off\n" if sys.platform == "win32" else "#!/bin/sh\n")
    binary.chmod(0o755)
    # Prepended, not replaced: emit's `git check-ignore` must still resolve git.
    monkeypatch.setenv("PATH", os.pathsep.join([str(claude_dir), os.environ.get("PATH", "")]))
    repo = tmp_path / "repo"
    repo.mkdir()
    script = emit.emit_script(_write_plan(tmp_path), repo_root=repo, **REVIEW_KW)
    return script, repo, claude_dir


def test_no_host_path_in_emitted_text(tmp_path, monkeypatch):
    script, repo, claude_dir = _emit(tmp_path, monkeypatch)
    assert "claude CLI:" in script
    assert repo.as_posix() not in script
    assert claude_dir.as_posix() not in script
    assert str(repo) not in script
    assert not _HOST_PATH.search(script), _HOST_PATH.search(script)


def test_the_script_reads_its_root_from_args_at_run_time(tmp_path, monkeypatch):
    script, _, _ = _emit(tmp_path, monkeypatch)
    assert "args.repoRoot" in script
    assert script.index("const _repoRoot") < script.index("const _shared")
    assert "cd ${_repoRoot}" in script


def test_the_commit_marker_carries_no_root(tmp_path, monkeypatch):
    script, _, _ = _emit(tmp_path, monkeypatch)
    request = parse_marker(script)
    assert request is not None and request.repo_root is None


