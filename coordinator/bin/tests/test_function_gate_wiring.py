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
import shutil
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


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
    """Delegates only the C4 callables the gate needs, straight to the
    real engine module -- everything else this fixture never touches."""

    run_function_gate = staticmethod(pct_engine.run_function_gate)
    run_parse_sweep = staticmethod(pct_engine.run_parse_sweep)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)
    hermetic_gate_env = staticmethod(pct_engine.hermetic_gate_env)
    mktcache_gate_env = staticmethod(pct_engine.mktcache_gate_env)
    run_entrypoint_gate = staticmethod(pct_engine.run_entrypoint_gate)
    enumerate_gate_entrypoints = staticmethod(pct_engine.enumerate_gate_entrypoints)
    derive_worker_cap = staticmethod(lambda: 1)


class _EngineCtxStub:
    def __init__(self):
        self.engine_claude_klabauter = _RealEngineClaudeKlabauter()


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

    def test_gate_env_is_hermetic_not_ambient(self, tmp_path, monkeypatch):
        """§ BLOCKER-2: the production call site must never pass the real
        ambient HOME/USERPROFILE/CLAUDE_HOME to the gate subprocess --
        pins that `dispatch_end_of_run_function_gate` uses `hermetic_gate_
        env` (isolated, empty temp dir) rather than bare `oss_shaped_
        subprocess_env()` (real environment passthrough)."""
        repo_root = tmp_path / "repo"
        _write_clean_gate_tree(repo_root)

        seen_envs = []
        real_run_function_gate = pct_engine.run_function_gate

        def _spying_run_function_gate(*args, **kwargs):
            seen_envs.append(dict(kwargs.get("env") or {}))
            return real_run_function_gate(*args, **kwargs)

        class _SpyingEngineClaudeKlabauter(_RealEngineClaudeKlabauter):
            run_function_gate = staticmethod(_spying_run_function_gate)

        class _SpyingEngineCtxStub:
            def __init__(self):
                self.engine_claude_klabauter = _SpyingEngineClaudeKlabauter()

        ambient_home = str(tmp_path / "ambient-home-should-never-be-passed")
        monkeypatch.setenv("HOME", ambient_home)
        monkeypatch.setenv("USERPROFILE", ambient_home)
        monkeypatch.setenv("CLAUDE_HOME", ambient_home)

        ok = publish.dispatch_end_of_run_function_gate(
            _SpyingEngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True
        assert seen_envs, "run_function_gate was never called"
        for env in seen_envs:
            for key in ("HOME", "USERPROFILE", "CLAUDE_HOME"):
                assert env.get(key) != ambient_home, (
                    f"{key} leaked the ambient value into the gate subprocess env"
                )

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
# Director-of-Engineering ruling -- the synthetic coordinator-registry
# manifest rung (`publish._synthetic_registry_manifest_overrides`,
# `dispatch_end_of_run_function_gate`'s own docstring "WHAT THIS GATE
# ACTUALLY ASSERTS" section). Reproduces the actual class-2 failure a bare
# engine mirror hit (`state/audits/2026-08-10-klabauter-gate-failure-
# classes.md`): the REAL `coordinator_registry.py`, hermetically gated with
# no coordinator-claude install anywhere in the environment, previously
# failed by construction with `FileNotFoundError: coordinator_registry:
# manifest not found`. `_write_bare_engine_mirror_gate_tree` copies the REAL
# module (never a synthetic stand-in) so these tests exercise the actual
# manifest-bootstrap ladder the ruling is about, not a re-test of the
# hand-rolled stubs `_write_clean_gate_tree` uses elsewhere in this file.
# ---------------------------------------------------------------------------
def _write_bare_engine_mirror_gate_tree(root: Path) -> None:
    """A bare engine-mirror payload shaped like `claude-klabauter`: ships the
    REAL `coordinator_registry.py` (copied verbatim from this repo's own
    `coordinator/bin/lib/`, the same module
    `test_gate_fires_hermetically_on_synthetic_manifest_fixture`
    (`coordinator_core/percolate/tests/test_function_gate.py`) imports
    directly) plus the real, load-bearing import-time chain it walks to reach
    the `.doe-root` pointer rung (`machine_local_impl_resolve.py` beside it,
    and `read_doe_root_pointer.py` + `settings_home.py` at the co-located
    `coordinator/lib/` layout `_mp_doe_root_pointer_rung` probes first) -- but
    never ships `coordinator/schemas/coordinator-registry.manifest.json`
    itself, since that artifact is DoE-claude's, delivered only via a
    coordinator-claude plugin install. Every OTHER top-level dependency
    `coordinator_registry.py` reaches for (`coordinator_core.*`) is imported
    LAZILY inside functions wrapped in a swallow-and-return-empty contract,
    so this copy is sufficient to reach the real manifest-bootstrap ladder at
    import time without dragging in the rest of the repo."""
    bin_lib_dir = root / "coordinator" / "bin" / "lib"
    bin_lib_dir.mkdir(parents=True)
    coordinator_lib_dir = root / "coordinator" / "lib"
    coordinator_lib_dir.mkdir(parents=True)

    real_bin_lib_dir = _REPO_ROOT / "coordinator" / "bin" / "lib"
    real_coordinator_lib_dir = _REPO_ROOT / "coordinator" / "lib"
    shutil.copy2(real_bin_lib_dir / "coordinator_registry.py", bin_lib_dir / "coordinator_registry.py")
    shutil.copy2(
        real_bin_lib_dir / "machine_local_impl_resolve.py",
        bin_lib_dir / "machine_local_impl_resolve.py",
    )
    shutil.copy2(
        real_coordinator_lib_dir / "read_doe_root_pointer.py",
        coordinator_lib_dir / "read_doe_root_pointer.py",
    )
    shutil.copy2(
        real_coordinator_lib_dir / "settings_home.py",
        coordinator_lib_dir / "settings_home.py",
    )


class TestFunctionGateSyntheticManifestRung:
    def test_bare_engine_mirror_now_passes_hermetically(self, tmp_path):
        """The positive direction: a bare engine mirror -- no coordinator-
        claude install anywhere, no ambient manifest -- now passes because
        `dispatch_end_of_run_function_gate` stages a resolvable SYNTHETIC
        manifest rung of its own. Before this fix, this exact tree failed
        the gate by construction (class 2, § the ruling this test pins)."""
        repo_root = tmp_path / "repo"
        _write_bare_engine_mirror_gate_tree(repo_root)

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True

    def test_gate_still_fails_closed_when_the_fixture_rung_cannot_resolve(
        self, tmp_path, monkeypatch, capsys
    ):
        """Fail-closed proof (deliverable #3, direction 2): if the manifest
        rung this fix stages does NOT resolve to anything (simulated here by
        monkeypatching `_synthetic_registry_manifest_overrides` to point
        `COORDINATOR_SETTINGS_HOME` at an empty directory with no `.doe-root`
        pointer at all -- the same negative control
        `test_gate_fires_when_manifest_fixture_is_absent` uses at the engine
        level), the REAL `coordinator_registry.py` still fails its own
        manifest-not-found check and the gate reports it -- proving this
        change did not make the gate unconditionally green."""
        repo_root = tmp_path / "repo"
        _write_bare_engine_mirror_gate_tree(repo_root)

        empty_settings_home = tmp_path / "empty-settings-home"
        empty_settings_home.mkdir()

        import contextlib

        @contextlib.contextmanager
        def _no_manifest_overrides():
            yield {"COORDINATOR_SETTINGS_HOME": str(empty_settings_home)}

        monkeypatch.setattr(publish, "_synthetic_registry_manifest_overrides", _no_manifest_overrides)

        ok = publish.dispatch_end_of_run_function_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "function gate FAILED" in captured.err
        assert "coordinator_registry" in captured.err


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
    run_parse_sweep = staticmethod(pct_engine.run_parse_sweep)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)
    hermetic_gate_env = staticmethod(pct_engine.hermetic_gate_env)
    mktcache_gate_env = staticmethod(pct_engine.mktcache_gate_env)
    run_entrypoint_gate = staticmethod(pct_engine.run_entrypoint_gate)
    enumerate_gate_entrypoints = staticmethod(pct_engine.enumerate_gate_entrypoints)
    derive_worker_cap = staticmethod(lambda: 1)

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
    monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, store_path: {"targets": {}})
    monkeypatch.setattr(publish, "check_identity_file_present", lambda *a, **k: None)
    monkeypatch.setattr(publish, "check_identity_file_safe", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "parse_percolate_identity",
        lambda *a, **k: publish.PercolateIdentity(review=["test-machine-slug"]),
    )
    monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "process_target",
        lambda target, setup_dir, totals, **kwargs: setattr(totals, "processed", totals.processed + 1),
    )


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


