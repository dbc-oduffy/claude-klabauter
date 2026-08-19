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
    hermetic_gate_env = staticmethod(pct_engine.hermetic_gate_env)


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

    def test_misplaced_future_import_fires(self, tmp_path):
        """The defect `ast.parse` cannot see. A `from __future__` import
        preceded by any other statement is valid to `ast.parse` and a hard
        SyntaxError to `compile()` and to every real import -- so a sweep
        built on `ast.parse` certifies an unimportable mirror as publishable.
        Observed live on 2026-08-14: an annotation sweep put `GENERATES`/
        `MUTATES` above the future import in 14 engine modules and took
        `archive-stamp-cli` down in every consumer repo on the machine, while
        parse-based checks reported the tree clean."""
        (tmp_path / "broken.py").write_text(
            '"""doc."""\n\nGENERATES = []\n\nfrom __future__ import annotations\n',
            encoding="utf-8",
        )

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.ok is False
        assert len(result.failures) == 1
        assert result.failures[0].path == "broken.py"
        assert "__future__" in result.failures[0].message

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

    def test_a_file_merely_named_with_staging_substring_is_not_dropped(self, tmp_path):
        """§ MINOR-5: the staging exclusion is directory-scoped -- a plain
        FILE whose own basename contains "publish-staging-" is shipped
        payload and must still be scanned, unlike the OLD unanchored regex
        (`^\\.?.*publish-staging-.*$`, tested against every path segment
        including the filename) which silently dropped it."""
        (tmp_path / "my-publish-staging-helper.py").write_text(
            "claude-klabauter = 1\n", encoding="utf-8"
        )

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.scanned == 1
        assert result.ok is False
        assert result.failures[0].path == "my-publish-staging-helper.py"

    def test_extensionless_shebang_cli_is_scanned(self, tmp_path):
        """§ BLOCKER-1: `coordinator/bin/` ships extensionless Python CLIs
        deliberately (the publish surface admits them via `surface.
        sniff_shebang`) -- this is the exact fixture from the finding
        (`coordinator/bin/cross-repo-memo.py` containing a hyphenated,
        scrub-broken identifier). Against the OLD `rglob("*.py")` admission
        this reported `ok=True, scanned=0` -- the fixture must fail loudly
        instead."""
        bin_dir = tmp_path / "coordinator" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "cross-repo-memo.py").write_text(
            "#!/usr/bin/env python3\nclaude-klabauter = 1\n", encoding="utf-8"
        )
        (bin_dir / "ok.py").write_text("x = 1\n", encoding="utf-8")

        result = pct_engine.run_parse_sweep(tmp_path)
        assert result.scanned == 2
        assert result.ok is False
        assert {f.path for f in result.failures} == {"coordinator/bin/cross-repo-memo.py"}

    def test_extensionless_file_without_a_python_shebang_is_not_scanned(self, tmp_path):
        """A non-Python extensionless file (e.g. a bash script) must not be
        admitted just because it lacks a suffix."""
        (tmp_path / "run-something").write_text("#!/bin/bash\necho hi\n", encoding="utf-8")
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


# ---------------------------------------------------------------------------
# ENTRYPOINT gate (§ chunk C2, "the FUNCTION gate runs what it ships, instead
# of importing three module names") -- run_entrypoint_gate EXECUTES each
# shipped bare entrypoint under a stripped environment, instead of import-
# reachability alone. Every test below NAMES its home shape (§ chunk C2
# brief item (a)/(b): "an unnamed home shape makes the result
# unattributable") by running through `mktcache_gate_env` explicitly.
# ---------------------------------------------------------------------------

_HELLO_SHEBANG = "#!/usr/bin/env python3\n"


def _write_bare_cli(path, body: str) -> None:
    path.write_text(_HELLO_SHEBANG + body, encoding="utf-8")


