"""test_check_doctrine_citations.py — Tier T tests for check-doctrine-citations.py.

Fixtures a synthetic three-tree corpus (doe_root, doe_coordinator, claude-klabauter)
under a temp directory rather than walking the live DoE-claude / claude-klabauter
repos, per the spike's measured ground truth
(docs/research/spike-verdicts/2026-08-29-doctrine-document-citation-resolution.md):
8 DoE-vs-consumer filename collisions and 2 intra-DoE filename collisions.

Negative-spec: does NOT invoke resolve-repo-path.py or any subprocess (always
passes --no-default-trees equivalents via `use_default_trees=False`, or the
CLI's --tree overrides), and does NOT walk any live repo path.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import tempfile
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_THIS_DIR, "check-doctrine-citations.py")

_spec = importlib.util.spec_from_file_location("check_doctrine_citations", _MODULE_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["check_doctrine_citations"] = _module
_spec.loader.exec_module(_module)

# The 8 DoE-vs-consumer collisions and 2 intra-DoE collisions the spike measured.
DOE_VS_CLAUDE_KLABAUTER_COLLISIONS = [
    "baton-lifecycle.md",
    "cockpit-contract.md",
    "code-review.md",
    "cross-repo.md",
    "guard-messaging.md",
    "test-infrastructure.md",
    "write-confinement.md",
]
DOE_VS_CLAUDE_KLABAUTER_PLANS_COLLISION = "INDEX.md"  # plans/INDEX.md
INTRA_DOE_COLLISIONS = ["DIRECTORY_GUIDE.md", "doctor-proportionality.md"]


def _write(path: str, content: str = "placeholder\n") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


class FixtureCorpus:
    """Builds a temp dir with doe_root/, doe_coordinator/, claude-klabauter/ trees and
    a separate corpus/ dir holding the .md files whose citations get scanned."""

    def __init__(self, tmp: str):
        self.tmp = tmp
        self.doe_root = os.path.join(tmp, "doe_root")
        self.doe_coordinator = os.path.join(tmp, "doe_coordinator")
        self.claude_klabauter = os.path.join(tmp, "claude-klabauter")
        self.corpus = os.path.join(tmp, "corpus")
        os.makedirs(self.corpus, exist_ok=True)

        for name in DOE_VS_CLAUDE_KLABAUTER_COLLISIONS:
            _write(os.path.join(self.doe_root, "docs", "wiki", name))
            _write(os.path.join(self.claude_klabauter, "docs", "wiki", name))

        _write(os.path.join(self.doe_root, "docs", "plans", DOE_VS_CLAUDE_KLABAUTER_PLANS_COLLISION))
        _write(os.path.join(self.claude_klabauter, "docs", "plans", DOE_VS_CLAUDE_KLABAUTER_PLANS_COLLISION))

        for name in INTRA_DOE_COLLISIONS:
            _write(os.path.join(self.doe_root, "docs", "wiki", name))
            _write(os.path.join(self.doe_coordinator, "docs", "wiki", name))

        _write(os.path.join(self.doe_coordinator, "docs", "wiki", "only-in-coordinator.md"))
        _write(os.path.join(self.claude_klabauter, "docs", "wiki", "only-in-claude-klabauter.md"))

    @property
    def tree_roots(self) -> dict[str, str]:
        return {
            "doe_root": self.doe_root,
            "doe_coordinator": self.doe_coordinator,
            "claude-klabauter": self.claude_klabauter,
        }

    def write_corpus_file(self, name: str, content: str) -> str:
        path = os.path.join(self.corpus, name)
        _write(path, content)
        return path


class ResolvableCitationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_uniquely_resolvable_bare_citation_is_still_flagged_unanchored(self):
        # Anchoring supersedes existence: a bare citation is flagged even
        # when it happens to resolve to exactly one tree on this machine --
        # that is precisely the false-safety the anchoring rule exists to
        # refuse (see module docstring's "Anchoring supersedes existence").
        self.fixture.write_corpus_file(
            "doc.md", "See docs/wiki/only-in-claude-klabauter.md for detail.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("only-in-claude-klabauter.md", findings[0])
        self.assertIn("unanchored", findings[0])

    def test_prefixed_citation_resolves_only_against_its_named_tree(self):
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/only-in-coordinator.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)

    def test_unresolvable_bare_citation_exits_nonzero_and_names_it_unanchored(self):
        # A bare citation is reported unanchored (the primary class) before
        # existence is ever consulted -- it never reaches "unresolvable".
        self.fixture.write_corpus_file(
            "doc.md", "See docs/wiki/does-not-exist-anywhere.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("does-not-exist-anywhere.md", findings[0])
        self.assertIn("unanchored", findings[0])

    def test_anchored_citation_missing_everywhere_is_unresolvable(self):
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/does-not-exist-anywhere.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("does-not-exist-anywhere.md", findings[0])
        self.assertIn("unresolvable", findings[0])

    def test_prefixed_citation_missing_in_its_named_tree_is_unresolvable_never_a_fallback(self):
        # coordinator/ names doe_coordinator explicitly; only-in-claude-klabauter.md
        # exists in claude-klabauter, not doe_coordinator, so this must refuse rather
        # than silently falling back to the claude-klabauter tree.
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/only-in-claude-klabauter.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1)
        self.assertIn("unresolvable", findings[0])


class AmbiguousCitationTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_each_doe_vs_claude_klabauter_collision_is_reported_unanchored(self):
        # Bare form: the anchoring rule (primary) catches this before the
        # collision would even reach "ambiguous" (secondary) -- see
        # test_each_doe_vs_claude_klabauter_collision_anchored_is_reported_ambiguous
        # for the anchored-form case that still exercises "ambiguous".
        for name in DOE_VS_CLAUDE_KLABAUTER_COLLISIONS:
            with self.subTest(name=name):
                fixture = FixtureCorpus(tempfile.mkdtemp())
                fixture.write_corpus_file("doc.md", f"See docs/wiki/{name} for detail.\n")
                code, findings, excluded, unresolved = _module.run(
                    [fixture.corpus], fixture.tree_roots, use_default_trees=False
                )
                self.assertEqual(code, 1, msg=f"{name}: {findings}")
                self.assertEqual(len(findings), 1)
                self.assertIn(name, findings[0])
                self.assertIn("unanchored", findings[0])
                self.assertIn("doe_root", findings[0])
                self.assertIn("claude-klabauter", findings[0])

    def test_each_doe_vs_claude_klabauter_collision_anchored_is_still_ambiguous(self):
        # An anchored citation with an unmapped prefix (bare "/") still
        # exercises the secondary ambiguity check -- ambiguity is not
        # retired by the anchoring rule, only demoted to secondary.
        for name in DOE_VS_CLAUDE_KLABAUTER_COLLISIONS:
            with self.subTest(name=name):
                fixture = FixtureCorpus(tempfile.mkdtemp())
                fixture.write_corpus_file("doc.md", f"See /docs/wiki/{name} for detail.\n")
                code, findings, excluded, unresolved = _module.run(
                    [fixture.corpus], fixture.tree_roots, use_default_trees=False
                )
                self.assertEqual(code, 1, msg=f"{name}: {findings}")
                self.assertEqual(len(findings), 1)
                self.assertIn(name, findings[0])
                self.assertIn("ambiguous", findings[0])
                self.assertIn("doe_root", findings[0])
                self.assertIn("claude-klabauter", findings[0])

    def test_plans_index_collision_bare_is_reported_unanchored(self):
        self.fixture.write_corpus_file("doc.md", "See docs/plans/INDEX.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1)
        self.assertIn("unanchored", findings[0])

    def test_each_intra_doe_collision_is_reported_unanchored(self):
        for name in INTRA_DOE_COLLISIONS:
            with self.subTest(name=name):
                fixture = FixtureCorpus(tempfile.mkdtemp())
                fixture.write_corpus_file("doc.md", f"See docs/wiki/{name}.\n")
                code, findings, excluded, unresolved = _module.run(
                    [fixture.corpus], fixture.tree_roots, use_default_trees=False
                )
                self.assertEqual(code, 1, msg=f"{name}: {findings}")
                self.assertEqual(len(findings), 1)
                self.assertIn(name, findings[0])
                self.assertIn("unanchored", findings[0])
                self.assertIn("doe_root", findings[0])
                self.assertIn("doe_coordinator", findings[0])

    def test_silence_on_a_bad_citation_is_a_test_failure(self):
        # Regression guard: if a future change made the lint quiet on an
        # ambiguous citation (e.g. exit 0 with an empty findings list), this
        # assertion is the one that must fail, not silently pass.
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/cross-repo.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertNotEqual(
            (code, findings),
            (0, []),
            msg="lint went silent on a known-ambiguous citation (cross-repo.md)",
        )
        self.assertEqual(code, 1)
        self.assertTrue(findings, msg="exit code was non-zero but no findings were named")


class DuplicateCitationOnOneLineTests(unittest.TestCase):
    """A markdown link whose text and target are the same path
    (`[docs/wiki/x.md](docs/wiki/x.md)`) makes `_CITATION_RE` match the
    same citation site twice on one line -- that must produce exactly one
    finding, not two, per (source_file, line_no, prefix+core_path)."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_markdown_self_link_produces_one_finding_not_two(self):
        self.fixture.write_corpus_file(
            "doc.md",
            "See [`docs/wiki/only-in-claude-klabauter.md`](docs/wiki/only-in-claude-klabauter.md) for detail.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1, msg=findings)
        self.assertIn("only-in-claude-klabauter.md", findings[0])
        self.assertIn("unanchored", findings[0])

    def test_two_distinct_citations_on_one_line_still_produce_two_findings(self):
        self.fixture.write_corpus_file(
            "doc.md",
            "See docs/wiki/only-in-claude-klabauter.md and docs/wiki/only-in-coordinator.md.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 2, msg=findings)
        self.assertTrue(any("only-in-claude-klabauter.md" in f for f in findings), msg=findings)
        self.assertTrue(any("only-in-coordinator.md" in f for f in findings), msg=findings)

    def test_duplicated_illustrative_citation_on_one_line_tallies_excluded_once(self):
        self.fixture.write_corpus_file(
            "doc.md",
            "See [docs/plans/*.md](docs/plans/*.md) for the family.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])
        self.assertEqual(excluded, 1)


class CliEntrypointTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_main_returns_nonzero_and_prints_finding_for_ambiguous_citation(self):
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/cross-repo.md.\n")
        tree_args = [f"--tree={name}={path}" for name, path in self.fixture.tree_roots.items()]
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
            *tree_args,
        ]
        code = _module.main(argv)
        self.assertEqual(code, 1)


class IllustrativeFormExclusionTests(unittest.TestCase):
    """Matches the census's own definition (state/audits/2026-07-23-doctrine-
    doc-reference-resolution-census.md): a glob metacharacter, a `{...}`
    template slot, a literal `YYYY-MM-DD-`/`path/to/` segment, or an
    `<...>` angle placeholder is illustrative, not a real citation, and must
    be excluded rather than folded into dangling/unresolvable."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_glob_star_citation_is_excluded_not_reported(self):
        self.fixture.write_corpus_file("doc.md", "See docs/plans/*.md for the family.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])
        self.assertEqual(excluded, 1)

    def test_brace_template_slot_citation_is_excluded_not_reported(self):
        self.fixture.write_corpus_file(
            "doc.md", "See docs/plans/YYYY-MM-DD-{topic-slug}.md for the family.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])
        self.assertEqual(excluded, 1)

    def test_run_stem_template_citation_is_excluded_not_reported(self):
        self.fixture.write_corpus_file(
            "doc.md", "See docs/research/{run-stem}-gap-report.md for the family.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])
        self.assertEqual(excluded, 1)

    def test_illustrative_exclusion_is_reported_in_summary_not_silent(self):
        self.fixture.write_corpus_file("doc.md", "See docs/plans/*.md.\n")
        tree_args = [f"--tree={name}={path}" for name, path in self.fixture.tree_roots.items()]
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
            *tree_args,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = _module.main(argv)
        self.assertEqual(code, 0)
        self.assertIn("1 illustrative citation(s) excluded", buf.getvalue())

    def test_genuine_citation_alongside_illustrative_ones_still_resolves_correctly(self):
        # The real hit from the coordinator's report: research-sweep.md's
        # genuine (anchored) citation must survive next to illustrative
        # siblings and not be excluded as illustrative itself.
        self.fixture.write_corpus_file(
            "research-sweep.md",
            "Template: docs/research/YYYY-MM-DD-{topic-slug}-nlm.md\n"
            "Real: coordinator/docs/wiki/only-in-coordinator.md\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(excluded, 1)


class ConsumerRootModeTests(unittest.TestCase):
    """--consumer-root answers 'does this citation resolve from where the
    agent actually stands', not 'does it resolve given the whole map'."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_citation_resolving_only_in_doe_is_reported_dead_from_consumer(self):
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/only-in-coordinator.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("only-in-coordinator.md", findings[0])
        self.assertIn("dead-from-consumer", findings[0])
        self.assertIn("doe_coordinator", findings[0])

    def test_citation_that_resolves_under_consumer_root_as_written_passes(self):
        # only-in-claude-klabauter.md exists at the literal path under claude-klabauter itself.
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/only-in-claude-klabauter.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_citation_dead_everywhere_is_not_reported_as_dead_from_consumer(self):
        # A genuine dangler (dead in DoE too) is not this bucket's concern —
        # find_dead_from_consumer only reports what's ALIVE in DoE.
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/does-not-exist-anywhere.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_illustrative_forms_are_excluded_in_consumer_mode_too(self):
        self.fixture.write_corpus_file("doc.md", "See docs/plans/*.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(excluded, 1)

    def test_anchored_plugin_root_citation_is_never_dead_from_consumer(self):
        # ${CLAUDE_PLUGIN_ROOT}/ is harness-expanded, cwd-independent by
        # construction -- the consumer-cwd question this mode asks does not
        # apply to it, even though its target exists only in a DoE tree and
        # is absent under consumer_root (self.fixture.claude-klabauter) verbatim.
        self.fixture.write_corpus_file(
            "doc.md", "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/only-in-coordinator.md.\n"
        )
        tree_roots = dict(self.fixture.tree_roots)
        tree_roots["plugin_root"] = self.fixture.doe_coordinator
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_unanchored_citation_alongside_anchored_one_is_still_reported(self):
        # Same fixture, but pairs the exempt anchored citation with a bare
        # citation resolving only in DoE -- the anchored one must not
        # suppress the genuine finding for its unanchored neighbor.
        self.fixture.write_corpus_file(
            "doc.md",
            "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/only-in-coordinator.md "
            "and coordinator/docs/wiki/only-in-coordinator.md.\n",
        )
        tree_roots = dict(self.fixture.tree_roots)
        tree_roots["plugin_root"] = self.fixture.doe_coordinator
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("coordinator/docs/wiki/only-in-coordinator.md", findings[0])
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", findings[0])


class DefaultTreeResolutionFailureTests(unittest.TestCase):
    """P1: a default tree (doe_root / doe_coordinator) that fails to resolve
    must never be silently dropped from the candidate set — that degraded
    mode is exactly the failure this lint exists to catch, reproduced
    inside the lint itself. Only the default-tree resolution path is
    exercised here (use_default_trees=True); everything else stays
    fixtured, and `_resolve_repo_path_shortname` is monkeypatched so no
    subprocess is actually spawned in this test."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)
        self._orig_resolver = _module._resolve_repo_path_shortname
        self.addCleanup(setattr, _module, "_resolve_repo_path_shortname", self._orig_resolver)

    def _fail_every_shortname(self, shortname):
        return "", f"resolve-repo-path.py exited 1: unregistered shortname '{shortname}'"

    def test_failed_default_tree_forces_nonzero_even_with_zero_citations(self):
        _module._resolve_repo_path_shortname = self._fail_every_shortname
        self.fixture.write_corpus_file("doc.md", "No citations here.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], {}, use_default_trees=True
        )
        self.assertEqual(code, 1)
        self.assertGreaterEqual(unresolved, 1)
        self.assertTrue(any("unresolved" in line for line in findings), msg=findings)

    def test_failed_default_tree_names_the_tree_and_surfaces_the_reason(self):
        _module._resolve_repo_path_shortname = self._fail_every_shortname
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], {}, use_default_trees=True
        )
        self.assertTrue(any("doe_root" in line for line in findings), msg=findings)
        self.assertTrue(any("unregistered shortname" in line for line in findings), msg=findings)

    def test_tree_override_fills_the_gap_and_suppresses_the_failure(self):
        _module._resolve_repo_path_shortname = self._fail_every_shortname
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            {
                "doe_root": self.fixture.doe_root,
                "doe_coordinator": self.fixture.doe_coordinator,
                "plugin_root": self.fixture.doe_coordinator,
            },
            use_default_trees=True,
        )
        self.assertEqual(unresolved, 0, msg=findings)

    def test_healthy_default_resolution_reports_zero_unresolved(self):
        _module._resolve_repo_path_shortname = lambda shortname: (self.fixture.doe_root, "")
        self.fixture.write_corpus_file("doc.md", "No citations here.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], {}, use_default_trees=True
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(unresolved, 0)


class ConfigErrorTests(unittest.TestCase):
    """P2: --no-default-trees with zero --tree flags configures nothing to
    scan against — a usage error (exit 2), not "every citation failed."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_no_default_trees_with_no_tree_override_exits_2(self):
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/only-in-claude-klabauter.md.\n")
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
        ]
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            code = _module.main(argv)
        self.assertEqual(code, 2)
        self.assertNotIn("citation(s) failed to resolve unambiguously", buf.getvalue())

    def test_no_default_trees_with_a_tree_override_does_not_hit_the_config_error(self):
        # This test's contract is narrowly "the config-error exit (2) is
        # avoided when a --tree override is supplied" -- it does not assert
        # full citation resolution, since the corpus's bare citation is now
        # separately flagged unanchored (exit 1) by the anchoring rule.
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/only-in-claude-klabauter.md.\n")
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
            f"--tree=claude-klabauter={self.fixture.claude_klabauter}",
        ]
        code = _module.main(argv)
        self.assertNotEqual(code, 2)


class ConsumerModeDotDotTraversalTests(unittest.TestCase):
    """Finding 4: a `../../`-prefixed citation must never perform a real
    filesystem traversal out of consumer_root when checked for literal
    existence — the census counted 5 citations of this shape, so it is a
    real corpus shape, not a hypothetical."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_dotdot_citation_does_not_silently_pass_via_traversal_outside_consumer_root(self):
        # Plant a decoy two levels above claude-klabauter's own root at exactly the
        # path a naive os.path.join(consumer_root, "../../docs/wiki/x.md")
        # traversal would land on. If the fix regresses, this decoy makes
        # the citation silently resolve as "correct as written" instead of
        # being checked against the DoE trees.
        decoy_root = os.path.dirname(os.path.dirname(self.fixture.claude_klabauter))
        _write(os.path.join(decoy_root, "docs", "wiki", "only-in-coordinator.md"))
        self.fixture.write_corpus_file(
            "doc.md", "See ../../docs/wiki/only-in-coordinator.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        # only-in-coordinator.md genuinely exists only in doe_coordinator —
        # the decoy must not cause this to be silently treated as resolved.
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("only-in-coordinator.md", findings[0])
        self.assertIn("dead-from-consumer", findings[0])

    def test_dotdot_citation_that_genuinely_only_lives_in_doe_is_still_reported(self):
        self.fixture.write_corpus_file(
            "doc.md", "See ../../docs/wiki/only-in-coordinator.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus],
            self.fixture.tree_roots,
            use_default_trees=False,
            consumer_root=self.fixture.claude_klabauter,
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertIn("dead-from-consumer", findings[0])


class UnscannableCorpusPathTests(unittest.TestCase):
    """A `--corpus` path that does not exist, or that names a file rather
    than a directory, must refuse (exit 2) rather than reach the scan as an
    empty contribution.

    Negative-spec: `os.walk()` yields nothing and raises nothing for either
    shape, so without this refusal the tool reports a clean corpus it never
    opened — the exact silent-skip this lint exists to catch, committed by
    the lint itself. Found by pointing `--corpus` at a single file by
    accident, not by the code review that was specifically looking for this
    class of defect.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_missing_corpus_dir_exits_2_and_names_the_path(self):
        missing = os.path.join(self._tmpdir.name, "does-not-exist-corpus")
        code, findings, excluded, unresolved = _module.run(
            [missing], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 2)
        self.assertTrue(
            any("does not exist" in line and repr(missing) in line for line in findings),
            msg=findings,
        )

    def test_corpus_path_naming_a_file_exits_2(self):
        file_path = os.path.join(self._tmpdir.name, "not-a-directory.md")
        _write(file_path)
        code, findings, excluded, unresolved = _module.run(
            [file_path], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 2)
        self.assertTrue(
            any(
                "is a file, not a directory" in line and repr(file_path) in line
                for line in findings
            ),
            msg=findings,
        )

    def test_one_bad_corpus_path_refuses_even_alongside_a_good_one(self):
        missing = os.path.join(self._tmpdir.name, "does-not-exist-corpus")
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/only-in-claude-klabauter.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus, missing], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 2, msg=findings)
        self.assertTrue(any("does not exist" in line for line in findings), msg=findings)
        self.assertEqual(excluded, 0, msg="a partial scan must not report a result")


