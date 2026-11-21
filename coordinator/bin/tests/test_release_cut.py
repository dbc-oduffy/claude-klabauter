"""Tests for coordinator/bin/release-cut.py, built on a tmp_path repo."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "release-cut.py"
_spec = importlib.util.spec_from_file_location("release_cut", _SCRIPT)
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)

_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _manifest(tmp_path: Path, **kw) -> Path:
    data = {"name": "t1", "base": "release/base", "picks": [], "together": []}
    data.update(kw)
    p = tmp_path / "m.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_no_repo_root_outside_repo_refuses(tmp_path, monkeypatch, capsys):
    m = _manifest(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    # A --basetemp inside a checkout puts tmp_path inside a repo; pin the walk's no-repo answer.
    monkeypatch.setattr("coordinator_core.git.repo_root.show_toplevel", lambda cwd=None: None)
    assert rc.main([str(m)]) == 1
    assert "not inside a git repo; pass --repo-root" in capsys.readouterr().err
