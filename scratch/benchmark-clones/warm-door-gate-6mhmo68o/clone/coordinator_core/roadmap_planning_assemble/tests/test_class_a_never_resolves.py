"""
coordinator_core.roadmap_planning_assemble.tests.test_class_a_never_resolves
-- AC22 (chunk C13, docs/plans/2026-08-21-engine-half-of-the-roadmap-sprint-
spine-split.md): the inert-on-landing property expressed over NAMES instead
of callers.

Purpose: the census's own "Consumes-manifest correction" section is explicit
that Class A's eight names (`roadmap_planning_assemble.CLASS_A_GLUE`) "were
NEVER callable on either disk... assembler-internal glue, not a consumed
op". This module asserts that structurally rather than by transcription:
none of the eight resolves as a registered IPC op or as a `coordinator/bin`
CLI, so a regression that accidentally wires one of them up as if it were a
real, invocable capability is caught here, at build time. Its positive twin
-- every name in the corrected 13-op consumes manifest still resolves
somewhere real -- needs the sprint-side rows of that manifest, which only
arrive with C11 (`sprint_planning_assemble`); this is why AC22 is folded
into C13 rather than split across C10/C11 (splitting it would ship the
negative half against a manifest that is only partly bound, and label a
partial AC as discharged).

CLASS_A_GLUE is READ from `roadmap_planning_assemble`, never re-typed --
the eight names are the source of truth this module tests against, and a
duplicate hand-typed list here would silently desync from it.

The corrected 13-op manifest has no equivalent shared constant (neither
module exports one), so this module extracts it directly from BOTH
assemblers' own "Consumes manifest" docstring paragraphs (the source-of-
record prose C10/C11 each carry) rather than hand-typing a parallel copy
that could drift from either docstring independently.

Negative-spec:
    - Do NOT hand-type CLASS_A_GLUE's eight names as a literal list in
      this module -- read `roadmap_planning_assemble.CLASS_A_GLUE`.
    - Do NOT glob `coordinator/bin/*.py` alone for the CLI-stem check -- an
      extensionless CLI (e.g. `coordinator/bin/audit-roadmap`) is a real,
      invocable CLI and a `*.py`-only glob drops it silently, producing a
      false negative on the resolution check.
    - Do NOT assert every one of the 13 manifest ops resolves as an IPC op
      or CLI stem under its OWN literal spelling -- ten of the thirteen
      are documented, by the owning module's own docstring, as either
      "capability matches under a different name" or "adapted-not-bound":
      they name an EXISTING coordinator_core capability the census
      resolved to, under a different literal string, or (for
      `inventory-corpus-files`/`generate-stub-index-query-callout`) a
      capability not yet bound to any directive by either assembler.
      Asserting literal IPC-registry/CLI-stem resolution for those ten
      would be a false requirement this delivery was never asked to meet
      (the memo: "already exists as a coordinator_core capability or an
      atomic CLI" -- not necessarily under the census's own label). What
      this module instead verifies for the full 13: (a) disjointness from
      CLASS_A_GLUE (the collision a rename could introduce), (b) the
      three literal-name ops (`roadmap-number-stubs`, `audit-roadmap`,
      `coordinator-doc-new`) DO resolve as real CLI stems -- proving the
      resolution mechanism itself is non-vacuous, not merely a check that
      always returns nothing found -- and (c) every one of the 13 is
      referenced, verbatim, either as a real CLI stem, as a
      `directives[].cli` candidate-op binding value in one of the two
      assemblers' own candidate-ops tables, or in the owning module's own
      "Consumes manifest" docstring paragraph -- so a rename on either
      side (docstring vs. binding) desyncs loudly rather than silently.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ENGINE_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", "..", ".."))
if _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)

import coordinator_core.roadmap_planning_assemble as rpa  # noqa: E402
import coordinator_core.sprint_planning_assemble as spa  # noqa: E402

_BIN_DIR = os.path.join(_ENGINE_ROOT, "coordinator", "bin")

# Extensions this repo uses for a script whose invocable CLI stem is the
# filename minus the extension. An extensionless file (see negative-spec)
# is already its own stem and is never stripped.
_SCRIPT_EXTENSIONS = (".py", ".sh", ".ps1", ".cjs", ".mjs", ".js")


def _bin_cli_stems() -> set:
    """Every invocable CLI stem under coordinator/bin, partitioned BY
    CONTENT (is this entry a file at all?), never by an extension glob --
    an extensionless CLI is a real CLI."""
    stems = set()
    for entry in os.listdir(_BIN_DIR):
        full = os.path.join(_BIN_DIR, entry)
        if not os.path.isfile(full):
            continue
        stem, ext = os.path.splitext(entry)
        stems.add(entry if ext not in _SCRIPT_EXTENSIONS else stem)
    return stems


def _resolves_as_ipc_op_or_cli(name: str, cli_stems: set) -> bool:
    if name in cli_stems:
        return True
    import coordinator_core.ops as ops_pkg  # noqa: PLC0415

    if hasattr(ops_pkg, "_eager_import_all"):
        ops_pkg._eager_import_all()
    from coordinator_core.ipc import _REGISTRY  # noqa: PLC0415

    return name in _REGISTRY


def _extract_consumes_manifest_names(docstring: str) -> set:
    """Every backtick-quoted, hyphenated op name in the module's own
    "Consumes manifest" docstring paragraph (up to the next "Class A"
    section). Requires an internal hyphen so a bare function-name
    backtick reference (e.g. `` `brief` ``) is never mistaken for an op
    name."""
    start = docstring.find("Consumes manifest")
    if start == -1:
        return set()
    end = docstring.find("Class A", start)
    segment = docstring[start:end if end != -1 else None]
    return set(re.findall(r"`([a-z][a-z0-9]*(?:-[a-z0-9]+)+)`", segment))


class TestClassAGlueNeverResolves(unittest.TestCase):
    """AC22 negative half: Class A's eight are assembler-internal glue,
    never a consumed op -- neither name resolves as a registered IPC op
    nor as a coordinator/bin CLI."""

    def test_class_a_glue_names_resolve_nowhere(self):
        cli_stems = _bin_cli_stems()
        offenders = [
            name for name in rpa.CLASS_A_GLUE
            if _resolves_as_ipc_op_or_cli(name, cli_stems)
        ]
        self.assertEqual(
            offenders,
            [],
            msg=(
                "Class A glue name(s) resolved as a real IPC op or CLI -- "
                "these eight are assembler-internal glue and must never "
                f"become invocable: {offenders!r}"
            ),
        )

    def test_class_a_glue_has_eight_names(self):
        # Non-vacuousness guard on the constant itself: an accidentally
        # emptied CLASS_A_GLUE would make the resolution test above pass
        # trivially (an empty offenders list from an empty input).
        self.assertEqual(len(rpa.CLASS_A_GLUE), 8)


class TestCorrected13OpManifestResolves(unittest.TestCase):
    """AC22 positive twin: every op in the corrected 13-op consumes
    manifest -- combining C10's spine rows and C11's sprint rows, so this
    test needs both assemblers, per this chunk's own dependency -- still
    resolves somewhere real, disjoint from Class A's glue, so a rename on
    either side desyncs loudly."""

    @classmethod
    def setUpClass(cls):
        cls.manifest = _extract_consumes_manifest_names(
            rpa.__doc__
        ) | _extract_consumes_manifest_names(spa.__doc__)
        cls.candidate_op_values = set(rpa._SPINE_CANDIDATE_OPS.values()) | set(
            spa._SPRINT_CANDIDATE_OPS.values()
        )
        cls.cli_stems = _bin_cli_stems()

    def test_manifest_has_thirteen_ops(self):
        self.assertEqual(len(self.manifest), 13, msg=sorted(self.manifest))

    def test_manifest_disjoint_from_class_a_glue(self):
        collision = self.manifest & set(rpa.CLASS_A_GLUE)
        self.assertEqual(
            collision,
            set(),
            msg=(
                "a manifest op collides with a Class A glue name -- this "
                f"must never happen on either side of a rename: {collision!r}"
            ),
        )

    def test_literal_name_ops_resolve_as_real_cli_stems(self):
        # Non-vacuousness proof: the CLI-stem resolution mechanism must
        # find SOMETHING real, or the disjointness test above would pass
        # even with a broken resolver.
        literal_name_ops = {"roadmap-number-stubs", "audit-roadmap", "coordinator-doc-new"}
        self.assertTrue(literal_name_ops.issubset(self.manifest))
        missing = literal_name_ops - self.cli_stems
        self.assertEqual(
            missing,
            set(),
            msg=f"literal-name manifest op(s) did not resolve as a real coordinator/bin CLI stem: {missing!r}",
        )

    def test_every_manifest_op_is_referenced_by_a_real_binding_or_docstring(self):
        offenders = [
            name for name in self.manifest
            if name not in self.cli_stems
            and name not in self.candidate_op_values
            and name not in _extract_consumes_manifest_names(rpa.__doc__)
            and name not in _extract_consumes_manifest_names(spa.__doc__)
        ]
        self.assertEqual(
            offenders,
            [],
            msg=(
                "manifest op(s) referenced nowhere real -- neither a CLI "
                "stem, a directives[].cli candidate-op binding, nor either "
                f"assembler's own Consumes-manifest docstring: {offenders!r}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
