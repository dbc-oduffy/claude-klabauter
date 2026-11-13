"""code_intel.workstate_index through the real dispatch path, against a fixture repo."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from coordinator_core import ipc
from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.symbol_knowledge import workstate_index as ws


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _fixture(tmp_path: Path) -> Path:
    root = tmp_path / "fx"
    (root / "pkg").mkdir(parents=True)
    (root / "docs/plans").mkdir(parents=True)
    (root / "pkg/mod.py").write_text("def fn():\n    return 1\n", encoding="utf-8")
    (root / "docs/plans/p.md").write_text("# P\n\nChanges `pkg.mod.fn` in pkg/mod.py.\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_op_resolves_through_the_real_dispatch_path_and_writes_the_index(tmp_path: Path) -> None:
    handler = ipc._REGISTRY.get("code_intel.workstate_index") or ipc._lazy_import_and_lookup(
        "code_intel.workstate_index"
    )
    assert handler is not None
    root = _fixture(tmp_path)
    out = handler({}, repo_root=root)
    assert out["docs"] == 1 and out["docs_reread"] == 1
    refs = json.loads((root / ws.INDEX_REL).read_text(encoding="utf-8"))["refs"]
    assert refs["pkg/mod.py::fn"] == [["docs/plans/p.md", "plan"]]
    assert (root / ws.ANCHORS_REL).is_file()
    again = handler({}, repo_root=root)
    assert again["docs_reread"] == 0 and again["wrote"] is False


def test_op_is_classified_mutating_and_scoped() -> None:
    assert OP_CLASSIFICATION["code_intel.workstate_index"] is OpClass.MUTATING
    assert ipc.OP_KEY_SCOPE.get("code_intel.workstate_index") == "show_top"
