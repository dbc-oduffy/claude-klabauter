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

import json
from pathlib import Path

import pytest

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


def _live_entries(
    names: list[str] | None = None, bin_dir: Path | None = None
) -> set[tuple[str, str, str]]:
    """Every current baseline-comparable entry over a population --
    defaults to the LIVE allowlist: `load_allowlist_names` reads
    `warm_entrypoint_allowlist.json` fresh each run, so a name C2/C5 add or
    remove changes this set on the next test run with no edit here. A
    caller may inject `names`/`bin_dir` (the C8 fail-closed leg does, over
    synthetic `tmp_path` scripts) without touching the live allowlist."""
    if names is None:
        names = sc.load_allowlist_names()
    kwargs = {} if bin_dir is None else {"bin_dir": bin_dir}
    verdicts = sc.classify_population(names, **kwargs)
    live: set[tuple[str, str, str]] = set()
    for verdict in verdicts:
        live.update(_verdict_entries(verdict))
    return live


def _assert_no_new(live: set[tuple[str, str, str]]) -> None:
    """The guard's own comparison, extracted so both the real guard
    (`test_no_new_warm_serve_violations`) and the fail-closed leg
    (`test_fails_closed_on_each_unservable_shape`) call the SAME function
    rather than the leg re-implementing a copy of it."""
    baseline = set(_BASELINE)
    new = sorted(live - baseline)
    rendered = "\n".join(f"  {name}: [{reason}] {text}" for name, reason, text in new)
    assert new == [], (
        f"Found {len(new)} NEW warm-serve violation(s) not in _BASELINE "
        f"-- resolve them or, if genuinely unfixable, add them to the "
        f"baseline with a stated reason:\n{rendered}"
    )


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
    _assert_no_new(_live_entries())


def test_live_population_matches_allowlist_file():
    """Pins the population itself, not just the derived findings: a
    silently-shrunk `load_allowlist_names()` (wrong `_ALLOWLIST_PATH`, a
    `bin_dir` regression, a swallowed exception) would make
    `test_no_new_warm_serve_violations` and the fail-closed leg both pass
    vacuously over an empty or partial set. This leg reads the allowlist
    file directly and cross-checks both that `load_allowlist_names()`
    matches it exactly and that `classify_population` returns exactly one
    verdict per name -- no silent drop, no silent duplicate."""
    names = sc.load_allowlist_names()
    on_disk = json.loads(Path(sc._ALLOWLIST_PATH).read_text(encoding="utf-8"))
    assert set(names) == set(on_disk["entrypoints"])
    verdicts = sc.classify_population(names)
    assert len(verdicts) == len(names)
    assert {v.name for v in verdicts} == set(names)


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
    assert _BASELINE == []


def test_scans_the_live_allowlist_not_a_frozen_copy(tmp_path):
    """Guards against the ratchet degrading into a static fixture: writes a
    real allowlist file, reads it via `load_allowlist_names(path=...)`,
    mutates the file on disk, and asserts the SECOND read reflects the
    mutation -- a frozen copy (a module-level constant, or an
    `lru_cache`-wrapped `_live_entries`) would return the stale first
    result and fail this test."""
    allowlist_path = tmp_path / "warm_entrypoint_allowlist.json"
    allowlist_path.write_text(
        json.dumps({"entrypoints": ["alpha", "beta"]}), encoding="utf-8"
    )
    names_first = sc.load_allowlist_names(path=allowlist_path)
    assert names_first == ["alpha", "beta"]

    allowlist_path.write_text(
        json.dumps({"entrypoints": ["alpha", "beta", "gamma"]}), encoding="utf-8"
    )
    names_second = sc.load_allowlist_names(path=allowlist_path)
    assert names_second == ["alpha", "beta", "gamma"], (
        "load_allowlist_names did not observe the on-disk mutation -- it "
        "is reading a frozen copy, not the live file"
    )


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

    live = _live_entries(names=names, bin_dir=tmp_path)

    # Drive the SAME comparison the real guard calls -- not a copy of it --
    # so this leg actually proves the guard fails closed rather than
    # merely proving `serve_classifier` + a set-difference flag shapes.
    with pytest.raises(AssertionError) as excinfo:
        _assert_no_new(live)
    message = str(excinfo.value)

    new = sorted(live - set(_BASELINE))
    flagged = {name for name, _reason, _text in new}
    assert flagged == set(names), (
        "the C8 comparison did not fail closed on every unservable shape; "
        f"missed {sorted(set(names) - flagged)}"
    )
    for name in names:
        assert name in message, f"{name} missing from _assert_no_new's failure message"

    # dict[str, set[str]] rather than a collapsed scalar -- a name with
    # multiple findings sharing a reason today must not make this
    # assertion depend on dict-comprehension overwrite order tomorrow.
    reasons: dict[str, set[str]] = {}
    for name, reason, _text in new:
        reasons.setdefault(name, set()).add(reason)
    assert reasons["shape-no-script"] == {"no_script"}
    assert reasons["shape-no-main"] == {"no_main"}
    assert reasons["shape-zero-arity"] == {"zero_arity_main"}
    assert "module-scope non-stdlib import" in reasons["shape-impure-body"]
    assert "module-scope process mutation" in reasons["shape-sys-path-mutation"]
