"""Import-budget gate for the three hottest hot-path hook interpreters.

Spec backlink: pln-spawn-storm-culprit-taxonomy-p-805aa9,
chunk D9 / AC11.

Targets one entrypoint per hook family, each a cold interpreter spawned by a
DoE-claude `coordinator/hooks/scripts/*.py` PLUMBING wrapper on every matching
tool call:

    1. `preuse-bash-dispatch.py`      -> coordinator_core.bash_guards.dispatch.evaluate_payload_json
    2. `preuse-write-dispatch.py`     -> coordinator_core.write_guards.engine.evaluate_payload_json
    3. `postuse-advisory-dispatch.py` -> coordinator_core.hooks.postuse_advisory_dispatch

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

Numbers below (module counts, CPU-time samples) were measured on THIS machine
at authorship time via the probe helpers in this file, not copied from any
other document.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pytest

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


_TARGETS: Sequence[_Target] = (
    _Target(
        name="bash_guards_dispatch",
        import_path="coordinator_core.bash_guards.dispatch",
        # Measured 149 modules on this machine/Python version at authorship.
        # ~11% / 16-module margin.
        module_count_ceiling=165,
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
        # Measured 99 modules on this machine/Python version at authorship --
        # includes the full 15-module coordinator_core.hooks eager-registration
        # cost documented in the module docstring's residual note. ~16% /
        # 16-module margin.
        module_count_ceiling=115,
        # `yaml` is confirmed absent here (unlike the other two targets --
        # see the per-target absence-list note on the dataclass field above),
        # alongside pydantic and asyncio.
        heavy_modules_expected_absent=("pydantic", "asyncio", "yaml"),
        # Idle min-of-2 CPU time measured 31.2ms on this machine.
        cpu_floor_ms=200.0,
    ),
)

_TARGETS_BY_NAME = {t.name: t for t in _TARGETS}


def _imported_module_names(
    import_path: str,
    extra_imports: Sequence[str] = (),
    extra_sys_path: str | None = None,
) -> list:
    """Return the `sys.modules` keys newly added by importing `import_path`.

    Runs in a fresh subprocess (no pollution from the test runner's own prior
    imports) and diffs `sys.modules` before/after, returning the new names as
    JSON on stdout. `extra_imports` (each run BEFORE `import_path`, after the
    "before" snapshot) and `extra_sys_path` exist solely for the planted-
    fixture demonstration test below -- production callers never pass them.
    """
    lines = ["import sys, json"]
    if extra_sys_path:
        lines.append(f"sys.path.insert(0, {extra_sys_path!r})")
    lines.append("before = set(sys.modules)")
    for extra in extra_imports:
        lines.append(f"import {extra}")
    lines.append(f"import {import_path}")
    lines.append("after = set(sys.modules)")
    lines.append("print(json.dumps(sorted(after - before)))")
    probe = "\n".join(lines) + "\n"
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
    )
    import json as _json

    return _json.loads(proc.stdout)


def _sample_import_cost_ms(import_path: str) -> float:
    """One fresh-interpreter sample of `import_path`'s CPU-time import cost.

    CPU time (`time.process_time`), NOT wall-clock -- see module docstring's
    "Secondary guard" and the cited 2026-08-03 lesson for why: descheduling
    under a parallel test tier inflates wall-clock without inflating consumed
    CPU, so process_time is the only one of the two that still measures the
    import rather than the machine's momentary load.
    """
    probe = (
        "import time\n"
        "t0 = time.process_time()\n"
        f"import {import_path}\n"
        "print((time.process_time() - t0) * 1000.0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        cwd=_REPO_ROOT,
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


@pytest.mark.parametrize("target", _TARGETS, ids=lambda t: t.name)
def test_hot_path_hook_import_modules(target: _Target) -> None:
    """PRIMARY guard: each target's imported module SET stays bounded and
    heavy-module-clear, deterministically. See module docstring."""
    imported = _imported_module_names(target.import_path)
    _assert_module_ceiling(target, imported)


@pytest.mark.parametrize("target", _TARGETS, ids=lambda t: t.name)
def test_hot_path_hook_import_floor(target: _Target) -> None:
    """SECONDARY, catastrophic-regression-only guard: `import target.import_path`
    completes within a widened CPU-time sanity bound. NOT the precise guard --
    see `test_hot_path_hook_import_modules` and the module docstring's dead-
    zone disclosure for what this floor does NOT catch."""
    samples_ms = [_sample_import_cost_ms(target.import_path) for _ in range(_SAMPLE_COUNT)]
    min_ms = min(samples_ms)
    assert min_ms <= target.cpu_floor_ms, (
        f"{target.import_path} import cost regressed: minimum of "
        f"{_SAMPLE_COUNT} samples was {min_ms:.1f}ms, exceeding {target.name}'s "
        f"widened catastrophic-regression bound of {target.cpu_floor_ms}ms. All "
        f"samples (ms): {[round(s, 1) for s in samples_ms]}."
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
    """
    target = _TARGETS_BY_NAME["write_guards_engine"]
    imported = _imported_module_names(
        target.import_path,
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
