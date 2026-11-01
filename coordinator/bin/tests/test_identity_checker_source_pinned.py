"""`publish._resolve_identity_checker_source_script` returns the round-pinned commit's checker,
never the live working tree's: the refresh is written into the tree the round commits.

Run: python -m pytest coordinator/bin/tests/test_identity_checker_source_pinned.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_checker_pin_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_checker_source_is_the_pinned_commit_not_the_working_tree(tmp_path):
    repo = tmp_path / "repo"
    checker = repo / "dist" / "gh" / "scripts" / "check-persona-names.py"
    checker.parent.mkdir(parents=True)
    checker.write_text("PINNED = 1\n", encoding="utf-8")
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    sha = _git(repo, "rev-parse", "HEAD")
    checker.write_text("LIVE = 1\n", encoding="utf-8")

    section = {"inject": [{"src": str(repo / "dist" / "gh"), "dst": ".github",
                           "required_children": ["scripts/check-persona-names.py"]}]}
    engine = SimpleNamespace(resolve_target=lambda store, name: section)
    pins = {str(Path(_git(repo, "rev-parse", "--show-toplevel"))): sha}
    script = publish._resolve_identity_checker_source_script(engine, {"targets": {"t": {}}}, tmp_path, pins)
    assert script is not None
    assert script.read_text(encoding="utf-8") == "PINNED = 1\n"
