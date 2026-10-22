"""Pins the claude-author -> DoE control-socket supervisor seam from the engine side.

`claude-author.py :: _run_supervisor` loads `<plugin_dir>/bin/lib/claude_author_control/supervisor.py`
by path and calls `run(full_argv, plugin_dir=..., control_base=...)`. The two halves live in
different repos, so each side pins the contract: this file checks the real DoE supervisor still
exposes the keyword-only signature, and that `_run_supervisor` calls it that way. The DoE-backed
test skips when no DoE plugin root is present (override with COORDINATOR_DOE_PLUGIN_ROOT).
"""

from __future__ import annotations

import importlib.util
import inspect
import os
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "claude-author.py"
_SUP_REL = Path("bin") / "lib" / "claude_author_control" / "supervisor.py"


def _doe_plugin_root() -> Path | None:
    override = os.environ.get("COORDINATOR_DOE_PLUGIN_ROOT", "").strip()
    candidates = (
        [Path(override)]
        if override
        else [Path(__file__).resolve().parents[3].parent / "coordinator-content-repo" / "coordinator"]
    )
    for cand in candidates:
        if (cand / _SUP_REL).is_file():
            return cand
    return None


def _load_claude_author():
    spec = importlib.util.spec_from_file_location("claude_author_seam_under_test", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_real_doe_supervisor_run_signature_is_keyword_only():
    root = _doe_plugin_root()
    if root is None:
        pytest.skip("DoE plugin root absent (no claude_author_control/supervisor.py)")
    spec = importlib.util.spec_from_file_location(
        "seam_real_supervisor", root / _SUP_REL
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    params = list(inspect.signature(mod.run).parameters.values())
    assert [p.name for p in params] == ["full_argv", "plugin_dir", "control_base"]
    assert params[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params[1].kind is inspect.Parameter.KEYWORD_ONLY
    assert params[2].kind is inspect.Parameter.KEYWORD_ONLY
    assert all(p.default is inspect.Parameter.empty for p in params)
    ret = inspect.signature(mod.run).return_annotation
    assert ret in (int, "int")


def test_run_supervisor_loads_by_path_and_calls_keyword_only(tmp_path, monkeypatch):
    pkg = tmp_path / "bin" / "lib" / "claude_author_control"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "supervisor.py").write_text(
        "def run(full_argv, *, plugin_dir, control_base):\n"
        f"    return 7 if (full_argv, plugin_dir, control_base) == (['c', 'x'], {str(tmp_path)!r}, 'B') else 99\n"
    )
    for name in ("claude_author_control", "claude_author_control.supervisor"):
        monkeypatch.delitem(__import__("sys").modules, name, raising=False)
    ca = _load_claude_author()
    try:
        assert ca._run_supervisor(["c", "x"], str(tmp_path), "B") == 7
    finally:
        for name in ("claude_author_control", "claude_author_control.supervisor"):
            __import__("sys").modules.pop(name, None)
