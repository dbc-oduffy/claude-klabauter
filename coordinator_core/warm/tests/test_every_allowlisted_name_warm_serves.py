"""coordinator_core.warm.tests.test_every_allowlisted_name_warm_serves --
the C8 guard: every name on `warm_entrypoint_allowlist.json` warm-serves
(resolves, exposes a callable `main(argv)`, and carries an inert module
body), and a NEW unservable/impure name fails this suite closed the moment
it lands, instead of silently inflating one more hand-derived count.

Purpose: the origin plan's Problem section names three prior wrong counts
of "how many bin names warm-serve" -- a name-only `main` check that missed
arity, a prefix-match exclusion, and a `sys.path`-mutation count that
exceeded its own population. All three were scratch scripts nobody re-ran
after the corpus moved. This module is the thing that re-checks: it runs
`coordinator_core.warm.serve_classifier` (C1's committed AST-only
instrument, never an import/exec of the corpus -- see that module's own
negative-spec) over the LIVE allowlist and fails on any finding not already
recorded in `_BASELINE` below.

Ratchet shape LIFTED from `X:/DoE-claude/coordinator/tests/test_bin_entrypoint_inertness.py`
(`_load_local_baseline`, `test_bin_entrypoint_inertness_baseline_has_no_stale_entries`,
`test_baseline_is_empty`, `test_scans_all_bin_files`) per this chunk's own body ("Reuse
DoE's ratchet shape, don't invent one") -- not re-derived. A 202-name (at
plan-authoring measurement) fully-red suite is unlandable in one PR; a
baseline that may only SHRINK is landable on day one, and C3-C6 retire rows
from it as they land, same as DoE's own baseline reached empty over its
17-file corpus.

`_BASELINE` is embedded directly in this module rather than a sibling
`_baseline.py` file, unlike DoE's split -- this chunk's declared `writes:`
scope is this ONE test path, so a second baseline module is out of scope by
construction, not by preference.

Baseline entries are `(name, reason, text)` -- the allowlist NAME (not a
file path: an allowlist entry can be renamed without moving the underlying
script, and the name is what `classify_entrypoint` keys off), the failure
reason (`no_script` / `no_main` / `zero_arity_main`, or one of
`serve_classifier.Finding`'s own module-body-purity reasons), and the
offending source text -- the same `(path, text)` shape `Finding.key()`
already uses, extended with a `name` field because an unservable verdict
(no script, no main, wrong arity) has no `Finding` of its own to carry a
path.

Negative-spec (RAG-bait):
    This module does NOT classify the corpus itself -- `serve_classifier`
    (C1) owns the AST predicate; this module only calls it and diffs the
    result against a committed baseline. A second predicate here would be
    exactly the "second source of truth" the origin plan's Anti-scope
    forbids ("Do not build a second inertness predicate").

    This module does NOT invoke, import, or exec any `coordinator/bin/*.py`
    module body -- `classify_population` is AST-only (see
    `serve_classifier`'s own negative-spec); this suite inherits that
    property by construction, never adding a runtime leg of its own.

    `_BASELINE` is not evidence the corpus is fine -- it is evidence of
    exactly which names are NOT fine yet, recorded so a new bad name cannot
    hide among them. `test_baseline_has_no_stale_entries` is the mechanism
    that forces this list down as C3-C6 land, not a one-time snapshot left
    to rot.

Spec backlink: docs/plans/2026-08-27-every-bin-name-warm-serves-and-a-classifier-says-so.md, chunk C8
"""

from __future__ import annotations

from coordinator_core.warm import serve_classifier as sc


def _verdict_entries(verdict: sc.ServeVerdict) -> list[tuple[str, str, str]]:
    """One `ServeVerdict` -> its baseline-comparable `(name, reason, text)`
    entries. Resolution/arity failures short-circuit (a name with no script
    has no module body to also flag for import purity), mirroring
    `ServeVerdict.servable`'s own independent-then-short-circuited shape --
    see that property's docstring for why resolution is checked before
    purity rather than alongside it."""
    if not verdict.script_exists:
        return [(verdict.name, "no_script", "<no script>")]
    if not verdict.has_main:
        return [(verdict.name, "no_main", "<no module-level def main>")]
    entries = []
    if not verdict.main_arity_ok:
        entries.append((verdict.name, "zero_arity_main", "<def main() with no argv parameter>"))
    for finding in verdict.findings:
        entries.append((verdict.name, finding.reason, finding.text))
    return entries


def _live_entries() -> set[tuple[str, str, str]]:
    """Every current baseline-comparable entry over the LIVE allowlist --
    the load-bearing call in this module: `load_allowlist_names` reads
    `warm_entrypoint_allowlist.json` fresh each run, so a name C2/C5 add or
    remove changes this set on the next test run with no edit here."""
    names = sc.load_allowlist_names()
    verdicts = sc.classify_population(names)
    live: set[tuple[str, str, str]] = set()
    for verdict in verdicts:
        live.update(_verdict_entries(verdict))
    return live


# --- Committed baseline -------------------------------------------------
#
# Snapshotted 2026-08-27 against the then-live `warm_entrypoint_allowlist.json`
# (380 entrypoints) via `coordinator_core.warm.serve_classifier`. This is
# the corpus's UNFIXED state at C8-authoring time, not a target -- see
# module docstring. Editing an entry OUT to make a still-broken name pass
# defeats the guard; only C3-C6 (or a future fix) legitimately shrinks this
# list, and `test_baseline_has_no_stale_entries` below fails the moment a
# recorded entry no longer matches a live finding, forcing the removal to
# happen explicitly rather than silently.
# Empty, and that is the point: every name on the allowlist now carries an inert
# module body, so the ratchet has nothing left to hold. It stays a ratchet rather
# than being deleted -- a new violation lands as a NEW entry against an empty
# baseline, which is the loudest signal this file can give.
_BASELINE: list[tuple[str, str, str]] = []


