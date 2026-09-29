"""
coordinator_core/roadmap/tests/test_prep_gate_doe_port.py — behaviors ported from
DoE-claude's retired `coordinator/tests/test_mise_prep_authoring_bar.py`
(deleted at DoE commit 292feda38, which shimmed `mise-prep-gate.py` to this
engine) that the engine's own `test_prep_gate.py`/`test_prep_gate_four_legs.py`
did not already assert under a different name.

Scope: the CENSUS-scan-shape behavior and the `epistemic-premise` held-row
exemption below were genuinely uncovered and targetable at
`coordinator_core.roadmap.prep_gate`. Every other DoE test in that file was
either an exact/behavioral duplicate already in `test_prep_gate.py` (see the
executor's coverage table) or exercised the corpus-scanning CLI surface,
which now lives in `test_prep_gate_cli.py` alongside
`coordinator_core.roadmap.prep_gate_cli`.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import prep_gate as pg
from coordinator_core.roadmap.tests.test_prep_gate import (
    _CLEAN_FM,
    _CLEAN_SPINE,
    _gate,
    _write_plan,
    prepped_plan,
)


def test_census_does_not_scan_prose_for_count_shaped_claims(tmp_path: Path) -> None:
    """DoE parity (`test_census_does_not_scan_prose_for_count_shaped_claims`).

    The CENSUS predicate reads the declared `census:` frontmatter key alone —
    never the body. A body full of counted prose still PASSES on
    `census: []`, and this pins that the cut heuristic (scan the declared key,
    not the sentence) is not silently reintroduced as a prose scan.
    """
    path = prepped_plan(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8")
        + "\n30 importers across 14 files, and 11 callers in 3 plans.\n",
        encoding="utf-8",
    )
    report = _gate(tmp_path, path)
    assert report["classes"]["CENSUS"]["status"] == "PASS"


# ---------------------------------------------------------------------------
# The epistemic-premise held-row exemption from writes-undeclared
# ---------------------------------------------------------------------------


def test_an_epistemic_premise_held_row_is_exempt_from_writes_undeclared(tmp_path: Path) -> None:
    """DoE parity (`test_an_epistemic_premise_held_row_is_exempt_from_writes_undeclared`).

    `_spine` refuses every UNDECLARED-writes row -- except one held out of the
    emit already, by `wave_map._compute_held_out`, because its predecessor
    decides its writes. Prep must not be stricter than emit on this shape.
    """
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    path = _write_plan(
        tmp_path,
        slug="held.md",
        frontmatter=_CLEAN_FM,
        spine=(
            "- id: C1\n"
            "  title: decide the shape\n"
            "  body: decide the shape per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  writes: [coordinator_core/roadmap/prep_gate.py]\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
            "- id: C2\n"
            "  title: t\n"
            "  body: t per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  depends_on:\n"
            "    - chunk: C1\n"
            "      gate_kind: epistemic-premise\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
        ),
    )
    report = _gate(tmp_path, path)
    assert report["classes"]["SPINE"]["status"] == "PASS", report["classes"]["SPINE"]


def test_an_output_consumption_runtime_gate_still_fails_writes_undeclared(tmp_path: Path) -> None:
    """DoE parity. Only `epistemic-premise` exempts UNDECLARED writes --
    `output-consumption-runtime` is the other declarable gate kind and does not
    hold a row out of the emit's wave graph, so it must not exempt it here
    either."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    path = _write_plan(
        tmp_path,
        slug="still-undeclared.md",
        frontmatter=_CLEAN_FM,
        spine=(
            "- id: C1\n"
            "  title: build the substrate\n"
            "  body: build the substrate per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  writes: [coordinator_core/roadmap/prep_gate.py]\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
            "- id: C2\n"
            "  title: t\n"
            "  body: t per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  depends_on:\n"
            "    - chunk: C1\n"
            "      gate_kind: output-consumption-runtime\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
        ),
    )
    report = _gate(tmp_path, path)
    assert report["classes"]["SPINE"]["kind"] == "writes-undeclared"
    assert "C2" in report["classes"]["SPINE"]["detail"]


