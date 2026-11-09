"""gem-triage-turn-intent places the engine through the shared seam, in both tree shapes."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent
_ENGINE = _BIN.parent.parent
_SCRIPT = _BIN / "gem-triage-turn-intent.py"

_SEAM_STUB = '''\
import os

PLACED = []


def resolve_claude_klabauter_root():
    return os.environ["GTTI_TEST_ENGINE"]


def place_engine_root_on_path(root):
    import sys

    sys.path.insert(1, root)
    PLACED.append(root)
    return root
'''


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _doe_shaped_mirror(tmp_path):
    """A DoE-shaped tree: the script, a bin/lib shipping no engine_bootstrap/cc_invoke, the seam."""
    bin_dir = tmp_path / "coordinator" / "bin"
    (bin_dir / "lib").mkdir(parents=True)
    shutil.copy(_SCRIPT, bin_dir / "gem-triage-turn-intent.py")
    (bin_dir / "lib" / "atomic_record.py").write_text("", encoding="utf-8")
    seam_dir = tmp_path / "coordinator" / "hooks" / "scripts"
    seam_dir.mkdir(parents=True)
    (seam_dir / "_engine_root.py").write_text(_SEAM_STUB, encoding="utf-8")
    assert not (bin_dir / "lib" / "engine_bootstrap.py").exists()
    assert not (bin_dir / "lib" / "cc_invoke.py").exists()
    return bin_dir


def test_script_text_has_no_engine_bootstrap_import_and_no_hand_rolled_insert():
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "engine_bootstrap" not in text
    assert "sys.path.insert" not in text


def test_doe_shaped_tree_places_through_the_seam(tmp_path, monkeypatch, capsys):
    from coordinator_core.group_em import gem_home

    bin_dir = _doe_shaped_mirror(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("GTTI_TEST_ENGINE", str(_ENGINE))
    for name in ("engine_bootstrap", "cc_invoke", "lib"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    mod = _load(bin_dir / "gem-triage-turn-intent.py", "gtti_doe_mirror")

    assert mod._place_engine_on_path() == str(_ENGINE)
    assert "engine_bootstrap" not in sys.modules
    assert str(tmp_path / "coordinator" / "hooks" / "scripts") not in sys.path

    monkeypatch.setattr(gem_home, "gem_holder", lambda: {"session_id": "other", "live": True})
    rc = mod.main(
        ["--session-id", "s1", "--repo-root", str(tmp_path), "--turn-ended-at", "1.0", "--outcome", "cleared", "--caller-session-id", "me"]
    )
    err = capsys.readouterr().err
    assert rc == 6
    assert "unresolvable" not in err


def test_doe_shaped_tree_with_unresolved_engine_exits_2(tmp_path, monkeypatch, capsys):
    bin_dir = _doe_shaped_mirror(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("GTTI_TEST_ENGINE", "")
    mod = _load(bin_dir / "gem-triage-turn-intent.py", "gtti_doe_unresolved")
    rc = mod.main(
        ["--session-id", "s1", "--repo-root", str(tmp_path), "--turn-ended-at", "1.0", "--outcome", "cleared"]
    )
    assert rc == 2
    assert "unresolvable" in capsys.readouterr().err


def _publish_rewritten(text):
    """The publish stem rewrite (claude-klabauter -> claude_klabauter): exact-substitute, then glued-stem pass."""
    from coordinator_core.percolate.rewrite_stem import rewrite_stems

    text = re.sub(r"\bclaude-klabauter\b", "claude_klabauter", text)
    return rewrite_stems(text, {"claude-klabauter": "claude_klabauter"})


def test_stem_rewritten_script_still_resolves_against_the_doe_seam(tmp_path, monkeypatch):
    bin_dir = _doe_shaped_mirror(tmp_path)
    rewritten = _publish_rewritten(_SCRIPT.read_text(encoding="utf-8"))
    assert "engine_root.resolve_claude_klabauter_root" not in rewritten
    (bin_dir / "gem-triage-turn-intent.py").write_text(rewritten, encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("GTTI_TEST_ENGINE", str(_ENGINE))
    mod = _load(bin_dir / "gem-triage-turn-intent.py", "gtti_doe_rewritten")
    assert mod._place_engine_on_path() == str(_ENGINE)


def test_a_seam_without_the_resolver_fails_loudly(tmp_path, monkeypatch):
    bin_dir = _doe_shaped_mirror(tmp_path)
    seam = tmp_path / "coordinator" / "hooks" / "scripts" / "_engine_root.py"
    seam.write_text(_SEAM_STUB.replace("resolve_claude_klabauter_root", "resolve_other_root"), encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    mod = _load(bin_dir / "gem-triage-turn-intent.py", "gtti_doe_no_resolver")
    with pytest.raises(AttributeError):
        mod._place_engine_on_path()


def test_engine_tree_without_seam_places_through_colocated_resolver(monkeypatch):
    assert not (_BIN.parent / "hooks" / "scripts" / "_engine_root.py").exists()
    monkeypatch.setattr(sys, "path", [str(_BIN)] + [p for p in sys.path if p != str(_BIN)])
    mod = _load(_SCRIPT, "gtti_engine_tree")
    root = mod._place_engine_on_path()
    assert os.path.realpath(root) == os.path.realpath(_ENGINE)
    assert os.path.realpath(sys.path[0]) == os.path.realpath(_ENGINE)


def test_gem_holder_contract(monkeypatch):
    from coordinator_core.group_em import gem_home, nomination

    live = {"session_id": "a", "live": True}
    monkeypatch.setattr(nomination, "box_holder", lambda: live)
    assert gem_home.gem_holder() == live
    monkeypatch.setattr(nomination, "box_holder", lambda: {"session_id": "a", "live": False})
    assert gem_home.gem_holder() is None
    monkeypatch.setattr(nomination, "box_holder", lambda: None)
    assert gem_home.gem_holder() is None

    def boom():
        raise OSError("x")

    monkeypatch.setattr(nomination, "box_holder", boom)
    assert gem_home.gem_holder() is None
