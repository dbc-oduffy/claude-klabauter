"""test_publish_gate_predicate_symmetry — regression tests for the three
publish-gate defects reported in
cross-repo/inbox/2026-08-05-doe-claude-em-three-publish-gates-block-the-4-0-0-percolate.md.

The load-bearing one is a PREDICATE ASYMMETRY in `file-count-delta`: the
OBSERVED side (`guards.check_file_count_delta` -> `guards._walk_for_guard`) walks
with the GUARD ENTRY's params and always narrows to `include_extensions`, while
the EXPECTED side (`publish.py::_compute_effective_source_count`) walked with the
SECTION's `file_surface` params and did NOT narrow. Post-admission-inversion
(surface.py, 2026-08-05) an un-narrowed walk admits everything not explicitly
excluded, so the expected side counted files the observed side structurally
cannot — e.g. `.percolate-ignore`, which the allowlist builder copies into the
restricted staging tree unconditionally. Live symptom, every full run:
`coordinator-claude-toplevel-wiki: file-count-delta: expected 28 (+/-0), got 27`.

`test_dotfile_in_source_does_not_produce_a_delta` is the case that would have
caught it: the two sides are run against the SAME tree, so any delta at all is
proof the two sides are asking different questions.

Covers:
  * expected/observed symmetry on a tree holding a non-`include_extensions`
    dotfile (`test_dotfile_in_source_does_not_produce_a_delta`);
  * the count uses the GUARD ENTRY's params, not the section's `file_surface`
    (`test_guard_entry_params_win_over_section_file_surface`);
  * a section with no `file-count-delta` entry yields `None`, not a
    differently-scoped fallback count (`test_no_guard_entry_yields_none`);
  * the depersonalize template's ratified unscanned-published exception is
    loadable and keyed by the destination-repo-root-relative path the check
    actually compares against (`TestUnscannedExceptionsRatification`);
  * the install-doc payload check is handed an explicit doc set that drops the
    changelog class and keeps everything else the tree ships
    (`TestInstallDocSet`).
  * `publish.py::_compute_always_swept_entrypoints` (the entrypoint gate's
    always-swept floor, § state/debt-backlog/2026-08-10-publish-py-reaches-
    into-engine-py-s-priv-e4309cadc0da) delegates to engine.py's own PUBLIC
    `derive_always_swept_entrypoints` seam rather than reaching through
    private closure-walk helpers, and the floor it computes agrees with
    calling that seam directly (`TestAlwaysSweptEntrypointFloor`).
  * `publish.py::dispatch_end_of_run_entrypoint_gate`'s own `--changed-only`
    dispatch-site logic (the row's noted gap: nothing called it with
    `changed_only=True` before) -- the `subset = selected | always_swept`
    union, the unmodeled-suffix full-sweep widen, and the no-changed-set
    fallback, exercised directly against the dispatcher rather than only
    against the helper/engine seam it calls
    (`TestEndOfRunEntrypointGateDispatchLogic`).

Run: python -m pytest coordinator/bin/tests/test_publish_gate_predicate_symmetry.py -q
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from coordinator_core.percolate import engine as pct_engine
from coordinator_core.percolate import guards as pct_guards
from coordinator_core.percolate import surface as pct_surface

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_gate_predicate_symmetry_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

# Only `iter_surface_files` is exercised by `_compute_effective_source_count`;
# the real one is used deliberately (a stub would defeat a test whose whole
# subject is which walk predicate runs).
_CLAUDE_KLABAUTER = SimpleNamespace(iter_surface_files=pct_surface.iter_surface_files)

_GUARD_PARAMS = {"tolerance": 0, "include_extensions": ["*.md"]}


def _section(guard_params=_GUARD_PARAMS, file_surface=None) -> dict:
    section: dict = {"guards": []}
    if guard_params is not None:
        section["guards"].append({"kind": "file-count-delta", "params": dict(guard_params)})
    if file_surface is not None:
        section["file_surface"] = file_surface
    return section


def _wiki_tree(root: Path, *, md_count: int = 3, with_ignore_dotfile: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for i in range(md_count):
        (root / f"page-{i}.md").write_text(f"# page {i}\n", encoding="utf-8")
    if with_ignore_dotfile:
        # The allowlist builder copies this into the restricted staging tree
        # unconditionally -- it is genuinely present on the source side.
        (root / ".percolate-ignore").write_text("scratch/\n", encoding="utf-8")
    return root


class TestFileCountDeltaPredicateSymmetry:
    def test_dotfile_in_source_does_not_produce_a_delta(self, tmp_path):
        tree = _wiki_tree(tmp_path / "tree", md_count=3, with_ignore_dotfile=True)
        section = _section()

        expected = publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, section)
        result = pct_guards.check_file_count_delta(
            tree, dict(_GUARD_PARAMS), effective_source_count=expected
        )

        # Same tree on both sides: a non-zero delta can only mean the two sides
        # applied different predicates.
        assert expected == 3, expected
        assert result.ok, result.message

    def test_dotfile_only_ever_counted_by_the_unnarrowed_walk(self, tmp_path):
        """Pins the mechanism, so a later change that silently drops the
        narrowing fails here rather than only in a full publish run."""
        tree = _wiki_tree(tmp_path / "tree", md_count=3, with_ignore_dotfile=True)

        unnarrowed = sum(1 for _ in pct_surface.iter_surface_files(tree, include_extensions=["*.md"]))
        narrowed = sum(
            1
            for _ in pct_surface.iter_surface_files(
                tree, include_extensions=["*.md"], narrow_to_include_extensions=True
            )
        )

        assert unnarrowed == 4, unnarrowed
        assert narrowed == 3, narrowed
        assert publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, _section()) == narrowed

    def test_guard_entry_params_win_over_section_file_surface(self, tmp_path):
        """The guard's observed side reads scoping off the guard ENTRY, so the
        expected side must too — a section-level `file_surface` that disagrees
        must not influence the count."""
        tree = _wiki_tree(tmp_path / "tree", md_count=3, with_ignore_dotfile=False)
        (tree / "notes.txt").write_text("not markdown\n", encoding="utf-8")

        section = _section(
            file_surface={"include_extensions": ["*.md", "*.txt"], "exclude_basenames": ["page-0.md"]}
        )
        expected = publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, section)

        assert expected == 3, expected
        assert pct_guards.check_file_count_delta(
            tree, dict(_GUARD_PARAMS), effective_source_count=expected
        ).ok

    def test_guard_entry_exclusions_are_applied_to_the_source_side(self, tmp_path):
        tree = _wiki_tree(tmp_path / "tree", md_count=3, with_ignore_dotfile=False)
        params = {"tolerance": 0, "include_extensions": ["*.md"], "exclude_basenames": ["page-0.md"]}

        expected = publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, _section(params))

        assert expected == 2, expected
        assert pct_guards.check_file_count_delta(tree, dict(params), effective_source_count=expected).ok

    def test_no_guard_entry_yields_none(self, tmp_path):
        tree = _wiki_tree(tmp_path / "tree")
        section = _section(guard_params=None, file_surface={"include_extensions": ["*.md"]})

        assert publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, section) is None
        assert publish._file_count_delta_guard_params(section) is None

    def test_guard_entry_with_no_params_yields_zero_not_a_crash(self, tmp_path):
        """An entry declaring no `params` narrows against an empty include set,
        which admits nothing — the same structurally-zero shape the guard's own
        observed side produces, so the two still agree (and the guard's
        `allow_empty` rule is what makes that loud, not this function)."""
        tree = _wiki_tree(tmp_path / "tree")
        section = {"guards": [{"kind": "file-count-delta"}]}

        assert publish._compute_effective_source_count(_CLAUDE_KLABAUTER, tree, section) == 0


class TestUnscannedExceptionsRatification:
    def test_depersonalize_template_exception_is_ratified_with_a_reason(self):
        exceptions = publish._load_unscanned_exceptions()
        path = "bin/depersonalize-identity.example.yaml"

        assert path in exceptions, sorted(exceptions)
        reason = exceptions[path]
        # A key that is not the destination-repo-root-relative POSIX path the
        # check compares against is silently inert, which is the worst outcome.
        assert not path.startswith("coordinator/")
        assert "2026-08-05-doe-claude-em-three-publish-gates" in reason

    def test_preexisting_exception_still_loads(self):
        exceptions = publish._load_unscanned_exceptions()
        assert ".github/scripts/check-persona-names.py" in exceptions


class TestInstallDocSet:
    def _module(self):
        return publish._import_check_install_doc_payload()

    def test_changelog_is_excluded_and_everything_else_kept(self, tmp_path):
        shipped = [
            "AGENTS.md",
            "CHANGELOG.md",
            "CONTRIBUTING.md",
            "INSTALL.md",
            "README.md",
            "SECURITY.md",
        ]
        for name in shipped:
            (tmp_path / name).write_text("# doc\n", encoding="utf-8")

        selected = {p.name for p in publish._install_doc_paths_for_repo_root(self._module(), tmp_path)}

        assert "CHANGELOG.md" not in selected
        # CONTRIBUTING.md stays: this gate has caught a genuine stale install
        # pointer in it, so it is not a doc class to drop.
        assert selected == set(shipped) - {"CHANGELOG.md"}

    def test_changelog_class_variants_are_excluded_case_insensitively(self, tmp_path):
        for name in ("Changelog.md", "CHANGES.md", "HISTORY.md", "RELEASE-NOTES.md", "RELEASES.md"):
            (tmp_path / name).write_text("# doc\n", encoding="utf-8")
        (tmp_path / "README.md").write_text("# doc\n", encoding="utf-8")

        selected = {p.name for p in publish._install_doc_paths_for_repo_root(self._module(), tmp_path)}

        assert selected == {"README.md"}

    def test_a_doc_merely_mentioning_changelog_is_not_dropped(self, tmp_path):
        (tmp_path / "changelog-policy.md").write_text("# doc\n", encoding="utf-8")

        selected = {p.name for p in publish._install_doc_paths_for_repo_root(self._module(), tmp_path)}

        assert selected == {"changelog-policy.md"}

    def test_changelog_stale_pointers_no_longer_reach_the_checker(self, tmp_path):
        """The 69-finding shape: a changelog entry naming a since-retired script
        is a correct historical statement, and must not be a finding."""
        module = self._module()
        (tmp_path / "CHANGELOG.md").write_text(
            "- **Cruft sweep.** `bin/cruft-sweep.sh` (mechanical) shipped in v3.\n",
            encoding="utf-8",
        )
        (tmp_path / "README.md").write_text("Install with `python3 scripts/setup.py`.\n", encoding="utf-8")
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "setup.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")

        assert module.check_tree(tmp_path), "the bare *.md default must still flag the changelog"
        assert not module.check_tree(
            tmp_path, doc_paths=publish._install_doc_paths_for_repo_root(module, tmp_path)
        )


class TestAlwaysSweptEntrypointFloor:
    """Regression coverage for state/debt-backlog/2026-08-10-publish-py-
    reaches-into-engine-py-s-priv-e4309cadc0da: `_compute_always_swept_
    entrypoints` used to reach three of engine.py's private closure-walk
    helpers via `engine_claude_klabauter.percolate_engine_module` (unguarded attribute
    access, `noqa: SLF001`). engine.py now exports `derive_always_swept_
    entrypoints` as a public seam, and the driver wrapper is a thin
    delegator over it."""

    def _linked_tree(self, root: Path) -> Path:
        bin_dir = root / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
        # Reaches `bin/helper.py` through a first-party import -- some
        # changed-set (one that names helper.py) could put this at risk, so
        # it must NOT be in the always-swept floor.
        (bin_dir / "entry_linked.py").write_text("from bin import helper\n\nUSE = helper.VALUE\n", encoding="utf-8")
        # No first-party imports at all -- no changed-set that omits this
        # file's own path could ever put it at risk, so it MUST be in the
        # always-swept floor.
        (bin_dir / "entry_isolated.py").write_text("VALUE = 2\n", encoding="utf-8")
        return root

    def test_engine_seam_floor_excludes_linked_includes_isolated(self, tmp_path):
        tree = self._linked_tree(tmp_path)
        entrypoints = ("bin/entry_linked.py", "bin/entry_isolated.py")

        floor = pct_engine.derive_always_swept_entrypoints(tree, entrypoints)

        assert floor == ("bin/entry_isolated.py",), floor

    def test_driver_wrapper_delegates_to_the_engine_seam_only(self, tmp_path):
        """The wrapper needs nothing but the resolved callable -- no
        `percolate_engine_module` attribute, and no reach into any
        underscore-prefixed engine name."""
        tree = self._linked_tree(tmp_path)
        entrypoints = ("bin/entry_linked.py", "bin/entry_isolated.py")
        calls: list = []

        def _recording_derive(repo_root, entrypoints_arg):
            calls.append((repo_root, tuple(entrypoints_arg)))
            return pct_engine.derive_always_swept_entrypoints(repo_root, entrypoints_arg)

        stub_claude_klabauter = SimpleNamespace(derive_always_swept_entrypoints=_recording_derive)
        assert not hasattr(stub_claude_klabauter, "percolate_engine_module")

        floor = publish._compute_always_swept_entrypoints(stub_claude_klabauter, tree, entrypoints)

        assert floor == ("bin/entry_isolated.py",), floor
        assert calls == [(tree, entrypoints)]


class TestEndOfRunEntrypointGateDispatchLogic:
    """Direct coverage of `dispatch_end_of_run_entrypoint_gate` itself (§
    state/debt-backlog/2026-08-10-publish-py-reaches-into-engine-py-s-priv-
    e4309cadc0da `proposed_action`'s "related gap": no test called this
    function with `changed_only=True`, so the branch at its own call site --
    deriving `selected` via `derive_changed_entrypoints`, unioning it with
    `_compute_always_swept_entrypoints`'s floor, and widening to a full
    sweep on an unmodeled-suffix hit or an undeterminable changed-set --
    was exercised by nothing; `TestAlwaysSweptEntrypointFloor` above only
    ever calls the helper/engine seam directly).

    `engine_claude_klabauter` is a `SimpleNamespace` stub for every field this
    dispatcher reads -- `enumerate_gate_entrypoints`, `derive_worker_cap`,
    `derive_changed_entrypoints`, `derive_always_swept_entrypoints`,
    `mktcache_gate_env`, `run_entrypoint_gate` -- so the subject under test
    is which `subset` this function COMPUTES and hands to `run_entrypoint_
    gate`, never whether a real CLI starts (that orthogonal question is
    `test_function_gate_wiring.py`'s REAL-subprocess concern for the
    sibling function gate)."""

    _ENTRYPOINTS = ("bin/entry_linked", "bin/entry_isolated", "bin/entry_other")

    @classmethod
    def _stub_claude_klabauter(cls, *, derive_changed, derive_always_swept, run_entrypoint_gate):
        @contextlib.contextmanager
        def _mktcache_gate_env(*, overrides):
            yield {"HOME": "/synthetic"}

        return SimpleNamespace(
            enumerate_gate_entrypoints=lambda repo_root: cls._ENTRYPOINTS,
            derive_worker_cap=lambda: 1,
            derive_changed_entrypoints=derive_changed,
            derive_always_swept_entrypoints=derive_always_swept,
            mktcache_gate_env=_mktcache_gate_env,
            run_entrypoint_gate=run_entrypoint_gate,
        )

    def test_changed_only_unions_selected_with_always_swept_floor(self, tmp_path):
        """`derive_changed_entrypoints` selects one entrypoint, the always-
        swept floor names a different one -- `run_entrypoint_gate` must
        receive their sorted union as `subset`, and both derivers must see
        the repo-relative changed path plus the full entrypoint population."""
        repo_root = tmp_path
        (repo_root / "changed.py").write_text("", encoding="utf-8")

        changed_calls: list = []
        always_swept_calls: list = []
        run_calls: list = []

        def _derive_changed(changed_paths, root, *, entrypoints):
            changed_calls.append((tuple(changed_paths), root, tuple(entrypoints)))
            return ("bin/entry_linked",)

        def _derive_always_swept(root, entrypoints):
            always_swept_calls.append((root, tuple(entrypoints)))
            return ("bin/entry_isolated",)

        def _run_entrypoint_gate(root, entrypoints, *, env, timeout, max_workers, aggregate_budget, subset):
            run_calls.append(subset)
            return pct_engine.EntrypointGateResult(ok=True, scanned=len(subset or entrypoints), home_shape="mktcache")

        stub = self._stub_claude_klabauter(
            derive_changed=_derive_changed,
            derive_always_swept=_derive_always_swept,
            run_entrypoint_gate=_run_entrypoint_gate,
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=stub, store={})

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            engine_ctx,
            [repo_root],
            target_filtered=False,
            changed_files_by_repo_root={repo_root: {repo_root / "changed.py"}},
            changed_only=True,
        )

        assert ok
        assert run_calls == [("bin/entry_isolated", "bin/entry_linked")]
        assert changed_calls == [(("changed.py",), repo_root, self._ENTRYPOINTS)]
        assert always_swept_calls == [(repo_root, self._ENTRYPOINTS)]

    def test_changed_only_widens_to_full_sweep_on_unmodeled_suffix(self, tmp_path):
        """A changed path carrying an unmodeled suffix (`.md`, §
        `_CHANGED_ONLY_UNMODELED_SUFFIXES`) must widen `subset` back to
        `None` (full sweep) at THIS dispatch site -- never narrow on a
        change the closure graph cannot model. Neither deriver may even be
        called once that widen fires."""
        repo_root = tmp_path
        (repo_root / "notes.md").write_text("", encoding="utf-8")

        run_calls: list = []

        def _must_not_be_called(*_args, **_kwargs):
            raise AssertionError("must not be called once an unmodeled suffix widens to full sweep")

        def _run_entrypoint_gate(root, entrypoints, *, env, timeout, max_workers, aggregate_budget, subset):
            run_calls.append(subset)
            return pct_engine.EntrypointGateResult(ok=True, scanned=len(entrypoints), home_shape="mktcache")

        stub = self._stub_claude_klabauter(
            derive_changed=_must_not_be_called,
            derive_always_swept=_must_not_be_called,
            run_entrypoint_gate=_run_entrypoint_gate,
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=stub, store={})

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            engine_ctx,
            [repo_root],
            target_filtered=False,
            changed_files_by_repo_root={repo_root: {repo_root / "notes.md"}},
            changed_only=True,
        )

        assert ok
        assert run_calls == [None]

    def test_changed_only_with_no_changed_set_falls_back_to_full_sweep(self, tmp_path):
        """`changed_files_by_repo_root=None` with `changed_only=True` --
        nothing to derive a subset from, so `subset=None` reaches `run_
        entrypoint_gate` unchanged, same as a pre-`changed_only` full sweep.
        Neither deriver may be called."""
        repo_root = tmp_path

        run_calls: list = []

        def _must_not_be_called(*_args, **_kwargs):
            raise AssertionError("must not be called with nothing to derive a subset from")

        def _run_entrypoint_gate(root, entrypoints, *, env, timeout, max_workers, aggregate_budget, subset):
            run_calls.append(subset)
            return pct_engine.EntrypointGateResult(ok=True, scanned=len(entrypoints), home_shape="mktcache")

        stub = self._stub_claude_klabauter(
            derive_changed=_must_not_be_called,
            derive_always_swept=_must_not_be_called,
            run_entrypoint_gate=_run_entrypoint_gate,
        )
        engine_ctx = publish.PercolateEngineContext(engine_claude_klabauter=stub, store={})

        ok = publish.dispatch_end_of_run_entrypoint_gate(
            engine_ctx,
            [repo_root],
            target_filtered=False,
            changed_files_by_repo_root=None,
            changed_only=True,
        )

        assert ok
        assert run_calls == [None]
