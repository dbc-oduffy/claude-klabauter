"""coordinator_core.tests.test_b2_doc_scaffold_emission_coverage -- C8's
coverage pin.

Purpose: `coordinator_core/ops/doctype_hosts.py` (C0) is the checked-in
`(type, ceremony)` table -- the SINGLE source of the B2 in-scope set. This
pin reads THAT table and verifies, for every row, that the table's own
claim holds: an `emitted` row's named `module` actually names the type's
scaffold directive id and the `coordinator-doc-new` CLI in its own source,
and an `excluded` row's `reason` is inside the closed set
(`EXCLUSION_REASONS`), with a citation present whenever the reason is
`prohibited-by-doctrine`. It re-enumerates NOTHING: every `(type,
ceremony, module)` triple this test checks comes from iterating
`DOCTYPE_HOSTS` (or its `emitted_rows()`/`excluded_rows()` helpers), never
a second hand-typed list living beside it (docs/plans/2026-09-11-document-
scaffolding-is-emitted-not-remembered.md § "Which shape is canonical" --
"a list that appears twice diverges once").

Why source inspection, not `brief()` invocation: each host's own
`test_scaffold_directive_parity.py` (C3-C6) already exercises `brief()`
end-to-end with that host's specific triggering kwargs, real-parser
parsing, and `--out` containment -- duplicating those per-host call
signatures here would be the second-list problem this pin exists to avoid
(the recipe, not just the type, would be re-enumerated). This pin checks a
narrower, table-driven property instead: does the row's own `module`
still carry the type's value as a quoted string literal AND name
`coordinator-doc-new` in its source at all -- catching the case a
follow-on edit silently drops the type's emission path or retargets it to
a different CLI, without hand-copying any host's invocation shape.
A quoted-literal check (not a `d-scaffold-<type>` directive-id check) is
deliberate: the two pre-existing donor hosts this table also carries
(`baton_assemble`'s `kind`-keyed dispatch, `backlog_grind_assemble`'s
`build_decision_scaffold_directive`'s caller-supplied `id`) do not follow
this plan's `d-scaffold-<type>` id convention -- `directives.py`'s own
docstring says so explicitly ("not a hard-coded `d-scaffold-decision`
singleton") -- so pinning on that id shape would fail every donor row by
construction, not just a genuinely broken one.

FALSIFIER GATE (C8 note): `FalsifierGateTest` below is the permanent
regression form of the "insert a bad row, run the pin, confirm it fails
naming the (type, ceremony) pair, remove the row" instruction -- it
constructs the bad row in-test (never edits the checked-in table) and
asserts the failure message names both the row's `type` and `ceremony`.
This was also observed by hand against the live table before this file
landed: a `DoctypeHostRow(type="nonexistent-scaffold-type", ...,
state="emitted")` spliced into `DOCTYPE_HOSTS` and run through `_run_pin`
failed naming `nonexistent-scaffold-type`/`no-such-ceremony`; reverted
before commit.

Negative-spec: does NOT invoke `coordinator-doc-new` or any CLI, does NOT
write to disk, does NOT decide which types are in scope (that is C0's
table, read here, never re-derived). Does NOT re-assert per-host `--out`
containment, required-flag computation, or mutex resolution -- those stay
each host's own C3-C6 parity pin's job.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C8.

Run:
    pytest coordinator_core/tests/test_b2_doc_scaffold_emission_coverage.py -v
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
import unittest

from coordinator_core.ops.doctype_hosts import (
    DOCTYPE_HOSTS,
    EXCLUSION_REASONS,
    DoctypeHostRow,
    emitted_rows,
    excluded_rows,
)


def _pair(row: DoctypeHostRow) -> str:
    return f"({row.type!r}, {row.ceremony!r})"


def _package_source(module) -> str:
    """Concatenate a module's own source with every direct submodule's
    source when `module` is a package -- a host like `backlog_grind_
    assemble` builds its directive in a submodule (`directives.py`), not
    in `__init__.py`, and this pin checks the package's construction
    surface as a whole rather than guessing which submodule to name."""
    chunks = [inspect.getsource(module)]
    path = getattr(module, "__path__", None)
    if path is not None:
        for info in pkgutil.iter_modules(path):
            if info.ispkg:
                continue
            submodule = importlib.import_module(f"{module.__name__}.{info.name}")
            chunks.append(inspect.getsource(submodule))
    return "\n".join(chunks)


def _check_row_emitted(row: DoctypeHostRow) -> tuple[bool, str]:
    """An `emitted` row's `module` must import cleanly and its source must
    carry the type's value as a quoted string literal, alongside the
    `coordinator-doc-new` CLI -- the table's own claim, checked, not
    reproduced."""
    if row.module is None:
        return False, f"{_pair(row)}: emitted row has no module"
    try:
        module = importlib.import_module(row.module)
        source = _package_source(module)
    except Exception as exc:  # noqa: BLE001 -- surfaced verbatim in the failure
        return False, f"{_pair(row)}: module {row.module!r} failed to import/read: {exc!r}"
    quoted_type = (f'"{row.type}"', f"'{row.type}'")
    if not any(q in source for q in quoted_type):
        return False, (
            f"{_pair(row)}: expected type {row.type!r} as a quoted string "
            f"literal, not found in {row.module}"
        )
    if "coordinator-doc-new" not in source:
        return False, (
            f"{_pair(row)}: module {row.module!r} never names cli "
            "'coordinator-doc-new'"
        )
    return True, ""


def _check_row_excluded(row: DoctypeHostRow) -> tuple[bool, str]:
    """An `excluded` row's `reason` must be in the closed set, and a
    `prohibited-by-doctrine` row must carry its citation."""
    if row.reason not in EXCLUSION_REASONS:
        return False, (
            f"{_pair(row)}: exclusion reason {row.reason!r} is outside the "
            f"closed set {sorted(EXCLUSION_REASONS)}"
        )
    if row.reason == "prohibited-by-doctrine" and not row.citation:
        return False, f"{_pair(row)}: prohibited-by-doctrine row is missing its citation"
    return True, ""


def _run_pin(rows) -> list[str]:
    """Check every row in `rows`, returning one failure message per row
    that fails -- each message names the row's `(type, ceremony)` pair."""
    failures: list[str] = []
    for row in rows:
        if row.state == "emitted":
            ok, msg = _check_row_emitted(row)
        elif row.state == "excluded":
            ok, msg = _check_row_excluded(row)
        else:
            ok, msg = False, f"{_pair(row)}: unrecognised state {row.state!r}"
        if not ok:
            failures.append(msg)
    return failures


