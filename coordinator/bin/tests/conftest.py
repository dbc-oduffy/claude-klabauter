"""Shared fixtures for the `coordinator/bin` CLI suites.

Exists for ONE cross-file need: a CLI test that really dispatches an op has to
reach the engine the BOX dispatches to, and by default it does not.

A test here spawns `python <coordinator/bin/some-cli.py>`. That child self-locates
its engine from its own `__file__`, lands on the source checkout, and the source
checkout carries no engine build stamp -- so `ipc.py`'s dispatch-axis stamp gate
refuses it, exactly as the PM ruling behind that gate requires ("no fallback to
Claude-klabauter. none whatsoever ... fail hard every time if it can't go via Klabauter").
The refusal is correct; the child asking the wrong engine is the defect, and it
cost 15 red lines across six files in the 2026-08-27 bin-suite triage
(`state/audits/2026-08-27-bin-suite-failure-inventory.md` § B1).

Negative-spec for `stamped_engine_env` -- the fixture this module exists for,
and the reason IT is opt-in rather than `autouse`. It does not govern
`real_state_dir_untouched_guard` below, which IS autouse and may safely be:
that one only snapshots two directory listings and asserts nothing was added,
setting no environment and pre-resolving nothing, so it cannot delete any
suite's subject the way pre-setting an engine root would:

  - It does NOT touch `--allow-unstamped-dispatch` / `is_unstamped_dispatch_allowed`.
    That carve-out is deliberately argv-typed per invocation; a suite-wide env
    switch onto it would be the ambient bypass the stamp gate exists to close, live
    on every developer box. This fixture instead hands the child a STAMPED engine,
    so the gate passes on its own terms and is never consulted about a carve-out.
  - It is NOT `autouse`. Several suites here (`test_doctor_probe_ladder_parity`,
    `test_machine_local_ladder_parity`, `test_cc_invoke_no_ambient_live_tree`) exist
    precisely to assert what the resolver ladder does with a hermetic, signal-free
    environment. Pre-setting the root for them would delete their subject.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_LIB_DIR = Path(__file__).resolve().parent.parent / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_stamp_probe import (  # noqa: E402  (import after path setup)
    _ENGINE_ROOT_VAR,
    _stamped_dispatch_root,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_REAL_STATE_DIRS_TO_GUARD = (
    _REPO_ROOT / "state" / "improvement-queue",
    _REPO_ROOT / "state" / "lessons-outbox",
)


@pytest.fixture(autouse=True)
def real_state_dir_untouched_guard(request: pytest.FixtureRequest):
    """Fail loudly if a test under `coordinator/bin/tests/` writes into this
    repo's REAL `state/improvement-queue/` or `state/lessons-outbox/`.

    This is the pytest-reachable form of the containment check that
    `test_harvest_doe_root_machine_local_leg.py::main()` already performed --
    but `main()` is only reached via `if __name__ == "__main__":`, which
    pytest never calls, so under the suite's actual runner the guard was
    inert (see
    state/bug-backlog/2026-09-01-the-harvest-suites-containment-guard-never-runs-under-pytest.yaml).
    Autouse here makes it fire on every test in this directory, not just the
    two harvest suites, on the same terms: snapshot before, snapshot after,
    fail if the real dir gained a file.
    """
    before = {
        d: (set(os.listdir(d)) if d.is_dir() else set()) for d in _REAL_STATE_DIRS_TO_GUARD
    }
    yield
    for d in _REAL_STATE_DIRS_TO_GUARD:
        after = set(os.listdir(d)) if d.is_dir() else set()
        gained = after - before[d]
        if gained:
            pytest.fail(
                f"real_state_dir_untouched_guard: {d} gained unexpected file(s) "
                f"{gained} during {request.node.nodeid} -- this suite must never "
                f"write to the real repo state dirs",
                pytrace=False,
            )


@pytest.fixture
def stamped_engine_env(monkeypatch) -> str:
    """Point spawned CLIs at the box's stamped engine for the duration of a test.

    Skips -- never fails -- when this box has no stamped engine to offer. A
    developer clone with no published mirror built yet is a legitimate state, and
    a red test there would report the absent build as a defect in the CLI under
    test. Returns the root so a test constructing its own `env=` dict can thread
    it through rather than relying on inheritance.
    """
    root = _stamped_dispatch_root()
    if root is None:
        pytest.skip(
            f"no stamped engine on this box ({_ENGINE_ROOT_VAR} unresolvable or "
            "the resolved root carries no build stamp) — a real dispatch cannot "
            "be exercised here"
        )
    monkeypatch.setenv(_ENGINE_ROOT_VAR, root)
    return root
