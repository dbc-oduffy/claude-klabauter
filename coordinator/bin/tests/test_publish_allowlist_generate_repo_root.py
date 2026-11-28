"""The field-7 generator reads declarations and tracked names from `--repo-root`, not from the tree holding the script."""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "publish-allowlist-generate.py"

_ENGINE_DIRS = ("hooks", "ops", "write_guards", "bash_guards", "frontmatter", "session", "contract", "extra")
_PORTABLE = (
    "claude-klabauter|m|d|coordinator_core|x|n|\n"
    "claude-klabauter-coordinator-bin|m|d|coordinator/bin|x|n|\n"
)
_DECLARATIONS = "rows:\n  claude-klabauter:\n    deny: []\n  claude-klabauter-coordinator-bin:\n    deny: []\n"


def _live_repo(root: Path) -> None:
    files = {f"coordinator_core/{d}/__init__.py": "" for d in _ENGINE_DIRS}
    files["coordinator_core/frontmatter/schema_validate.py"] = ""
    files["coordinator_core/contract/cockpit_schema/emit_schema.py"] = ""
    files["coordinator/bin/tool.py"] = ""
    files["setup/publish-targets.portable"] = _PORTABLE
    files["setup/publish-allowlist-declarations.yaml"] = _DECLARATIONS
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


def _load_from(script_dir: Path):
    copy = script_dir / "publish-allowlist-generate.py"
    shutil.copy(_SCRIPT, copy)
    spec = importlib.util.spec_from_file_location("_allowlist_generate_under_test", copy)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_repo_root_points_the_regeneration_at_a_tree_the_script_does_not_live_in(tmp_path):
    live = tmp_path / "live"
    clone_bin = tmp_path / "clone" / "coordinator" / "bin"
    clone_bin.mkdir(parents=True)
    live.mkdir()
    _live_repo(live)
    module = _load_from(clone_bin)

    assert module.main(["--repo-root", str(live)]) == 0

    written = (live / "setup" / "publish-targets.portable").read_text(encoding="utf-8")
    assert "hooks" in written and "tool.py" in written
    assert not (tmp_path / "clone" / "setup").exists()


def test_without_repo_root_a_script_outside_the_live_tree_cannot_find_declarations(tmp_path):
    clone_bin = tmp_path / "clone" / "coordinator" / "bin"
    clone_bin.mkdir(parents=True)
    module = _load_from(clone_bin)

    try:
        module.main([])
    except FileNotFoundError as exc:
        assert "publish-allowlist-declarations.yaml" in str(exc)
    else:
        raise AssertionError("expected FileNotFoundError")