class TestEnumerateGateEntrypoints:
    def test_bare_top_level_files_are_admitted_by_construction_root(self, tmp_path):
        bin_dir = tmp_path / "coordinator" / "bin"
        bin_dir.mkdir(parents=True)
        _write_bare_cli(bin_dir / "some-cli", "pass\n")
        (bin_dir / "helper.py").write_text("x = 1\n", encoding="utf-8")
        (bin_dir / ".hidden").write_text("data\n", encoding="utf-8")
        nested = bin_dir / "lib"
        nested.mkdir()
        _write_bare_cli(nested / "nested-cli", "pass\n")

        names = pct_engine.enumerate_gate_entrypoints(tmp_path)
        assert "coordinator/bin/some-cli" in names
        assert not any(n.endswith("helper.py") for n in names)
        assert not any(".hidden" in n for n in names)
        assert not any("nested-cli" in n for n in names)

    def test_mixed_root_requires_main_guard(self, tmp_path):
        lib_dir = tmp_path / "coordinator" / "lib"
        lib_dir.mkdir(parents=True)
        _write_bare_cli(lib_dir / "real-entrypoint", "if __name__ == '__main__':\n    pass\n")
        _write_bare_cli(lib_dir / "library-helper", "X = 1\n")

        names = pct_engine.enumerate_gate_entrypoints(tmp_path)
        assert "coordinator/lib/real-entrypoint" in names
        assert "coordinator/lib/library-helper" not in names

    def test_missing_scan_root_contributes_nothing(self, tmp_path):
        # No coordinator/bin, coordinator/lib, etc under tmp_path at all --
        # a --target-scoped publish row that never wrote any of them.
        assert pct_engine.enumerate_gate_entrypoints(tmp_path) == ()


class TestMktcacheGateEnv:
    def test_yields_the_mktcache_shape_under_home(self, tmp_path):
        with pct_engine.mktcache_gate_env(version="1.2.3") as env:
            home = Path(env["HOME"])
            assert env["USERPROFILE"] == str(home)
            assert env["CLAUDE_HOME"] == str(home)
            cache_dir = home / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "1.2.3"
            assert cache_dir.is_dir()
            captured_home = home
        # Cleaned up on exit, mirroring hermetic_gate_env's own guarantee.
        assert not captured_home.exists()

    def test_seed_dir_is_copied_into_the_resolved_version_directory(self, tmp_path):
        seed = tmp_path / "seed"
        (seed / "state").mkdir(parents=True)
        (seed / "state" / "marker.txt").write_text("x", encoding="utf-8")

        with pct_engine.mktcache_gate_env(seed_dir=seed, version="9.9.9") as env:
            cache_dir = (
                Path(env["HOME"]) / ".claude" / "plugins" / "cache" / "coordinator-claude" / "coordinator" / "9.9.9"
            )
            assert (cache_dir / "state" / "marker.txt").read_text(encoding="utf-8") == "x"


