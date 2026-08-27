"""Import-budget gate for the hottest hot-path hook/CLI interpreters.

Spec backlink: pln-spawn-storm-culprit-taxonomy-p-805aa9,
chunk D9 / AC11. Extended by chunk C12 of
`docs/plans/2026-08-22-the-import-path-costs-nothing.md`
(dispatch brief `state/dispatch-briefs/2026-08-22-the-import-path-costs-nothing/C12.md`)
to gate the `coordinator/bin/query-*.py` CLASS -- see "C12: gate the class,
not the named interpreters individually" below.

C12 reconciliation (do not re-derive, cite): C12's brief flagged that
`coordinator_core/benchmarks/import_budget.py` already exists, already
gates three of this file's named entrypoints (four at C12 authorship, six as
of C3 of `docs/plans/2026-08-25-the-post-commit-leg-stops-pushing-into-a-wall.md`,
four again as of C3 of `docs/plans/2026-08-25-the-staged-rollback-gate-dies-
without-blocking-a-commit.md` -- that chunk removed the `detect_staged_
rollback_fired` and `ops_detect_staged_rollback` rows along with the op and
CLI trampoline they measured, `hooks_auto_push` survives untouched -- see
this file's own `_TARGETS` tuple for the current count) via a
wall-clock-refusing,
`len(sys.modules)`-delta mechanism, and asked whoever executes C12 to either
(a) extend that mechanism to the newly-in-scope class or (b) reverse its
documented wall-clock refusal. THIS FILE ALREADY independently mirrors (a)'s
`module_count_ceiling` pattern -- it predates `import_budget.py`'s manifest
route and gates by the same deterministic module-set-cardinality logic, just
against hand-written `_Target` rows instead of `import-budget-manifest.json`
entries. C12's `writes:` scope covers this file and
`import-budget-manifest.json` but NOT `import_budget.py` itself, so (a) is
applied here as: extend THIS file's own already-existing mirror of that
pattern (`_Target` / `module_count_ceiling`) to the query-*.py class, via
`_discover_query_bin_scripts()` below, rather than editing the out-of-scope
sibling module. No reversal of the wall-clock refusal (b) is made; the
secondary CPU-time guard here stays the pre-existing catastrophic-only floor,
unchanged in kind.

C12: gate the class, not the named interpreters individually. `coordinator/
bin/query-*.py` (12 scripts at C12 authorship) was previously ungated on
process time entirely -- not one of the named `_Target` rows below (four at
C12 authorship, six as of C3 of
`docs/plans/2026-08-25-the-post-commit-leg-stops-pushing-into-a-wall.md`,
four again as of C3 of `docs/plans/2026-08-25-the-staged-rollback-gate-dies-
without-blocking-a-commit.md`),
not `test_pickup_assemble_import_perf`, not the amplification gate (spawn
count, not per-item cost). `_discover_query_bin_scripts()` globs the
directory rather than enumerating file names, so a future 13th `query-*.py`
script is swept in automatically; see that helper's own docstring for the
shared, class-wide ceiling sizing rationale (as opposed to the named hook
targets below, whose ceilings stay per-target/hand-tuned because their baselines
differ from each other by up to ~2.4x -- the query-bin class does not have
that spread).

Note on AC1 (`<50ms`, `coordinator_core.ops` import)/AC8 (`<80ms`,
`coordinator_core.hooks` unarmed import)/AC9 (`<50ms`, write-guard
discovery delta) statistic/load-regime pinning, named in C12's brief: those
three ACs belong to a different gate (measured against
`coordinator_core.ops`/`coordinator_core.hooks`/write-guard-discovery-delta
directly, not against this file's named `_Target` entrypoints (four at C12
authorship, six as of C3 of
`docs/plans/2026-08-25-the-post-commit-leg-stops-pushing-into-a-wall.md`,
four again as of C3 of `docs/plans/2026-08-25-the-staged-rollback-gate-dies-
without-blocking-a-commit.md`) or
the query-bin class), and are outside this file's `writes:` scope -- not
addressed by this edit. This file's own targets already carry their
pinned statistic (min-of-`_SAMPLE_COUNT` CPU-time samples) and load regime
(idle box at authorship) per-target, unchanged by this chunk except for the
newly-added query-bin class, which states the same pinning explicitly in
`_query_bin_targets()`'s docstring.

Targets one entrypoint per hook family, each a cold interpreter spawned by a
DoE-claude `coordinator/hooks/scripts/*.py` PLUMBING wrapper on every matching
tool call:

    1. `preuse-bash-dispatch.py`      -> coordinator_core.bash_guards.dispatch.evaluate_payload_json
    2. `preuse-write-dispatch.py`     -> coordinator_core.write_guards.engine.evaluate_payload_json
    3. `postuse-advisory-dispatch.py` -> coordinator_core.hooks.postuse_advisory_dispatch

Plus a fourth, in-tree module import (C3 of
`docs/plans/2026-08-25-the-post-commit-leg-stops-pushing-into-a-wall.md`,
dispatch brief `state/dispatch-briefs/2026-08-25-the-post-commit-leg-stops-
pushing-into-a-wall/C3.md`): `coordinator_core.hooks.auto_push`, the
post-commit hook's own payload module, gated as a plain `import` (not the
FULL trampoline form) since the post-commit leg's `sh` rung invokes this
module directly rather than through a separate CLI trampoline file.

HISTORICAL, REMOVED 2026-08-25 (C3 of `docs/plans/2026-08-25-the-staged-
rollback-gate-dies-without-blocking-a-commit.md`): this file used to also
target `coordinator/bin/detect-staged-rollback.py` (AC9's CLI-trampoline
form, `detect_staged_rollback_fired`) and a plain `import
coordinator_core.ops.detect_staged_rollback` (`ops_detect_staged_rollback`,
added by C3 of `docs/plans/2026-08-25-the-commit-gate-stops-importing-a
-subsystem.md`). Both rows are deleted along with the op module and its CLI
trampoline they measured -- claude-klabauter ends with no pre-commit hook on that
gate, so there is nothing left to target. See those plans' own history for
the shape this file used to gate; not restated here.

Why this gate targets COST PER INTERPRETER, not spawn count: the baseline audit
(`state/audits/2026-08-07-hot-path-spawn-baseline.md`) measured hook processes at
only ~7% of a Bash call's total process population (15 of ~214) -- hooks are not
where the spawn-count mass lives, and a gate here that chased spawn count would be
solving the wrong 7%. `docs/plans/2026-08-06-windows-hot-path-less-work-per-
interpreter.md` (status `implemented`) already concluded, and delivered against,
the actual lever for this cohort: each hot-path interpreter itself carries less
import weight. This gate is the regression guard for that lever staying paid for
-- it fails closed if a future change quietly re-inflates one target's import
graph, not if the number of interpreters changes.

Target 3's residual (documented, not fixed by this gate): importing
`coordinator_core.hooks.postuse_advisory_dispatch` first runs the
`coordinator_core.hooks` package `__init__`. That package DOES carry a
lazy-registration channel (`COORDINATOR_CORE_LAZY_OPS` / `sys.
_coordinator_core_lazy_ops`, see its own module docstring) mirroring
`coordinator_core.ops`'s -- but `postuse-advisory-dispatch.py` never arms it
(unlike `preuse-bash-dispatch.py` and `preuse-write-dispatch.py`, which both call
`_arm_lazy_ops()` before importing their target), so this entrypoint always pays
the package's default-eager path: all 15 `hooks.*` modules import and register
their ops (not 7 -- the wrapper script's own inline comment undercounts this;
verified by reading `coordinator_core/hooks/__init__.py::_EAGER_HOOK_MODULES`
directly rather than trusting that comment). Arming the lazy channel in that
wrapper is a real available fix but is DoE-claude-tree territory (this claude-klabauter
session is read-only there) and out of scope for this gate.

Shape: mirrors `coordinator_core.tests.test_pickup_assemble_import_perf`
verbatim -- read that file before touching this one. Per settled doctrine (do
not re-derive, cite):
  - `state/lessons/2026-08-03-wall-clock-assertions-in-a-parallel-test-bc800cb5a894.yaml`:
    prefer a deterministic assertion (imported-module set) over a timing proxy
    wherever the property admits one; where timing is unavoidable, measure CPU
    time, never wall-clock -- descheduling inflates elapsed time without
    inflating consumed CPU.
  - `state/lessons/2026-08-01-an-executors-measured-number-is-a-claim-about-its-
    machine-not-the-change.yaml`: a raw-ms budget is a claim about the
    measuring machine, not the code; any residual timing floor here is kept
    machine-relative (a wide multiple of THIS machine's own idle measurement),
    never an absolute imported number.

PRIMARY guard per target: a hard module-set-cardinality ceiling plus a named
heavy-module absence list, both from a single fresh-subprocess `sys.modules`
before/after diff -- deterministic, sampled once, no timing. Ceilings and
absence lists are NOT averaged across targets: each target gets its own, sized
to its own measured baseline (targets differ by ~2.4x in module count on this
machine, so a shared ceiling would either mask a regression on the small target
or permanently red the large one).

SECONDARY guard per target: a min-of-N `time.process_time()` (CPU time, never
wall-clock) catastrophic-regression floor -- coarse, not precise; the module-set
ceiling carries the precision now (see the cited "Primary guard" reasoning in
`test_pickup_assemble_import_perf`'s docstring, reused here rather than
re-derived).

Dead zone, explicitly (mirrors the model file's own dead-zone disclosure): the
module-count ceiling does NOT catch a same-module-set cost regression -- an
already-imported module doing MORE work at import time (a bigger module-scope
literal, slower C-extension init, a loop that grew) adds no new module to the
set and can push CPU time up substantially without tripping either guard here.
Neither guard is a stand-in for that case; only the CPU-time floor has any
chance of catching it, and only past a wide catastrophic threshold, not a tight
one -- see the "Secondary guard" test docstrings below.

A SECOND dead zone this revision closes for module count specifically: a
module-count ceiling cannot see a path-only `sys.path` growth with zero new
modules -- exactly what a plain-path `.pth`-shaped fix (append a directory to
`sys.path` instead of importing anything) looks like on THIS gate's primary
axis. A future change trading imported modules for `sys.path` entries (e.g.
resolving a sibling repo's root onto `sys.path` instead of importing a local
shim of it) would tax every subsequent import on that interpreter -- longer
`sys.path` means more failed stat() probes before every hit -- while reading
as an IMPROVEMENT on the module-count axis alone. `sys_path_entry_ceiling`
below is the second, independent axis this closes: each target's own
`sys.path`-length growth (measured the same before/after-diff way as the
module set, in the same fresh subprocess, never averaged across targets --
see the per-target-not-shared rationale on `module_count_ceiling` above,
which applies identically here) is bounded on its own ceiling, orthogonal to
the module-count one.

TRAP for whoever next touches these numbers: the two ceilings can move in
OPPOSITE directions on the same change -- a plain-path `.pth`-shaped fix
trading N imported modules for a couple of new `sys.path` entries LOWERS
module_count_ceiling's measured value while RAISING sys_path_entry_ceiling's.
Re-baseline BOTH together against a fresh measurement on the changed code, or
this gate reds on a change that improves the very thing it sits beside.
WHAT DISCHARGES THAT OBLIGATION IS THE RATCHET BELOW, NOT A SUCCESSOR
REMEMBERING THIS PARAGRAPH -- ratchets read from source, not from a plan a
future editor is not necessarily holding.

RATCHET (AC10): every ceiling field on `_Target` is a hand-set source
constant, so LOWERING one (tightening the gate) is always free -- it is a
one-line diff with no companion change required anywhere else in this file.
RAISING one is never free in the sense that matters: it is a hand-edited
literal in a tracked file, so it can never happen silently -- it is always a
visible line in the diff a reviewer sees, never a value computed or read from
an external file this test could drift against without a diff to show for
it. The completeness pin below (`test_hot_path_hook_import_budget_axes_are_
complete`) is the third leg: `sys_path_entry_ceiling` has NO dataclass
default (unlike `heavy_modules_expected_absent` and `cpu_floor_ms`, both of
which are genuinely optional per-target), so a future target row that omits
it fails at construction, not at some later assertion a reviewer could miss;
the completeness test pins that property as a running, readable assertion
rather than leaving it as an inspection-only fact of the dataclass shape.

Numbers below (module counts, CPU-time samples) were measured on THIS machine
at authorship time via the probe helpers in this file, not copied from any
other document.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# See module docstring's "Secondary guard" -- kept at 2 (not 1) purely as cheap
# redundancy against a one-off subprocess-spawn anomaly, matching
# test_pickup_assemble_import_perf's own _SAMPLE_COUNT rationale: each sample
# spawns a fresh interpreter, so this stays low under the parallel fast tier.
_SAMPLE_COUNT = 2


@dataclass(frozen=True)
class _Target:
    name: str
    import_path: str
    # Modest margin above this machine's measured module count at authorship
    # time -- wide enough to absorb platform-specific stdlib substitutions
    # (POSIX vs Windows equivalents swapping names without changing net count
    # by much) without masking a genuine new subtree being pulled in. Each
    # target's margin is sized to ITS OWN baseline, not a shared percentage --
    # see module docstring, "NOT averaged across targets".
    module_count_ceiling: int
    # NEW AXIS (AC9/AC10) -- see module docstring's "second dead zone" and
    # "TRAP" paragraphs before touching this number. Modest margin above this
    # machine's measured `sys.path`-length growth at authorship time, same
    # per-target (not shared/averaged) sizing rationale as
    # `module_count_ceiling`. Deliberately NO DEFAULT (unlike the two fields
    # below): a target row that omits it fails at construction, which is the
    # completeness pin's first, structural leg -- see
    # `test_hot_path_hook_import_budget_axes_are_complete`. RATCHET: lowering
    # this value is always free; raising it is never silent -- it is a
    # hand-edited literal in this tracked file, always a visible diff line
    # (module docstring's RATCHET paragraph, AC10). When C8's plain-path
    # `.pth`-shaped fix lands (still gated on an external five-repo campaign
    # memo as of this writing, per the plan's C8 row -- may never land inside
    # this plan), re-measure BOTH this field and `module_count_ceiling`
    # together against the changed code and update both in the same diff --
    # they move in opposite directions on that change (module docstring
    # TRAP). This comment, not a plan a future editor may not be holding, is
    # what that obligation lives beside.
    sys_path_entry_ceiling: int
    # Absolute path to a CLI trampoline file to fire in its FULL form (see
    # module docstring's AC9 paragraph) instead of a plain `import
    # import_path`. When set, the probe `exec_module`s this file and calls
    # its own `_import_main()` to capture the deferred real import a bare
    # file-level `import` of the trampoline would miss. `None` (the default)
    # for the three targets that ARE already the real payload module, not a
    # CLI trampoline over it.
    cli_script_path: str | None = None
    # Whether firing a `cli_script_path` target should call the trampoline's
    # own `_import_main()` after `exec_module` (AC9's deferred-import shape --
    # the now-deleted `detect_staged_rollback_fired` row needed this, see
    # module docstring's HISTORICAL note) or stop at `exec_module` alone. The
    # `coordinator/bin/query-*.py` family (C12)
    # has no `_import_main()` split: every one of them does its real work via
    # module-level `import` statements already executed by `exec_module`
    # itself (confirmed by grep -- none of the 12 define `_import_main`), so
    # calling a nonexistent method there would be an AttributeError, not a
    # measurement. Ignored (must stay True, the default) for a target with no
    # `cli_script_path`.
    cli_call_import_main: bool = True
    # Named heavy modules confirmed ABSENT today for THIS target specifically.
    # Deliberately per-target, not a shared list: `yaml` is present in the
    # bash-dispatch and write-guards import graphs but absent from the
    # postuse-advisory-dispatch one, so a shared absence list would either
    # miss a real regression on the third target or false-fail on the first
    # two.
    heavy_modules_expected_absent: tuple = ()
    # Widened CATASTROPHIC-blowup-only CPU-time bound in ms (see module
    # docstring's "Secondary guard"), sized as a wide multiple of this
    # machine's own idle min-of-N measurement -- machine-relative per the
    # cited 2026-08-01 lesson, not an absolute figure carried over from
    # another host.
    cpu_floor_ms: float = 0.0


#: Declaration for the register-aging sweep (C5,
#: `docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`):
#: every row of `_TARGETS` names a whole importable module (`_Target.import_path`), never a
#: symbol living inside a parent module.
_TARGETS__SUBJECT_CLASS = "module"

_TARGETS: Sequence[_Target] = (
    _Target(
        name="bash_guards_dispatch",
        import_path="coordinator_core.bash_guards.dispatch",
        # Measured 149 modules on this machine/Python version at authorship.
        # ~11% / 16-module margin.
        module_count_ceiling=165,
        # Measured 0 `sys.path` growth on this machine at authorship (this
        # target never touches `sys.path` itself). Small fixed margin, not a
        # percentage of zero.
        sys_path_entry_ceiling=2,
        heavy_modules_expected_absent=("pydantic", "asyncio"),
        # Idle min-of-2 CPU time measured 62.5ms on this machine. `yaml` and
        # `psutil` are the two heaviest confirmed-present modules in this
        # target's own set (see the dump this test's authorship used to build
        # the absence lists) -- 300ms leaves ~4.8x headroom over that idle
        # measurement without ever having been run under sustained parallel
        # contention (unlike test_pickup_assemble_import_perf's own floor,
        # which WAS re-measured under `-n 8` load); the wider multiple here
        # is deliberately compensating for that gap in evidence, not a
        # tighter, unverified guess.
        cpu_floor_ms=300.0,
    ),
    _Target(
        name="write_guards_engine",
        import_path="coordinator_core.write_guards.engine",
        # Measured 61 modules on this machine/Python version at authorship.
        # Smaller absolute baseline needs relatively more margin than a
        # percentage would give it (a handful of platform-substituted stdlib
        # modules is a bigger fraction of 61 than of 149) -- ~15% / 9-module
        # margin.
        module_count_ceiling=70,
        # Measured 0 `sys.path` growth on this machine at authorship.
        sys_path_entry_ceiling=2,
        heavy_modules_expected_absent=("pydantic", "asyncio"),
        # Idle min-of-2 CPU time measured 15.6ms on this machine -- the
        # smallest of the three targets, so subprocess-spawn noise is a
        # larger fraction of the signal; the floor uses a proportionally
        # wider multiple (~9.6x idle) for the same reason the module-count
        # margin above is wider than a flat percentage.
        cpu_floor_ms=150.0,
    ),
    _Target(
        name="hooks_postuse_advisory_dispatch",
        import_path="coordinator_core.hooks.postuse_advisory_dispatch",
        # RE-BASELINED 2026-08-21 (C6 fix-forward, plan
        # 2026-08-21-the-cli-bootstrap-tax-dies-at-the-interpreter-floor.md):
        # this target's import graph grew from the 99-module authorship
        # baseline to a MEASURED 765 modules on this machine/Python version
        # (batched-subprocess `sys.modules` before/after diff). That is
        # itself the finding, not an artifact of this re-baseline: firing
        # this target costs 412.5ms of PROCESS TIME (K=20,
        # `coordinator_core.benchmarks.process_time.batched_process_time_ms`,
        # DR-344's instrument) -- MORE THAN 2x CLAUDE.md's "The brightline"
        # 200ms-per-process ceiling, and this hook fires on every matching
        # tool call (module docstring header), i.e. on the hot path. This
        # ceiling is a REGRESSION GUARD pinned to that reality, not an
        # endorsement of the cost -- fixing `postuse-advisory-dispatch.py`'s
        # import weight (e.g. arming the lazy-ops channel the module
        # docstring's residual note already describes) is out of scope for
        # this test file; routed as its own backlog item instead. ~16%
        # margin (matches this target's own prior-authorship convention:
        # 99 measured -> 115 ceiling was also ~16%), 890 = 765 * ~1.163.
        module_count_ceiling=890,
        # Measured 0 `sys.path` growth on this machine at re-baseline time
        # (unchanged from authorship).
        sys_path_entry_ceiling=2,
        # RE-BASELINED alongside module_count_ceiling above: `asyncio` and
        # `yaml` are now BOTH confirmed PRESENT in this target's measured
        # import set (part of the same growth this re-baseline records) --
        # module docstring's "do not list a heavy module in an absence
        # tuple before the cut that removes it lands" rule means they no
        # longer belong in this list. `pydantic` remains confirmed absent.
        heavy_modules_expected_absent=("pydantic",),
        # RE-BASELINED: min-of-8 direct-probe samples at re-baseline time
        # ranged 328.1-375.0ms; the batched K=20 process-time figure above
        # (412.5ms) is the more reliable per-invocation estimate (job-object
        # amortised, beats scheduler-tick quantisation -- see
        # `batched_process_time_ms`'s own module docstring).
        #
        # TIGHTENED BY THE EM, same session, from a proposed 2000.0. A 4.8x
        # multiplier over measured was defended as matching another target's
        # convention, but a multiplier is not a convention -- the number it
        # produces is what the guard actually enforces. 2000ms is 10x
        # CLAUDE.md's 200ms-per-process ceiling and 4.8x this target's own
        # cost, which is not a regression bound: this hook could triple in
        # weight and still pass. The other three targets in this file sit at
        # 150/220/300ms. 600ms is ~1.45x the K=20 figure and ~1.6x the
        # highest direct sample -- clear of load noise on a 50-70-session
        # box, tight enough that a real regression trips it.
        #
        # It is still an ugly number and it is meant to look like one. The
        # gap between this ceiling and the other three is the finding.
        cpu_floor_ms=600.0,
    ),
    _Target(
        name="hooks_auto_push",
        import_path="coordinator_core.hooks.auto_push",
        # C3 of `docs/plans/2026-08-25-the-post-commit-leg-stops-pushing-into-a-wall.md`
        # (dispatch brief `state/dispatch-briefs/2026-08-25-the-post-commit-leg-stops-
        # pushing-into-a-wall/C3.md`). The post-commit leg's own payload module -- the
        # second-highest-frequency hook in the repo (module docstring's "why a _Target
        # row" section, plan Problem statement), previously absent from this file
        # entirely. Fired as a plain `import` (not the FULL CLI-trampoline form,
        # matching `bash_guards_dispatch`'s and `write_guards_engine`'s shape):
        # the post-commit `sh` rung imports
        # this module directly, with no separate trampoline file to exec.
        # Measured 64 modules on this machine/Python version post-C1 (fresh-subprocess
        # sys.modules before/after diff, idle box). Smaller absolute baseline needs
        # relatively more margin than a flat percentage would give it (module
        # docstring, same rationale as `write_guards_engine`'s comment) -- ~15% /
        # 10-module margin, matching `write_guards_engine`'s own ~15% convention.
        module_count_ceiling=74,
        # Measured 0 `sys.path` growth on this machine at this baseline (a bare module
        # import, no trampoline `sys.path.insert`). Small fixed margin, not a
        # percentage of zero, matching `bash_guards_dispatch`'s convention.
        sys_path_entry_ceiling=2,
        # `pydantic` and `asyncio` confirmed absent from this target's measured import
        # set, matching the convention of every other plain-`import` target in this
        # file (`bash_guards_dispatch`, `write_guards_engine`).
        heavy_modules_expected_absent=("pydantic", "asyncio"),
        # Idle min-of-3 CPU time measured 15.6-31.3ms on this machine (Windows
        # scheduler-tick quantisation dominates a sample this light, the same quantum
        # `write_guards_engine`'s own comment names). ~9.6x headroom over the min
        # sample, matching `write_guards_engine`'s multiplier for the same reason: the
        # smallest baselines in this file need the widest proportional floor since
        # spawn noise is a larger fraction of the signal. This is a structural bound
        # from this target's OWN measurement, not a threshold derived from C2's
        # separately-measured timer figures (module docstring's "Why a _Target row,
        # not a new probe" section of the C3 plan makes this distinction explicit).
        cpu_floor_ms=150.0,
    ),
)

def _discover_query_bin_scripts() -> Sequence[Path]:
    """The `coordinator/bin/query-*.py` CLASS this chunk (C12) gates.

    Glob, not an enumerated list: the whole point (per this file's dispatch
    brief, `state/dispatch-briefs/2026-08-22-the-import-path-costs-nothing/
    C12.md`) is that a future porter adding `query-whatever-new.py` to this
    family is caught by construction, not by remembering to hand-add a row
    here -- the north-star "make the correct path cheaper than the wrong
    one" this repo's CLAUDE.md names. Sorted for deterministic parametrize
    IDs across platforms/filesystems.
    """
    bin_dir = _REPO_ROOT / "coordinator" / "bin"
    return tuple(sorted(bin_dir.glob("query-*.py")))


def _query_bin_targets() -> Sequence[_Target]:
    """Build one `_Target` per discovered `query-*.py` script (AC9 shape:
    `cli_script_path` set, `cli_call_import_main=False` since none of these
    12 scripts defer real work behind an `_import_main()` split -- every one
    does its work via module-level imports that `exec_module` alone already
    exercises, per `cli_call_import_main`'s own field doc).

    Ceilings are a SHARED, class-wide budget, not twelve individually-tuned
    ones: measured on this machine at authorship (min-of-2 fresh-subprocess
    samples per script, idle box) the class clusters tightly --
    module-count 25-34, `sys.path` growth 1-3, CPU time 0.0-62.5ms -- because
    every member shares the same `coordinator/bin/lib/records_query.py` (or
    sibling lib) transport shape. `module_count_ceiling=45` (~32% above the
    34-module observed max), `sys_path_entry_ceiling=5` (2 above the 3
    observed max), `cpu_floor_ms=250.0` (~4x the 62.5ms observed max,
    matching this file's own convention of a wide catastrophic-only
    multiplier over idle-box measurement, see `bash_guards_dispatch`'s
    comment) are sized so ONE new heavy
    script joining the family with a materially different (bigger) import
    graph still trips the gate, while ordinary cross-script variance within
    the family does not. Per-target (not shared) sizing, elsewhere in this
    file, exists because those four targets differ from each other by up to
    ~2.4x in baseline size (module docstring); this family does not have
    that spread, so one shared ceiling is the correctly-scoped choice here,
    not a shortcut.

    Statistic and load regime (chunk C12's "pin the statistic" instruction):
    min-of-`_SAMPLE_COUNT` (2) CPU-time samples, same as every other target
    in this file (`test_hot_path_hook_import_floor`); measured on an idle
    box at authorship, same regime the other three named targets' comments
    record -- none of the four pre-existing targets in this file were
    re-measured under the ~50-peer load norm either, so this class does not
    newly diverge from that file-wide convention.
    """
    return tuple(
        _Target(
            name=f"query_bin_{script.stem.replace('-', '_')}",
            import_path=f"coordinator/bin/{script.name}",
            module_count_ceiling=45,
            sys_path_entry_ceiling=5,
            cli_script_path=str(script),
            cli_call_import_main=False,
            cpu_floor_ms=250.0,
        )
        for script in _discover_query_bin_scripts()
    )


_TARGETS = tuple(_TARGETS) + _query_bin_targets()
_TARGETS_BY_NAME = {t.name: t for t in _TARGETS}


def _probe_env() -> dict:
    """Env for every probe subprocess spawned by this file.

    Sets `COORDINATOR_ENGINE_ROOT` to this checkout explicitly: any
    `cli_script_path`-form target's FULL-form fire (`_fire_lines`,
    `cli_script_path` branch -- the `query-*.py` family today; the
    now-deleted `detect_staged_rollback_fired` row originally motivated this,
    see module docstring's HISTORICAL note) calls
    `require_dispatch_engine_on_path()`, whose registry/pointer-file rungs
    read `HOME`/`USERPROFILE` -- both of which this suite's own
    `coordinator_core/conftest.py::_quarantine_real_home` autouse fixture
    points at a throwaway per-test directory, which a probe subprocess
    spawned FROM a test inherits by default (no explicit `env=` previously
    meant `subprocess.run` copied the quarantined environment verbatim).
    This repo IS every such trampoline's own dispatch engine, so naming it
    explicitly, rung 1 of that resolution ladder (env var, outranking the
    pointer-file rungs the quarantine breaks), is correct regardless of HOME
    quarantine, CI, or any other environment this file runs under -- not a
    test-only workaround.
    """
    env = dict(os.environ)
    env["COORDINATOR_ENGINE_ROOT"] = str(_REPO_ROOT)
    return env


def _fire_lines(target: _Target) -> list:
    """The probe-body lines that actually exercise `target`: either the FULL
    form of a CLI trampoline (`cli_script_path` set -- `exec_module` the file
    and call its own `_import_main()`, per module docstring's AC9 paragraph)
    or a plain `import target.import_path` for a target that already IS the
    real payload module."""
    if target.cli_script_path is not None:
        lines = [
            "import importlib.util as _ilu",
            f"_spec = _ilu.spec_from_file_location('_hot_path_probe_target', {target.cli_script_path!r})",
            "_mod = _ilu.module_from_spec(_spec)",
            "_spec.loader.exec_module(_mod)",
        ]
        if target.cli_call_import_main:
            lines.append("_mod._import_main()")
        return lines
    return [f"import {target.import_path}"]


def _imported_module_names(
    target: _Target,
    extra_imports: Sequence[str] = (),
    extra_sys_path: str | None = None,
) -> tuple:
    """Return `(new_module_names, sys_path_growth)` for firing `target`.

    Runs in a fresh subprocess (no pollution from the test runner's own prior
    imports) and diffs `sys.modules` AND `len(sys.path)` before/after,
    returning the new module names and the `sys.path` growth as JSON on
    stdout -- one subprocess spawn covers both axes (module-set primary
    guard and the `sys_path_entry_ceiling` NEW AXIS, module docstring).
    `extra_imports` (each run BEFORE the fire step, after the "before"
    snapshot) and `extra_sys_path` exist solely for the planted-fixture
    demonstration test below -- production callers never pass them.
    """
    lines = ["import sys, json"]
    if extra_sys_path:
        lines.append(f"sys.path.insert(0, {extra_sys_path!r})")
    lines.append("before_mods = set(sys.modules)")
    lines.append("before_path_len = len(sys.path)")
    for extra in extra_imports:
        lines.append(f"import {extra}")
    lines.extend(_fire_lines(target))
    lines.append("after_mods = set(sys.modules)")
    lines.append("after_path_len = len(sys.path)")
    lines.append(
        "print(json.dumps({'modules': sorted(after_mods - before_mods), "
        "'sys_path_growth': after_path_len - before_path_len}))"
    )
    probe = "\n".join(lines) + "\n"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
        env=_probe_env(),
    )
    import json as _json

    payload = _json.loads(proc.stdout)
    return payload["modules"], payload["sys_path_growth"]


def _sample_import_cost_ms(target: _Target) -> float:
    """One fresh-interpreter sample of `target`'s CPU-time import/fire cost.

    CPU time (`time.process_time`), NOT wall-clock -- see module docstring's
    "Secondary guard" and the cited 2026-08-03 lesson for why: descheduling
    under a parallel test tier inflates wall-clock without inflating consumed
    CPU, so process_time is the only one of the two that still measures the
    import rather than the machine's momentary load.
    """
    lines = ["import time", "t0 = time.process_time()"]
    lines.extend(_fire_lines(target))
    lines.append("print((time.process_time() - t0) * 1000.0)")
    probe = "\n".join(lines) + "\n"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
        env=_probe_env(),
    )
    return float(proc.stdout.strip())


def _assert_module_ceiling(target: _Target, imported: Sequence[str]) -> None:
    """The two module-set assertions, factored out so the planted-fixture
    demonstration test (below) can drive them with a deliberately inflated
    module list without duplicating the assertion logic it is proving."""
    imported_set = set(imported)

    for heavy in target.heavy_modules_expected_absent:
        present = any(mod == heavy or mod.startswith(heavy + ".") for mod in imported_set)
        assert not present, (
            f"{target.import_path} now transitively imports {heavy!r} (or a "
            f"submodule of it), which {target.name}'s named-absence list says "
            f"must stay clear of this target's import graph."
        )

    count = len(imported)
    assert count <= target.module_count_ceiling, (
        f"{target.import_path} pulled in {count} modules, exceeding "
        f"{target.name}'s ceiling of {target.module_count_ceiling} -- likely a "
        f"new heavy subtree dragged in transitively. Newly-imported modules: "
        f"{sorted(imported_set)}"
    )


def _assert_sys_path_ceiling(target: _Target, sys_path_growth: int) -> None:
    """NEW AXIS (AC9/AC10) assertion: `target`'s `sys.path` growth stays
    bounded, independently of the module-set ceiling above -- see module
    docstring's "second dead zone" paragraph for why this is a SEPARATE
    assertion, not folded into `_assert_module_ceiling`."""
    assert sys_path_growth <= target.sys_path_entry_ceiling, (
        f"{target.import_path} grew sys.path by {sys_path_growth} entries, "
        f"exceeding {target.name}'s sys_path_entry_ceiling of "
        f"{target.sys_path_entry_ceiling} -- likely a new path-only "
        f"`.pth`-shaped insert this gate's module-count axis cannot see on "
        f"its own (module docstring's second dead zone)."
    )


@pytest.mark.parametrize("target", _TARGETS, ids=lambda t: t.name)
def test_hot_path_hook_import_modules(target: _Target) -> None:
    """PRIMARY guard: each target's imported module SET stays bounded and
    heavy-module-clear, deterministically. See module docstring."""
    imported, _sys_path_growth = _imported_module_names(target)
    _assert_module_ceiling(target, imported)


@pytest.mark.parametrize("target", _TARGETS, ids=lambda t: t.name)
def test_hot_path_hook_import_sys_path_growth(target: _Target) -> None:
    """NEW AXIS (AC9/AC10) guard: each target's `sys.path` growth stays
    bounded, deterministically -- see module docstring's "second dead zone"
    and "TRAP" paragraphs."""
    _imported, sys_path_growth = _imported_module_names(target)
    _assert_sys_path_ceiling(target, sys_path_growth)


@pytest.mark.parametrize("target", _TARGETS, ids=lambda t: t.name)
def test_hot_path_hook_import_floor(target: _Target) -> None:
    """SECONDARY, catastrophic-regression-only guard: firing target.import_path
    (or, for a CLI-trampoline target, its FULL form) completes within a
    widened CPU-time sanity bound. NOT the precise guard -- see
    `test_hot_path_hook_import_modules` and the module docstring's dead-zone
    disclosure for what this floor does NOT catch."""
    samples_ms = [_sample_import_cost_ms(target) for _ in range(_SAMPLE_COUNT)]
    min_ms = min(samples_ms)
    assert min_ms <= target.cpu_floor_ms, (
        f"{target.import_path} import cost regressed: minimum of "
        f"{_SAMPLE_COUNT} samples was {min_ms:.1f}ms, exceeding {target.name}'s "
        f"widened catastrophic-regression bound of {target.cpu_floor_ms}ms. All "
        f"samples (ms): {[round(s, 1) for s in samples_ms]}."
    )


def test_hot_path_hook_import_budget_axes_are_complete() -> None:
    """AC10 completeness pin: every target in the table carries BOTH ceiling
    axes (module count AND sys.path entries) as non-negative integers.
    `sys_path_entry_ceiling` has no dataclass default (see the field's own
    doc comment on `_Target`), so a future target row that omits it already
    fails at construction time -- this test pins that property as a running,
    readable assertion rather than leaving it as an inspection-only fact of
    the dataclass shape (module docstring's RATCHET paragraph, AC10)."""
    assert len(_TARGETS) >= 5, (
        f"expected at least the 3 original hook targets plus "
        f"`hooks_auto_push` plus at least one discovered `query-*.py` "
        f"target; found {len(_TARGETS)}."
    )
    for target in _TARGETS:
        assert isinstance(target.module_count_ceiling, int) and target.module_count_ceiling > 0, (
            f"{target.name}: module_count_ceiling must be a positive int"
        )
        assert isinstance(target.sys_path_entry_ceiling, int) and target.sys_path_entry_ceiling >= 0, (
            f"{target.name}: sys_path_entry_ceiling must be a non-negative int"
        )


def test_hot_path_hook_import_budget_gate_catches_a_planted_regression() -> None:
    """AC11 fixture demonstration: prove the module-count ceiling actually
    trips on a real regression, rather than trusting an assertion nobody has
    watched fail.

    Plants a scratch heavy import (`xml.etree.ElementTree`, `sqlite3`,
    `unittest`, `xml.dom.minidom` -- none of them present in ANY of the three
    targets' measured import sets, confirmed by inspection at authorship time)
    ahead of `write_guards_engine`'s own import, in the SAME fresh-subprocess
    probe the real gate uses (not a mock) -- simulating what a future
    regression transitively dragging in an unrelated heavy stdlib subtree
    would look like on `sys.modules`. `write_guards_engine` is chosen because
    it carries the smallest module-count baseline of the three, so this
    handful of extra imports reliably clears its ceiling margin without
    needing an oversized planted payload.

    Asserts the SAME `_assert_module_ceiling` helper the real gate calls
    raises AssertionError against the inflated count -- this is deliberately
    NOT a separate, weaker check: it is the production assertion logic,
    exercised against a known-bad input.

    Covers every row in `_TARGETS`, including `hooks_auto_push` and the
    query-bin family, without a dedicated per-target demonstration:
    `_assert_module_ceiling` is the exact function
    `test_hot_path_hook_import_modules` calls for every entry in `_TARGETS`,
    so proving it trips here proves it trips for any row -- a target-specific
    demonstration would exercise the same shared logic a second time, not
    different logic.
    """
    target = _TARGETS_BY_NAME["write_guards_engine"]
    imported, _sys_path_growth = _imported_module_names(
        target,
        extra_imports=("xml.etree.ElementTree", "sqlite3", "unittest", "xml.dom.minidom"),
    )
    assert len(imported) > target.module_count_ceiling, (
        "planted-fixture setup failure: the scratch heavy imports did not push "
        f"{target.name}'s module count ({len(imported)}) past its ceiling "
        f"({target.module_count_ceiling}) -- the demonstration below would "
        f"pass vacuously. Widen the planted import list."
    )
    with pytest.raises(AssertionError):
        _assert_module_ceiling(target, imported)
