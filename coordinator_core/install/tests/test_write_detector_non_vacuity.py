"""
coordinator_core.install.tests.test_write_detector_non_vacuity -- the
non-vacuity harness for the inverted write-detector
(``test_write_reaching_modules_declare.py``, C2 of the same plan).

Spec backlink: docs/plans/2026-09-11-invert-the-write-detector-prove-read-only.md
(C3, gated on C1's spike verdict and C2's rewritten detector).
Spike verdict consulted: docs/research/spike-verdicts/2026-09-11-write-detector-mutation-harness-viability.md
-- VIABLE against the pre-C2 file (59 sites, 12 tests, 0.32s), but this
row's own body requires a RE-MEASURE against the actual post-C2 file
before committing to ARM A, because C2 adds ``_proves_read_only``, a
verdict pass, a stale self-check, the AC1 corpus, and the P1/P2
accept/reject pairs -- a materially larger mutant-times-test product.

Re-measured result: 19 target functions (every module-level non-``test_``
function in the post-C2 file, DERIVED by AST walk -- never hand-listed),
112 branch sites (the same closed operator list as the spike: negate an
``if``/``elif``/``IfExp`` test; flip a ``return True``/``return False``;
swap ``and``/``or`` in a ``BoolOp``), against 40 kill-set tests (every
``test_`` function that does not call ``_install_modules`` -- the tests
that scan the real 45 modules are excluded, same exclusion the spike's
row specified). Full per-function-deepcopy + shallow-module-body-splice
harness, run three times: 3.69s / 3.71s / 3.76s process time
(``time.process_time()``), zero subprocess spawns. This is roughly 11-12x
the spike's own 0.32s on ~1.9x the site count and ~3.3x the test count --
consistent with the spike's own linear-extrapolation warning, but the
actual multiplier lands past the 0.6s-1.0s estimate because the kill-set
tests themselves call detector functions (``_flagged_calls`` and friends)
that re-``ast.parse`` their module argument on every call, and 40 tests x
up to 112 mutants multiplies that parse cost far past the spike's smaller
8-function/12-test product. ALL FOUR runs are over the 500ms/zero-spawn
budget on the process-time leg -- ARM A is NOT-VIABLE at this file's
actual size. Per this row's body ("if the re-measured run goes over the
500ms/zero-spawn budget, switch to ARM B and name the leg that went over
budget"): the process-time leg is the one that went over budget (spawns
stayed at zero throughout). This file implements ARM B.

This file's own six tests (ARM B, below) run three times: 0.31s / 0.35s / 0.33s wall (pytest collection + run), well under the 500ms bar, zero subprocess spawns -- the coverage/staleness/self-check tests are pure AST walks over one already-parsed tree, and the witness-execution test calls at most 40 already-defined functions once each, none of which spawn a process.

ARM B -- per-branch fixture table. ``_BRANCH_FIXTURES`` below is keyed by
(function name, operator kind, in-function occurrence index) -- the same
three-part identity the coverage/staleness checks below re-derive fresh
from the live detector AST every run, so a site that moves, disappears,
or a new one that appears is caught structurally rather than by a stale
hand list. The occurrence index stands in for ``ast.unparse`` of the
site's test/return/boolop expression as this row's body asks for; the
literal unparsed text is carried alongside each derived site (see
``_derive_branch_sites``) for a human reading the table, but the index is
the part actually compared for equality, because two distinct sites
occasionally share identical unparsed text (e.g. ``return False`` appears
twice in ``_declares_write_surface``) and only the occurrence index tells
them apart.

Each entry's reason records the result of a ONE-TIME, by-hand
revert-and-observe pass performed once against the real (pre-this-row)
detector file at authoring time (2026-09-19), using the exact ARM A
mechanism above as a throwaway authoring tool, never shipped or re-run by
this file: negate/flip/swap the one site, exec the mutant, run every
kill-set test against it, and record which test (if any) raised. Where a
test raised, that test's name is the fixture's witness: this file calls
that witness test once more, at every future run, against the REAL
(unmutated) detector code loaded fresh from disk, and asserts it does not
raise -- proof the witness still exists and still passes, not a
re-verification that reverting the branch still fails it. THIS IS THE
NAMED GAP ARM A would have closed and ARM B does not: a future edit that
silently guts a branch's behavior while leaving its witness test
unchanged and still green is invisible to this file. Where no kill-set
test raised (26 of 112 sites), the entry says so plainly -- a real gap in
C2's file's synthetic-fixture coverage, reported here (see ``_KNOWN_GAPS``
below) and to the dispatching EM, per this row's own instruction not to
edit C2's file from this row.

Negative spec: this file does not re-verify on future edits that a branch
site's behavior still matters -- only that (a) the site still exists,
(b) its recorded witness test (if any) still exists and still passes
against live code, and (c) a newly-introduced, unwitnessed site is
flagged as newly missing coverage. A change to C2's file that both
removes a branch's real effect AND leaves its witness test's assertions
still true by coincidence defeats this harness exactly as ARM A's own
"does not re-verify" gap is described in this row's spec -- except ARM B
never had the closing property to begin with, so this is not a
regression, it is the accepted cost of switching arms.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

_THIS_FILE = Path(__file__).resolve()
_DETECTOR_FILE = _THIS_FILE.parent / "test_write_reaching_modules_declare.py"

# Every module-level non-`test_` function is a target -- DERIVED below by
# `_target_function_names`, never hand-listed. Nothing is excluded: every
# such function in the post-C2 file happens to contain at least a
# recognized site kind, or none at all (contributing zero sites, still
# "targeted" in the sense of having been scanned).
_UNTARGETED: dict[str, str] = {}


def _detector_source() -> str:
    return _DETECTOR_FILE.read_text(encoding="utf-8")


def _detector_tree() -> ast.Module:
    return ast.parse(_detector_source(), filename=str(_DETECTOR_FILE))


def _detector_namespace() -> dict:
    """Exec the detector module fresh, by path -- never `import`, which
    would resolve the installed package copy rather than proving anything
    about the actual bytes on disk this row's exit criterion cares about."""
    ns: dict = {"__file__": str(_DETECTOR_FILE), "__name__": "write_detector_under_test"}
    code = compile(_detector_tree(), filename=str(_DETECTOR_FILE), mode="exec")
    exec(code, ns)  # noqa: S102 -- loading the module-under-test by path, per this row's instruction
    return ns