class TestRunEntrypointGate:
    def _payload(self, tmp_path):
        bin_dir = tmp_path / "coordinator" / "bin"
        bin_dir.mkdir(parents=True)
        return bin_dir

    def test_clean_payload_passes_under_mktcache(self, tmp_path):
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "clean-cli", "import sys\nsys.exit(0)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)
        assert entrypoints == ("coordinator/bin/clean-cli",)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(tmp_path, entrypoints, env=env)

        assert result.ok is True
        assert result.home_shape == pct_engine.GATE_HOME_SHAPE_MKTCACHE
        assert result.started == ("coordinator/bin/clean-cli",)
        assert result.failures == ()

    def test_broken_entrypoint_turns_the_gate_red_under_mktcache(self, tmp_path):
        """§ chunk C2 brief item (b): "IT MUST BE ABLE TO GO RED" -- a
        deliberately broken entrypoint in a scratch payload built under the
        SAME mktcache shape (a996) must fail this gate, not merely a bare
        empty-HOME advisory run."""
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "broken-cli", "import sys\nsys.stderr.write('boom')\nsys.exit(1)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(tmp_path, entrypoints, env=env)

        assert result.ok is False
        assert result.home_shape == pct_engine.GATE_HOME_SHAPE_MKTCACHE
        assert len(result.failures) == 1
        assert result.failures[0].entrypoint == "coordinator/bin/broken-cli"
        assert result.failures[0].returncode == 1

    def test_usage_nonzero_entry_is_not_a_failure(self, tmp_path):
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "picky-cli", "import sys\nsys.exit(2)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(
                tmp_path,
                entrypoints,
                env=env,
                usage_nonzero=frozenset({"coordinator/bin/picky-cli"}),
                waivers={},
            )

        assert result.ok is True
        assert result.usage_nonzero == ("coordinator/bin/picky-cli",)
        assert result.failures == ()

    def test_usage_nonzero_entry_that_starts_clean_is_a_failure(self, tmp_path):
        """Self-draining (§ chunk C2 brief): a listed entry that now exits 0
        must be reported as a failure, forcing its removal from the list --
        never silently folded into `started`."""
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "fixed-cli", "import sys\nsys.exit(0)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(
                tmp_path,
                entrypoints,
                env=env,
                usage_nonzero=frozenset({"coordinator/bin/fixed-cli"}),
                waivers={},
            )

        assert result.ok is False
        assert len(result.failures) == 1
        assert "exited 0" in result.failures[0].message
        assert "_USAGE_NONZERO_ENTRYPOINTS" in result.failures[0].message

    def test_waived_entry_is_not_a_failure_but_clean_waiver_is(self, tmp_path):
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "waived-cli", "import sys\nsys.exit(1)\n")
        _write_bare_cli(bin_dir / "resolved-cli", "import sys\nsys.exit(0)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)
        waivers = {
            "coordinator/bin/waived-cli": "synthetic waiver, still broken",
            "coordinator/bin/resolved-cli": "synthetic waiver, already fixed -- must self-drain",
        }

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(
                tmp_path, entrypoints, env=env, usage_nonzero=frozenset(), waivers=waivers
            )

        assert result.ok is False
        assert result.waived == ("coordinator/bin/waived-cli",)
        assert len(result.failures) == 1
        assert result.failures[0].entrypoint == "coordinator/bin/resolved-cli"
        assert "waiver" in result.failures[0].message

    def test_aggregate_budget_fails_entrypoints_not_yet_dispatched(self, tmp_path):
        bin_dir = self._payload(tmp_path)
        _write_bare_cli(bin_dir / "slow-cli", "import time\ntime.sleep(5)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(
                tmp_path, entrypoints, env=env, aggregate_budget=0.0
            )

        assert result.ok is False
        assert result.failures[0].entrypoint == "coordinator/bin/slow-cli"
        assert "budget" in result.failures[0].message

    def test_max_workers_still_scans_every_entrypoint(self, tmp_path):
        bin_dir = self._payload(tmp_path)
        for i in range(4):
            _write_bare_cli(bin_dir / f"cli-{i}", "import sys\nsys.exit(0)\n")
        entrypoints = pct_engine.enumerate_gate_entrypoints(tmp_path)

        with pct_engine.mktcache_gate_env() as env:
            result = pct_engine.run_entrypoint_gate(tmp_path, entrypoints, env=env, max_workers=4)

        assert result.ok is True
        assert result.scanned == 4
        assert sorted(result.started) == sorted(entrypoints)


class TestEntrypointGateDataLists:
    def test_usage_nonzero_membership_is_pinned(self):
        """Mechanical bar (§ chunk C2 brief): the constant cannot grow
        without this test being edited in the same review."""
        assert pct_engine._USAGE_NONZERO_ENTRYPOINTS == frozenset(
            {
                "coordinator/bin/backlog-grind-assemble.py",
                "coordinator/bin/consolidate-assemble.py",
                "coordinator/bin/coordinator-fold-execution-record.py",
                "coordinator/bin/coordinator-safe-name.py",
                "coordinator/bin/handoff-carry-gate.py",
                "coordinator/bin/learn-lessons-reconcile-candidates.py",
                "coordinator/bin/merge-assemble.py",
                "coordinator/bin/review-assemble.py",
                "coordinator/bin/review-exec-auth-stamp.py",
                "coordinator/bin/roadmap-number-stubs.py",
                "coordinator/bin/schema-drift-gate.py",
                "coordinator/bin/sizing-assemble.py",
                "coordinator/bin/staff-session-assemble.py",
                "coordinator/bin/workstream-complete-assemble.py",
                # C6 sweep, 2026-08-10 — observed starting then rejecting
                # --help in their own parsers.
                "coordinator/bin/coordinator-safe-commit.py",
                "coordinator/bin/handoff-gate-aging.py",
                "coordinator/bin/pickup-assemble.py",
                "coordinator/bin/scoped-git-commit",
                "coordinator/scripts/first-run.py",
                "coordinator/scripts/normalize-env.py",
                # klabauter-mirror gate closure, 2026-08-10 (11/12 -> 12/12):
                # unmasked once the entrypoint gate's synthetic manifest
                # fixture made its "snippets" data dir resolvable.
                "coordinator/bin/snippet-registry.py",
                # klabauter-mirror gate closure, 2026-08-10 (12/12 -> next):
                # own-parser usage rejection, no plugin-root dependency at
                # all -- see `_USAGE_NONZERO_ENTRYPOINTS`'s own comment.
                "coordinator/bin/orient-assemble.py",
                # four-coordinator-bin-entrypoints sweep (state/bug-backlog,
                # filed the day this fix landed): both reach their own
                # parser and print a recognizable own-parser usage complaint,
                # not a root-resolution/import failure -- re-pinned here,
                # entrant not code drift.
                "coordinator/bin/chunk-commits",
                "coordinator/bin/with-suite-mutex",
                # coordinator-claude mirror gate, 2026-08-16 (12/13 -> 13/13):
                # newly reaching the mirror via the multi-source `bin` entry.
                # Verified in-tree, not taken on the reporting sibling's word:
                # no args -> rc=2 with its own "expected at least one path."
                # + usage block, which is its documented usage-error code, not
                # the rc=3 its trampoline reserves for transport failure.
                "coordinator/bin/claim-neighbours",
            }
        )

    def test_waiver_list_membership_is_pinned(self):
        # Split by PROVENANCE, not merged into one flat set: the first four are
        # sourced from hermetic-ac-reverify.md Finding F3; the last two were
        # measured in this tree on 2026-08-10 (rc-127, same "resolver not
        # installed" cause the machine-local entry already accepts as waivable).
        # Left unwaived they turn the first publish red for an already-accepted
        # cause. Keeping the two groups distinct here means a later reader can
        # tell which entries carry an external source and which carry a
        # measurement, without reading the constant's comment.
        # detect-initiative-candidates.py was here and was REMOVED by the C6
        # sweep (2026-08-10) after it started clean -- the self-draining rule
        # firing, not a regression. coordinator-lesson-promote.py was ALSO here
        # and was REMOVED by the klabauter-mirror gate closure (2026-08-10)
        # for the same reason: the entrypoint gate's synthetic manifest
        # fixture made the native seam resolvable, it started clean, and the
        # self-draining rule demanded its removal. Two sourced entries remain.
        sourced_f3 = {
            "coordinator/bin/claude-doe.py",
            "coordinator/bin/machine-local",
        }
        measured_2026_08_10 = {
            "coordinator/bin/claude-home",
            "coordinator/bin/coordinator-settings-home",
        }
        # Measured 2026-08-14 against a PUBLISHED coordinator-claude payload
        # (not this source tree) -- `_canonical_gate_entrypoint_id`'s layout
        # canonicalisation makes these keys match there for the first time.
        # All rc=1 for the same root cause: engine-dependent distribution via
        # cc_invoke env/registry rungs the gate's hermetic env strips.
        # Re-pinned here, entrants not code drift.
        #
        # Five as authored, one now. `coordinator/bin/coordinator-ensure-
        # hooks-fleet` drained 2026-08-16 -- it started CLEAN against a
        # published payload and the gate fails closed on a waiver that no
        # longer fires, so the entry itself blocked the round (doe-claude-em,
        # claude-klabauter `f0009090d`). `coordinator-queue-close`, `plan-tasks-resolve`
        # and `plan-tasks-stamp` drained 2026-08-19 the same way, in the round
        # that completed the `claude-klabauter-coordinator-bin` allowlist:
        # that allowlist had drifted to 708 of 948 tracked entries, so the
        # `coordinator/bin` siblings these three resolve through were never in
        # the published payload. Shipping the full set let them start cleanly.
        # Same self-draining rule as the removals noted above, not a
        # regression. `with-suite-mutex` alone remains, and it is inert (see
        # its note in the constant).
        measured_2026_08_14 = {
            "coordinator/bin/with-suite-mutex",
        }
        assert set(pct_engine._ENTRYPOINT_GATE_WAIVERS) == (
            sourced_f3 | measured_2026_08_10 | measured_2026_08_14
        )
        for name, reason in pct_engine._ENTRYPOINT_GATE_WAIVERS.items():
            assert isinstance(reason, str) and len(reason.split()) >= 5, (
                f"_ENTRYPOINT_GATE_WAIVERS[{name!r}] needs a real reason, got {reason!r}"
            )
