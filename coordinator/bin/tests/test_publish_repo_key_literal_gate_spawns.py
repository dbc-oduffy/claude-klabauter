"""`publish.dispatch_end_of_run_repo_key_literal_gate`: fails when a row's
content transform changes a registry repo-key literal in source.

Run: python -m pytest coordinator/bin/tests/test_publish_repo_key_literal_gate.py -q
"""

from __future__ import annotations

import pytest
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("publish_repo_key_gate_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_SOURCE = 'KEY = "repos.x"\n'


_KROW = "claude-klabauter-row"


def _git_source(tmp_path: Path, tracked: "dict[str, str]") -> Path:
    import subprocess

    src = tmp_path / "src"
    src.mkdir()
    for rel, text in tracked.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "add", "--", *tracked], check=True)
    return src


def _install_md_run(src: Path, allowlist: str = "INSTALL.md") -> bool:
    target = SimpleNamespace(name="row", source_dir=src, allowlist=allowlist)
    ctx = SimpleNamespace(engine_claude_klabauter=object())
    section = {"hooks": ["substitute"], "substitute": [{"key": "repos.x", "value": "repos.y"}]}
    return publish.dispatch_end_of_run_repo_key_literal_gate(ctx, [(target, section)])


def test_gitignored_scratch_copy_is_not_scanned_on_a_dot_source_install_md_row(tmp_path: Path) -> None:
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", ".gitignore": "scratch/\n"})
    (src / "scratch").mkdir()
    (src / "scratch" / "_engine_root.py").write_text(_SOURCE, encoding="utf-8")
    assert _install_md_run(src) is True


def test_untracked_drifting_file_is_not_scanned(tmp_path: Path) -> None:
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n"})
    (src / "other.py").write_text(_SOURCE, encoding="utf-8")
    assert _install_md_run(src) is True


def test_tracked_allowlisted_drift_still_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", "mod.py": _SOURCE})
    assert _install_md_run(src, "INSTALL.md,mod.py") is False
    err = capsys.readouterr().err
    assert "repos.x" in err and "mod.py" in err


def test_tracked_file_the_allowlist_withholds_is_not_scanned(tmp_path: Path) -> None:
    src = _git_source(tmp_path, {"INSTALL.md": "hello\n", "held.py": _SOURCE})
    assert _install_md_run(src) is True
