"""Parity tests for coordinator_core.bash_guards.commit_tripwires -- the
in-process ports of check_validate_commit's Checks 9-11 (schema-version-bump,
bin/sh polyglot shebang, machine-path-leak), which previously delegated to
bin/*.sh via subprocess-by-filename (see the module's own docstring for the
defect this port removes: a DoE-side rename silently disabled the guard).

Oracles: Port of: check-schema-version-bump.sh (DoE 51851112, 2026-07-21),
check-bin-sh-polyglot.sh (DoE 51851112, 2026-07-21); coordinator/bin/check-machine-path-leak.py
(still alive, already renamed pre-port).

Each check gets three cases: fires (the tripwire condition is met), clean
(nothing to flag), and guard-cannot-run (the plumbing that locates the
guard's inputs comes up empty -- must fail open, not raise or deny).
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable, List, Set

import pytest

from coordinator_core.bash_guards import commit_tripwires

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _init_repo(tmp_path: Path) -> str:
    root = str(tmp_path)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "init")
    return root


# ---------------------------------------------------------------------------
# Check 9 -- check_schema_version_bump
# ---------------------------------------------------------------------------


class TestCheckSchemaVersionBump:
    def test_canonical_changed_without_version_bump_fires(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (tmp_path / "canonical-structure.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / "coordinator-schema-version").write_text("3\n", encoding="utf-8")
        _git(root, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(root, "commit", "-q", "-m", "seed")

        (tmp_path / "canonical-structure.yaml").write_text("a: 2\n", encoding="utf-8")
        _git(root, "add", "canonical-structure.yaml")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        result = commit_tripwires.check_schema_version_bump()
        assert result is not None
        assert "VIOLATION" in result
        assert "coordinator-schema-version" in result

    def test_canonical_changed_with_version_bump_clean(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (tmp_path / "canonical-structure.yaml").write_text("a: 1\n", encoding="utf-8")
        (tmp_path / "coordinator-schema-version").write_text("3\n", encoding="utf-8")
        _git(root, "add", "canonical-structure.yaml", "coordinator-schema-version")
        _git(root, "commit", "-q", "-m", "seed")

        (tmp_path / "canonical-structure.yaml").write_text("a: 2\n", encoding="utf-8")
        (tmp_path / "coordinator-schema-version").write_text("4\n", encoding="utf-8")
        _git(root, "add", "canonical-structure.yaml", "coordinator-schema-version")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_schema_version_bump() is None

    def test_canonical_not_staged_clean(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (tmp_path / "other.txt").write_text("x\n", encoding="utf-8")
        _git(root, "add", "other.txt")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_schema_version_bump() is None

    def test_doe_root_unresolvable_fails_open(self, monkeypatch):
        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: None)
        assert commit_tripwires.check_schema_version_bump() is None


# ---------------------------------------------------------------------------
# Check 10 -- check_bin_sh_polyglot
# ---------------------------------------------------------------------------

_TRAMPOLINE = commit_tripwires._TRAMPOLINE


def _write_polyglot(path: Path, *, shebang: str) -> None:
    path.write_text(
        "{}\n"
        '{}\n'
        "import sys\n".format(shebang, _TRAMPOLINE),
        encoding="utf-8",
    )


class TestCheckBinShPolyglot:
    def test_missing_sh_shebang_fires(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_polyglot(bin_dir / "some-tool", shebang="#!/usr/bin/env python3")
        _git(root, "add", "bin/some-tool")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        result = commit_tripwires.check_bin_sh_polyglot()
        assert result is not None
        assert "BIN-SH-POLYGLOT-INVARIANT" in result
        assert "some-tool" in result

    def test_correct_sh_shebang_clean(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_polyglot(bin_dir / "some-tool", shebang="#!/bin/sh")
        _git(root, "add", "bin/some-tool")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_bin_sh_polyglot() is None

    def test_non_polyglot_file_no_trampoline_clean(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "plain-tool.py").write_text("#!/usr/bin/env python3\nprint('hi')\n", encoding="utf-8")
        _git(root, "add", "bin/plain-tool.py")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_bin_sh_polyglot() is None

    def test_self_file_skipped_even_when_offending(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_polyglot(bin_dir / "check-bin-sh-polyglot.py", shebang="#!/usr/bin/env python3")
        _git(root, "add", "bin/check-bin-sh-polyglot.py")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_bin_sh_polyglot() is None

    def test_sibling_guard_skipped_even_when_offending(self, tmp_path, monkeypatch):
        """The repo-wide sibling guard (check-sh-suffix-polyglot.py) quotes the
        trampoline in its module docstring, inside the 20-line header window,
        under a `#!/usr/bin/env python3` shebang -- exactly the shape this check
        flags. It is not a polyglot CLI, so it must be skipped, not reported.

        Regression net for a real false-positive: _SELF_SKIP_BASENAMES once
        covered only this guard's own two names, so the sibling fired on every
        commit that touched it. Drop the sibling's entry and this goes red."""
        root = _init_repo(tmp_path)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_polyglot(bin_dir / "check-sh-suffix-polyglot.py", shebang="#!/usr/bin/env python3")
        _git(root, "add", "bin/check-sh-suffix-polyglot.py")

        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)

        assert commit_tripwires.check_bin_sh_polyglot() is None

    def test_doe_root_unresolvable_fails_open(self, monkeypatch):
        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: None)
        assert commit_tripwires.check_bin_sh_polyglot() is None

    def test_no_bin_dir_fails_open(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        monkeypatch.setattr(commit_tripwires, "_resolve_doe_coordinator_root", lambda: root)
        assert commit_tripwires.check_bin_sh_polyglot() is None


# ---------------------------------------------------------------------------
# Cross-surface agreement for the guard-self-skip exemption
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module_from_path(mod_name: str, path: Path) -> ModuleType:
    """Import a hyphen-named CLI by file path. `check-bin-sh-polyglot.py` is
    not a legal dotted identifier, so this is the only route in; the module
    is side-effect-free at import."""
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    assert spec is not None and spec.loader is not None, "cannot load {}".format(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_invariant_module(monkeypatch) -> ModuleType:
    """Import `test_no_bin_polyglot_invariant` by its real dotted name.

    `coordinator/bin/tests/` carries an `__init__.py` (a real package, not a
    namespace package) and the module does `from ._polyglot_git_scan import
    ...` -- a relative import that requires `__package__` to be set to that
    package. Loading it via `spec_from_file_location` with a synthetic module
    name (the prior approach here) execs it with no package context at all,
    so the relative import raises `ImportError: attempted relative import
    with no known parent package` -- this broke the day the invariant suite's
    git-scan helper was extracted into the sibling `_polyglot_git_scan.py`
    module and the direct inline code became a relative import. A plain
    dotted `importlib.import_module` goes through the normal package
    machinery instead, so the relative import resolves exactly as it does
    under a normal pytest collection run of that file.

    Takes the caller's `monkeypatch` fixture and prepends via
    `monkeypatch.syspath_prepend` rather than a raw `sys.path.insert` --
    this same test file's own predecessor bug (the `ImportError` this
    function fixes) is a direct instance of import-machinery fragility, so
    a second unrestored global-state mutation here is worth avoiding;
    pytest restores `sys.path` to its pre-test state on teardown.
    """
    monkeypatch.syspath_prepend(str(_REPO_ROOT))
    return importlib.import_module("coordinator.bin.tests.test_no_bin_polyglot_invariant")


def _live_basenames(entries: Iterable[str]) -> Set[str]:
    """Normalize an exemption set to the basenames of the entries that name a
    file actually on disk. Absorbs the two keying conventions in play (bare
    basename, implicitly under coordinator/bin/; and repo-relative path) so the
    three sets become comparable, and drops entries naming files that no longer
    exist so a deliberately-retained dead name is not read as drift."""
    live: Set[str] = set()
    for entry in entries:
        rel = entry if "/" in entry else "coordinator/bin/" + entry
        if (_REPO_ROOT / rel).is_file():
            live.add(Path(rel).name)
    return live


class TestLoadInvariantModuleRestoresSysPath:
    def test_sys_path_mutation_is_undone_after_use(self):
        """`_load_invariant_module` used to `sys.path.insert` with no
        teardown -- a raw, permanent global-state mutation in the same test
        file whose own predecessor bug was import-machinery fragility. Using
        `monkeypatch.syspath_prepend` means the entry is gone once the
        monkeypatch context exits, not just "probably still there but
        harmless"."""
        import _pytest.monkeypatch

        before = list(sys.path)
        with _pytest.monkeypatch.MonkeyPatch.context() as mp:
            module = _load_invariant_module(mp)
            assert module is not None
            assert str(_REPO_ROOT) in sys.path
        assert sys.path == before


class TestGuardSelfSkipCrossSetAgreement:
    """The two guards that quote the trampoline as data are exempted
    independently on three surfaces -- this module's `_SELF_SKIP_BASENAMES`,
    the CLI's `_GUARD_SELF_SKIP_BASENAMES`, and the pytest invariant's
    `EXCLUDED_TRAMPOLINE_DOC_FILES`. Nothing links them, so an entry added or
    dropped on one surface leaves the others reading green while that surface
    fires on the same file.

    The checkable invariant is agreement on the LIVE-FILE SUBSET, not strict
    set equality: the sets use different keying, and only `_SELF_SKIP_BASENAMES`
    retains the pre-rename `check-bin-sh-polyglot.sh`, which is inert wherever
    the rename has landed. Both comments at the set definitions state this
    invariant; this class is what makes it more than a comment.
    """

    def _sets(self, monkeypatch):
        cli = _load_module_from_path(
            "_xset_check_bin_sh_polyglot",
            _REPO_ROOT / "coordinator" / "bin" / "check-bin-sh-polyglot.py",
        )
        invariant = _load_invariant_module(monkeypatch)
        return (
            cli._GUARD_SELF_SKIP_BASENAMES,
            invariant.EXCLUDED_TRAMPOLINE_DOC_FILES,
            commit_tripwires._SELF_SKIP_BASENAMES,
        )

    def test_three_exemption_sets_agree_on_live_file_subset(self, monkeypatch):
        cli_set, invariant_set, tripwire_set = self._sets(monkeypatch)

        cli_live = _live_basenames(cli_set)
        invariant_live = _live_basenames(invariant_set)
        tripwire_live = _live_basenames(tripwire_set)

        assert cli_live, (
            "the CLI exemption set resolved to zero live files -- repo-root "
            "resolution is almost certainly wrong ({}), not a genuinely empty "
            "set".format(_REPO_ROOT)
        )
        assert cli_live == invariant_live == tripwire_live, (
            "guard-self-skip exemption sets disagree on their live-file subset:\n"
            "  coordinator/bin/check-bin-sh-polyglot.py::_GUARD_SELF_SKIP_BASENAMES = {}\n"
            "  coordinator/bin/tests/test_no_bin_polyglot_invariant.py::"
            "EXCLUDED_TRAMPOLINE_DOC_FILES = {}\n"
            "  coordinator_core/bash_guards/commit_tripwires.py::_SELF_SKIP_BASENAMES = {}\n"
            "  Fix: add the missing entry to whichever surface lacks it -- a "
            "file exempt on one surface and not another means one guard fires "
            "on it while the others read green.".format(
                sorted(cli_live), sorted(invariant_live), sorted(tripwire_live)
            )
        )

    def test_entries_absent_from_the_live_subset_are_dead_on_disk(self, monkeypatch):
        """The live-subset filter is a licence to retain a dead name, not a
        licence to hide a live disagreement: every entry the filter drops must
        genuinely name no file on disk."""
        for label, entries in zip(
            ("_GUARD_SELF_SKIP_BASENAMES", "EXCLUDED_TRAMPOLINE_DOC_FILES", "_SELF_SKIP_BASENAMES"),
            self._sets(monkeypatch),
        ):
            live = _live_basenames(entries)
            for entry in entries:
                rel = entry if "/" in entry else "coordinator/bin/" + entry
                if Path(rel).name not in live:
                    assert not (_REPO_ROOT / rel).exists(), (
                        "{}: entry {!r} was filtered out of the live subset but "
                        "exists on disk".format(label, entry)
                    )


# ---------------------------------------------------------------------------
# Check 11 -- check_machine_path_leak
# ---------------------------------------------------------------------------


class TestCheckMachinePathLeak:
    def test_machine_abs_path_leaf_fires(self, tmp_path):
        root = _init_repo(tmp_path)
        settings = {"mcpServers": {"foo": {"cwd": "/Users/alice/repo"}}}
        import json

        (tmp_path / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        _git(root, "add", "settings.json")

        result = commit_tripwires.check_machine_path_leak("settings.json", root)
        assert result is not None
        assert "VIOLATION" in result
        assert "/Users/alice/repo" in result

    def test_clean_settings_no_machine_path(self, tmp_path):
        root = _init_repo(tmp_path)
        import json

        (tmp_path / "settings.json").write_text(
            json.dumps({"theme": "dark"}), encoding="utf-8"
        )
        _git(root, "add", "settings.json")

        assert commit_tripwires.check_machine_path_leak("settings.json", root) is None

    def test_unreadable_settings_fails_open(self, tmp_path):
        root = _init_repo(tmp_path)
        # settings.json neither on disk nor in the git index -- e.g. a stale
        # staged-file name from a rename/race.
        assert commit_tripwires.check_machine_path_leak("settings.json", root) is None

    def test_malformed_json_reports_parse_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "settings.json").write_text("{not valid json", encoding="utf-8")
        _git(root, "add", "settings.json")

        result = commit_tripwires.check_machine_path_leak("settings.json", root)
        assert result is not None
        assert "ERROR" in result


# ---------------------------------------------------------------------------
# Check 12 -- check_registration_quad_completeness
#
# Spec: docs/plans/2026-07-25-registration-quad-completeness-gate.md § C4
# ---------------------------------------------------------------------------


def _stage_op_file(root: str, rel_path: str, op_key: str) -> None:
    """Stage a fake op-registering .py file under coordinator_core/ whose
    content contains a real ``@register_op("<op_key>")`` decorator call --
    the exact shape stage 1's content gate and the AC17 extraction regex key
    on."""
    abs_path = Path(root) / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(
        '@register_op("{}")\n'
        "def handler(params):\n"
        "    return {{}}\n".format(op_key),
        encoding="utf-8",
    )
    _git(root, "add", rel_path)


class TestCheckRegistrationQuadCompleteness:
    def test_incomplete_registration_fixture_fires(self, tmp_path, monkeypatch):
        from coordinator_core.authz.registration_quad import QuadViolation

        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_new_op.py", "fake.new_op")

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        import coordinator_core.authz.classification as classification_module
        import coordinator_core.op_scopes as op_scopes_module
        import coordinator_core.ops._registry_map as registry_map_module
        import coordinator_core.authz.registration_quad as registration_quad_module

        monkeypatch.setattr(classification_module, "OP_CLASSIFICATION", {})
        monkeypatch.setattr(op_scopes_module, "_OP_KEY_SCOPE", {})
        monkeypatch.setattr(registry_map_module, "OP_MODULE_MAP", {})

        planted = [
            QuadViolation(
                op_key="fake.new_op",
                surfaces_present=(),
                surfaces_missing=("OP_CLASSIFICATION", "_OP_KEY_SCOPE", "OP_MODULE_MAP"),
                missing_surface_files=(
                    ("OP_CLASSIFICATION", "coordinator_core/authz/classification.py"),
                    ("_OP_KEY_SCOPE", "coordinator_core/op_scopes.py"),
                    ("OP_MODULE_MAP", "coordinator_core/ops/_registry_map.py"),
                ),
            ),
            # A second, unrelated violation NOT extracted from this commit's
            # staged diff -- must NOT appear in the report (AC17: the gate
            # judges the commit, not the worktree).
            QuadViolation(
                op_key="unrelated.worktree_op",
                surfaces_present=(),
                surfaces_missing=("OP_CLASSIFICATION",),
                missing_surface_files=(
                    ("OP_CLASSIFICATION", "coordinator_core/authz/classification.py"),
                ),
            ),
        ]
        monkeypatch.setattr(registration_quad_module, "check_registration_quad", lambda: planted)
        # Review: code-reviewer (Finding 5) -- isolate against the real 65-entry
        # production baseline, matching TestRegistrationQuadBaselinePruning's own
        # explicit-injection pattern; this test's fixture keys ("fake.new_op",
        # "unrelated.worktree_op") happen never to collide with real baseline
        # entries today, but that was an implicit dependency on production data.
        monkeypatch.setattr(registration_quad_module, "_KNOWN_UNCLASSIFIED_OPS_DEBT", frozenset())

        result = commit_tripwires.check_registration_quad_completeness(root)
        assert result is not None
        assert result.startswith("VIOLATION: REGISTRATION-QUAD-INVARIANT")
        assert "fake.new_op" in result
        assert "unrelated.worktree_op" not in result

    def test_complete_registration_stays_silent(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_complete_op.py", "fake.complete_op")

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        import coordinator_core.authz.classification as classification_module
        import coordinator_core.op_scopes as op_scopes_module
        import coordinator_core.ops._registry_map as registry_map_module

        monkeypatch.setattr(classification_module, "OP_CLASSIFICATION", {"fake.complete_op": "read"})
        monkeypatch.setattr(op_scopes_module, "_OP_KEY_SCOPE", {"fake.complete_op": "none"})
        monkeypatch.setattr(registry_map_module, "OP_MODULE_MAP", {"fake.complete_op": "x.y"})

        assert commit_tripwires.check_registration_quad_completeness(root) is None

    def test_no_registration_files_staged_returns_before_expensive_walk(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("unrelated change\n", encoding="utf-8")
        _git(root, "add", "README.md")

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        import coordinator_core.authz.registration_quad as registration_quad_module

        def _boom():
            raise AssertionError("expensive full-tree walk must not run (AC11)")

        monkeypatch.setattr(registration_quad_module, "check_registration_quad", _boom)

        assert commit_tripwires.check_registration_quad_completeness(root) is None

    def test_all_staged_keys_already_complete_returns_before_expensive_walk(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_already_ok_op.py", "fake.already_ok")

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        import coordinator_core.authz.classification as classification_module
        import coordinator_core.op_scopes as op_scopes_module
        import coordinator_core.ops._registry_map as registry_map_module
        import coordinator_core.authz.registration_quad as registration_quad_module

        monkeypatch.setattr(classification_module, "OP_CLASSIFICATION", {"fake.already_ok": "read"})
        monkeypatch.setattr(op_scopes_module, "_OP_KEY_SCOPE", {"fake.already_ok": "none"})
        monkeypatch.setattr(registry_map_module, "OP_MODULE_MAP", {"fake.already_ok": "x.y"})
        # Review: code-reviewer (Finding 1) -- stage 1.5's fast path now also
        # requires the op's OP_MODULE_MAP module path ("x.y") to be present in
        # the live _EAGER_OP_MODULES set; without this the op is no longer
        # "already complete" on all five surfaces and the fast path correctly
        # falls through to the full walk this test asserts must NOT happen.
        import coordinator_core.ops as ops_module

        monkeypatch.setattr(ops_module, "_EAGER_OP_MODULES", [("x.y", "test fixture")])

        def _boom():
            raise AssertionError("middle tier should have short-circuited before the walk (AC19)")

        monkeypatch.setattr(registration_quad_module, "check_registration_quad", _boom)

        assert commit_tripwires.check_registration_quad_completeness(root) is None

    # C5 (2026-08-22-the-import-path-costs-nothing) -- premise correction: this
    # chunk originally expected retiring the discovery apparatus to remove
    # _EAGER_OP_MODULES, making commit_tripwires.py:658's import fail and the
    # fast path fall through to check_registration_quad() every time. C6 keeps
    # _eager_import_all() as the registry-miss fallback, and _EAGER_OP_MODULES
    # is the table that function iterates, so the table -- and this import --
    # survive untouched. This test pins that against the LIVE module (no
    # monkeypatch on _EAGER_OP_MODULES) using a real registered op, proving
    # the fifth surface stays populated and the fast path still fires.
    def test_fifth_surface_import_resolves_live_and_fast_path_fires(self, tmp_path, monkeypatch):
        import coordinator_core.ops as ops_module
        import coordinator_core.authz.classification as classification_module
        import coordinator_core.op_scopes as op_scopes_module
        import coordinator_core.ops._registry_map as registry_map_module
        import coordinator_core.authz.registration_quad as registration_quad_module

        assert hasattr(ops_module, "_EAGER_OP_MODULES")
        eager_module_paths = frozenset(mp for mp, _note in ops_module._EAGER_OP_MODULES)
        assert eager_module_paths, "the fifth surface must not be empty post-C6"

        # Pick a real op key complete on all five surfaces off the live tables.
        live_op_key = next(
            k
            for k, mp in registry_map_module.OP_MODULE_MAP.items()
            if k in classification_module.OP_CLASSIFICATION
            and k in op_scopes_module._OP_KEY_SCOPE
            and mp in eager_module_paths
        )

        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_live_fifth_surface_op.py", live_op_key)
        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        def _boom():
            raise AssertionError("fast path should short-circuit before the full walk")

        monkeypatch.setattr(registration_quad_module, "check_registration_quad", _boom)

        assert commit_tripwires.check_registration_quad_completeness(root) is None

    # Review: code-reviewer (Finding 1) -- proves the gate now denies an op
    # complete on OP_CLASSIFICATION/_OP_KEY_SCOPE/OP_MODULE_MAP but missing
    # only from _EAGER_OP_MODULES -- the exact live gap (roadmap.link_stubs,
    # 2026-08-05) the stage-1.5 fast path used to let sail through unreachable.
    def test_op_missing_only_from_eager_modules_is_denied(self, tmp_path, monkeypatch):
        from coordinator_core.authz.registration_quad import QuadViolation

        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_eager_gap_op.py", "fake.eager_gap")

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        import coordinator_core.authz.classification as classification_module
        import coordinator_core.op_scopes as op_scopes_module
        import coordinator_core.ops._registry_map as registry_map_module
        import coordinator_core.authz.registration_quad as registration_quad_module
        import coordinator_core.ops as ops_module

        monkeypatch.setattr(classification_module, "OP_CLASSIFICATION", {"fake.eager_gap": "read"})
        monkeypatch.setattr(op_scopes_module, "_OP_KEY_SCOPE", {"fake.eager_gap": "none"})
        monkeypatch.setattr(registry_map_module, "OP_MODULE_MAP", {"fake.eager_gap": "x.y"})
        # Deliberately empty -- "x.y" is complete on the other three surfaces
        # but absent here, so the fast path must NOT short-circuit clean.
        monkeypatch.setattr(ops_module, "_EAGER_OP_MODULES", [])

        planted = [
            QuadViolation(
                op_key="fake.eager_gap",
                surfaces_present=("OP_CLASSIFICATION", "_OP_KEY_SCOPE", "OP_MODULE_MAP"),
                surfaces_missing=("_EAGER_OP_MODULES",),
                missing_surface_files=(
                    ("_EAGER_OP_MODULES", "coordinator_core/ops/__init__.py"),
                ),
            ),
        ]
        monkeypatch.setattr(registration_quad_module, "check_registration_quad", lambda: planted)
        monkeypatch.setattr(registration_quad_module, "_KNOWN_UNCLASSIFIED_OPS_DEBT", frozenset())

        result = commit_tripwires.check_registration_quad_completeness(root)
        assert result is not None
        assert result.startswith("VIOLATION: REGISTRATION-QUAD-INVARIANT")
        assert "fake.eager_gap" in result
        assert "_EAGER_OP_MODULES" in result

    def test_wrong_repo_returns_before_stage_one_fires(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        _stage_op_file(root, "coordinator_core/ops/fake_wrong_repo_op.py", "fake.wrong_repo_op")

        other_root = str(tmp_path / "not-the-same-tree")
        Path(other_root).mkdir()
        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: other_root)

        calls: List[str] = []
        real_run_git = commit_tripwires._run_git

        def _tracking_run_git(args, cwd=None, timeout=2.0):
            calls.append(" ".join(args))
            return real_run_git(args, cwd=cwd, timeout=timeout)

        monkeypatch.setattr(commit_tripwires, "_run_git", _tracking_run_git)

        assert commit_tripwires.check_registration_quad_completeness(root) is None
        assert not any("diff" in c and "--cached" in c for c in calls)

    # Review: code-reviewer (Finding 1) -- proves the "cheap" stage-1 gate stays
    # O(1) subprocess spawns regardless of how many non-registering .py files a
    # commit touches under coordinator_core/, mirroring the call-counting pattern
    # in test_wrong_repo_returns_before_stage_one_fires. The prior implementation
    # spawned one `git show :<path>` PER staged file here -- this test stages
    # several to prove that regression cannot silently return.
    def test_many_non_registering_files_staged_stays_subprocess_bounded(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        for i in range(8):
            rel = "coordinator_core/ops/plain_module_{}.py".format(i)
            (Path(root) / rel).parent.mkdir(parents=True, exist_ok=True)
            (Path(root) / rel).write_text(
                "def helper_{}():\n    return {}\n".format(i, i), encoding="utf-8"
            )
            _git(root, "add", rel)

        monkeypatch.setattr(commit_tripwires, "_coordinator_core_repo_root", lambda: root)

        calls: List[str] = []
        real_run_git = commit_tripwires._run_git

        def _tracking_run_git(args, cwd=None, timeout=2.0):
            calls.append(" ".join(args))
            return real_run_git(args, cwd=cwd, timeout=timeout)

        monkeypatch.setattr(commit_tripwires, "_run_git", _tracking_run_git)

        import coordinator_core.authz.registration_quad as registration_quad_module

        def _boom():
            raise AssertionError("expensive full-tree walk must not run (AC11)")

        monkeypatch.setattr(registration_quad_module, "check_registration_quad", _boom)

        assert commit_tripwires.check_registration_quad_completeness(root) is None
        # rev-parse --show-toplevel, diff --cached --name-only, and ONE batched
        # grep --cached -- never one subprocess per staged file (8 files staged).
        assert len(calls) <= 4
        assert not any(c.startswith("show ") for c in calls)


class TestRegistrationQuadOverrideAtCallSite:
    """Override behavior lives in dispatch_checks.check_validate_commit (the
    call site), not commit_tripwires itself -- see commit_tripwires' own
    module docstring. Covered here per the C4 test-surface spec (AC18)."""

    def test_override_token_downgrades_deny_to_advisory(self, tmp_path, monkeypatch):
        from coordinator_core.bash_guards import dispatch_checks

        root = _init_repo(tmp_path)
        (tmp_path / "README.md").write_text("bump\n", encoding="utf-8")
        _git(root, "add", "README.md")

        monkeypatch.setattr(
            dispatch_checks.commit_tripwires,
            "check_registration_quad_completeness",
            lambda cwd=None: "VIOLATION: REGISTRATION-QUAD-INVARIANT — planted for override test",
        )

        monkeypatch.delenv("COORDINATOR_OVERRIDE_REGISTRATION_QUAD", raising=False)
        denied = dispatch_checks.check_validate_commit("git commit -m x", cwd=root)
        assert denied is not None
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert "REGISTRATION-QUAD-INVARIANT" in denied["hookSpecificOutput"]["permissionDecisionReason"]

        monkeypatch.setenv("COORDINATOR_OVERRIDE_REGISTRATION_QUAD", "1")
        advisory = dispatch_checks.check_validate_commit("git commit -m x", cwd=root)
        assert advisory is not None
        assert advisory["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "REGISTRATION-QUAD-INVARIANT" in advisory["hookSpecificOutput"]["additionalContext"]


class TestRegistrationQuadBaselinePruning:
    """AC4's commit-time leg: the frozen known-debt baseline is consumed by the
    tripwire, not only by the test-side guard.

    Without this, re-staging one of the baselined ops' `@register_op(...)` lines
    (a file move, a rename, a reformat) hard-denies the commit on debt the plan
    placed explicitly out of scope.
    """

    def _violation(self, op_key, missing):
        from coordinator_core.authz.registration_quad import QuadViolation

        all_surfaces = ("OP_CLASSIFICATION", "_OP_KEY_SCOPE", "OP_MODULE_MAP")
        paths = {
            "OP_CLASSIFICATION": "coordinator_core/authz/classification.py",
            "_OP_KEY_SCOPE": "coordinator_core/op_scopes.py",
            "OP_MODULE_MAP": "coordinator_core/ops/_registry_map.py",
        }
        return QuadViolation(
            op_key=op_key,
            surfaces_present=tuple(s for s in all_surfaces if s not in missing),
            surfaces_missing=tuple(missing),
            missing_surface_files=tuple((s, paths[s]) for s in missing),
        )

    def test_baselined_op_missing_only_classification_is_pruned(self):
        v = self._violation("cartography.stack", ["OP_CLASSIFICATION"])
        assert (
            commit_tripwires._prune_baselined_classification(v, frozenset({"cartography.stack"}))
            is None
        )

    def test_baselined_op_still_reports_other_missing_surfaces(self):
        """The baseline covers OP_CLASSIFICATION only — a baselined op missing its
        module-map entry is still a live defect and must survive pruning."""
        v = self._violation("cartography.stack", ["OP_CLASSIFICATION", "OP_MODULE_MAP"])
        pruned = commit_tripwires._prune_baselined_classification(
            v, frozenset({"cartography.stack"})
        )
        assert pruned is not None
        assert pruned.surfaces_missing == ("OP_MODULE_MAP",)
        assert pruned.missing_surface_files == (
            ("OP_MODULE_MAP", "coordinator_core/ops/_registry_map.py"),
        )

    def test_non_baselined_op_is_untouched(self):
        v = self._violation("brand.new_op", ["OP_CLASSIFICATION"])
        assert commit_tripwires._prune_baselined_classification(v, frozenset()) is v


# ---------------------------------------------------------------------------
# Check 13 -- check_staged_pathspec_divergence
#
# Spec: SC-DR-015 (DoE-claude coordinator/docs/wiki/scoped-safety-commits.md
# § SC-DR-015). Empirical basis: commit 506748a0.
# ---------------------------------------------------------------------------


class TestCheckStagedPathspecDivergence:
    def test_diverging_path_offer_fires(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        # Stage MINE, then let the worktree diverge further (PEER-shaped edit
        # never staged) -- the exact 506748a0 shape: index holds one thing,
        # worktree holds another.
        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        # The override-keys pointer is AUDIENCE-GATED: `operator_override_note`
        # emits it only when `session.identity.resolves_em_audience` is
        # satisfied, and under the PM's 2026-08-13 ruling the default is
        # inverted -- an absent envelope, or any resolution failure, degrades
        # to NOT-EM and renders no pointer at all. This test called with no
        # payload and asserted the pointer anyway, so it went red when that
        # inversion landed rather than when anything broke. Pass an EM-shaped
        # envelope (a real payload carrying neither `agent_id` nor
        # `subagent_type`) so the assertion pins the property it exists for.
        em_payload = {
            "tool_name": "Bash",
            "session_id": "11111111-2222-3333-4444-555555555555",
            "tool_input": {"command": 'git commit -m "test" -- shared.txt'},
        }
        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root, payload=em_payload
        )
        assert result is not None
        assert result.startswith("OFFER:")
        assert "shared.txt" in result
        assert "SC-DR-015" in result
        # The offer routes to the override-keys doc rather than naming the key
        # itself -- register rule B6 (BYPASS-KEY IN THE DENIAL). Asserting the
        # literal key back would re-pin the shape that rule exists to forbid.
        assert "guard-override-keys.md" in result
        assert "COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE" not in result

    def test_unreadable_index_says_so_instead_of_going_silent(self, tmp_path, monkeypatch):
        """DEMONSTRATED-RED before the `fail_loud=True` fix.

        The guard used to take `diverging_paths`' default `fail_loud=False`, so
        an `IndexParseError` collapsed to `[]`, `not diverging` was true, and the
        commit was waved through with NO warning. The condition that makes the
        index unreadable -- a peer holding `.git/index` -- is the SAME contention
        that causes the discarding this check exists to catch, so the guard went
        silent exactly when it was most needed. Measured at 2/200 under 12-way
        concurrency on one repo.

        Asserts the indeterminate branch NAMES what it could not establish. It
        must not assert a divergence it never measured either -- that would be
        the mirror defect of the silence.
        """
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        def _unreadable(*_a, **kwargs):
            assert kwargs.get("fail_loud") is True, (
                "the guard must ask for the loud contract -- with the default "
                "this raise would be swallowed to [] and the commit waved through"
            )
            raise commit_tripwires.DivergenceCheckFailed("index held by a peer")

        monkeypatch.setattr(commit_tripwires, "_diverging_paths", _unreadable)

        em_payload = {
            "tool_name": "Bash",
            "session_id": "11111111-2222-3333-4444-555555555555",
            "tool_input": {"command": 'git commit -m "test" -- shared.txt'},
        }
        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root, payload=em_payload
        )

        assert result is not None, "an unreadable index must not read as clean"
        assert result.startswith("OFFER:")
        assert "shared.txt" in result
        assert "could NOT" in result
        assert "SC-DR-015" in result
        # It reports an indeterminate READ, never an established divergence.
        # Pinned on the ASSERTIVE clause from the real-divergence offer ("and the
        # STAGED content there differs"), not on the bare phrase: the
        # indeterminate text legitimately contains "whether the STAGED content
        # there differs ... could NOT be determined", which is the opposite claim.
        assert "and the STAGED content there differs" not in result
        assert "could NOT be determined" in result

    def test_offer_to_a_non_em_audience_carries_no_override_pointer(self, tmp_path):
        """The other half of the audience gate, so the inversion above cannot
        silently flip back: with no envelope to resolve, the offer still fires
        (it is the load-bearing warning) but carries no override pointer."""
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root
        )
        assert result is not None
        assert result.startswith("OFFER:")
        assert "guard-override-keys.md" not in result

    def test_index_matches_worktree_silent_pass(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        # No further worktree edit past what's staged -- index == worktree.

        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root
        )
        assert result is None

    def test_no_trailing_pathspec_not_applicable(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test"', root
        )
        assert result is None

    def test_override_bypasses_and_logs(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        # A real session's hub dir is created by `core.init`. The override
        # logger no longer creates one itself -- it used to `os.makedirs` the
        # `<sid>/` path, which let an audit write MINT a phantom session that
        # `liveness.live_session_ids` then enumerated as real. Stand the dir up
        # here so this test exercises the production shape; the
        # no-such-session fallback is covered by
        # `bash_guards/tests/test_override_log_path.py`.
        (Path(root) / ".git" / "coordinator-sessions" / "test-session").mkdir(parents=True)

        monkeypatch.setenv("COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", "1")
        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root, session_id="test-session"
        )
        assert result is None

        log_path = Path(root) / ".git" / "coordinator-sessions" / "test-session" / "overrides.log"
        assert log_path.is_file()
        contents = log_path.read_text(encoding="utf-8")
        assert "OVERRIDE-PATHSPEC-DIVERGENCE" in contents
        assert "test-session" in contents

    def test_override_of_an_unknown_session_still_records_the_audit_line(
        self, tmp_path, monkeypatch
    ):
        """The override audit trail must survive even when the session has no
        hub directory -- it records a deliberately-bypassed safety check, so
        dropping the line to avoid minting a phantom session would trade a
        bookkeeping defect for a security-visibility one. It lands in the
        `no-session` bucket, which `liveness` already denylists."""
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        monkeypatch.setenv("COORDINATOR_OVERRIDE_PATHSPEC_DIVERGENCE", "1")
        commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" -- shared.txt', root, session_id="sess-no-such-dir"
        )

        sessions = Path(root) / ".git" / "coordinator-sessions"
        assert not (sessions / "sess-no-such-dir").exists()
        contents = (sessions / "no-session" / "overrides.log").read_text(encoding="utf-8")
        assert "OVERRIDE-PATHSPEC-DIVERGENCE" in contents
        assert "sess-no-such-dir" in contents

    def test_multi_segment_command_line_parsed(self, tmp_path):
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        result = commit_tripwires.check_staged_pathspec_divergence(
            'echo hello && git commit -m "test" -- shared.txt', root
        )
        assert result is not None
        assert "shared.txt" in result

    def test_empty_trailing_pathspec_means_whole_index_not_nothing(self, tmp_path):
        """`git commit -- ` (nothing after the `--`) is git's own
        "no pathspec given" form -- same semantics as no `--` at all, NOT a
        zero-path scoped commit. Must not crash and must not fire (there is
        no path being scoped, so no worktree-vs-index substitution risk),
        even though the tree genuinely has a diverging file sitting in it."""
        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        result = commit_tripwires.check_staged_pathspec_divergence(
            'git commit -m "test" --', root
        )
        assert result is None

    def test_no_git_commit_at_all_not_applicable(self, tmp_path):
        root = _init_repo(tmp_path)
        result = commit_tripwires.check_staged_pathspec_divergence(
            "git status -- shared.txt", root
        )
        assert result is None

    def test_empty_command_not_applicable(self):
        assert commit_tripwires.check_staged_pathspec_divergence("", "/tmp") is None


# ---------------------------------------------------------------------------
# coordinator_core.git.divergence.diverging_paths -- the extracted predicate
# itself, direct unit coverage (C1: docs/plans/2026-07-27-commit-mechanism-
# selection.md). Check 13's own tests above already cover this transitively
# through check_staged_pathspec_divergence; these exercise the helper
# in isolation, including inputs Check 13 never routes to it (empty list,
# multiple paths with only one diverging).
# ---------------------------------------------------------------------------


class TestDivergingPathsHelper:
    def test_diverging_path_detected(self, tmp_path):
        from coordinator_core.git.divergence import diverging_paths

        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        assert diverging_paths(["shared.txt"], root) == ["shared.txt"]

    def test_index_matches_worktree_is_clean(self, tmp_path):
        from coordinator_core.git.divergence import diverging_paths

        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")

        assert diverging_paths(["shared.txt"], root) == []

    def test_only_diverging_subset_returned(self, tmp_path):
        from coordinator_core.git.divergence import diverging_paths

        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        (tmp_path / "clean.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt", "clean.txt")
        _git(root, "commit", "-q", "-m", "seed both")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        (tmp_path / "clean.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "clean.txt")
        # clean.txt: index == worktree, no further edit past staging.

        assert diverging_paths(["shared.txt", "clean.txt"], root) == ["shared.txt"]

    def test_empty_paths_returns_empty(self, tmp_path):
        from coordinator_core.git.divergence import diverging_paths

        root = _init_repo(tmp_path)
        assert diverging_paths([], root) == []

    def test_git_diff_failure_default_fail_loud_false_collapses_to_empty(self, tmp_path, monkeypatch):
        """Check 13's own posture, unchanged: a `git diff` failure (rc != 0)
        collapses to `[]` -- "nothing to report" -- when `fail_loud` is left
        at its default `False`. This is the exact call shape
        `check_staged_pathspec_divergence` uses; it must keep failing open."""
        from coordinator_core.git import divergence
        from coordinator_core.git.divergence import diverging_paths

        root = _init_repo(tmp_path)

        def _boom(args, cwd=None, timeout=2.0):
            return 128, ""

        monkeypatch.setattr(divergence, "_run_git", _boom)

        assert diverging_paths(["shared.txt"], root) == []

    def test_git_diff_failure_fail_loud_true_raises(self, tmp_path, monkeypatch):
        """The commit-mechanism-selector posture (`git_native.commit_scoped`,
        `commit_pipeline.explicit_stage`): the identical `git diff` failure
        must NOT collapse to `[]` when `fail_loud=True` -- it raises
        `DivergenceCheckFailed` so the caller can tell "clean" apart from
        "indeterminate" instead of silently treating a broken check as
        proof of no divergence."""
        from coordinator_core.git import divergence
        from coordinator_core.git.divergence import DivergenceCheckFailed, diverging_paths

        root = _init_repo(tmp_path)

        def _boom(args, cwd=None, timeout=2.0):
            return 128, ""

        monkeypatch.setattr(divergence, "_run_git", _boom)

        with pytest.raises(DivergenceCheckFailed):
            diverging_paths(["shared.txt"], root, fail_loud=True)

    def test_git_diff_failure_on_second_call_fail_loud_true_raises(self, tmp_path, monkeypatch):
        """The second `git diff` call (plain, non-`--cached`) can fail
        independently of the first -- must raise there too, not only when
        the first call fails."""
        from coordinator_core.git import divergence
        from coordinator_core.git.divergence import DivergenceCheckFailed, diverging_paths

        root = _init_repo(tmp_path)

        def _fail_second(args, cwd=None, timeout=2.0):
            if "--cached" in args:
                return 0, ""
            return 128, ""

        monkeypatch.setattr(divergence, "_run_git", _fail_second)

        with pytest.raises(DivergenceCheckFailed):
            diverging_paths(["shared.txt"], root, fail_loud=True)


class TestCheckStagedPathspecDivergenceCallSiteIntegration:
    """End-to-end through `check_validate_commit` (the real dispatch entry
    point) -- confirms the advisory (never deny) verdict actually reaches
    the hook's `allow` + `additionalContext` shape, mirroring
    TestRegistrationQuadOverrideAtCallSite's call-site-level coverage."""

    def test_diverging_commit_surfaces_as_advisory_never_deny(self, tmp_path):
        from coordinator_core.bash_guards import dispatch_checks

        root = _init_repo(tmp_path)
        (tmp_path / "shared.txt").write_text("line1\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        _git(root, "commit", "-q", "-m", "seed shared.txt")

        (tmp_path / "shared.txt").write_text("line1\nMINE\n", encoding="utf-8")
        _git(root, "add", "shared.txt")
        (tmp_path / "shared.txt").write_text("line1\nMINE\nPEER\n", encoding="utf-8")

        result = dispatch_checks.check_validate_commit(
            'git commit -m "test" -- shared.txt', cwd=root
        )
        assert result is not None
        assert result["hookSpecificOutput"]["permissionDecision"] == "allow"
        assert "OFFER:" in result["hookSpecificOutput"]["additionalContext"]
        assert "SC-DR-015" in result["hookSpecificOutput"]["additionalContext"]