def _target_function_names(tree: ast.Module) -> list[str]:
    return [
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("test_")
    ]


def _find_func(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise LookupError(name)


def _references_install_modules(func_node: ast.FunctionDef) -> bool:
    """True iff `func_node`'s body calls `_install_modules()` anywhere --
    the scan-the-real-45-modules exclusion this row's body carries over
    from C1's spike row."""
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "_install_modules":
                return True
    return False


def _kill_set_test_names(tree: ast.Module) -> set[str]:
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and not _references_install_modules(node)
    }


class _Site:
    __slots__ = ("func", "kind", "idx", "unparse", "lineno")

    def __init__(self, func: str, kind: str, idx: int, unparse: str, lineno: int) -> None:
        self.func = func
        self.kind = kind
        self.idx = idx
        self.unparse = unparse
        self.lineno = lineno

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.func, self.kind, self.idx)


def _derive_branch_sites(tree: ast.Module) -> list[_Site]:
    """The closed operator list: negate an `if`/`elif`/`IfExp` test (`elif`
    is a nested `If` in `orelse`, already covered by `ast.walk`); flip a
    `return True`/`return False`; swap `and`/`or` in a `BoolOp`. One `_Site`
    per occurrence, in `ast.walk` traversal order -- deterministic given an
    unchanged function body, which is exactly what makes the occurrence
    index a stable part of a site's identity across runs of this file."""
    sites: list[_Site] = []
    for fname in _target_function_names(tree):
        func_node = _find_func(tree, fname)
        idx = 0
        for node in ast.walk(func_node):
            kind: Optional[str] = None
            relevant: Optional[ast.AST] = None
            if isinstance(node, (ast.If, ast.IfExp)):
                kind = "negate_test"
                relevant = node.test
            elif (
                isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, bool)
            ):
                kind = "flip_return_bool"
                relevant = node
            elif isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
                kind = "swap_bool_op"
                relevant = node
            if kind is None:
                continue
            sites.append(_Site(fname, kind, idx, ast.unparse(relevant), node.lineno))
            idx += 1
    return sites


# ---------------------------------------------------------------------------
# _BRANCH_FIXTURES -- the by-hand revert-and-observe record (D-section of
# the module docstring above). Key: (function name, operator kind,
# in-function occurrence index). Value: the witness `test_` function name
# that failed when this site was reverted at authoring time, or `None` for
# a site whose revert killed nothing in the current kill-set (a real gap,
# see `_KNOWN_GAPS`).
# ---------------------------------------------------------------------------

