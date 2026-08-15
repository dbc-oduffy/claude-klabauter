"""Standing tripwire (C6b, docs/plans/2026-08-14-baton-closes-when-its-plan-ships.md
§ C6b, AC11): pins the raw-`deliverable_id`-reader population C6a censused
(`state/audits/2026-08-14-raw-deliverable-id-readers.md`) so a NEW raw reader
cannot silently join the corpus on `deliverable_id` string-equality without
first being classified.

Mechanical predicate, not a human judgment call (Review: staff-eng-063f0261
finding 5 — "joiner" is a human classification a test cannot evaluate): this
gate does NOT try to decide whether a new module's `deliverable_id` touch is a
JOINER, a CARRIER, or a WRITE-PATH SITE — that classification happens in the
audit, by a human, exactly as C6a did it for the current 72-file population.
What this gate asserts is purely mechanical: the set of non-test `.py` modules
under `coordinator_core/`/`coordinator/` that reference `deliverable_id`
WITHOUT routing through `coordinator_core.ops.deliverable_equivalence.
canonicalize()` equals a checked-in allowlist (`_ALLOWLISTED_RAW_READERS`
below), derived from C6a's audit plus C6b's own conversions. A module falling
outside the allowlist — new file, new raw `deliverable_id` touch in a
previously-clean file — fails this test until a human classifies it in the
audit and updates the allowlist.

Detection is AST-based (mirrors `test_no_node_schema_shellout.py`'s own
approach for the same reason: a regex misses a multi-line parenthesized
`from ... import (canonicalize, ...)`, exactly as `deliverable_fork_detect.py`
demonstrates on this corpus — a naive single-line grep classifies it "raw"
when it already routes through `canonicalize()`). A module is ROUTED when
ANY of the following holds:
  - it IS `coordinator_core/ops/deliverable_equivalence.py` itself (the
    definition site — nothing to route to);
  - it has `from coordinator_core.ops.deliverable_equivalence import
    canonicalize[, ...]` (any name-list ordering/wrapping — ast handles the
    multi-line/parenthesized form transparently);
  - it imports the whole `deliverable_equivalence` module and later writes
    `<alias>.canonicalize(...)`;
  - the literal substring `deliverable_equivalence.canonicalize` appears
    anywhere (covers a lazily-`import coordinator_core.ops.deliverable_
    equivalence as X` + `X.canonicalize`-shaped call this AST walk's narrower
    branches might miss — a deliberately generous fallback, since a FALSE
    "routed" here only means a genuinely-uncanonicalized reader slips past
    this one gate, not that it becomes uncatchable — the census's own human
    read is the backstop).

DOCUMENTED LIMIT (per the C6b chunk body, not a silent gap): this gate catches
a NEW MODULE reaching the raw population, not a new raw `deliverable_id` join
added INSIDE an already-"routed" module (a module can import `canonicalize`
for one join and add a second, uncanonicalized one elsewhere — this predicate
only sees the import, not which specific comparison it guards). Catching that
narrower case would require per-call-site provenance this predicate does not
attempt.

Residual note (see `_ALLOWLISTED_RAW_READERS`'s own inline comments): two
raw readers in the current population — `coordinator_core/ops/
slug_prefix_family.py` and `coordinator_core/ops/handoff_archive_transition.py`
— postdate C6a's audit (introduced by C4/C7, which landed after the census
was taken) and are therefore not yet classified in the audit's own JOINER/
CARRIER/WRITE-PATH-SITE partition. Allowlisted here (with the reasoning this
module's own read supports) so this chunk's own scoped test run is green;
the audit itself is C6a's file, out of this chunk's write scope, and should
be updated to formally classify both the next time it is touched.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCAN_ROOTS = (_REPO_ROOT / "coordinator_core", _REPO_ROOT / "coordinator")

_TARGET_MODULE = "coordinator_core.ops.deliverable_equivalence"
_DEF_SITE_RELPATH = "coordinator_core/ops/deliverable_equivalence.py"

# Checked-in allowlist (C6b/AC11): every non-test module that references
# `deliverable_id` WITHOUT routing through `canonicalize()`, as of this
# chunk's own landing. Derived from C6a's audit (state/audits/2026-08-14-
# raw-deliverable-id-readers.md) CARRIER (22) + WRITE-PATH SITE (11) +
# Rubric-gap (23) classes verbatim, MINUS the 15 JOINER files C6b converted
# (cascade_backstop_sweep.py, handoff_close_origin_stub.py, commit_trailers.py,
# deliverable_cascade.py, coordinator-prepare-commit-msg.py,
# baton_assemble/__init__.py, coverage.py, cascade_retract.py,
# consumed_handoff_stamp.py, git_native.py, renderers.py,
# handoff_transition.py, promote_shipped_in_flight_stubs.py,
# review_trail_write.py, spec_backlink_resolve.py), PLUS
# `cascade_baton_rows.py` (JOINER-classified by the audit but requiring no
# code change — see its own module: it forwards `deliverable_id` verbatim to
# `execute_plan_assemble.close_out_and_stamp._committed_chunk_shas`, which
# already canonicalizes both sides internally; converting the pass-through
# caller would be redundant, not a fix), PLUS the two post-audit residual
# files named in the module docstring above.
_ALLOWLISTED_RAW_READERS: frozenset[str] = frozenset(
    {
        # --- CARRIER (22, C6a) -- fork-indifferent, left alone verbatim ---
        "coordinator_core/ops/read_frontmatter_field.py",
        "coordinator/bin/read-frontmatter-field.py",
        "coordinator/bin/handoff-deliverable-carry.py",
        "coordinator/bin/mint-deliverable-id.py",
        "coordinator/bin/reap-orphaned-in-flight-handoffs.py",
        "coordinator_core/archive_stamp.py",
        "coordinator_core/benchmarks/op_fixtures.py",
        "coordinator_core/ceremony_common/_phantom_sweep_providers.py",
        "coordinator_core/ops/assert_no_dangling_plan_backlinks.py",
        "coordinator_core/ops/ceremony/commit_pipeline.py",
        "coordinator_core/ops/ceremony/post_commit_tail.py",
        "coordinator_core/ops/ceremony/scoped_git_commit.py",
        "coordinator_core/ops/coordinator_render_rollup.py",
        "coordinator_core/ops/docgen/volatility.py",
        "coordinator_core/ops/emit/sections/handoffs.py",
        "coordinator_core/ops/emit/sections/plans.py",
        "coordinator_core/ops/emit/sections/roadmaps.py",
        "coordinator_core/ops/fleet/archive_handoffs.py",
        "coordinator_core/ops/rewrite_spec_backlinks.py",
        "coordinator_core/ops/session/boot_sweep.py",
        "coordinator_core/workstream_complete/directives_lessons_plan.py",
        "coordinator_core/frontmatter/schema_validate.py",
        # --- WRITE-PATH SITE (11, C6a) -- excluded from conversion by the
        # plan body's own negative-spec (canonicalizing a writer would stamp
        # the canonical winner over the author-supplied id) ---
        "coordinator_core/ops/backfill_deliverable_spine.py",
        "coordinator/bin/coordinator-doc-new.py",
        "coordinator/bin/spinoff-deliverable-and-commit.py",
        "coordinator_core/baton_assemble/apply.py",
        "coordinator_core/ops/coordinator_complete_entry.py",
        "coordinator_core/ops/handoff_author_fork.py",
        "coordinator_core/ops/handoff_normalize.py",
        "coordinator_core/ops/mint_deliverable_id.py",
        "coordinator_core/ops/queue_scaffold_baton.py",
        "coordinator_core/ops/session/record_pickup.py",
        "coordinator_core/ops/deliverable_ledger_write.py",
        # --- Rubric gap (23, C6a) -- comment/docstring/type-decl only, no
        # runtime deliverable_id touch to convert or leave alone ---
        "coordinator/bin/backfill-deliverable-spine.py",
        "coordinator/bin/coordinator-render-rollup.py",
        "coordinator/bin/promote-shipped-in-flight-stubs.py",
        "coordinator/bin/spawn-trace-capture.py",
        "coordinator/lib/spawn-trace/sitecustomize.py",
        "coordinator_core/authz/classification.py",
        "coordinator_core/op_scopes.py",
        "coordinator_core/contract/cockpit_schema/entities/plan_summary.py",
        "coordinator_core/contract/cockpit_schema/entities/roadmap_summary.py",
        "coordinator_core/contract/cockpit_schema/entities/summaries.py",
        "coordinator_core/ops/docgen/render.py",
        "coordinator_core/ops/docgen/template_format.py",
        "coordinator_core/ops/emit_artifact_shape_contract.py",
        "coordinator_core/ops/plan_capture_persist.py",
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "coordinator_core/ops/ceremony/wsc_tail.py",
        "coordinator_core/session/claimed_plan.py",
        "coordinator_core/session/claims.py",
        "coordinator_core/session/producer_resolve.py",
        "coordinator_core/tracker_entities.py",
        "coordinator_core/workstream_complete/judgments.py",
        "coordinator_core/write_guards/nudge_handoff_ac_shape.py",
        "coordinator_core/frontmatter/primitives.py",
        # --- JOINER, converted by C6b but requiring no code change: this
        # module forwards `deliverable_id` verbatim to a callee
        # (execute_plan_assemble.close_out_and_stamp._committed_chunk_shas)
        # that already canonicalizes both sides internally (that callee is
        # in the B/routed set) -- see cascade_baton_rows.py's own module for
        # the full note. ---
        "coordinator_core/ops/cascade_baton_rows.py",
        # --- Post-audit residuals (introduced by C4/C7, which landed after
        # C6a's census; not yet classified in the audit -- see module
        # docstring). ---
        # slug_prefix_family.py: a DIFFERENT join type by design (slug-
        # PREFIX family, not exact-id equality) -- C4's own module docstring
        # and cascade_backstop_sweep.py's module docstring both establish
        # this check is deliberately NOT routed through canonicalize(); it
        # exists specifically to catch a fork the equivalence map does not
        # yet declare.
        "coordinator_core/ops/slug_prefix_family.py",
        # handoff_archive_transition.py:841 passes a literal `deliverable_id
        # =None` keyword-argument default -- no comparison, no join.
        "coordinator_core/ops/handoff_archive_transition.py",
    }
)


def _relpath(path: Path) -> str:
    return path.resolve().relative_to(_REPO_ROOT).as_posix()


def _is_excluded_source_path(path: Path) -> bool:
    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _is_routed_through_canonicalize(relpath: str, text: str, tree: ast.AST) -> bool:
    if relpath == _DEF_SITE_RELPATH:
        return True

    whole_module_alias: str | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module == _TARGET_MODULE:
                for alias in node.names:
                    if alias.name == "canonicalize":
                        return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _TARGET_MODULE:
                    whole_module_alias = alias.asname or alias.name.split(".")[-1]

    if whole_module_alias is not None:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "canonicalize"
                and isinstance(node.value, ast.Name)
                and node.value.id == whole_module_alias
            ):
                return True

    # Generous fallback (see module docstring): a literal qualified
    # reference anywhere in the source, however it was bound in scope.
    return "deliverable_equivalence.canonicalize" in text


def find_raw_deliverable_id_readers() -> list[str]:
    """Return sorted repo-relative paths of every non-test `.py` module under
    `coordinator_core/`/`coordinator/` that references `deliverable_id`
    without routing through `canonicalize()` — the live, current population
    this test pins against `_ALLOWLISTED_RAW_READERS`.
    """
    raw: list[str] = []
    for scan_root in _SCAN_ROOTS:
        for path in sorted(scan_root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            if _is_excluded_source_path(path):
                continue
            relpath = _relpath(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            if "deliverable_id" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError:
                raw.append(relpath)
                continue
            if not _is_routed_through_canonicalize(relpath, text, tree):
                raw.append(relpath)
    return sorted(raw)


def test_raw_deliverable_id_reader_population_matches_allowlist():
    """The mechanical AC11 gate: a new raw `deliverable_id` reader (new file,
    or a previously-clean file gaining its first uncanonicalized touch) must
    be classified in the audit and added here before it can land.
    """
    found = set(find_raw_deliverable_id_readers())
    allowlisted = set(_ALLOWLISTED_RAW_READERS)

    unexpected_new = sorted(found - allowlisted)
    stale_allowlist_entries = sorted(allowlisted - found)

    assert not unexpected_new, (
        "New raw deliverable_id reader(s) outside the checked-in allowlist -- "
        "classify each in state/audits/2026-08-14-raw-deliverable-id-readers.md "
        "(JOINER/CARRIER/WRITE-PATH SITE) and add to "
        f"_ALLOWLISTED_RAW_READERS: {unexpected_new}"
    )
    assert not stale_allowlist_entries, (
        "Allowlist entry no longer a raw deliverable_id reader (file deleted, "
        "or now routes through canonicalize()) -- remove from "
        f"_ALLOWLISTED_RAW_READERS: {stale_allowlist_entries}"
    )


def test_gate_detects_a_planted_raw_deliverable_id_joiner(tmp_path, monkeypatch):
    """Proves the gate has teeth: a freshly-planted module that compares raw
    `deliverable_id` values with no canonicalize import is flagged, not
    silently passed by absence.
    """
    fixture_root = tmp_path / "coordinator_core"
    fixture_root.mkdir()
    fixture = fixture_root / "fixture_planted_joiner.py"
    fixture.write_text(
        "def matches(fm, deliverable_id):\n"
        "    return fm.get('deliverable_id') == deliverable_id\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("coordinator_core.ops.tests.test_deliverable_id_readers_canonicalize._SCAN_ROOTS", (fixture_root,))
    monkeypatch.setattr("coordinator_core.ops.tests.test_deliverable_id_readers_canonicalize._REPO_ROOT", tmp_path)

    found = find_raw_deliverable_id_readers()

    assert "coordinator_core/fixture_planted_joiner.py" in found


def test_gate_recognizes_a_multiline_canonicalize_import(tmp_path, monkeypatch):
    """Negative control mirroring the real corpus's own edge case
    (`deliverable_fork_detect.py`'s multi-line parenthesized import): a
    module that imports `canonicalize` via a wrapped `from ... import (`
    block must be recognized as ROUTED, not misclassified raw by a
    single-line-only detector.
    """
    fixture_root = tmp_path / "coordinator_core"
    fixture_root.mkdir()
    fixture = fixture_root / "fixture_multiline_import.py"
    fixture.write_text(
        "from coordinator_core.ops.deliverable_equivalence import (\n"
        "    canonicalize,\n"
        "    load_equivalence_map,\n"
        ")\n\n"
        "def matches(fm, deliverable_id, equivalence_map):\n"
        "    return canonicalize(fm.get('deliverable_id'), equivalence_map) == "
        "canonicalize(deliverable_id, equivalence_map)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr("coordinator_core.ops.tests.test_deliverable_id_readers_canonicalize._SCAN_ROOTS", (fixture_root,))
    monkeypatch.setattr("coordinator_core.ops.tests.test_deliverable_id_readers_canonicalize._REPO_ROOT", tmp_path)

    found = find_raw_deliverable_id_readers()

    assert "coordinator_core/fixture_multiline_import.py" not in found
    assert found == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
