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

Negative-spec, and the reason this is an opt-in fixture rather than `autouse`:

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