_BRANCH_FIXTURES: dict[tuple[str, str, int], Optional[str]] = {
    ("_is_str_replace_false_positive", "negate_test", 0): None,
    ("_is_str_replace_false_positive", "flip_return_bool", 1): None,
    ("_subprocess_imported_names", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_subprocess_imported_names", "swap_bool_op", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_subprocess_imported_names", "swap_bool_op", 2): None,
    ("_module_imported_as", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_module_imported_as", "negate_test", 1): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_module_imported_as", "swap_bool_op", 2): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_from_import_bound_names", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_from_import_bound_names", "negate_test", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_from_import_bound_names", "swap_bool_op", 2): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_bare_open_fdopen_names_rebound", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_bare_open_fdopen_names_rebound", "negate_test", 1): "test_bare_open_rebound_by_from_import_is_detected",
    ("_bare_open_fdopen_names_rebound", "swap_bool_op", 2): "test_bare_open_rebound_by_from_import_is_detected",
    ("_qualname_for_scope", "negate_test", 0): "test_proves_read_only_referenced_only_by_exemption_pass_and_tests",
    ("_walk_calls", "negate_test", 0): "test_proves_read_only_referenced_only_by_exemption_pass_and_tests",
    ("_walk_calls", "negate_test", 1): "test_proves_read_only_referenced_only_by_exemption_pass_and_tests",
    ("_walk_calls", "negate_test", 2): "test_proves_read_only_referenced_only_by_exemption_pass_and_tests",
    ("_flagged_calls", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_flagged_calls", "negate_test", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_flagged_calls", "negate_test", 2): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_flagged_calls", "negate_test", 3): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_flagged_calls", "negate_test", 4): None,
    ("_flagged_calls", "negate_test", 5): None,
    ("_flagged_calls", "negate_test", 6): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_flagged_calls", "negate_test", 7): None,
    ("_flagged_calls", "swap_bool_op", 8): None,
    ("_flagged_calls", "negate_test", 9): None,
    ("_flagged_calls", "swap_bool_op", 10): None,
    ("_declares_write_surface", "flip_return_bool", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_declares_write_surface", "negate_test", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_declares_write_surface", "negate_test", 2): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_declares_write_surface", "swap_bool_op", 3): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_declares_write_surface", "negate_test", 4): None,
    ("_declares_write_surface", "negate_test", 5): "test_read_only_opens_staleness_detects_module_now_declares",
    ("_declares_write_surface", "flip_return_bool", 6): None,
    ("_declares_write_surface", "swap_bool_op", 7): None,
    ("_declares_write_surface", "flip_return_bool", 8): "test_read_only_opens_staleness_detects_module_now_declares",
    ("_install_modules", "swap_bool_op", 0): None,
    ("_p1_mode_is_read_only", "negate_test", 0): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_p1_mode_is_read_only", "negate_test", 1): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_p1_mode_is_read_only", "negate_test", 2): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_p1_mode_is_read_only", "negate_test", 3): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_p1_mode_is_read_only", "flip_return_bool", 4): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_p1_mode_is_read_only", "flip_return_bool", 5): None,
    ("_p1_mode_is_read_only", "flip_return_bool", 6): None,
    ("_p1_mode_is_read_only", "negate_test", 7): None,
    ("_p1_mode_is_read_only", "swap_bool_op", 8): None,
    ("_p2_flags_are_read_only", "negate_test", 0): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "negate_test", 1): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "flip_return_bool", 2): "test_proves_read_only_p2_os_open_no_flags_rejects",
    ("_p2_flags_are_read_only", "negate_test", 3): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "negate_test", 4): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "flip_return_bool", 5): None,
    ("_p2_flags_are_read_only", "negate_test", 6): None,
    ("_p2_flags_are_read_only", "swap_bool_op", 7): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "swap_bool_op", 8): None,
    ("_p2_flags_are_read_only", "negate_test", 9): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_p2_flags_are_read_only", "swap_bool_op", 10): "test_proves_read_only_p2_os_open_mixed_flags_rejects",
    ("_p2_flags_are_read_only", "flip_return_bool", 11): None,
    ("_proves_read_only", "negate_test", 0): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "negate_test", 1): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "negate_test", 2): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "negate_test", 3): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "negate_test", 4): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "negate_test", 5): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_proves_read_only", "flip_return_bool", 6): None,
    ("_proves_read_only", "flip_return_bool", 7): "test_proves_read_only_rejects_star_args",
    ("_proves_read_only", "flip_return_bool", 8): "test_proves_read_only_rejects_double_star_kwargs",
    ("_proves_read_only", "flip_return_bool", 9): "test_proves_read_only_rejects_opener_keyword",
    ("_proves_read_only", "flip_return_bool", 10): "test_proves_read_only_rejects_eighth_positional_opener_slot",
    ("_proves_read_only", "negate_test", 11): "test_proves_read_only_p1_bare_open_no_mode_accepts",
    ("_proves_read_only", "flip_return_bool", 12): None,
    ("_proves_read_only", "negate_test", 13): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_proves_read_only", "negate_test", 14): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_proves_read_only", "negate_test", 15): "test_proves_read_only_p1_os_fdopen_read_mode_accepts",
    ("_proves_read_only", "negate_test", 16): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_proves_read_only", "flip_return_bool", 17): "test_proves_read_only_unresolved_receiver_never_proves",
    ("_proves_read_only", "negate_test", 18): "test_proves_read_only_p1_io_open_read_mode_accepts",
    ("_proves_read_only", "flip_return_bool", 19): None,
    ("_proves_read_only", "swap_bool_op", 20): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_proves_read_only", "swap_bool_op", 21): "test_proves_read_only_p2_os_open_read_flags_accepts",
    ("_proves_read_only", "swap_bool_op", 22): None,
    ("_open_family_hits_excused", "flip_return_bool", 0): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_open_family_hits_excused", "negate_test", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_open_family_hits_excused", "negate_test", 2): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_open_family_hits_excused", "negate_test", 3): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_open_family_hits_excused", "negate_test", 4): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_open_family_hits_excused", "flip_return_bool", 5): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_open_family_hits_excused", "swap_bool_op", 6): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_open_family_hits_excused", "flip_return_bool", 7): "test_open_family_hits_excused_rejects_rebound_bare_name",
    ("_open_family_hits_excused", "flip_return_bool", 8): "test_open_family_hits_excused_requires_both_entry_and_proof",
    ("_module_verdicts", "negate_test", 0): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_module_verdicts", "negate_test", 1): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_module_verdicts", "negate_test", 2): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_module_verdicts", "negate_test", 3): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_module_verdicts", "negate_test", 4): "test_ac1_open_family_shapes_all_flag_as_undeclared",
    ("_read_only_opens_staleness", "swap_bool_op", 0): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "negate_test", 1): "test_read_only_opens_staleness_detects_missing_module",
    ("_read_only_opens_staleness", "negate_test", 2): "test_read_only_opens_staleness_detects_missing_site",
    ("_read_only_opens_staleness", "negate_test", 3): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "negate_test", 4): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "negate_test", 5): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "negate_test", 6): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "swap_bool_op", 7): "test_read_only_opens_staleness_detects_disproven_site",
    ("_read_only_opens_staleness", "swap_bool_op", 8): "test_read_only_opens_staleness_detects_missing_site",
    ("_find_open_fdopen_arm", "negate_test", 0): "test_flagged_calls_open_arm_is_argument_blind",
    ("_find_open_fdopen_arm", "swap_bool_op", 1): "test_flagged_calls_open_arm_is_argument_blind",
    ("_find_open_fdopen_arm", "negate_test", 2): "test_flagged_calls_open_arm_is_argument_blind",
    ("_find_open_fdopen_arm", "swap_bool_op", 3): "test_flagged_calls_open_arm_is_argument_blind",
    ("_find_open_fdopen_arm", "negate_test", 4): "test_flagged_calls_open_arm_is_argument_blind",
    ("_find_open_fdopen_arm", "swap_bool_op", 5): None,
}

