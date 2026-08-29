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

    def test_uniquely_resolvable_bare_citation_passes(self):
        self.fixture.write_corpus_file(
            "doc.md", "See docs/wiki/only-in-claude-klabauter.md for detail.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)
        self.assertEqual(findings, [])

    def test_prefixed_citation_resolves_only_against_its_named_tree(self):
        self.fixture.write_corpus_file(
            "doc.md", "See coordinator/docs/wiki/only-in-coordinator.md.\n"
        )
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 0, msg=findings)

    def test_unresolvable_citation_exits_nonzero_and_names_it(self):
        self.fixture.write_corpus_file(
            "doc.md", "See docs/wiki/does-not-exist-anywhere.md.\n"
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

    def test_each_doe_vs_claude_klabauter_collision_is_reported_ambiguous(self):
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
                self.assertIn("ambiguous", findings[0])
                self.assertIn("doe_root", findings[0])
                self.assertIn("claude-klabauter", findings[0])

    def test_plans_index_collision_is_reported_ambiguous(self):
        self.fixture.write_corpus_file("doc.md", "See docs/plans/INDEX.md.\n")
        code, findings, excluded, unresolved = _module.run(
            [self.fixture.corpus], self.fixture.tree_roots, use_default_trees=False
        )
        self.assertEqual(code, 1)
        self.assertIn("ambiguous", findings[0])

    def test_each_intra_doe_collision_is_reported_ambiguous(self):
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
                self.assertIn("ambiguous", findings[0])
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
        # genuine citation must survive next to illustrative siblings.
        self.fixture.write_corpus_file(
            "research-sweep.md",
            "Template: docs/research/YYYY-MM-DD-{topic-slug}-nlm.md\n"
            "Real: docs/wiki/only-in-claude-klabauter.md\n",
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
            {"doe_root": self.fixture.doe_root, "doe_coordinator": self.fixture.doe_coordinator},
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
        self.fixture.write_corpus_file("doc.md", "See docs/wiki/only-in-claude-klabauter.md.\n")
        argv = [
            "check-doctrine-citations.py",
            "--corpus",
            self.fixture.corpus,
            "--no-default-trees",
            f"--tree=claude-klabauter={self.fixture.claude_klabauter}",
        ]
        code = _module.main(argv)
        self.assertEqual(code, 0)


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


if __name__ == "__main__":
    unittest.main()
