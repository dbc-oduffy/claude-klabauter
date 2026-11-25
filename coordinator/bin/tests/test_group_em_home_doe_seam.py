"""group-em-home places the engine through the hooks/scripts seam in a DoE-shaped mirror (no engine_bootstrap), before and after the publish stem rewrite."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_BIN = Path(__file__).resolve().parent.parent
_ENGINE = _BIN.parent.parent
_SCRIPT = _BIN / "group-em-home.py"

_SEAM_STUB = '''\
import os


def resolve_claude_klabauter_root():
    return os.environ["GEMH_TEST_ENGINE"]


def place_engine_root_on_path(root):
    import sys

    sys.path.insert(1, root)
    return root
'''


def _load(path, name) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doe_shaped_mirror(tmp_path, text):
    bin_dir = tmp_path / "coordinator" / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    (bin_dir / "group-em-home.py").write_text(text, encoding="utf-8")
    seam_dir = tmp_path / "coordinator" / "hooks" / "scripts"
    seam_dir.mkdir(parents=True)
    (seam_dir / "_engine_root.py").write_text(_SEAM_STUB, encoding="utf-8")
    assert not (bin_dir / "lib" / "engine_bootstrap.py").exists()
    return bin_dir


def _publish_rewritten(text) -> str:
    from coordinator_core.percolate.rewrite_stem import rewrite_stems

    text = re.sub(r"\bclaude-klabauter\b", "claude_klabauter", text)
    return rewrite_stems(text, {"claude-klabauter": "claude_klabauter"})


def test_doe_shaped_tree_places_through_the_seam_before_and_after_the_stem_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _SCRIPT.read_text(encoding="utf-8")
    for label, text in (("plain", source), ("rewritten", _publish_rewritten(source))):
        root = tmp_path / label
        bin_dir = _doe_shaped_mirror(root, text)
        monkeypatch.setattr(sys, "path", list(sys.path))
        monkeypatch.setenv("GEMH_TEST_ENGINE", str(_ENGINE))
        for name in ("engine_bootstrap", "cc_invoke", "lib"):
            monkeypatch.delitem(sys.modules, name, raising=False)
        mod = _load(bin_dir / "group-em-home.py", f"gemh_doe_{label}")
        assert mod._place_engine_on_path() == str(_ENGINE)
        assert "engine_bootstrap" not in sys.modules
        assert str(root / "coordinator" / "hooks" / "scripts") not in sys.path


def test_ensure_refresh_saves_diverged_copy_then_replaces_claude_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from coordinator_core.group_em import gem_home, session_registry

    settings, reg, elsewhere = tmp_path / "settings", tmp_path / "registry", tmp_path / "elsewhere"
    for d in (settings, reg, elsewhere):
        d.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(settings))
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    for k in ("MACHINE_LOCAL_COORDINATOR_GROUP_EM_HOME", "MACHINE_LOCAL_COORDINATOR_GENERAL_WORK_HOME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(gem_home.shutil, "which", lambda name: None)
    monkeypatch.setattr(gem_home, "gem_holder", lambda: None)
    monkeypatch.setattr(session_registry, "read_rows", lambda *a, **k: [])
    monkeypatch.chdir(elsewhere)
    tpl = tmp_path / "tpl.md"
    tpl.write_text("seed v1", encoding="utf-8")
    monkeypatch.setattr(gem_home, "default_template", lambda: tpl)
    mod = _load(_SCRIPT, "gemh_diverged")
    monkeypatch.setattr(mod, "_ensure_tail", lambda gem, report, args: 0)

    home = gem_home.ensure_home_report()["home"]
    tpl.write_text("seed v2", encoding="utf-8")
    assert mod.main(["ensure", "--refresh"]) == 0
    assert (home / "CLAUDE.md").read_text(encoding="utf-8") == "seed v2"
    assert "differed" not in capsys.readouterr().err

    (home / "CLAUDE.md").write_bytes(b"PM-ruled local content")
    tpl.write_text("seed v3", encoding="utf-8")
    assert mod.main(["ensure", "--refresh"]) == 0
    err = capsys.readouterr().err
    assert (home / "CLAUDE.md").read_text(encoding="utf-8") == "seed v3"
    saved = list((home / gem_home.DIVERGED_DIR).glob("*.md"))
    assert len(saved) == 1 and saved[0].read_bytes() == b"PM-ruled local content"
    assert "differed" in err and str(saved[0]) in err