# Sites whose revert killed nothing in the current kill-set -- real gaps in
# C2's file's synthetic-fixture coverage, reported (per this row's body: "a
# survivor or unkilled branch that is a real gap is a missing test in C2's
# file. Report it. Do not edit C2's file from this row.") rather than
# fixed here.
_KNOWN_GAPS: frozenset[tuple[str, str, int]] = frozenset(
    key for key, witness in _BRANCH_FIXTURES.items() if witness is None
)


def test_branch_sites_have_full_fixture_coverage() -> None:
    """Every branch site the live detector file's AST derives right now
    must have a `_BRANCH_FIXTURES` entry -- the coverage half of the
    stale-check shape `_ALLOWLIST`/`_READ_ONLY_OPENS` use elsewhere in this
    package."""
    tree = _detector_tree()
    current_keys = {site.key for site in _derive_branch_sites(tree)}
    missing = sorted(current_keys - _BRANCH_FIXTURES.keys())
    assert not missing, (
        f"branch site(s) with no _BRANCH_FIXTURES entry: {missing} -- "
        "revert-and-observe by hand once and add a reasoned entry (witness "
        "test name, or None with a gap reported to the EM)"
    )


def test_branch_fixtures_have_no_stale_entries() -> None:
    """Every `_BRANCH_FIXTURES` key must still name a site the live AST
    actually derives -- otherwise the entry is dead weight, same failure
    mode `_ALLOWLIST`'s own staleness test guards against."""
    tree = _detector_tree()
    current_keys = {site.key for site in _derive_branch_sites(tree)}
    stale = sorted(set(_BRANCH_FIXTURES.keys()) - current_keys)
    assert not stale, f"stale _BRANCH_FIXTURES entries (site no longer derived): {stale}"


