"""Oracle for the amplification exemption at
`coordinator_core/benchmarks/shim_fanin_measure.py::_spawn_n_processes::run`.

The claim (state/audits/2026-08-19-amplification-register-remaining-fourteen-dispositions.md,
"Oracle-decidable" section): the N-spawn cost in `_spawn_n_processes` IS the quantity the
benchmark measures -- it is the control arm of an A/B comparison whose opposing arm
(`_spawn_one_process_importing_all`) already does the batched form in the same module.
Collapsing the loop would not fix an amplification bug here; it would measure a different
question than the one the module exists to answer.

No static predicate can carry that distinction -- both arms spawn `sys.executable` from the
same module, so any AST rule that flags one must flag the other. A test can pin the CONTRACT
instead: the control arm spawns once per module, the fan-in arm spawns exactly once, and both
facts are asserted by counting calls through a monkeypatched spawn seam rather than by
launching real interpreters (this suite runs on the fast tier, and this module runs beside a
~50-concurrent-session load). If a future "batching fix" collapses `_spawn_n_processes`'s loop,
this oracle turns red -- which is correct, because that would silently swap the benchmark's
control arm for a second copy of its treatment arm.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import coordinator_core.benchmarks.shim_fanin_measure as shim_fanin_measure

#: Declaration for the register-aging sweep (C5,
#: `docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`):
#: every row of `_MODULES` names a whole importable module, never a symbol living inside a
#: parent module.
_MODULES__SUBJECT_CLASS = "module"

_MODULES = (
    "coordinator_core.ops.check_harvest_debt",
    "coordinator_core.ops.check_auto_memory_drained",
    "coordinator_core.ops.check_version_consistency",
)


def _fake_run(calls: list) -> "callable":
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_spawn_n_processes_issues_one_spawn_per_module():
    """The control arm: N modules must cost exactly N spawns, one `python -c "import <mod>"`
    child per module -- collapsing this into fewer spawns would measure warm-vs-cold-cache
    instead of N-processes-vs-one-process (see the module's own docstring)."""
    calls: list = []
    with patch.object(shim_fanin_measure.subprocess, "run", new=_fake_run(calls)):
        shim_fanin_measure._spawn_n_processes(_MODULES)

    assert len(calls) == len(_MODULES), (
        f"_spawn_n_processes issued {len(calls)} spawns for {len(_MODULES)} modules -- the "
        "control arm no longer spawns one process per module, so it no longer measures the "
        "quantity the benchmark's A/B comparison depends on. Re-read the module before "
        "collapsing this loop; batching it changes what is being measured, not just how."
    )
    for argv, module in zip(calls, _MODULES):
        assert argv[0] == shim_fanin_measure.sys.executable and f"import {module}" in argv[2], (
            f"_spawn_n_processes's spawn for {module!r} no longer imports exactly that module "
            "in its own child -- the per-module attribution this oracle pins has drifted."
        )


def test_spawn_one_process_importing_all_issues_exactly_one_spawn():
    """The opposing (already-batched) arm: same module set, exactly one spawn, all imports in
    one `-c` script. This is the batched form the register points to as proof that batching
    `_spawn_n_processes` is possible in principle but wrong for this benchmark's purpose."""
    calls: list = []
    with patch.object(shim_fanin_measure.subprocess, "run", new=_fake_run(calls)):
        shim_fanin_measure._spawn_one_process_importing_all(_MODULES)

    assert len(calls) == 1, (
        f"_spawn_one_process_importing_all issued {len(calls)} spawns for one fan-in draw -- "
        "the batched arm no longer spawns exactly once, which breaks the A/B comparison's "
        "other half."
    )
    script = calls[0][2]
    missing = [m for m in _MODULES if f"import {m}" not in script]
    assert not missing, (
        f"the single fan-in spawn's script is missing imports for {missing} -- the batched arm "
        "no longer imports every module the control arm imports, so the two arms are not "
        "comparing the same work."
    )


def test_oracle_fails_when_the_control_arm_is_collapsed():
    """Proves the oracle above is not vacuous: a deliberately-batched stand-in for
    `_spawn_n_processes` (one spawn instead of N) must FAIL the per-module spawn-count
    assertion. Without this, a future edit that quietly collapses the loop would pass this
    file's other test for the wrong reason -- an oracle that cannot fail is worthless."""
    calls: list = []

    def collapsed(modules):
        import_stmt = "; ".join(f"import {m}" for m in modules)
        shim_fanin_measure.subprocess.run(
            [shim_fanin_measure.sys.executable, "-c", import_stmt],
            capture_output=True,
            text=True,
        )

    with patch.object(shim_fanin_measure.subprocess, "run", new=_fake_run(calls)):
        collapsed(_MODULES)

    assert len(calls) != len(_MODULES), (
        "sanity check itself is broken: the collapsed stand-in was supposed to issue fewer "
        "spawns than modules, so it could demonstrate the real oracle catches a collapse."
    )
