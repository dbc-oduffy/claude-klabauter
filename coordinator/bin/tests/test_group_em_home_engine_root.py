"""group-em-home imports its home contracts from the engine in a klabauter-shaped tree with no hooks/scripts."""

from __future__ import annotations

import importlib.util
import shutil
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
    assert mod.main(["ensure"]) == 0
    cap = capsys.readouterr()
    assert cap.out.strip() == str(home)
    assert cap.err.count("\n") == 1
    assert "CLAUDE_PLUGIN_ROOT" in cap.err and "repos.content_root" in cap.err


def test_ensure_with_template_prints_no_notice(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    mod = _load_mirror(tmp_path, monkeypatch)
    home = tmp_path / "home"
    tpl = tmp_path / "tpl.md"
    tpl.write_text("seed", encoding="utf-8")
    monkeypatch.setattr(gem_home, "resolve_home", lambda: home)
    monkeypatch.setattr(gem_home, "migrate_legacy_home", lambda: "none")
    monkeypatch.setattr(gem_home, "default_template", lambda: tpl)
    assert mod.main(["ensure"]) == 0
    assert capsys.readouterr().err == ""
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


def test_script_has_no_legacy_imports():
    text = (_BIN / "group-em-home.py").read_text(encoding="utf-8")
    assert "import _gem_home" not in text
    assert "hooks" + "/scripts" not in text