def test_branch_fixture_witnesses_still_pass_against_live_code() -> None:
    """Every witnessed entry's recorded test function must still exist in
    the live detector file and still pass when run against the REAL
    (unmutated) code -- proof the witness survives, not a re-verification
    that reverting the branch still fails it (see the module docstring's
    negative spec)."""
    ns = _detector_namespace()
    tree = _detector_tree()
    kill_set = _kill_set_test_names(tree)
    missing_witnesses: list[str] = []
    failing_witnesses: list[str] = []
    for key, witness in _BRANCH_FIXTURES.items():
        if witness is None:
            continue
        if witness not in kill_set or witness not in ns:
            missing_witnesses.append(f"{key} -> {witness}")
            continue
        test_fn = ns[witness]
        try:
            if "tmp_path" in test_fn.__code__.co_varnames[: test_fn.__code__.co_argcount]:
                import tempfile

                with tempfile.TemporaryDirectory(prefix="c3-witness-") as td:
                    test_fn(Path(td))
            else:
                test_fn()
        except Exception as exc:  # noqa: BLE001
            failing_witnesses.append(f"{key} -> {witness}: {exc!r}")
    assert not missing_witnesses, f"witness test(s) no longer present: {missing_witnesses}"
    assert not failing_witnesses, f"witness test(s) now fail against live code: {failing_witnesses}"


def test_known_gaps_are_all_and_only_the_unwitnessed_sites() -> None:
    """`_KNOWN_GAPS` must derive exactly from `_BRANCH_FIXTURES`'s `None`
    entries -- guards the gap-reporting set itself against silent drift."""
    derived_gaps = frozenset(key for key, witness in _BRANCH_FIXTURES.items() if witness is None)
    assert _KNOWN_GAPS == derived_gaps


def test_self_check_flags_a_site_with_no_fixture_entry() -> None:
    """Self-test (per this row's body): plant a synthetic branch site with
    no `_BRANCH_FIXTURES` entry and assert the coverage check reports it.
    Uses a throwaway augmented copy of the detector AST -- never touches
    the real file on disk."""
    tree = _detector_tree()
    synthetic_source = (
        "def _synthetic_untested_probe(x):\n"
        "    if x:\n"
        "        return True\n"
        "    return False\n"
    )
    synthetic_node = ast.parse(synthetic_source).body[0]
    ast.copy_location(synthetic_node, tree.body[0])
    ast.fix_missing_locations(synthetic_node)
    augmented_body = list(tree.body) + [synthetic_node]
    augmented_tree = ast.Module(body=augmented_body, type_ignores=[])

    augmented_keys = {site.key for site in _derive_branch_sites(augmented_tree)}
    missing = augmented_keys - _BRANCH_FIXTURES.keys()
    assert missing, "self-check failed to plant a detectable uncovered site"
    assert all(key[0] == "_synthetic_untested_probe" for key in missing), (
        f"self-check's planted site produced unexpected missing keys: {missing}"
    )


def test_branch_fixtures_key_count_matches_derived_site_count() -> None:
    """Sanity floor mirroring `test_at_least_five_modules_declare_write_surface`'s
    own role in the detector file -- guards against the derivation itself
    silently seeing nothing (e.g. a target-function-name typo)."""
    tree = _detector_tree()
    derived = _derive_branch_sites(tree)
    assert len(derived) == len(_BRANCH_FIXTURES) == 112, (
        f"expected 112 derived sites and 112 _BRANCH_FIXTURES entries, "
        f"got {len(derived)} derived / {len(_BRANCH_FIXTURES)} fixtures -- "
        "the detector file changed shape; re-run the revert-and-observe "
        "pass and update this table"
    )
