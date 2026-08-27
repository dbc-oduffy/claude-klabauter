"""
coordinator_core.ops.ceremony.tests.test_commit_pipeline_import_cost — pins that
importing the commit pipeline does NOT drag `asyncio` (nor the fleet
archive-sweep module that needs it), nor `socket`, into the interpreter.

WHY THIS IS A GUARD AND NOT A COMMENT. The commit path is a process before it is
anything else: every ceremony commit pays interpreter start plus this module's
import graph before a single git call happens. `asyncio` is the single largest
avoidable node in that graph — measured 2026-08-26 on the reference box, k=61
process-time samples of a cold `python -c "import <mod>"`:

    bare interpreter                          ~21ms
    + asyncio (and its ssl/socket subtree)    ~52ms
    commit_pipeline, asyncio eager            ~88ms
    commit_pipeline, asyncio deferred         ~61ms

Against the direct counterfactual -- the same import with `socket`, `asyncio`
and the fleet module forced in first -- `-X importtime` summed over every module
gives 59.1ms deferred against 83.0ms eager, k=15, spread under 5ms. That is the
low-noise form of the same result and the number to re-derive when changing
this: process time on a shared box moves with peer load, import self-time does
not.

asyncio is on a path that never awaits anything unless the cadence-gated in-plane
archive sweep actually runs, which is once per `_ARCHIVE_SWEEP_INTERVAL_S`, not
once per commit. The two imports live inside `_run_in_plane_archive_sweep`,
BELOW its cadence gate; `_archive_sweep_cap()` exists as a function for the same
reason — binding the cap as a module-level constant is what forced the eager
import in the first place, and would silently re-force it.

The regression this guards is invisible by inspection: adding
`from coordinator_core.ops.fleet import archive_terminal_handoffs` back at
module scope, or any new top-level `import asyncio`, costs ~26ms per commit
across ~50 concurrent sessions and breaks nothing a functional test can see.

Negative-spec:
    Do NOT turn this into a timing assertion. The numbers above are the
    motivation, not the property — a wall-clock or process-time budget here
    would measure peer load on a shared box (CLAUDE.md § Load norm) and flake.
    The property is import-graph membership, which is deterministic.
    Do NOT arm lazy ops in the probe. Arming eagerly imports the ops package
    and would make the assertion pass for the wrong reason.
    Do NOT relax this to "asyncio only" — the fleet module is a coroutine
    module, so re-importing it eagerly re-imports asyncio transitively and the
    asyncio half of the assertion would catch it only by accident.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pytest

# Spawns a real subprocess (fresh interpreter) — the property under test is
# `sys.modules` absence, and this test process has `asyncio` loaded already via
# pytest and its own plugins, so an in-process assertion is unmeasurable.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

#: Modules that must stay OUT of the interpreter after a bare
#: `import coordinator_core.ops.ceremony.commit_pipeline`. Prefix-matched, so a
#: submodule (`asyncio.events`) counts as a hit for its parent.
#:
#: `socket` (with `selectors`/`select`, ~4.3ms) arrives by a SECOND route:
#: `machine_resolver._hostname_short`, reached through `doe_root_pointer` ->
#: `commit_trailers`. `compute_machine` resolves `$COORDINATOR_MACHINE` and the
#: settings file first, so most invocations never ask for a hostname at all --
#: the import there is deferred for the same reason asyncio's is here, and is
#: pinned here because this is the path that pays for it.
_FORBIDDEN_PREFIXES = (
    "asyncio",
    "coordinator_core.ops.fleet.archive_terminal_handoffs",
    "select",
    "selectors",
    "socket",
    "ssl",
)

_PROBE = textwrap.dedent(
    """
    import json
    import sys

    # Lazy ops deliberately left UNARMED — no COORDINATOR_CORE_LAZY_OPS env
    # var, no sys._coordinator_core_lazy_ops attribute. Arming it sweeps the
    # whole ops package in eagerly and the assertion would pass vacuously.
    import coordinator_core.ops.ceremony.commit_pipeline  # noqa: F401

    sys.stdout.write(json.dumps(sorted(sys.modules)))
    """
)


def _loaded_modules() -> list[str]:
    """Return `sys.modules` of a fresh interpreter that imported nothing but
    the commit pipeline."""
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, (
        "import probe failed to import commit_pipeline at all -- that is a "
        f"broken import, not an import-cost regression:\n{proc.stderr}"
    )
    return json.loads(proc.stdout)


def test_commit_pipeline_import_does_not_pull_asyncio() -> None:
    """A cold `import commit_pipeline` leaves `asyncio` and the fleet
    archive-sweep module unimported."""
    loaded = _loaded_modules()
    offenders = [
        name
        for name in loaded
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in _FORBIDDEN_PREFIXES
        )
    ]
    assert not offenders, (
        "importing commit_pipeline pulled in modules the commit hot path must "
        f"not pay for: {', '.join(offenders)}.\n"
        "These belong inside `_run_in_plane_archive_sweep`, below its cadence "
        "gate -- see `_archive_sweep_cap()` for why the cap must stay a "
        "function rather than a module-level constant."
    )


def test_sweep_still_reaches_its_deferred_imports() -> None:
    """The deferral is a move, not a deletion: the sweep's own module and cap
    still resolve when the sweep runs."""
    from coordinator_core.ops.ceremony import commit_pipeline

    cap = commit_pipeline._archive_sweep_cap()
    assert isinstance(cap, int) and cap > 0, (
        f"_archive_sweep_cap() returned {cap!r} -- plan_sweep's `cap` param is "
        "required with no unbounded default, so this call site must supply a "
        "real positive bound."
    )


def test_hostname_resolution_survives_the_socket_deferral() -> None:
    """Same move on `machine_resolver`: the hostname rung still resolves, so the
    fallback chain below it (`$HOSTNAME`, then `"unknown"`) is not silently
    doing the work now."""
    from coordinator_core.machine_resolver import _hostname_short

    host = _hostname_short()
    assert host, (
        "_hostname_short() returned no hostname -- if the deferred `import "
        "socket` failed it would raise, so an empty result means gethostname "
        "itself is failing and compute_machine has quietly fallen through to "
        "$HOSTNAME or 'unknown'."
    )
    assert "." not in host, f"expected a domain-stripped short name, got {host!r}"