class CoverageAgainstCheckedInTableTest(unittest.TestCase):
    """Every row in the checked-in table is either emitted by its named
    ceremony's module or carries a closed-set exclusion reason -- AC5's
    coverage claim, read straight off `DOCTYPE_HOSTS`."""

    def test_every_row_is_emitted_or_excused(self) -> None:
        failures = _run_pin(DOCTYPE_HOSTS)
        self.assertEqual(failures, [], "\n".join(failures))

    def test_emitted_rows_all_verified_individually(self) -> None:
        for row in emitted_rows():
            with self.subTest(row=_pair(row)):
                ok, msg = _check_row_emitted(row)
                self.assertTrue(ok, msg)

    def test_excluded_rows_all_verified_individually(self) -> None:
        for row in excluded_rows():
            with self.subTest(row=_pair(row)):
                ok, msg = _check_row_excluded(row)
                self.assertTrue(ok, msg)

    def test_every_row_is_emitted_xor_excluded(self) -> None:
        for row in DOCTYPE_HOSTS:
            with self.subTest(row=_pair(row)):
                self.assertIn(row.state, ("emitted", "excluded"))


class FalsifierGateTest(unittest.TestCase):
    """C8's note: the pin must fail, naming the `(type, ceremony)` pair,
    when a row is neither correctly emitted nor correctly excused. This is
    the permanent regression form of the manual red-state observation
    performed before this file landed (see module docstring)."""

    def test_an_emitted_row_with_no_matching_directive_id_fails_naming_the_pair(self) -> None:
        bad_row = DoctypeHostRow(
            type="nonexistent-scaffold-type",
            ceremony="no-such-ceremony",
            module="coordinator_core.goals",
            state="emitted",
        )
        failures = _run_pin((bad_row,))
        self.assertEqual(len(failures), 1)
        self.assertIn("nonexistent-scaffold-type", failures[0])
        self.assertIn("no-such-ceremony", failures[0])

    def test_an_emitted_row_naming_a_nonexistent_module_fails_naming_the_pair(self) -> None:
        bad_row = DoctypeHostRow(
            type="some-type",
            ceremony="some-ceremony",
            module="coordinator_core.this_module_does_not_exist",
            state="emitted",
        )
        failures = _run_pin((bad_row,))
        self.assertEqual(len(failures), 1)
        self.assertIn("some-type", failures[0])
        self.assertIn("some-ceremony", failures[0])

    def test_an_excluded_row_with_reason_outside_the_closed_set_fails_naming_the_pair(
        self,
    ) -> None:
        bad_row = DoctypeHostRow(
            type="some-type",
            ceremony="some-ceremony",
            module=None,
            state="excluded",
            reason="just-because",
        )
        failures = _run_pin((bad_row,))
        self.assertEqual(len(failures), 1)
        self.assertIn("some-type", failures[0])
        self.assertIn("some-ceremony", failures[0])

    def test_a_prohibited_by_doctrine_row_missing_its_citation_fails(self) -> None:
        bad_row = DoctypeHostRow(
            type="some-type",
            ceremony="some-ceremony",
            module=None,
            state="excluded",
            reason="prohibited-by-doctrine",
            citation=None,
        )
        failures = _run_pin((bad_row,))
        self.assertEqual(len(failures), 1)
        self.assertIn("some-type", failures[0])
        self.assertIn("some-ceremony", failures[0])


if __name__ == "__main__":
    unittest.main()
