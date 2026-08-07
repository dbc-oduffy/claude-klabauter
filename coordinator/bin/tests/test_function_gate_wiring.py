"""test_function_gate_wiring -- wiring tests for
`dispatch_end_of_run_function_gate`, the publish-side call site for chunk
C4's `run_function_gate`/`oss_shaped_subprocess_env`
(`coordinator_core/percolate/engine.py`).

Chunk C4 landed a hermetic FUNCTION gate (import the payload's own
entrypoint modules in a subprocess whose environment carries none of the
private registry keys) with ZERO production call sites -- `grep -rn
run_function_gate --include=*.py` returned only its own definition,
docstrings, and unit test. AC3 ("the publish pipeline fails when the
payload cannot import its own entrypoint modules") is not closed by a
callable nobody calls; this chunk (C4B) supplies the call site, and these
tests prove it FIRES THROUGH THE DRIVER PATH, not merely that
`run_function_gate` itself works (C4's own unit test already covers that).

SEVERITY judgement call (stated per this chunk's brief): FAIL-HARD,
unconditionally -- unlike `dispatch_end_of_run_identity_check`/
`..._install_doc_payload_check`, this leg does NOT degrade to advisory
under `--target` (see `dispatch_end_of_run_function_gate`'s own docstring
for the full rationale). `test_target_filtered_broken_module_still_hard_
fails` below is the test that pins this decision down.

Run: python -m pytest coordinator/bin/tests/test_function_gate_wiring.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_function_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

# Real engine module (not a fake) -- these tests exist to prove the WIRING
# fires a genuine hermetic subprocess failure through the driver, not to
# re-test `run_function_gate` itself (already covered by C4's
# `coordinator_core/percolate/tests/test_function_gate.py`).
from coordinator_core.percolate import engine as pct_engine  # noqa: E402


def _write_clean_gate_tree(root: Path) -> None:
    """A payload whose seed modules (§ `_FUNCTION_GATE_SEED_MODULES`) all
    import cleanly -- stdlib-only content, no coordinator_registry
    dependency of their own (this fixture only needs to prove the IMPORT
    succeeds, not that the real module's own logic runs)."""
    lib_dir = root / "coordinator" / "bin" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "coordinator_registry.py").write_text(
        "import json\n\ndef get(key):\n    return None\n", encoding="utf-8"
    )
    (lib_dir / "coordinator_data_root.py").write_text(
        "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
    )
    core_dir = root / "coordinator_core"
    core_dir.mkdir(parents=True)
    (core_dir / "__init__.py").write_text("", encoding="utf-8")
    (core_dir / "data_root.py").write_text(
        "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
    )


def _write_broken_gate_tree(root: Path) -> None:
    """The deliberately broken payload fixture AC3 asks for: a published
    `coordinator_registry.py` that cannot import at all -- the exact shape
    C4's gate exists to catch (a scrub/depersonalize/publish-time defect
    that leaves the shipped module unimportable)."""
    lib_dir = root / "coordinator" / "bin" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "coordinator_registry.py").write_text(
        "import this_module_does_not_exist_anywhere_c4b\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# dispatch_end_of_run_function_gate -- direct unit tests, REAL subprocess.
# ---------------------------------------------------------------------------
class _RealEngineClaudeKlabauter:
    """Delegates only the two C4 callables the gate needs, straight to the
    real engine module -- everything else this fixture never touches."""

    run_function_gate = staticmethod(pct_engine.run_function_gate)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)


class _EngineCtxStub:
    def __init__(self):
        self.claude-klabauter = _RealEngineClaudeKlabauter()


class TestEndOfRunFunctionGateLeg:
    def test_clean_tree_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        _write_clean_gate_tree(repo_root)

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True

    def test_unfiltered_broken_module_is_hard_failure(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        _write_broken_gate_tree(repo_root)

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err
        assert "coordinator_registry" in captured.err

    def test_target_filtered_broken_module_still_hard_fails(self, tmp_path, capsys):
        """Pins the stated judgement call: unlike the identity/install-doc
        legs, --target filtering does NOT downgrade a real import failure
        to advisory here."""
        repo_root = tmp_path / "repo"
        _write_broken_gate_tree(repo_root)

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=True
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err

    def test_repo_root_with_no_seed_modules_present_is_a_noop_pass(self, tmp_path):
        """A repo root that never published any of the 3 seed modules (§
        _FUNCTION_GATE_SEED_MODULES) is not gated on a module it never
        shipped -- distinct from a module that WAS shipped and is broken."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text("nothing here\n", encoding="utf-8")

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True


# ---------------------------------------------------------------------------
# publish.main() wiring -- proves the leg is actually invoked by the driver
# on a broken payload, dry-run never fires it, and a clean payload still
# passes end-to-end (the wiring under test here, not run_function_gate
# itself -- C4 already unit-tested the callable).
# ---------------------------------------------------------------------------
class _StubClaudeKlabauter:
    """Trivial fake `ClaudeKlabauterPercolate` for every phase call EXCEPT the two
    C4 gate callables, which delegate to the real engine module so a
    broken fixture genuinely fails the gate through the driver path (not a
    mocked pass/fail)."""

    run_function_gate = staticmethod(pct_engine.run_function_gate)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)

    def resolve_target(self, store, name):
        return {
            "hooks": [],
            "file_surface": {"include_extensions": ["*.md", "*.py"]},
            "guards": [],
            "inject": [],
        }

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())

    def run_identity_check(self, dest):
        return {"ran": True, "skipped": False, "exit_code": 0, "findings": "clean"}


def _wire_main_preconditions(monkeypatch, *, setup_dir: Path, rows: list) -> None:
    percolate_root = setup_dir.parent
    monkeypatch.setattr(
        publish, "_resolve_percolate_root_and_rung", lambda **kwargs: (percolate_root, "test-rung")
    )
    monkeypatch.setattr(
        publish, "load_targets", lambda setup_dir, target_filter="", **kwargs: rows
    )
    monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: setup_dir / "store.yaml")
    monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauter())
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda claude-klabauter, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "check_identity_file_present", lambda *a, **k: None)
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda *a, **k: publish.PercolateIdentity(review=["test-machine-slug"]),
    )
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish, "process_target", lambda *a, **k: None)


def _single_row(name: str, repo_root: Path) -> list:
    return [f"{name}|mirror|{repo_root / 'src'}|{repo_root}"]


class TestFunctionGateMainWiring:
    def test_full_run_broken_module_fails(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_broken_gate_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err
        assert "coordinator_registry" in captured.err

    def test_full_run_clean_tree_passes(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_gate_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc == 0

    def test_dry_run_never_fires_the_leg(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_broken_gate_tree(repo_root)  # would fail loudly if the leg fired

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main(["--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "function gate" not in captured.err
