"""test_function_gate_parse_sweep -- coverage for the parse-sweep leg of
`dispatch_end_of_run_function_gate` (§ chunk C4C brief "the FUNCTION gate
must parse the payload, not just import three modules").

The gap this closes, execution-verified against the pre-fix published
mirror: `dispatch_end_of_run_function_gate` imported only 3 seed modules
(`_FUNCTION_GATE_SEED_MODULES`) in a hermetic subprocess, and returned
`GATE_OK` while `coordinator_core/state_root.py` carried a live
`SyntaxError` -- because nothing in the seed set's transitive closure
imports `state_root.py`, AND `coordinator_core/ops/__init__.py`'s eager-
import loop swallows a per-module `SyntaxError` at op-registration time
(logs, never raises), so even a REACHABLE broken module is invisible to an
import-based gate.

These tests prove `coordinator_core.percolate.engine.run_parse_sweep` (and
its wiring into `dispatch_end_of_run_function_gate`) closes exactly that
gap: total `ast.parse` coverage over every `.py` file in the payload, no
imports, no reachability question at all.

Run: python -m pytest coordinator/bin/tests/test_function_gate_parse_sweep.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_parse_sweep_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

from coordinator_core.percolate import engine as pct_engine  # noqa: E402


class _RealEngineClaudeKlabauter:
    """Delegates exactly the callables `dispatch_end_of_run_function_gate`
    needs, straight to the real engine module -- no mocking of the leg
    under test."""

    run_function_gate = staticmethod(pct_engine.run_function_gate)
    run_parse_sweep = staticmethod(pct_engine.run_parse_sweep)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)


class _EngineCtxStub:
    def __init__(self):
        self.engine_claude_klabauter = _RealEngineClaudeKlabauter()


# ---------------------------------------------------------------------------
# run_parse_sweep -- direct unit tests against the engine callable.
# ---------------------------------------------------------------------------
class TestRunParseSweep:
    def test_clean_tree_passes(self, tmp_path):
        (tmp_path / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        sub = tmp_path / "pkg"
        sub.mkdir()
        (sub / "b.py").write_text("x = 1\n", encoding="utf-8")

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is True
        assert result.failures == ()
        assert result.scanned == 2

    def test_hyphenated_identifier_syntax_error_fires(self, tmp_path):
        """The exact fixture shape named by the brief: a file with a
        hyphenated identifier (invalid Python syntax)."""
        (tmp_path / "broken.py").write_text(
            "claude-klabauter = foo()\n", encoding="utf-8"
        )

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is False
        assert len(result.failures) == 1
        assert result.failures[0].path == "broken.py"
        assert result.failures[0].lineno == 1

    def test_catches_a_module_nothing_imports(self, tmp_path):
        """The exact case the OLD import-reachability gate missed: a module
        with a syntax error that no other file in the payload imports."""
        (tmp_path / "entrypoint.py").write_text(
            "import json\n\ndef main():\n    return json.dumps({})\n",
            encoding="utf-8",
        )
        orphan_dir = tmp_path / "unreferenced"
        orphan_dir.mkdir()
        (orphan_dir / "state_root.py").write_text(
            "claude-klabauter = 1\n", encoding="utf-8"
        )

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is False
        failed_paths = {f.path for f in result.failures}
        assert "unreferenced/state_root.py" in failed_paths

    def test_reports_every_failure_not_just_the_first(self, tmp_path):
        for name in ("one.py", "two.py", "three.py"):
            (tmp_path / name).write_text("claude-klabauter = 1\n", encoding="utf-8")
        (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is False
        assert len(result.failures) == 3
        assert {f.path for f in result.failures} == {"one.py", "two.py", "three.py"}

    def test_excludes_publish_staging_leftovers(self, tmp_path):
        staging_dir = tmp_path / ".publish-staging-abc123"
        staging_dir.mkdir()
        (staging_dir / "broken.py").write_text("claude-klabauter = 1\n", encoding="utf-8")
        (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is True
        assert result.scanned == 1


# ---------------------------------------------------------------------------
# dispatch_end_of_run_function_gate -- proves the sweep is wired into the
# publish-side driver call site, not just the engine callable.
# ---------------------------------------------------------------------------
class TestDispatchWiresParseSweep:
    def test_unimported_broken_module_fails_the_driver_leg(self, tmp_path, capsys):
        """Reproduces the exact production gap: a seed module that imports
        cleanly, plus an unrelated file nothing imports that carries a
        syntax error -- the old import-only gate returned GATE_OK for this
        tree; the parse sweep must fail it."""
        repo_root = tmp_path / "repo"
        lib_dir = repo_root / "coordinator" / "bin" / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "coordinator_registry.py").write_text(
            "import json\n\ndef get(key):\n    return None\n", encoding="utf-8"
        )
        (repo_root / "coordinator_core").mkdir()
        (repo_root / "coordinator_core" / "state_root.py").write_text(
            "claude-klabauter = 1\n", encoding="utf-8"
        )

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err
        assert "state_root.py" in captured.err

    def test_clean_tree_with_no_seed_modules_still_runs_the_sweep(self, tmp_path, capsys):
        """A repo root with no seed modules present used to be a pure no-op
        (`continue` before any check ran) -- the sweep must still fire and
        can still fail the leg on its own."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "broken.py").write_text("claude-klabauter = 1\n", encoding="utf-8")

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err
        assert "broken.py" in captured.err

    def test_fully_clean_tree_still_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        lib_dir = repo_root / "coordinator" / "bin" / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "coordinator_registry.py").write_text(
            "import json\n\ndef get(key):\n    return None\n", encoding="utf-8"
        )
        (lib_dir / "coordinator_data_root.py").write_text(
            "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
        )
        core_dir = repo_root / "coordinator_core"
        core_dir.mkdir()
        (core_dir / "__init__.py").write_text("", encoding="utf-8")
        (core_dir / "data_root.py").write_text(
            "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
        )

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True