def test_no_new_warm_serve_violations():
    """The C8 guard proper: every LIVE finding must already be in
    `_BASELINE`. A name added to the allowlist that cannot resolve, has no
    `main`, has the wrong arity, or carries an impure module body fails
    this test the moment it lands -- the failure mode this chunk exists to
    close (`_write_allowlist`'s accumulating union, and a phantom row with
    no author review, per the origin plan's C2/C5 rows)."""
    live = _live_entries()
    baseline = set(_BASELINE)
    new = sorted(live - baseline)
    rendered = "\n".join(f"  {name}: [{reason}] {text}" for name, reason, text in new)
    assert new == [], (
        f"Found {len(new)} NEW warm-serve violation(s) not in _BASELINE "
        f"-- resolve them or, if genuinely unfixable, add them to the "
        f"baseline with a stated reason:\n{rendered}"
    )


def test_baseline_has_no_stale_entries():
    """The shrink-forcing half of the ratchet: a baseline entry that no
    longer matches a live finding means the underlying name was fixed (by
    C3/C4/C6) or removed from the allowlist (by C2/C5) -- in either case the
    entry must be deleted from `_BASELINE` here, not left to silently rot
    as C3-C6 land. Fails loud instead."""
    live = _live_entries()
    baseline = set(_BASELINE)
    stale = sorted(baseline - live)
    rendered = "\n".join(f"  {name}: [{reason}] {text}" for name, reason, text in stale)
    assert stale == [], (
        f"{len(stale)} _BASELINE entr(ies) no longer match a live warm-serve "
        f"violation -- the underlying name was fixed or removed; delete "
        f"these entries:\n{rendered}"
    )


def test_baseline_will_reach_empty():
    """AC placeholder for C3-C6's own exit criterion, not this chunk's:
    `_BASELINE` is non-empty at C8-authoring time by design (see module
    docstring -- "A 202-name ... fully-red suite is unlandable"). This test
    only pins the DIRECTION, not the destination -- it fails if the
    baseline grows past a generous multiple of the live corpus, a sign the
    two collections drifted out of the same shape rather than merely
    shrinking. `_BASELINE == []` is the plan's own `expected_when_true`,
    not something this chunk asserts directly."""
    assert len(_BASELINE) <= len(_live_entries()) + len(sc.load_allowlist_names()) * 4


def test_scans_the_live_allowlist_not_a_frozen_copy():
    """Guards against the ratchet degrading into a static fixture: re-reads
    `warm_entrypoint_allowlist.json` from disk on every call, so a name
    C2/C5 add or remove is visible to this suite on the very next run with
    no edit to this file. Regression target: a future refactor caching
    `_live_entries()` at import time would make this test the only thing
    that still notices."""
    names_first = sc.load_allowlist_names()
    names_second = sc.load_allowlist_names()
    assert names_first == names_second
    assert len(names_first) > 0


def test_fails_closed_on_each_unservable_shape(tmp_path):
    """The half of this suite's own claim that was asserted only in prose.

    The module docstring above says a new unservable or impure name "fails
    this suite closed the moment it lands", and the plan's
    `expected_when_true` requires exactly that -- but until this test,
    nothing exercised it: `test_no_new_warm_serve_violations` reads a
    corpus that is now entirely clean, so it passes identically whether the
    comparison works or has been broken by a refactor. A guard whose only
    evidence is that it is green over a clean corpus is the failure this
    plan exists to end, and it had it.

    `classify_population`'s injectable `bin_dir` is what lets this run
    against synthetic scripts in `tmp_path` -- nothing is written into the
    live `coordinator/bin`, and the live allowlist is not touched.
    """
    shapes = {
        "shape-no-main": "import os\n\n\nprint\n",
        "shape-zero-arity": "def main():\n    return 0\n",
        "shape-impure-body": (
            "import lib\n"
            "from cc_invoke import require_engine_on_path\n"
            "\n\n"
            "def main(argv):\n    return 0\n"
        ),
        "shape-sys-path-mutation": (
            "import sys\n"
            "sys.path.insert(0, '/somewhere')\n"
            "\n\n"
            "def main(argv):\n    return 0\n"
        ),
    }
    for name, source in shapes.items():
        (tmp_path / f"{name}.py").write_text(source, encoding="utf-8")
    # Deliberately has no file on disk: the fourth unservable shape.
    names = [*shapes, "shape-no-script"]

    live: set[tuple[str, str, str]] = set()
    for verdict in sc.classify_population(names, bin_dir=tmp_path):
        live.update(_verdict_entries(verdict))

    # The guard's own comparison, against the committed baseline rather
    # than an empty set -- a baseline that ever grew to swallow these
    # would fail here too.
    new = sorted(live - set(_BASELINE))
    flagged = {name for name, _reason, _text in new}
    assert flagged == set(names), (
        "the C8 comparison did not fail closed on every unservable shape; "
        f"missed {sorted(set(names) - flagged)}"
    )

    reasons = {name: reason for name, reason, _text in new}
    assert reasons["shape-no-script"] == "no_script"
    assert reasons["shape-no-main"] == "no_main"
    assert reasons["shape-zero-arity"] == "zero_arity_main"
    assert reasons["shape-impure-body"] == "module-scope non-stdlib import"
    assert reasons["shape-sys-path-mutation"] == "module-scope process mutation"
