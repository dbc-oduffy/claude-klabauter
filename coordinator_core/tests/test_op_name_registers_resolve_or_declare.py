"""Op-name population derivation, with a canary (P060-C2).

Spec: `docs/plans/2026-09-11-clear-the-op-name-class-from-the-outlier.md` (C2). This is a NEW
sibling module, not an edit to `test_every_register_resolves_or_declares.py`: that module's green
run means "every CORE-45 row resolves", and widening its population would silently change what a
citation of it means. Sibling-imports `REPO_ROOT` from the established sweep next door rather than
re-deriving it -- the established shape `test_deep_per_item_spawn_worklist.py` already uses against
`test_no_unbatched_per_item_git_spawn.py`.

`_derive_op_names()` feeds `register_rows.resolve_row`'s injected `op_names` oracle
(`coordinator_core/tests/register_rows.py`) for `SubjectClass.OP_NAME` rows -- C3 enrols the
registers that consume it. Nothing here declares an op-name row itself.

`_REGISTRY_MAP_REJECTION`. `coordinator_core/ops/_registry_map.py` (the hand-maintained
best-effort op-name -> owning-module lazy-import seam) was considered and REJECTED as the
resolution oracle: its own docstring declares it a PERFORMANCE OPTIMIZATION whose staleness
degrades to a fallback import, not a correctness gate, and two live counter-examples prove it
under-covers the true registry --
`@register_op("baton.carry_forward")` (`coordinator_core/ops/baton_carry_forward.py`) and
`register_op("memo.check_deliveries")` (`coordinator_core/ops/fleet/memo_send.py`) are both
registered live yet the map (as measured 2026-09-11, census row 4) has zero hits for either.
Resolving op-name rows against it alone would manufacture false-ABSENT verdicts -- the Q2
false-positive failure this workstream exists to avoid. This sweep therefore derives its own
population by byte scan rather than reading that map, and never will
(`test_does_not_read_the_registry_map` below pins the negative). Do not add a test asserting some
derived op name is absent from that map either -- such a test guards an argument, not a behaviour:
this sweep never reads the map, the rejection already lives here plus the plan's own census/anti-
scope, and the check is INVERTED (it goes red the day the map becomes complete, and the only remedy
is deleting it).

MEASUREMENT, FAILURE DIRECTION, HORIZON (P060-C5). Three things the baton's last two ACs require,
all landing here rather than in a fourth module.

  1. `test_full_sweep_process_time_and_spawn_count_within_the_brightline` reports
     `time.process_time()` for `_derive_op_names()` plus `TrackedFileIndex.build()` -- the two
     components a full sweep run is made of, membership checks against the derived frozenset being
     O(1) and therefore not separately timed -- and the spawn count, against DR-344's 500ms. A
     number over the bar is a design finding about the shape, never something to trim rows to fit
     (four rounds on `ceremony.scoped_git_commit` proved shaving does not work).
  2. FAILURE DIRECTION. This sweep derives its oracle every run, so it cannot fail as
     absence-reads-as-THE-OLD-VALUE -- there is no frozen artifact to go stale. It CAN fail three
     other ways: (a) a derivation that silently returns a wrong set (wrong root, broken regex,
     empty substrate) -- what `_OP_CANARY` exists to catch; (b) a register nobody enrolled, which
     stays invisible to this sweep entirely and is what C6's residual count makes visible, never
     this module; (c) the byte scan OVER-approximates -- it sees every `register_op` literal,
     including a registration in a module the live dispatcher never imports or a dead/string-
     embedded call site, so the sweep can report RESOLVED for a row naming an op registered in
     source but unreachable at dispatch (a false-GREEN, the mirror of the false-ABSENT
     `_registry_map.py` would manufacture). Measured 2026-09-24 (this dispatch's box): 296 op names
     the live registry actually populates against a raw-scan population that over-approximates it
     -- a fixed relationship of this scan's shape, not a number this module re-derives at runtime;
     re-deriving it here would mean importing the ops package per run, which is exactly the 312.5ms
     leg the anti-scope forbids. This is chosen deliberately over `_registry_map.py`'s
     under-approximation direction: a false-RESOLVED leaves a stale row in place, a false-ABSENT
     manufactures work on a live op (the Q2 failure this workstream exists to avoid).
  3. HORIZON. `COVERAGE_HORIZON` plus a closed-citer test, below -- the shape
     `test_the_one_hop_horizon_is_published_where_this_gate_is_cited` uses in
     `test_no_unbatched_per_item_git_spawn.py` and
     `test_the_resolution_horizon_is_published_where_this_sweep_is_cited` uses in
     `test_every_register_resolves_or_declares.py`. This is the THIRD hand-copied instance of the
     horizon-plus-citer-test convention; the next plan needing a fourth should find the count
     already in front of it rather than discover it -- this module does not factor the three into a
     shared helper, that is a fourth-instance decision left to whoever needs it.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.tests.register_rows import TrackedFileIndex
from coordinator_core.tests.test_every_register_resolves_or_declares import REPO_ROOT

#: `register_op(<literal-string>)` -- a plain or f-prefixed-looking call is not matched on
#: purpose: the byte scan only trusts a quoted string literal as the first positional argument,
#: single- or double-quoted, so a call passing a variable (`register_op(OP_NAME)`,
#: `register_op(_OP_NAME)`) is silently skipped rather than mis-parsed -- undercounting, never
#: fabricating a name. No `ast.parse`, no import, no subprocess.
_REGISTER_OP_CALL_RE = re.compile(r"register_op\(\s*[\"']([^\"']+)[\"']")

#: Cheap pre-filter: a file whose raw bytes never contain this substring cannot contain a
#: `register_op(...)` call, so it is skipped before any regex work.
_REGISTER_OP_SUBSTRING = "register_op("

#: The package this scan walks -- the same root the plan's own census measured (313 raw names,
#: 296 after root-discipline exclusion, 2026-09-11).
_SCAN_ROOT = "coordinator_core"


def _is_test_only_path(relpath: str) -> bool:
    """ROOT DISCIPLINE: exclude any path with a `tests/` segment OR a `test_*.py` basename.

    Not a single `**/tests/**` glob, which misses modules registering ops from `test_*.py` files
    that sit OUTSIDE a `tests/` directory -- e.g. `coordinator_core/install/test_prereq_probe.py`,
    `coordinator_core/ops/test_baton_drift_sweep.py`. A test-only op name surviving into a
    production register is a FINDING for a later chunk to disposition, not a resolution this scan
    should absorb by widening its population.
    """
    parts = relpath.split("/")
    if "tests" in parts[:-1]:
        return True
    return Path(relpath).name.startswith("test_")


def _derive_op_names(repo_root: Path = REPO_ROOT, scan_root: str = _SCAN_ROOT) -> frozenset[str]:
    """Byte-level scan for `register_op(<literal>)` over `scan_root`, root-discipline-excluded.

    No `ast.parse`, no import, no subprocess -- a plain substring pre-filter plus one regex per
    surviving file. Measured 180.5ms process time / 0 spawns over 4078 `coordinator_core/*.py`
    files on 2026-09-24 (this dispatch's box) -- the corpus grew since the plan's own 2026-09-11
    figure (156.2ms / 3620 files); report the number actually measured on the run, never assert
    either one as a pin.
    """
    root = repo_root / scan_root
    names: set[str] = set()
    for file_path in root.rglob("*.py"):
        relpath = file_path.relative_to(repo_root).as_posix()
        if _is_test_only_path(relpath):
            continue
        text = file_path.read_text(encoding="utf-8", errors="replace")
        if _REGISTER_OP_SUBSTRING not in text:
            continue
        for match in _REGISTER_OP_CALL_RE.finditer(text):
            names.add(match.group(1))
    return frozenset(names)


#: AC2-shaped canary: names that MUST be present in the derived set, picked from different
#: registering packages so a root that silently narrowed to one subtree fails loudly rather than
#: reading as a smaller-but-plausible population. Each member's LIVE registration was verified by
#: hand at authoring time (2026-09-24) -- the live registry is the oracle for the canary, never
#: for the run itself:
#:   - `coordinator_core/ops/percolate_ci_smoke_check.py:134` -- `@register_op("percolate.run_ci_smoke_check")`
#:   - `coordinator_core/hooks/agent_completion_log.py:90` -- `@register_op("hooks.agent_completion_log")`
#:   - `coordinator_core/baton_assemble/ops.py:59` -- `@register_op("baton_assemble.brief")`
_OP_CANARY: frozenset[str] = frozenset(
    {
        "percolate.run_ci_smoke_check",
        "hooks.agent_completion_log",
        "baton_assemble.brief",
    }
)


def test_derived_population_contains_the_canary() -> None:
    """AC2's reads-as-THE-OLD-VALUE check: collapses wrong-root, broken-regex and empty-
    substrate into one loud failure. Three canary members, three different registering packages
    (`ops/`, `hooks/`, `baton_assemble/`) -- a root that silently narrowed to any one of them
    still fails here."""
    derived = _derive_op_names()
    missing = _OP_CANARY - derived
    assert not missing, (
        f"canary op name(s) missing from the derived population: {sorted(missing)!r} -- "
        "the derivation likely regressed to a wrong root, a broken regex, or an empty substrate"
    )


def test_root_discipline_excludes_tests_dir_and_test_star_basenames(tmp_path: Path) -> None:
    """A synthetic corpus with three `register_op(...)` sites -- one production, one inside a
    `tests/` directory, one a `test_*.py` file OUTSIDE any `tests/` directory (the seven-module
    leak a bare `**/tests/**` glob would miss) -- and only the production site survives."""
    scan_root = tmp_path / "coordinator_core"

    prod_file = scan_root / "ops" / "real_op.py"
    prod_file.parent.mkdir(parents=True)
    prod_file.write_text('register_op("prod.real_op")\n', encoding="utf-8")

    tests_dir_file = scan_root / "ops" / "tests" / "test_something.py"
    tests_dir_file.parent.mkdir(parents=True)
    tests_dir_file.write_text('register_op("synthetic.in_tests_dir")\n', encoding="utf-8")

    test_star_outside_tests_dir = scan_root / "install" / "test_prereq_probe.py"
    test_star_outside_tests_dir.parent.mkdir(parents=True)
    test_star_outside_tests_dir.write_text(
        'register_op("synthetic.test_star_outside_tests")\n', encoding="utf-8"
    )

    derived = _derive_op_names(repo_root=tmp_path, scan_root="coordinator_core")
    assert derived == frozenset({"prod.real_op"})


def test_only_a_quoted_literal_first_argument_is_recognized(tmp_path: Path) -> None:
    """A `register_op(SOME_VARIABLE)` call -- no quoted literal in first-argument position -- is
    silently skipped, never mis-parsed into a fabricated name. Undercounting on purpose: this scan
    has no AST and cannot resolve what `SOME_VARIABLE` binds to."""
    scan_root = tmp_path / "coordinator_core"
    scan_root.mkdir(parents=True)
    (scan_root / "indirect_op.py").write_text(
        "OP_NAME = 'indirect.example'\n"
        "register_op(OP_NAME)\n"
        'register_op("direct.example")\n',
        encoding="utf-8",
    )

    derived = _derive_op_names(repo_root=tmp_path, scan_root="coordinator_core")
    assert derived == frozenset({"direct.example"})


def test_root_discipline_scan_finds_more_names_before_exclusion_than_after() -> None:
    """Sanity over the real corpus: test-only registrations exist (synthetic ops registered by
    test modules), so the raw scan population must be strictly larger than the root-discipline-
    excluded one -- census row 5's finding (313 raw vs 296 post-exclusion, measured 2026-09-11).
    Neither figure is asserted as a pin; this only asserts the FINDING's direction survives."""

    def _raw_derive() -> frozenset[str]:
        root = REPO_ROOT / _SCAN_ROOT
        names: set[str] = set()
        for file_path in root.rglob("*.py"):
            text = file_path.read_text(encoding="utf-8", errors="replace")
            if _REGISTER_OP_SUBSTRING not in text:
                continue
            for match in _REGISTER_OP_CALL_RE.finditer(text):
                names.add(match.group(1))
        return frozenset(names)

    raw = _raw_derive()
    excluded = _derive_op_names()
    assert excluded <= raw
    assert len(excluded) < len(raw), (
        "expected the root-discipline exclusion to drop at least one test-only registration "
        f"(raw={len(raw)}, post-exclusion={len(excluded)}) -- a test-only op name surviving into "
        "a production register is a finding for a later chunk to disposition, not silence here"
    )


# ---------------------------------------------------------------------------
# P060-C5 -- measurement against DR-344's 500ms brightline
# ---------------------------------------------------------------------------


def test_full_sweep_process_time_and_spawn_count_within_the_brightline(monkeypatch) -> None:
    """Reports `time.process_time()` and the subprocess count for a full sweep run -- op
    derivation plus `TrackedFileIndex.build()`, the two components an enrolled row's resolution is
    built from (per-row membership against the derived frozenset is O(1), nothing left to time).
    Process time and spawn count, never wall clock (DR-344).

    A total over 500ms, or more than the one sanctioned `git ls-files` spawn
    `TrackedFileIndex.build()` already owns, is a design finding about the shape -- STOP and
    report rather than trimming rows to fit; see the module docstring's § MEASUREMENT.
    """
    spawn_count = 0
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    start = time.process_time()
    op_names = _derive_op_names()
    TrackedFileIndex.build(REPO_ROOT)
    elapsed_ms = (time.process_time() - start) * 1000.0

    assert op_names, "derived population must not be empty -- see _OP_CANARY"
    assert spawn_count == 1, (
        f"expected exactly the one sanctioned TrackedFileIndex.build() git spawn, "
        f"got {spawn_count}"
    )
    assert elapsed_ms < 500.0, (
        f"full sweep measured {elapsed_ms:.1f}ms process time against DR-344's 500ms brightline "
        "-- do not trim rows to fit; report and stop"
    )


# ---------------------------------------------------------------------------
# P060-C5 -- the resolution horizon is published where this sweep is cited
# ---------------------------------------------------------------------------

#: What a green run of THIS sweep does and does not establish. Mirrors `COVERAGE_HORIZON` in
#: `test_no_unbatched_per_item_git_spawn.py` and `test_every_register_resolves_or_declares.py` --
#: this is the third hand-copied instance of the convention (module docstring § HORIZON).
COVERAGE_HORIZON = """op-name-resolution-horizon: a green run of this sweep establishes only that \
every ENROLLED op-name row names a registered op. It does NOT establish that the unenrolled \
84-minus-residual outlier registers are guarded, that the derived op population is the RIGHT \
population, or that a resolving row still describes real debt. The derivation over-approximates \
(module docstring, § FAILURE DIRECTION (c)): a RESOLVED row can still name an op unreachable at \
dispatch."""

#: Closed list of citation sites leaning on this sweep as their enforcement mechanism. Empty
#: today -- this is a brand-new module (P060) with no known citer yet; a future citer is added
#: here alongside the qualifying clause it must carry, never assumed to exist.
_HORIZON_CITERS: tuple[str, ...] = ()

#: The marker every citer's block must carry alongside this file's name.
_HORIZON_MARKER = "op-name-resolution-horizon"


def test_the_op_name_resolution_horizon_is_published_where_this_sweep_is_cited() -> None:
    """Same shape as `test_the_one_hop_horizon_is_published_where_this_gate_is_cited`
    (`test_no_unbatched_per_item_git_spawn.py`) and
    `test_the_resolution_horizon_is_published_where_this_sweep_is_cited`
    (`test_every_register_resolves_or_declares.py`): a CLOSED citer list, scoped to the citing
    BLOCK (blank-line-delimited) rather than the whole file, so the qualifier cannot silently
    drift out of the bullet that names this module while surviving elsewhere in the same citer.
    `_HORIZON_CITERS` is empty today, so this passes vacuously until a real citer is added."""
    this_file_name = Path(__file__).name
    missing: list[str] = []
    for rel in _HORIZON_CITERS:
        path = REPO_ROOT / rel
        if not path.is_file():
            missing.append(f"{rel} -- listed citer does not exist")
            continue
        blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8"))
        citing = [block for block in blocks if this_file_name in block]
        if not citing:
            missing.append(f"{rel} -- no longer cites this sweep; drop it from _HORIZON_CITERS")
        elif not any(_HORIZON_MARKER in block for block in citing):
            missing.append(
                f"{rel} -- cites this sweep without naming the {_HORIZON_MARKER} horizon"
            )
    assert not missing, (
        "the sweep's resolution horizon is not published where it is cited:\n"
        + "\n".join(f"  {row}" for row in missing)
        + f"\n\nhorizon: {COVERAGE_HORIZON}"
    )


def test_does_not_read_the_registry_map() -> None:
    """Negative pin for the `_registry_map.py` rejection (module docstring): this sweep's own
    source carries no `import` statement naming the generated map module, so a future edit cannot
    quietly re-introduce it as a resolution oracle. AST-checked, over actual `Import`/`ImportFrom`
    nodes -- a plain substring check would trip over this test's own assertion strings, which
    necessarily quote the forbidden import text as data, not as code."""
    import ast as _ast

    tree = _ast.parse(Path(__file__).read_text(encoding="utf-8"))
    offending: list[str] = []
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            offending.extend(
                alias.name for alias in node.names if "_registry_map" in alias.name
            )
        elif isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            if "_registry_map" in module or any(
                "_registry_map" in alias.name for alias in node.names
            ):
                offending.append(module)
    assert not offending, f"forbidden import of the rejected registry map: {offending!r}"