class PluginRootAnchorTests(unittest.TestCase):
    """${CLAUDE_PLUGIN_ROOT}/ is a first-class anchor, resolved against a
    dedicated `plugin_root` tree -- never mis-parsed as a leading-slash-
    absolute citation, and never assumed to equal the repo root."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_anchored_plugin_root_citation_passes(self):
        self.fixture.write_corpus_file(
            "doc.md", "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/only-in-coordinator.md.\n"
        )
        tree_roots = dict(self.fixture.tree_roots)
        tree_roots["plugin_root"] = self.fixture.doe_coordinator
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_plugin_root_anchor_is_never_reported_as_leading_slash_absolute(self):
        # The false-positive class this fix corrects: the `}` boundary must
        # never be mis-captured as a bare `/` prefix that then resolves
        # (or fails to) against the wrong, unmapped bucket.
        self.fixture.write_corpus_file(
            "doc.md", "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/only-in-coordinator.md.\n"
        )
        # Deliberately omit a plugin_root tree entry -- the citation is
        # unresolvable, but it must be reported against the `plugin_root`
        # tree name specifically, never left ambiguous against unrelated
        # trees the way a mis-parsed bare "/" prefix would be.
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("unresolvable", findings[0])
        self.assertNotIn("ambiguous", findings[0])
        self.assertIn("only-in-coordinator.md", findings[0])

    def test_plugin_root_need_not_equal_repo_root(self):
        # A plugin root that is a subdirectory distinct from any registered
        # repo root (e.g. an install dir under a marketplace source) must
        # still resolve correctly -- plugin root is never assumed == repo root.
        install_dir = os.path.join(self._tmpdir.name, "some-marketplace-install", "plugin")
        os.makedirs(os.path.join(install_dir, "docs", "wiki"), exist_ok=True)
        with open(os.path.join(install_dir, "docs", "wiki", "installed-only.md"), "w", encoding="utf-8") as fh:
            fh.write("placeholder\n")
        self.fixture.write_corpus_file(
            "doc.md", "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/installed-only.md.\n"
        )
        tree_roots = dict(self.fixture.tree_roots)
        tree_roots["plugin_root"] = install_dir
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertNotEqual(install_dir, self.fixture.tmp)

    def test_cli_plugin_root_flag_resolves_the_anchor(self):
        self.fixture.write_corpus_file(
            "doc.md", "See ${CLAUDE_PLUGIN_ROOT}/docs/wiki/only-in-coordinator.md.\n"
        )
        tree_args = [
            f"--tree={name}={path}"
            for name, path in self.fixture.tree_roots.items()
        ]
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
            *tree_args,
            f"--plugin-root={self.fixture.doe_coordinator}",
        ]
        code = _module.main(argv)
        self.assertEqual(code, 0)


class TestFixtureTreeExclusionTests(unittest.TestCase):
    """A path under tests/fixtures/ is an artifact read as an oracle, not a
    doctrine citation a session is meant to follow -- excluded from the
    scanned corpus entirely, distinct from tests/ prose more broadly."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_citation_inside_tests_fixtures_is_not_scanned(self):
        self.fixture.write_corpus_file(
            os.path.join("tests", "fixtures", "expected-manifest.md"),
            "See docs/wiki/does-not-exist-anywhere.md.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])
        self.assertEqual(excluded, 0)

    def test_citation_inside_tests_but_not_fixtures_is_still_scanned(self):
        # Narrower than excluding all of tests/: a genuine doctrine citation
        # in test prose (not under a fixtures/ subdir) is still caught.
        self.fixture.write_corpus_file(
            os.path.join("tests", "some_test_doc.md"),
            "See docs/wiki/does-not-exist-anywhere.md.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)

    def test_nested_path_under_tests_fixtures_is_not_scanned(self):
        self.fixture.write_corpus_file(
            os.path.join("tests", "fixtures", "lesson-triage", "expected-manifest.md"),
            "See docs/wiki/does-not-exist-anywhere.md.\n",
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])


class TemplateCorpusWideningTests(unittest.TestCase):
    """`_iter_corpus_files` must scan `*.template` files (README.md.template
    and siblings) alongside `*.md` -- a citation written verbatim into a
    consumer repo's README at repo-setup time must not be invisible to this
    lint solely because of its file extension."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)

    def test_dot_template_file_is_scanned(self):
        self.fixture.write_corpus_file(
            "README.md.template", "See docs/wiki/does-not-exist-anywhere.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("does-not-exist-anywhere.md", findings[0])

    def test_non_template_non_md_file_is_still_ignored(self):
        self.fixture.write_corpus_file(
            "notes.txt", "See docs/wiki/does-not-exist-anywhere.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])


class RepoRelativeNonDocsCitationTests(unittest.TestCase):
    """`snippets/`, `pipelines/`, and `templates/` citations are the same
    defect as a bare `docs/wiki/` one and were invisible to this lint
    (2026-08-30, doe-claude-em memo `doctrine-citation-form-is-claude-plugin-
    root`): both forms resolve only with cwd = the citing repo's root. The
    core pattern was `docs/<section>/...` only, so a repo-relative citation
    prefixed with the citing repo's own top-level directory name -- 40 sites
    in DoE-claude's `coordinator/{commands,skills,agents}` corpus at the time
    of the memo -- passed a scan that reported clean without ever having
    matched them."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)
        _write(os.path.join(self.fixture.doe_coordinator, "snippets", "resolve-coordinator-bin.md"))
        _write(os.path.join(self.fixture.doe_coordinator, "pipelines", "bug-sweep", "chunk.md"))
        _write(os.path.join(self.fixture.doe_coordinator, "templates", "onboarding.md"))

    def _run(self):
        return _module.run([self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False)

    def test_bare_snippets_citation_is_reported_unanchored(self):
        self.fixture.write_corpus_file("doc.md", "See snippets/resolve-coordinator-bin.md.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("snippets/resolve-coordinator-bin.md", findings[0])
        self.assertIn("unanchored", findings[0])

    def test_repo_relative_snippets_citation_resolves_against_its_named_tree(self):
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/snippets/resolve-coordinator-bin.md.\n"
        )
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 0, msg=findings)

    def test_repo_relative_snippets_citation_missing_in_its_tree_is_unresolvable(self):
        self.fixture.write_corpus_file("doc.md", "See coordinator/snippets/absent.md.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(len(findings), 1)
        self.assertIn("snippets/absent.md", findings[0])
        self.assertIn("unresolvable", findings[0])

    def test_nested_pipelines_citation_is_matched(self):
        self.fixture.write_corpus_file("doc.md", "See pipelines/bug-sweep/chunk.md.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("pipelines/bug-sweep/chunk.md", findings[0])

    def test_templates_citation_is_matched(self):
        self.fixture.write_corpus_file("doc.md", "See coordinator/templates/onboarding.md.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 0, msg=findings)

    def test_dot_md_followed_by_a_further_extension_is_not_a_citation(self):
        # `templates/<name>.md.tmpl` names a TEMPLATE, not the document --
        # without the extension boundary the core backtracks to `.md` and
        # reports a dead citation to a file nobody wrote (this fired live on
        # DoE-claude's commands/install.md:175 render-template invocation).
        self.fixture.write_corpus_file(
            "doc.md", "render-template coordinator/templates/onboarding.md.tmpl -o out\n"
        )
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_trailing_sentence_punctuation_still_ends_a_citation(self):
        # The extension boundary must reject `.md.tmpl` without also
        # rejecting an ordinary citation at the end of a sentence.
        self.fixture.write_corpus_file("doc.md", "See snippets/absent.md, then stop.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 1, msg=findings)
        self.assertEqual(len(findings), 1)
        self.assertIn("snippets/absent.md", findings[0])

    def test_unrelated_top_level_directory_is_not_matched(self):
        # Deliberately narrow: only the three doc-bearing directories the
        # memo named widen the core. `hooks/`, `bin/`, `state/` and friends
        # carry code paths, not doctrine citations, and matching them would
        # turn every incidental path mention into a finding.
        self.fixture.write_corpus_file("doc.md", "Edit hooks/scripts/does-not-exist.md.\n")
        code, findings, excluded, unresolved = self._run()
        self.assertEqual(code, 0, msg=findings)


class InProcessResolveRepoPathTests(unittest.TestCase):
    """`_resolve_repo_path_shortname` must resolve via an in-process import
    of resolve-repo-path.py -- never a `python <script>` subprocess -- and
    must still fail loud (named tree, non-zero exit, reason in output) when
    a shortname does not resolve."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.fixture = FixtureCorpus(self._tmpdir.name)
        self._orig_module = _module._RESOLVE_REPO_PATH_MODULE
        self.addCleanup(setattr, _module, "_RESOLVE_REPO_PATH_MODULE", self._orig_module)

    def test_no_subprocess_module_imported_by_this_file(self):
        with open(_MODULE_PATH, "r", encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("import subprocess", source)

    def test_loads_resolve_repo_path_module_in_process_not_via_subprocess(self):
        _module._RESOLVE_REPO_PATH_MODULE = None
        rrp = _module._load_resolve_repo_path_module()
        self.assertTrue(hasattr(rrp, "_resolve_registry_value"))
        self.assertIs(_module._load_resolve_repo_path_module(), rrp)

    def test_unregistered_shortname_still_fails_loud_via_in_process_call(self):
        _module._RESOLVE_REPO_PATH_MODULE = None
        rrp = _module._load_resolve_repo_path_module()
        rrp._resolve_registry_value = lambda key: ""
        resolved, err = _module._resolve_repo_path_shortname("definitely-unregistered-shortname")
        self.assertEqual(resolved, "")
        self.assertTrue(err, msg="a failed resolution must still name a reason")

    def test_healthy_resolution_returns_path_with_no_error(self):
        _module._RESOLVE_REPO_PATH_MODULE = None
        rrp = _module._load_resolve_repo_path_module()
        rrp._resolve_registry_value = lambda key: self.fixture.doe_root
        resolved, err = _module._resolve_repo_path_shortname("doe-claude")
        self.assertEqual(resolved, self.fixture.doe_root)
        self.assertEqual(err, "")


if __name__ == "__main__":
    unittest.main()