# ---------------------------------------------------------------------------
# dispatch_end_of_run_entrypoint_gate -- chunk C3's own wiring: pins that
# C2's `run_entrypoint_gate` has a production caller (§ EM remit-extension,
# "a gate with no caller passing its own unit tests is exactly the shape we
# just caught"), and pins the derived worker cap / enforced aggregate budget
# this chunk's own AC4 is about.
# ---------------------------------------------------------------------------
import sys as _sys  # noqa: E402


def _write_bare_entrypoint(repo_root: Path, name: str, *, body: str) -> Path:
    """A `coordinator/bin/<name>` bare (extensionless) entrypoint --
    `require_main_guard=False` for that scan root (§ `test_bin_launcher_
    parity.SCAN_ROOTS`), so any top-level file there is an entrypoint by
    construction; no `__main__` guard needed."""
    bin_dir = repo_root / "coordinator" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env python3\n{body}\n", encoding="utf-8", newline="\n")
    path.chmod(0o755)
    return path


def _write_clean_entrypoint_tree(root: Path) -> None:
    _write_bare_entrypoint(
        root,
        "clean-cli",
        body="import sys\nprint('help text')\nsys.exit(0)\n",
    )


def _write_broken_entrypoint_tree(root: Path) -> None:
    _write_bare_entrypoint(
        root,
        "broken-cli",
        body="import sys\nsys.stderr.write('boom: cannot start\\n')\nsys.exit(1)\n",
    )