def test_a_transitively_held_row_is_also_exempt_from_writes_undeclared(tmp_path: Path) -> None:
    """DoE parity. A direct-edge-only check covers route 1 of
    `wave_map._compute_held_out`'s walk but not route 2, the transitive one: a
    row with UNDECLARED writes that reaches a held predecessor only through a
    chain of non-epistemic-premise edges is still held out of the emit by the
    engine and must not be reported here. C1 carries its own direct
    epistemic-premise edge on P and is held (route 1); C2 has UNDECLARED
    writes and depends on C1 only via an output-consumption-runtime edge, so
    C2 is held transitively (route 2) even though its own edge is not
    epistemic-premise."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    path = _write_plan(
        tmp_path,
        slug="transitively-held.md",
        frontmatter=_CLEAN_FM,
        spine=(
            "- id: P\n"
            "  title: decide the shape\n"
            "  body: decide the shape per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  writes: [coordinator_core/roadmap/prep_gate.py]\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
            "- id: C1\n"
            "  title: t\n"
            "  body: t per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  depends_on:\n"
            "    - chunk: P\n"
            "      gate_kind: epistemic-premise\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
            "- id: C2\n"
            "  title: t\n"
            "  body: t per the row\n"
            "  change_kind: code-edit\n"
            "  surface: coordinator_core/roadmap/prep_gate.py\n"
            "  depends_on:\n"
            "    - chunk: C1\n"
            "      gate_kind: output-consumption-runtime\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
        ),
    )
    report = _gate(tmp_path, path)
    assert report["classes"]["SPINE"]["status"] == "PASS", report["classes"]["SPINE"]


def test_the_writes_undeclared_detail_names_the_epistemic_premise_spelling(tmp_path: Path) -> None:
    """DoE parity (`test_the_writes_undeclared_detail_names_the_epistemic_premise_spelling`)."""
    (tmp_path / "coordinator_core").mkdir(parents=True, exist_ok=True)
    path = _write_plan(
        tmp_path,
        slug="detail.md",
        frontmatter=_CLEAN_FM,
        spine=(
            "- id: C1\n"
            "  title: t\n"
            "  body: t per the row\n"
            "  change_kind: verification\n"
            "  surface: coordinator_core/roadmap\n"
            "  queue_scope: project\n"
            "  disposition: open\n"
        ),
    )
    detail = _gate(tmp_path, path)["classes"]["SPINE"]["detail"]
    assert "writes: []" in detail
    assert "epistemic-premise" in detail


# ---------------------------------------------------------------------------
# The NOT-PREPPED fix: line names only a path that resolves on disk
# ---------------------------------------------------------------------------


def test_the_not_prepped_fix_line_names_only_paths_that_resolve_on_disk(tmp_path: Path) -> None:
    """DoE parity (`test_the_not_prepped_fix_line_names_only_paths_that_resolve_on_disk`,
    `test_the_upgrade_fix_line_never_names_a_dead_doe_relative_path`).

    `_upgrade_fix_line` resolves the converter off this module's OWN location
    rather than printing the bare DoE-relative literal
    `coordinator/bin/mise-prep-upgrade.py`, which does not exist in the repo
    this gate is reporting on. Either the printed `fix:` line names an
    absolute path that `Path.is_file()` confirms exists, or it says the
    converter is absent -- never a specific, plausible, dead path.
    """
    path = _write_plan(tmp_path, frontmatter="census: []\n")  # no prime_exit_criterion -> NOT-PREPPED
    detail = _gate(tmp_path, path)["message"]
    fix_lines = [line for line in detail.splitlines() if line.strip().startswith("fix:")]
    assert fix_lines, detail
    fix_line = fix_lines[0]
    assert "coordinator/bin/mise-prep-upgrade.py" not in fix_line or pg._UPGRADE_SCRIPT.is_file()
    if pg._UPGRADE_SCRIPT.is_file():
        assert str(pg._UPGRADE_SCRIPT) in fix_line
    else:
        assert "not present" in fix_line
