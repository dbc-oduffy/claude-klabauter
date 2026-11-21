"""group-em-home imports its home contracts from the engine in a klabauter-shaped tree with no hooks/scripts."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent
_ENGINE = _BIN.parent.parent


def _load_mirror(tmp_path, monkeypatch):
    mirror_bin = tmp_path / "coordinator" / "bin"
    mirror_bin.mkdir(parents=True)
    shutil.copy(_BIN / "group-em-home.py", mirror_bin / "group-em-home.py")
    shutil.copytree(_BIN / "lib", mirror_bin / "lib", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    assert not (tmp_path / "coordinator" / "hooks").exists()
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "engine_bootstrap", raising=False)
    spec = importlib.util.spec_from_file_location("geh_mirror", mirror_bin / "group-em-home.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import engine_bootstrap

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: str(_ENGINE))
    return mod


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True
    )


def _stub_home(gem_home, monkeypatch, home, remote):
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(gem_home, "migrate_legacy_home", lambda: "none")
    monkeypatch.setattr(gem_home, "default_template", lambda: None)
    monkeypatch.setattr(gem_home, "remote_status", lambda: remote)


def test_sync_push_to_bare_remote_then_pull(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    bare = tmp_path / "remote.git"
    assert _git(tmp_path, "init", "--bare", "-q", str(bare)).returncode == 0
    _stub_home(gem_home, monkeypatch, home, (str(bare), None))
    assert mod.main(["ensure", "--json"]) == 0
    capsys.readouterr()
    inbox = home / "state" / "cross-repo" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "m.md").write_text("memo", encoding="utf-8")
    for k in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{k}_NAME", "t")
        monkeypatch.setenv(f"GIT_{k}_EMAIL", "t@t")
    assert _git(home, "symbolic-ref", "HEAD", f"refs/heads/{gem_home.DEFAULT_REMOTE_BRANCH}").returncode == 0
    assert mod.main(["sync", "--push"]) == 0
    assert json.loads(capsys.readouterr().out)["sync"] == "ok"
    assert _git(bare, "log", "--all", "--oneline").stdout.strip()
    assert mod.main(["sync"]) == 0
    assert json.loads(capsys.readouterr().out)["sync"] == "ok"


def test_sync_without_remote_is_noop_exit_0(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    _git(home, "init", "-q")
    _stub_home(gem_home, monkeypatch, home, (None, "gh-absent"))
    assert mod.main(["sync", "--push"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["sync"] == "noop" and rep["remote_opt_out"] == "gh-absent"


_PROVENANCE_SEAM = '''\
import sys


def resolve_claude_klabauter_root():
    raise AssertionError("the plain resolver must not answer when provenance is published")


def resolve_claude_klabauter_root_with_provenance():
    return ("PROV_ROOT", "published", "published-target")


def place_engine_root_on_path(root):
    sys.path.insert(1, root)
    return root
'''
