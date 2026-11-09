"""group-em-home places the engine through the hooks/scripts seam in a DoE-shaped mirror (no engine_bootstrap), before and after the publish stem rewrite."""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path

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


def _load(path, name):
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


def _publish_rewritten(text):
    from coordinator_core.percolate.rewrite_stem import rewrite_stems

    text = re.sub(r"\bclaude-klabauter\b", "claude_klabauter", text)
    return rewrite_stems(text, {"claude-klabauter": "claude_klabauter"})


def test_doe_shaped_tree_places_through_the_seam_before_and_after_the_stem_rewrite(tmp_path, monkeypatch):
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
