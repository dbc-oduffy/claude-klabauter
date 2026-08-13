"""test_publish_gate_predicate_symmetry — regression tests for the three
publish-gate defects reported in
cross-repo/inbox/2026-08-05-coordinator-claude-em-three-publish-gates-block-the-4-0-0-percolate.md.

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

Run: python -m pytest coordinator/bin/tests/test_publish_gate_predicate_symmetry.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

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
        assert "2026-08-05-coordinator-claude-em-three-publish-gates" in reason

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
