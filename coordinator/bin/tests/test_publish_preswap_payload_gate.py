"""test_publish_preswap_payload_gate -- tests for chunk C1 of
state/dispatch-briefs/2026-08-21-the-payload-proves-itself-before-it-
overwrites-the-engine/C1.md: `dispatch_preswap_function_gate`, the
per-row FUNCTION gate `process_target` runs against the STAGED tree
immediately before `_swap_publish_staging_into_dest`.

CORRECTED framing this chunk exists to close: `dispatch_end_of_run_
function_gate`'s seed-presence probe assumes the root it is handed looks
like a repo root. A per-row `staging_dir` does not -- the `coordinator_core`
row's staging tree holds `data_root.py` at ITS OWN root, not
`coordinator_core/data_root.py`, so feeding `staging_dir` through the
end-of-run probe unmodified misses all three `_FUNCTION_GATE_SEED_MODULES`
entries (`modules == []`) and silently degrades to parse-sweep-only on the
one row that matters. These tests pin the `rel_root`/module-prefix fix
directly (`_function_gate_modules_and_search_paths_for_repo_root`), the
EXPECTED-SUBSET EQUALITY assertion (AC4 -- never bare non-emptiness), and
the wiring into `process_target` (gate runs BEFORE the swap; a failure
refuses it).

Run: python -m pytest coordinator/bin/tests/test_publish_preswap_payload_gate.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_preswap_gate_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

# Real engine module (not a fake) -- these tests exist to prove the WIRING
# fires a genuine hermetic subprocess check through the driver, not to
# re-test `run_function_gate` itself (already covered by C4's own unit
# test, `coordinator_core/percolate/tests/test_function_gate.py`).
from coordinator_core.percolate import engine as pct_engine  # noqa: E402


class _RealEngineClaudeKlabauter:
    """Delegates only the C4 gate callables straight to the real engine
    module -- everything else this fixture never touches."""

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


def _make_target(name: str, repo_root: Path, dest_subdir: str, *, source_dir: Path | None = None) -> "publish.ResolvedTarget":
    """A `ResolvedTarget` whose `dest_dir` sits `dest_subdir` below a repo
    root carrying a real `.git` marker -- `_dest_prefix_for` (the row's
    `dest_subdir`/module-prefix, § this chunk's own brief) resolves off
    that walk, so the marker is load-bearing, not decoration."""
    (repo_root / ".git").mkdir(parents=True, exist_ok=True)
    dest_dir = repo_root / dest_subdir if dest_subdir else repo_root
    return publish.ResolvedTarget(
        name=name,
        mode="mirror",
        source_dir=source_dir if source_dir is not None else (repo_root.parent / "src"),
        dest_dir=dest_dir,
    )


# ---------------------------------------------------------------------------
# _function_gate_modules_and_search_paths_for_repo_root / _function_gate_
# expected_seed_rel_paths_for_rel_root -- the rel_root/module-prefix fix
# itself, direct unit tests, no subprocess.
# ---------------------------------------------------------------------------
class TestModulePrefixResolution:
    def test_toplevel_rel_root_is_unchanged_from_pre_fix_behavior(self, tmp_path):
        """`rel_root=""` (the end-of-run caller's own repo root) must resolve
        identically to the pre-fix single-argument call -- this is the
        regression guard for the ADDITIVE claim in the brief ("Keep the
        existing end-of-run invocation")."""
        repo_root = tmp_path / "repo"
        lib_dir = repo_root / "coordinator" / "bin" / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "coordinator_registry.py").write_text("x = 1\n", encoding="utf-8")
        core_dir = repo_root / "coordinator_core"
        core_dir.mkdir()
        (core_dir / "__init__.py").write_text("", encoding="utf-8")
        (core_dir / "data_root.py").write_text("x = 1\n", encoding="utf-8")

        modules, search_paths, resolved = publish._function_gate_modules_and_search_paths_for_repo_root(repo_root)
        assert set(modules) == {"coordinator_registry", "coordinator_core.data_root"}
        assert resolved == {
            "coordinator/bin/lib/coordinator_registry.py",
            "coordinator_core/data_root.py",
        }

    def test_coordinator_core_row_mis_rooting_bug_named_in_brief(self, tmp_path):
        """The EXACT miss the brief names: a `coordinator_core` row's
        staging tree holds `data_root.py` at its own root. Probing it as if
        it were a genuine repo root (rel_root="", the pre-fix call shape)
        finds NOTHING -- decorative on the one row that matters. Probing it
        with `rel_root="coordinator_core"` finds exactly the one seed
        module the brief says is expected."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "data_root.py").write_text(
            "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
        )

        pre_fix_modules, _, pre_fix_resolved = publish._function_gate_modules_and_search_paths_for_repo_root(
            staging_dir
        )
        assert pre_fix_modules == [], "the un-rooted probe must miss (pins the bug the brief names)"
        assert pre_fix_resolved == frozenset()

        modules, search_paths, resolved = publish._function_gate_modules_and_search_paths_for_repo_root(
            staging_dir, "coordinator_core"
        )
        assert modules == ["data_root"], (
            "the dotted seed name must be rewritten to match the STAGED file location "
            "(no coordinator_core/ package directory exists inside the staging tree)"
        )
        assert resolved == {"coordinator_core/data_root.py"}
        assert search_paths == [""]

    def test_bare_seed_entries_out_of_scope_for_coordinator_core_rel_root(self, tmp_path):
        """AC4's own example: the coordinator_core row must never pick up
        the two lib-based entries even if (implausibly) files of those
        names existed at its staging root -- they are out of SCOPE by
        rel_path prefix, not merely absent."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "coordinator_registry.py").write_text("x = 1\n", encoding="utf-8")
        (staging_dir / "coordinator_data_root.py").write_text("x = 1\n", encoding="utf-8")

        modules, _, resolved = publish._function_gate_modules_and_search_paths_for_repo_root(
            staging_dir, "coordinator_core"
        )
        assert modules == []
        assert resolved == frozenset()

    def test_bin_row_prefix_out_of_scope_for_lib_entries(self, tmp_path):
        """The `bin` row's real `dest_subdir` is `bin` (§ `setup/publish-
        targets.portable`), not `coordinator/bin` -- so neither lib-based
        seed entry (rooted at `coordinator/bin/lib/...`) is even in scope,
        matching the brief's "four of six live rows... legitimately contain
        no seed module" for this row specifically."""
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "cli-entry").write_text("print('hi')\n", encoding="utf-8")

        modules, _, resolved = publish._function_gate_modules_and_search_paths_for_repo_root(staging_dir, "bin")
        assert modules == []
        assert resolved == frozenset()
        assert publish._function_gate_expected_seed_rel_paths_for_rel_root("bin") == frozenset()

    def test_coordinator_bin_row_keeps_bare_import_shape(self, tmp_path):
        """A row whose `dest_subdir` genuinely is `coordinator/bin`
        (`_dest_prefix_for` == "coordinator/bin") DOES bring the two
        lib-based (bare-import) entries into scope, staged one level down
        (`lib/coordinator_registry.py`) -- their module NAMES stay bare
        (`coordinator_registry`), only their SEARCH DIR shifts to the
        staged-relative `lib`, unlike the dotted `coordinator_core.data_root`
        entry which needs its name rewritten too."""
        staging_dir = tmp_path / "staging"
        lib_dir = staging_dir / "lib"
        lib_dir.mkdir(parents=True)
        (lib_dir / "coordinator_registry.py").write_text("x = 1\n", encoding="utf-8")

        modules, search_paths, resolved = publish._function_gate_modules_and_search_paths_for_repo_root(
            staging_dir, "coordinator/bin"
        )
        assert modules == ["coordinator_registry"]
        assert resolved == {"coordinator/bin/lib/coordinator_registry.py"}
        assert "lib" in search_paths


# ---------------------------------------------------------------------------
# dispatch_preswap_function_gate -- direct unit tests, REAL subprocess.
# ---------------------------------------------------------------------------
class TestDispatchPreswapFunctionGate:
    def test_clean_coordinator_core_row_passes(self, tmp_path):
        repo_root = tmp_path / "dest-repo"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "data_root.py").write_text(
            "import os\n\ndef data_root(dir_name):\n    return None\n", encoding="utf-8"
        )
        target = _make_target("claude-klabauter", repo_root, "coordinator_core")

        ok = publish.dispatch_preswap_function_gate(_EngineCtxStub(), target, staging_dir)
        assert ok is True

    def test_broken_coordinator_core_row_fails_hermetically(self, tmp_path, capsys):
        repo_root = tmp_path / "dest-repo"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "data_root.py").write_text(
            "import this_module_does_not_exist_anywhere_c1\n", encoding="utf-8"
        )
        target = _make_target("claude-klabauter", repo_root, "coordinator_core")

        ok = publish.dispatch_preswap_function_gate(_EngineCtxStub(), target, staging_dir)
        assert ok is False
        captured = capsys.readouterr()
        assert "pre-swap function gate FAILED" in captured.err
        assert "data_root" in captured.err

    def test_row_with_no_seed_module_in_scope_is_a_noop_pass(self, tmp_path):
        """AC4's positive control: a row (`docs/reference`-shaped) whose
        `dest_subdir` never brings any seed module into scope must PASS,
        not be treated as a probe failure -- the bare-non-emptiness
        alternative this chunk's brief explicitly forbids."""
        repo_root = tmp_path / "dest-repo"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "some-doc.md").write_text("nothing here\n", encoding="utf-8")
        target = _make_target("claude-klabauter-toplevel-reference", repo_root, "docs/reference")

        ok = publish.dispatch_preswap_function_gate(_EngineCtxStub(), target, staging_dir)
        assert ok is True

    def test_expected_subset_mismatch_is_a_hard_failure(self, tmp_path, capsys):
        """AC4's negative control for the mis-rooting class itself: a
        `coordinator_core` row's staging tree is IN SCOPE for exactly one
        seed module (`coordinator_core/data_root.py`), but that file is
        genuinely absent here -- the shape a mis-rooted probe (or a payload
        that legitimately never shipped it) produces. Must fail on the
        EXPECTED-SUBSET assertion, distinctly from an import failure, since
        `modules` is trivially `[]` here too and a bare non-emptiness check
        would wrongly pass it."""
        repo_root = tmp_path / "dest-repo"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        (staging_dir / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
        target = _make_target("claude-klabauter", repo_root, "coordinator_core")

        ok = publish.dispatch_preswap_function_gate(_EngineCtxStub(), target, staging_dir)
        assert ok is False
        captured = capsys.readouterr()
        assert "pre-swap function gate FAILED" in captured.err
        assert "does not equal the expected subset" in captured.err

    def test_engine_claude_klabauter_none_asserts_narrowed_by_caller(self, tmp_path):
        """Item 1 of the brief: this caller (unlike the end-of-run leg) has
        no `not dry_run` narrowing of its own -- it must assert directly
        rather than silently no-op on a `None` engine."""
        repo_root = tmp_path / "dest-repo"
        staging_dir = tmp_path / "staging"
        staging_dir.mkdir()
        target = _make_target("claude-klabauter", repo_root, "coordinator_core")

        class _NoEngineCtx:
            engine_claude_klabauter = None

        with pytest.raises(AssertionError):
            publish.dispatch_preswap_function_gate(_NoEngineCtx(), target, staging_dir)


# ---------------------------------------------------------------------------
# process_target wiring -- proves the gate is actually called, and called
# BEFORE the swap: a failure must refuse the swap for that row (§ brief
# "call the gate ... immediately before _swap_publish_staging_into_dest;
# a failure refuses the swap for that row").
# ---------------------------------------------------------------------------
class _NoOpEngineClaudeKlabauter(_RealEngineClaudeKlabauter):
    """Real gate callables (so a broken payload genuinely fails the gate
    through the driver path), everything else a harmless no-op so
    `process_target`'s percolate-engine phases never need a real store."""

    def resolve_target(self, store, name):
        return {
            "hooks": [],
            "file_surface": {"include_extensions": ["*.py", "*.md"]},
            "guards": [],
            "inject": [],
        }

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def iter_surface_files(self, root, **kwargs):
        return iter(())


def _wire_process_target_preconditions(monkeypatch, *, setup_dir: Path) -> None:
    """Bypasses every pre-sync gate / percolate phase `process_target`
    dispatches EXCEPT the sync itself and the pre-swap FUNCTION gate under
    test -- this proves the CALL SITE ORDERING (gate before swap), not a
    re-test of the gates/phases those other dispatchers already cover
    elsewhere."""

    def _fake_pre_sync_gates(target, *a, **k):
        return publish.GateResult(
            proceed=True, source_dir=target.source_dir, restricted_tmp_src=None, shadow_roots=()
        )

    monkeypatch.setattr(publish, "run_pre_sync_gates", _fake_pre_sync_gates)
    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", lambda *a, **k: (None, frozenset()))
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
    monkeypatch.setattr(publish, "write_lastsync_marker", lambda *a, **k: None)


class TestPreswapGateWiredBeforeSwap:
    def _run_row(self, tmp_path, monkeypatch, *, source_content: str):
        setup_dir = tmp_path / "percolate-root" / "setup"
        setup_dir.mkdir(parents=True)
        repo_root = tmp_path / "dest-repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        source_dir = tmp_path / "src"
        source_dir.mkdir()
        (source_dir / "data_root.py").write_text(source_content, encoding="utf-8")

        target = publish.ResolvedTarget(
            name="claude-klabauter",
            mode="mirror",
            source_dir=source_dir,
            dest_dir=repo_root / "coordinator_core",
        )

        _wire_process_target_preconditions(monkeypatch, setup_dir=setup_dir)
        publish_sync_module = publish._import_publish_sync(setup_dir)

        totals = publish.RunTotals()
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=_NoOpEngineClaudeKlabauter(), store={})

        swap_calls = []
        real_swap = publish._swap_publish_staging_into_dest

        def _spying_swap(dest_dir, staging_dir):
            swap_calls.append((dest_dir, staging_dir))
            return real_swap(dest_dir, staging_dir)

        monkeypatch.setattr(publish, "_swap_publish_staging_into_dest", _spying_swap)

        publish.process_target(
            target,
            setup_dir,
            totals,
            identity_file_exists=False,
            identity=None,
            dry_run=False,
            round_pinned_shas={},
            engine_ctx=engine_ctx,
            percolate_store_path=setup_dir / "store.yaml",
            publish_sync_module=publish_sync_module,
        )
        return target, swap_calls, totals

    def test_broken_payload_refuses_the_swap(self, tmp_path, monkeypatch, capsys):
        target, swap_calls, totals = self._run_row(
            tmp_path, monkeypatch, source_content="import this_module_does_not_exist_anywhere_c1\n"
        )

        assert swap_calls == [], "the swap must never fire when the pre-swap gate fails"
        # `_ensure_dest_ready` may bootstrap an empty `dest_dir` ahead of the
        # swap (a virgin mirror precondition, unrelated to this gate) -- the
        # INVARIANT under test is that no STAGED CONTENT ever lands there,
        # not that the directory itself is absent.
        assert not (target.dest_dir / "data_root.py").exists(), (
            "staged content must never land in dest_dir on a refused row"
        )
        assert totals.processed == 0
        captured = capsys.readouterr()
        assert "pre-swap function gate FAILED" in captured.err

    def test_clean_payload_swaps_through(self, tmp_path, monkeypatch):
        target, swap_calls, totals = self._run_row(
            tmp_path, monkeypatch, source_content="import os\n\ndef data_root(dir_name):\n    return None\n"
        )

        assert len(swap_calls) == 1, "the swap must fire exactly once when the pre-swap gate passes"
        assert (target.dest_dir / "data_root.py").is_file()
        assert totals.processed == 1
