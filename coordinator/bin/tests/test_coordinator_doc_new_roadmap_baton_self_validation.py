"""test_coordinator_doc_new_roadmap_baton_self_validation.py -- coverage for
break-class defects in ``coordinator-doc-new``'s roadmap-baton path, fixed
together (2026-08-01):

1. ``--type roadmap-baton`` invoked with no ``--roadmap-id``/``--stub-id``
   used to fall back to uppercase literals (``PLACEHOLDER-RM`` /
   ``PLACEHOLDER-stub-1``) that FAIL the CLI's own ``_SLUG_RE``
   (``^[a-z0-9][a-z0-9-]*$``) -- a slug shape the CLI enforces on every
   EXPLICITLY-supplied ``--roadmap-id``/``--stub-id``, but never checked
   against its own fallback. The fallbacks are now lowercase
   (``placeholder-rm`` / ``placeholder-stub-1``), so the no-flags path mints
   a baton whose graph fields are themselves valid slugs (still pointing at
   no real roadmap cluster -- that is expected, not this fix's remit).

2. The emitter never validated its own generated content against this
   repo's vendored schema corpus before writing -- an unguarded
   ``open(...).write(content)``. ``_assert_scaffold_content_valid`` (new)
   now runs immediately before that write and hard-fails (non-zero exit,
   nothing written) when the generated frontmatter fails the schema its own
   `kind`/path resolves to.

3. AC13 (docs/plans/2026-08-01-baton-spine-information-integrity.md § A5):
   roadmap-baton batons minted from ``state/roadmap/<id>/`` carried
   ``stub_id``/``deliverable_id`` but NO ``handoff_id`` at all -- a distinct
   class of record any fleet-side ``handoff_id`` join would silently miss
   entirely. ``roadmap-baton`` is now in the minting doc_type tuple
   (alongside handoff/spinoff/recovery/goal-seed/roadmap-seed) and
   ``_scaffold_roadmap_baton`` accepts/emits ``handoff_id`` like every other
   handoff-family scaffold (optional-omit convention).

Loaded by file path (``importlib.machinery.SourceFileLoader``) since
``coordinator-doc-new`` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

Spec backlink: cross-repo memo
2026-08-01-doe-claude-em-roadmap-baton-write-guard-warns-where-claim-gate-denies.md
Spec backlink (AC13): DoE-claude:pln-baton-spine-information-integr-d3e1d7 § A5
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from coordinator_core.win_portability import no_console_creationflags

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_roadmap_baton_self_validation_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_roadmap_baton_self_validation_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


@contextlib.contextmanager
def _tmp_git_repo():
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "testrepo"
        repo.mkdir()
        _init_git_repo(repo)
        out_path = repo / "custom-out.md"
        yield repo, out_path


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, **_NO_CONSOLE)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.email=test@test", "-c", "user.name=Test",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        capture_output=True,
        **_NO_CONSOLE,
    )


class RoadmapBatonNoFlagsFallbackTest(unittest.TestCase):
    """Defect 1: the no-flags fallback literals must themselves be valid
    slugs against the CLI's own `_SLUG_RE`."""

    def test_no_flags_fallback_literals_are_valid_slugs(self):
        content = _cli._scaffold_roadmap_baton(
            title="t",
            branch="b",
            roadmap_id="placeholder-rm",
            stub_id="placeholder-stub-1",
            deliverable_id=None,
            initiative=None,
            category=None,
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("roadmap_id"), "placeholder-rm")
        self.assertEqual(fields.get("stub_id"), "placeholder-stub-1")
        self.assertTrue(_cli._SLUG_RE.match(fields["roadmap_id"]))
        self.assertTrue(_cli._SLUG_RE.match(fields["stub_id"]))

    def test_full_cli_no_flags_roadmap_baton_mints_valid_slugs(self):
        """End-to-end: invoking the real CLI surface with no --roadmap-id/
        --stub-id must produce the same lowercase, _SLUG_RE-conformant
        fallback -- not just the internal scaffolder helper."""
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                # --no-sizing-object is not a "flag" in this test's sense: roadmap-baton
                # is held to the same explicit-sizing-answer bar as --type plan, so it
                # is a floor for every invocation. The slug fallback under test is the
                # --roadmap-id/--stub-id absence, which is unchanged.
                [
                    sys.executable, str(_CLI_PATH), "--type", "roadmap-baton",
                    "--no-sizing-object", "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertTrue(_cli._SLUG_RE.match(fields["roadmap_id"]), fields["roadmap_id"])
            self.assertTrue(_cli._SLUG_RE.match(fields["stub_id"]), fields["stub_id"])
            self.assertNotEqual(fields["roadmap_id"], "PLACEHOLDER-RM")
            self.assertNotEqual(fields["stub_id"], "PLACEHOLDER-stub-1")


class ScaffoldSelfValidationTest(unittest.TestCase):
    """Defect 2: `_assert_scaffold_content_valid` must hard-fail (SystemExit,
    non-zero) when generated content fails the schema its own `kind`/path
    resolves to, and must be a no-op for content this repo's ~14-schema
    corpus has no schema for."""

    def test_rejects_content_failing_its_own_schema(self):
        """A `kind: roadmap-baton` document missing every required field
        (title/created/branch/status/predecessor, plus the roadmap-baton
        cross-field graph set) must be refused -- constructed honestly via
        a minimal hand-built frontmatter block, not through the CLI's own
        scaffolder (which cannot itself be made to omit these -- see module
        docstring)."""
        content = "---\nkind: roadmap-baton\n---\n\nbody\n"
        with tempfile.TemporaryDirectory() as td:
            repo_root = str(Path(td))
            out_path = str(Path(td) / "state" / "handoffs" / "invalid.md")
            with self.assertRaises(SystemExit):
                _cli._assert_scaffold_content_valid(content, out_path, repo_root)

    def test_valid_handoff_scaffold_passes_self_validation(self):
        """Non-regression: a real, conformant scaffold from
        `_scaffold_handoff` must NOT trip the new self-check."""
        content = _cli._scaffold_handoff(title="t", branch="b")
        with tempfile.TemporaryDirectory() as td:
            repo_root = str(Path(td))
            out_path = str(Path(td) / "state" / "handoffs" / "valid.md")
            # Must not raise.
            _cli._assert_scaffold_content_valid(content, out_path, repo_root)

    def test_no_schema_doc_type_is_a_noop(self):
        """A doc type with no schema in claude-klabauter's own vendored corpus (e.g.
        `memo`, whose out_path is a bare `<date>-<slug>.md` at repo root --
        no schema's `applies_to` glob matches an extensionless-directory
        path, and no `memo.schema.json` exists in the corpus at all) must
        not be blocked by this self-check -- scoped to doc types WITH a
        schema, not weakened into a warning for the rest.

        NOTE (re-verify on every schema-corpus vendor): `plan` was this
        test's original example until commit cb35ee4b1 vendored
        `plan.schema.json` (re-vendoring `grouping_approvals`), silently
        invalidating the example without failing until the next full-file
        run -- this test's failure mode is exactly that kind of silent
        staleness. Before reusing/adding an example here, confirm BOTH (a)
        no `<doc_type>.schema.json` exists under
        `coordinator_core/frontmatter/schemas/`, and (b) no existing
        schema's `applies_to` glob matches the chosen `out_path`."""
        content = "---\ntitle: t\n---\n\nbody\n"
        with tempfile.TemporaryDirectory() as td:
            repo_root = str(Path(td))
            out_path = str(Path(td) / "2026-08-01-example.md")
            # Must not raise -- no schema in the corpus matches this shape.
            _cli._assert_scaffold_content_valid(content, out_path, repo_root)


class RoadmapBatonMintsHandoffIdTest(unittest.TestCase):
    """AC13 (docs/plans/2026-08-01-baton-spine-information-integrity.md § A5):
    minted roadmap batons must carry `handoff_id`, matching every other
    handoff-family doc_type -- this was the reported break-class defect
    (neither of example-market-data-repo's roadmap batons carries `handoff_id` at
    all; batons minted from `state/roadmap/<id>/` got `stub_id` +
    `deliverable_id` instead)."""

    def test_scaffolder_emits_handoff_id_when_supplied(self):
        content = _cli._scaffold_roadmap_baton(
            title="t",
            branch="b",
            roadmap_id="rm-1",
            stub_id="rm-1-01",
            deliverable_id=None,
            initiative=None,
            category=None,
            handoff_id="hnd-t-abc123",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("handoff_id"), "hnd-t-abc123")

    def test_scaffolder_omits_handoff_id_when_not_supplied(self):
        """Optional-omit convention (not `present_as_null`): an absent
        handoff_id is OMITTED entirely, not written as `handoff_id: null`,
        matching spinoff/goal-seed/roadmap-seed's existing convention."""
        content = _cli._scaffold_roadmap_baton(
            title="t",
            branch="b",
            roadmap_id="rm-1",
            stub_id="rm-1-01",
            deliverable_id=None,
            initiative=None,
            category=None,
        )
        self.assertNotIn("handoff_id", content)

    def test_full_cli_roadmap_baton_mints_handoff_id(self):
        """End-to-end: the real CLI surface (no explicit handoff_id flag
        exists -- it is always minted fresh) must produce a `hnd-<slug>-
        <6hex>`-shaped `handoff_id` on a freshly-scaffolded roadmap-baton."""
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "roadmap-baton",
                    "--title", "AC13 roadmap baton", "--roadmap-id", "rm-ac13",
                    "--stub-id", "rm-ac13-01", "--no-sizing-object", "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertIsNotNone(fields.get("handoff_id"))
            self.assertRegex(fields["handoff_id"], r"^hnd-[a-z0-9-]+-[0-9a-f]{6}$")


class RoadmapBatonBackfillClassRegressionTest(unittest.TestCase):
    """AC13 regression: confirm the `handoff_id` backfill mechanism (the
    same `coordinator_core.frontmatter.primitives` anchored-insert seam
    `coordinator/lib/backfill-handoff-id.py` mutates through) correctly
    covers the ROADMAP-BATON CLASS of record, not merely the handoff/spinoff
    shapes it was first exercised against.

    Fixture text below mirrors the two live example-market-data-repo roadmap
    batons' shape (kind: roadmap-baton, stub_id + deliverable_id present,
    handoff_id absent, predecessor: none as the anchor field) -- an
    equivalent fixture, per this chunk's brief, since editing another repo's
    files from here is out of scope."""

    _FIXTURE = (
        "---\n"
        'title: "C5 slice-5 analytic theme extraction (KR2)"\n'
        "created: 2026-07-14\n"
        'branch: "work/machine-b/2026-07-10to13"\n'
        "status: open\n"
        "predecessor: none\n"
        "kind: roadmap-baton\n"
        'roadmap_id: "q4-sentiment"\n'
        'stub_id: "qsent-05"\n'
        "sprint: 2\n"
        "wave: 1\n"
        "category: roadmap\n"
        'deliverable_id: "dlv-qsent-05"\n'
        "---\n"
        "\n"
        "# C5 slice-5 analytic theme extraction (KR2)\n"
    )

    def test_fixture_has_no_handoff_id_before_backfill(self):
        """Sanity check on the fixture itself: mirrors the reported defect
        exactly (stub_id/deliverable_id present, handoff_id absent)."""
        from coordinator_core.frontmatter.primitives import (
            read_fm_field,
            read_fm_field_unquoted,
            split_frontmatter,
        )

        split = split_frontmatter(self._FIXTURE)
        self.assertIsNotNone(split)
        self.assertIsNone(read_fm_field(split.fm_text, "handoff_id"))
        self.assertEqual(read_fm_field_unquoted(split.fm_text, "stub_id"), "qsent-05")
        self.assertEqual(read_fm_field_unquoted(split.fm_text, "deliverable_id"), "dlv-qsent-05")

    def test_backfill_primitives_cover_the_roadmap_baton_class(self):
        """The anchored-insert seam (insert `handoff_id` after `predecessor:`)
        works identically on a `kind: roadmap-baton` file as on any other
        handoff-family kind -- proving the backfill mechanism is kind-
        agnostic and therefore already covers this whole record class once
        pointed at it, closing the "entire class missed" blast-radius the
        sender flagged."""
        from coordinator_core.frontmatter.primitives import (
            insert_fm_field,
            read_fm_field_unquoted,
            rebuild,
            split_frontmatter,
        )

        split = split_frontmatter(self._FIXTURE)
        self.assertIsNotNone(split)
        new_fm = insert_fm_field(
            split.fm_text, "handoff_id", "hnd-qsent-05-abc123", after_key="predecessor"
        )
        new_text = rebuild(split, new_fm)

        resplit = split_frontmatter(new_text)
        self.assertIsNotNone(resplit)
        self.assertEqual(
            read_fm_field_unquoted(resplit.fm_text, "handoff_id"), "hnd-qsent-05-abc123"
        )
        # Every other field (including the roadmap-baton-specific graph
        # fields stub_id/deliverable_id/roadmap_id) is untouched.
        self.assertEqual(read_fm_field_unquoted(resplit.fm_text, "kind"), "roadmap-baton")
        self.assertEqual(read_fm_field_unquoted(resplit.fm_text, "stub_id"), "qsent-05")
        self.assertEqual(read_fm_field_unquoted(resplit.fm_text, "roadmap_id"), "q4-sentiment")
        self.assertEqual(
            read_fm_field_unquoted(resplit.fm_text, "deliverable_id"), "dlv-qsent-05"
        )


class RoadmapBatonBlocksCarryTest(unittest.TestCase):
    """`--blocks` (repeatable): the one graph field carried rather than stubbed.

    A continuation minted under its predecessor's `stub_id` used to author
    `blocks: []` while the whole down-graph still resolved on that `stub_id`, so
    `reconcile.gate_eval._has_asymmetry` read every dependent's edge as severed
    and reported a symmetric graph as a data defect. A lost down-edge is not a
    placeholder awaiting fill -- nothing surfaces that it went missing.
    """

    def _fm(self, **kwargs):
        content = _cli._scaffold_roadmap_baton(
            title="t",
            branch="b",
            roadmap_id="rm-blocks",
            stub_id="stub-blocks",
            deliverable_id=None,
            initiative=None,
            category=None,
            **kwargs,
        )
        return yaml.safe_load(content.split("---", 2)[1])

    def test_blocks_are_emitted_verbatim_in_order(self):
        fm = self._fm(blocks=["the-meter-02", "archival-sweeps-03"])
        self.assertEqual(fm["blocks"], ["the-meter-02", "archival-sweeps-03"])

    def test_omitted_blocks_stay_the_empty_list(self):
        """Byte-identical to every caller that does not pass the flag."""
        self.assertEqual(self._fm()["blocks"], [])

    def test_blank_entries_are_dropped_never_emitted_as_empty_strings(self):
        """An empty entry would author a `blocks` member nothing can resolve --
        a dangling edge is worse than the missing one this flag exists to fix."""
        fm = self._fm(blocks=["dep-01", "   ", ""])
        self.assertEqual(fm["blocks"], ["dep-01"])

    def test_carried_blocks_still_validate_against_the_schema(self):
        """The emitter validates its own output before writing (Defect 2 above);
        the carried list must not be what breaks that."""
        content = _cli._scaffold_roadmap_baton(
            title="t",
            branch="b",
            roadmap_id="rm-blocks",
            stub_id="stub-blocks",
            deliverable_id=None,
            initiative=None,
            category=None,
            blocks=["dep-01", "dep-02"],
        )
        _cli._assert_scaffold_content_valid(content, "roadmap-baton", "state/handoffs/x.md")


if __name__ == "__main__":
    unittest.main()
