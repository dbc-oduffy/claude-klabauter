"""group-em-home imports its home contracts from the engine in a klabauter-shaped tree with no hooks/scripts."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.complete

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


def test_path_verb_runs_from_mirror_tree(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    assert mod.main(["path"]) == 0
    assert capsys.readouterr().out.strip() == str(home)


def test_ensure_prints_no_template_notice_exit_0(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(gem_home, "migrate_legacy_home", lambda: "none")
    monkeypatch.setattr(gem_home, "default_template", lambda: None)
    monkeypatch.setattr(gem_home, "remote_status", lambda: (None, "gh-absent"))
    assert mod.main(["ensure"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == str(home)
    assert cap.err.count("\n") == 2
    assert "CLAUDE_PLUGIN_ROOT" in cap.err and "repos.content_root" in cap.err
    assert "local-only" in cap.err and "gh-absent" in cap.err


def test_ensure_with_template_prints_no_notice(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    tpl = tmp_path / "tpl.md"
    tpl.write_text("seed", encoding="utf-8")
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(gem_home, "migrate_legacy_home", lambda: "none")
    monkeypatch.setattr(gem_home, "default_template", lambda: tpl)
    monkeypatch.setattr(gem_home, "remote_status", lambda: (None, "gh-absent"))
    assert mod.main(["ensure"]) == 0
    assert "CLAUDE_PLUGIN_ROOT" not in capsys.readouterr().err
    assert (home / "CLAUDE.md").read_text(encoding="utf-8") == "seed"


def test_refused_home_exits_3(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)

    def refuse():
        raise gem_home.HomeRefused("/h", "/c")

    monkeypatch.setattr(gem_home, "resolve_home", refuse)
    assert mod.main(["path"]) == 3
    assert "inside a checkout" in capsys.readouterr().err


def test_unresolved_engine_exits_2(tmp_path, monkeypatch, capsys):
    mod = _load_mirror(tmp_path, monkeypatch)
    import engine_bootstrap

    monkeypatch.setattr(engine_bootstrap, "_resolve_engine_root", lambda caller_file=None: "")
    assert mod.main(["path"]) == 2
    assert "unresolvable" in capsys.readouterr().err


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, capture_output=True, text=True
    )


def _stub_home(gem_home, monkeypatch, home, remote):
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(gem_home, "migrate_legacy_home", lambda: "none")
    monkeypatch.setattr(gem_home, "default_template", lambda: None)
    monkeypatch.setattr(gem_home, "remote_status", lambda: remote)


def test_ensure_json_reports_opt_out_installs_guard(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    _stub_home(gem_home, monkeypatch, home, (None, "gh-absent"))
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    assert mod.main(["ensure", "--json", "--create-remote"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["remote"] is None and rep["remote_opt_out"] == "gh-absent"
    assert rep["sync"]["sync"] == "noop"
    assert (home / ".git" / "hooks" / "pre-commit").is_file()
    assert "state/cross-repo" not in (home / ".gitignore").read_text(encoding="utf-8")


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


def test_sync_conflict_exits_5_naming_paths(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home, gem_home_sync

    mod = _load_mirror(tmp_path, monkeypatch)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: tmp_path)
    monkeypatch.setattr(
        gem_home_sync, "pull_on_entry", lambda h: {"sync": "conflict", "paths": ["a.md"], "remote_opt_out": None}
    )
    assert mod.main(["sync"]) == 5
    err = capsys.readouterr().err
    assert "conflict" in err and "a.md" in err


def test_prune_verb_calls_prune(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home, gem_home_prune

    mod = _load_mirror(tmp_path, monkeypatch)
    monkeypatch.setattr(gem_home, "resolve_home", lambda: tmp_path)
    monkeypatch.setattr(
        gem_home_prune,
        "prune",
        lambda h, fix_paths=False: {"removed": [], "archived": [], "backup": [], "refused": [], "fix_paths": fix_paths},
    )
    assert mod.main(["prune"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == [] and out["fix_paths"] is False
    assert mod.main(["prune", "--fix-paths"]) == 0
    assert json.loads(capsys.readouterr().out)["fix_paths"] is True


def test_script_has_no_legacy_imports():
    text = (_BIN / "group-em-home.py").read_text(encoding="utf-8")
    assert "import _gem_home" not in text
    assert "hooks" + "/scripts" not in text