class TestEndOfRunEntrypointGateLeg:
    def test_clean_tree_passes(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_clean_entrypoint_tree(repo_root)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True

    def test_non_starting_entrypoint_is_a_hard_failure(self, tmp_path, capsys):
        """AC3's own negative test: a deliberately broken entrypoint in a
        scratch payload turns the gate red."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_broken_entrypoint_tree(repo_root)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "entrypoint gate FAILED" in captured.err
        assert "broken-cli" in captured.err

    def test_target_filtered_broken_entrypoint_still_hard_fails(self, tmp_path, capsys):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_broken_entrypoint_tree(repo_root)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=True
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "entrypoint gate FAILED" in captured.err

    def test_no_entrypoints_present_is_a_noop_pass(self, tmp_path):
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text("nothing here\n", encoding="utf-8")

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True

    def test_worker_cap_is_derived_not_hardcoded(self, tmp_path, monkeypatch):
        """AC4 (a): the cap must respond to a monkeypatched core/RAM figure,
        not be a literal pinned in the call site -- a test asserting a fixed
        number would defeat its own purpose (§ this chunk's brief item 1)."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_clean_entrypoint_tree(repo_root)

        seen_caps = []
        real_run_entrypoint_gate = pct_engine.run_entrypoint_gate

        def _spying_run_entrypoint_gate(*args, **kwargs):
            seen_caps.append(kwargs.get("max_workers"))
            return real_run_entrypoint_gate(*args, **kwargs)

        class _SpyingEngineClaudeKlabauter(_RealEngineClaudeKlabauter):
            run_entrypoint_gate = staticmethod(_spying_run_entrypoint_gate)
            derive_worker_cap = staticmethod(lambda: 7)

        class _SpyingEngineCtxStub:
            def __init__(self):
                self.engine_claude_klabauter = _SpyingEngineClaudeKlabauter()

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _SpyingEngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True
        assert seen_caps == [7], (
            "the call site must pass through whatever engine_claude_klabauter.derive_worker_cap() "
            "returns, not a value of its own choosing"
        )

    def test_worker_cap_formula_carries_both_terms(self, monkeypatch):
        """AC4 (a), the specific regression named in the brief: a cap that
        silently drops the RAM term must fail this test. Pins `derive_worker_
        cap` itself (the canonical two-term formula this chunk reuses, §
        `coordinator_core/diagnostics/contained_run.py`) against a
        monkeypatched psutil so both the core term AND the RAM term are
        provably load-bearing, not just the core term alone."""
        from coordinator_core.diagnostics import contained_run

        class _FakeVirtualMemory:
            available = 1 * (1024 ** 3)  # 1 GiB -- deliberately tiny

        class _FakePsutil:
            @staticmethod
            def cpu_count(logical=False):
                return 64  # deliberately huge core count

            @staticmethod
            def virtual_memory():
                return _FakeVirtualMemory()

        monkeypatch.setitem(_sys.modules, "psutil", _FakePsutil())
        cap = contained_run.derive_worker_cap()
        # physical_cores/2 = 32; usable_ram_gb*1024/150 = 1024/150 ~= 6.8 -> floor 6.
        # If the RAM term were dropped, this would be 32, not 6 -- the exact
        # regression shape this test pins.
        assert cap == 6, f"expected the RAM term to bind (cap=6), got {cap}"

    def test_aggregate_budget_is_enforced_not_merely_accepted(self, tmp_path, monkeypatch):
        """AC4 (b), the load-bearing half: an already-elapsed aggregate
        budget must turn entrypoints that have not yet been dispatched into
        reported failures, not merely be accepted as an unused kwarg. Forces
        `time.monotonic` to report the budget as already exhausted before
        the first entrypoint dispatches."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        _write_clean_entrypoint_tree(repo_root)

        real_monotonic = pct_engine.time.monotonic
        calls = {"n": 0}

        def _budget_already_blown(*args, **kwargs):
            calls["n"] += 1
            # First call establishes start_time; every subsequent call (the
            # pre-dispatch budget check) reports it as already far exceeded.
            if calls["n"] == 1:
                return real_monotonic()
            return real_monotonic() + 10_000.0

        monkeypatch.setattr(pct_engine.time, "monotonic", _budget_already_blown)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False, (
            "an aggregate budget that has already elapsed before dispatch must "
            "fail the gate -- a call site that only threads the kwarg through "
            "without it ever binding would pass here incorrectly"
        )


# ---------------------------------------------------------------------------
# Director-of-Engineering ruling, entrypoint-gate leg -- mirrors
# `TestFunctionGateSyntheticManifestRung` above for `dispatch_end_of_run_
# entrypoint_gate`. The function gate and entrypoint gate are TWO SEPARATE
# end-of-run legs (§ that function's own docstring); staging the synthetic
# manifest rung for one does not wire the other -- a bare engine mirror's
# shipped entrypoints spawn their own subprocesses, each hitting the same
# `coordinator_registry: manifest not found` FileNotFoundError the function
# gate hit before its own fix (§ state/audits/2026-08-10-klabauter-gate-
# failure-classes.md, class 2, 11/12 entrypoint-gate failures this class).
# ---------------------------------------------------------------------------
def _write_bare_engine_mirror_entrypoint_tree(root: Path) -> None:
    """A bare engine-mirror payload shaped like `claude-klabauter`: one
    shipped bare entrypoint (`coordinator/bin/mirror-cli`) that imports the
    REAL `coordinator_registry.py` (copied verbatim, same module `_write_
    bare_engine_mirror_gate_tree` above copies for the FUNCTION gate) via the
    same `sys.path.insert(0, <script-dir>/lib)` + module-level `import
    coordinator_registry` shape every real `coordinator/bin/coordinator-*`
    CLI uses (e.g. `coordinator-doc-new`) -- so `--help` still triggers the
    import before argparse ever runs, exactly like the real CLIs this
    fixture stands in for. Never ships `coordinator/schemas/coordinator-
    registry.manifest.json` itself -- that artifact is DoE-claude's,
    delivered only via a coordinator-claude plugin install."""
    bin_dir = root / "coordinator" / "bin"
    bin_lib_dir = bin_dir / "lib"
    bin_lib_dir.mkdir(parents=True)
    coordinator_lib_dir = root / "coordinator" / "lib"
    coordinator_lib_dir.mkdir(parents=True)

    real_bin_lib_dir = _REPO_ROOT / "coordinator" / "bin" / "lib"
    real_coordinator_lib_dir = _REPO_ROOT / "coordinator" / "lib"
    shutil.copy2(real_bin_lib_dir / "coordinator_registry.py", bin_lib_dir / "coordinator_registry.py")
    shutil.copy2(
        real_bin_lib_dir / "machine_local_impl_resolve.py",
        bin_lib_dir / "machine_local_impl_resolve.py",
    )
    shutil.copy2(
        real_coordinator_lib_dir / "read_doe_root_pointer.py",
        coordinator_lib_dir / "read_doe_root_pointer.py",
    )
    shutil.copy2(
        real_coordinator_lib_dir / "settings_home.py",
        coordinator_lib_dir / "settings_home.py",
    )

    entrypoint_path = bin_dir / "mirror-cli"
    entrypoint_path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import sys\n"
        "_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))\n"
        "_LIB_DIR = os.path.join(_SCRIPT_DIR, 'lib')\n"
        "if _LIB_DIR not in sys.path:\n"
        "    sys.path.insert(0, _LIB_DIR)\n"
        "import coordinator_registry  # noqa: F401 -- same shape every real bin CLI uses\n"
        "print('help text')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
        newline="\n",
    )
    entrypoint_path.chmod(0o755)


class TestEntrypointGateSyntheticManifestRung:
    def test_bare_engine_mirror_entrypoints_now_start_cleanly(self, tmp_path):
        """The positive direction: a bare engine mirror -- no coordinator-
        claude install anywhere, no ambient manifest -- now passes because
        `dispatch_end_of_run_entrypoint_gate` stages the same resolvable
        SYNTHETIC manifest rung `dispatch_end_of_run_function_gate` already
        stages, reused rather than re-derived. Before this fix, this exact
        entrypoint failed the gate by construction (class 2)."""
        repo_root = tmp_path / "repo"
        _write_bare_engine_mirror_entrypoint_tree(repo_root)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is True

    def test_gate_still_fails_closed_when_the_fixture_rung_cannot_resolve(
        self, tmp_path, monkeypatch, capsys
    ):
        """Fail-closed proof (direction 2, the one that matters): if the
        manifest rung this fix stages does NOT resolve to anything
        (monkeypatched here to point `COORDINATOR_SETTINGS_HOME` at an empty
        directory with no `.doe-root` pointer at all -- same negative
        control `TestFunctionGateSyntheticManifestRung` uses above), the
        REAL `coordinator_registry.py` inside the entrypoint's own subprocess
        still fails its own manifest-not-found check and the gate reports
        it -- proving this change did not make the gate unconditionally
        green. The pre-existing broken-entrypoint hard-failure tests
        (`test_non_starting_entrypoint_is_a_hard_failure`, `test_target_
        filtered_broken_entrypoint_still_hard_fails`,
        `test_full_run_broken_entrypoint_fails`) already cover the OTHER
        broken-entrypoint shape (a CLI that exits non-zero on its own, no
        manifest involved) and are unaffected by this rung."""
        repo_root = tmp_path / "repo"
        _write_bare_engine_mirror_entrypoint_tree(repo_root)

        empty_settings_home = tmp_path / "empty-settings-home"
        empty_settings_home.mkdir()

        import contextlib

        @contextlib.contextmanager
        def _no_manifest_overrides():
            yield {"COORDINATOR_SETTINGS_HOME": str(empty_settings_home)}

        monkeypatch.setattr(publish, "_synthetic_registry_manifest_overrides", _no_manifest_overrides)

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            _EngineCtxStub(), [repo_root], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "entrypoint gate FAILED" in captured.err
        assert "mirror-cli" in captured.err


class TestEntrypointGateMainWiring:
    def test_full_run_broken_entrypoint_fails(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_gate_tree(repo_root)  # FUNCTION gate leg must still pass
        _write_broken_entrypoint_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc != 0
        captured = capsys.readouterr()
        assert "entrypoint gate FAILED" in captured.err
        assert "broken-cli" in captured.err

    def test_full_run_clean_entrypoints_pass(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_gate_tree(repo_root)
        _write_clean_entrypoint_tree(repo_root)

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main([])
        assert rc == 0

    def test_dry_run_never_fires_the_entrypoint_leg(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        _write_clean_gate_tree(repo_root)
        _write_broken_entrypoint_tree(repo_root)  # would fail loudly if the leg fired

        _wire_main_preconditions(monkeypatch, setup_dir=setup_dir, rows=_single_row("t", repo_root))

        rc = publish.main(["--dry-run"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "entrypoint gate" not in captured.err


# ---------------------------------------------------------------------------
# dispatch_end_of_run_functional_identifier_output_drift_check -- wiring
# tests for MAJOR-2 of state/review-findings/2026-08-08-codename-free-
# partitioned/slice-D-drift-store.md ("nothing calls the gate; it is a
# library, not a gate"). Real `coordinator_core.percolate.store` functions
# (not fakes) -- these tests exist to prove the WIRING fires a genuine
# drift detection through the driver, not to re-test
# `find_functional_identifier_output_drift_in_tree` itself.
# ---------------------------------------------------------------------------
from coordinator_core.percolate import store as pct_store  # noqa: E402


class _ResolvedTargetStub:
    """Minimal stand-in for `publish.ResolvedTarget` -- only the two fields
    `dispatch_end_of_run_functional_identifier_output_drift_check` reads."""

    def __init__(self, name, source_dir, dest_dir):
        self.name = name
        self.source_dir = source_dir
        self.dest_dir = dest_dir


class _DriftEngineClaudeKlabauter:
    """Delegates only the two drift callables to the real store module."""

    find_functional_identifier_output_drift_in_tree = staticmethod(
        pct_store.find_functional_identifier_output_drift_in_tree
    )
    load_functional_identifier_output_drift_baseline = staticmethod(
        pct_store.load_functional_identifier_output_drift_baseline
    )


class _DriftEngineCtxStub:
    def __init__(self):
        self.engine_claude_klabauter = _DriftEngineClaudeKlabauter()


_DRIFTED_SECTION = {
    "substitute": [{"key": "CLAUDE_KLABAUTER_ROOT", "value": "EXAMPLE_ROOT"}],
}


def _write_drifted_pair(tmp_path):
    """A source/dest pair where the destination genuinely drifted a
    functional identifier the resolved section's own substitute rule
    explains -- `CLAUDE_KLABAUTER_ROOT` -> `EXAMPLE_ROOT`."""
    source_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    (source_dir / "config.env").write_text("export CLAUDE_KLABAUTER_ROOT=/opt/x\n", encoding="utf-8")
    (dest_dir / "config.env").write_text("export EXAMPLE_ROOT=/opt/x\n", encoding="utf-8")
    return source_dir, dest_dir


def _write_clean_pair(tmp_path):
    source_dir = tmp_path / "src"
    dest_dir = tmp_path / "dest"
    source_dir.mkdir()
    dest_dir.mkdir()
    (source_dir / "config.env").write_text("export CLAUDE_KLABAUTER_ROOT=/opt/x\n", encoding="utf-8")
    (dest_dir / "config.env").write_text("export CLAUDE_KLABAUTER_ROOT=/opt/x\n", encoding="utf-8")
    return source_dir, dest_dir


class TestEndOfRunFunctionalIdentifierOutputDriftLeg:
    def test_leg_is_reachable_and_no_drift_passes(self, tmp_path):
        source_dir, dest_dir = _write_clean_pair(tmp_path)
        target = _ResolvedTargetStub("t", source_dir, dest_dir)

        ok = publish.dispatch_end_of_run_functional_identifier_output_drift_check(
            _DriftEngineCtxStub(), [(target, _DRIFTED_SECTION)], target_filtered=False
        )
        assert ok is True

    def test_drifted_identifier_is_a_hard_failure(self, tmp_path, capsys):
        source_dir, dest_dir = _write_drifted_pair(tmp_path)
        target = _ResolvedTargetStub("t", source_dir, dest_dir)

        ok = publish.dispatch_end_of_run_functional_identifier_output_drift_check(
            _DriftEngineCtxStub(), [(target, _DRIFTED_SECTION)], target_filtered=False
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "functional-identifier output-drift check FAILED" in captured.err
        assert "CLAUDE_KLABAUTER_ROOT" in captured.err
        assert "EXAMPLE_ROOT" in captured.err

    def test_target_filtered_drift_still_hard_fails(self, tmp_path, capsys):
        """Same judgement call as the FUNCTION gate leg (§
        test_target_filtered_broken_module_still_hard_fails above): --target
        filtering must not soften a genuine drift finding to advisory."""
        source_dir, dest_dir = _write_drifted_pair(tmp_path)
        target = _ResolvedTargetStub("t", source_dir, dest_dir)

        ok = publish.dispatch_end_of_run_functional_identifier_output_drift_check(
            _DriftEngineCtxStub(), [(target, _DRIFTED_SECTION)], target_filtered=True
        )
        assert ok is False
        captured = capsys.readouterr()
        assert "functional-identifier output-drift check FAILED" in captured.err

    def test_missing_source_dir_is_a_noop_pass(self, tmp_path):
        """A row whose source tree does not exist (e.g. a fixture with no
        real source) is skipped, not treated as a failure -- mirrors the
        FUNCTION gate leg's own `repo_root.is_dir()` guard."""
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        target = _ResolvedTargetStub("t", tmp_path / "does-not-exist", dest_dir)

        ok = publish.dispatch_end_of_run_functional_identifier_output_drift_check(
            _DriftEngineCtxStub(), [(target, _DRIFTED_SECTION)], target_filtered=False
        )
        assert ok is True


class TestFunctionalIdentifierOutputDriftMainWiring:
    def test_full_run_drifted_identifier_fails(self, tmp_path, monkeypatch, capsys):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        source_dir, dest_dir = _write_drifted_pair(tmp_path)
        _write_clean_gate_tree(dest_dir)  # FUNCTION gate leg must still pass so
        # this test isolates the drift leg's own fatal-ness, not a coincidental
        # FUNCTION-gate failure on an otherwise-empty dest tree.

        class _StubClaudeKlabauterWithDrift(_StubClaudeKlabauter):
            find_functional_identifier_output_drift_in_tree = staticmethod(
                pct_store.find_functional_identifier_output_drift_in_tree
            )
            load_functional_identifier_output_drift_baseline = staticmethod(
                pct_store.load_functional_identifier_output_drift_baseline
            )

            def resolve_target(self, store, name):
                return dict(super().resolve_target(store, name), **_DRIFTED_SECTION)

        monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauterWithDrift())
        _wire_main_preconditions(
            monkeypatch,
            setup_dir=setup_dir,
            rows=[f"t|mirror|{source_dir}|{dest_dir}"],
        )
        # `_wire_main_preconditions` overwrites `_import_claude_klabauter_percolate` with the
        # plain `_StubClaudeKlabauter` -- reassert the drift-capable stub after it.
        monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauterWithDrift())

        rc = publish.main([])
        # STOOD DOWN, deliberately: `main()` does not call the drift leg. This
        # assertion is inverted from what it pins on a live leg, and that is the
        # point -- re-enabling the leg in `publish.py` flips this test red, so
        # nobody re-enables it silently.
        #
        # Why it is stood down (docs/research/spike-verdicts/
        # 2026-08-08-drift-gate-discriminator-position-validity.md): measured over
        # all 7 real targets the leg reports 7236 pairs containing 0 defects, and
        # the SyntaxError-across-15-files defect it exists to catch is not
        # reportable by it under ANY discriminator -- `_extract_functional_tokens`
        # classifies both sides of that pair as 'mention' and never emits them.
        # Live, it blocks every publish while detecting nothing.
        #
        # The leg itself remains correct and is still exercised directly by
        # TestEndOfRunFunctionalIdentifierOutputDriftLeg above; only its call
        # from `main()` is withdrawn.
        assert rc == 0
        captured = capsys.readouterr()
        assert "functional-identifier output-drift check FAILED" not in captured.err

    def test_full_run_clean_identifiers_pass(self, tmp_path, monkeypatch):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        source_dir, dest_dir = _write_clean_pair(tmp_path)
        _write_clean_gate_tree(dest_dir)

        class _StubClaudeKlabauterWithDrift(_StubClaudeKlabauter):
            find_functional_identifier_output_drift_in_tree = staticmethod(
                pct_store.find_functional_identifier_output_drift_in_tree
            )
            load_functional_identifier_output_drift_baseline = staticmethod(
                pct_store.load_functional_identifier_output_drift_baseline
            )

            def resolve_target(self, store, name):
                return dict(super().resolve_target(store, name), **_DRIFTED_SECTION)

        _wire_main_preconditions(
            monkeypatch,
            setup_dir=setup_dir,
            rows=[f"t|mirror|{source_dir}|{dest_dir}"],
        )
        monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _StubClaudeKlabauterWithDrift())

        rc = publish.main([])
        assert rc == 0
