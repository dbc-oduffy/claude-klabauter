"""Install-time warm-engine start: `scripts/setup.py::start_warm_engine`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15/C23

WHY THIS IS LOAD-BEARING. `engine.warm.enabled = true` makes a warm engine
PERMITTED, not present. Before this step existed, an install printed
`PASS [registration] engine.warm.enabled = true` and left the box with no
resident engine at all -- indistinguishable, at install time, from a box
where a server can never come up. The claim this file pins is the one that
made the difference: PASS is printed only when a server actually SERVED a
request, never merely because a spawn was issued.

NEGATIVE-SPEC:
  - Does NOT assert a real server can be spawned here -- that is dogfooding,
    not a unit test; the spawn and dispatch seams are substituted.
  - Does NOT assert the install fails when warmth cannot start: it must not.
    An optional performance feature never fails an install (the raising-seam
    case below).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SETUP_PY = _REPO_ROOT / "scripts" / "setup.py"


def _load_setup():
    """Load `scripts/setup.py` by path -- it is a script, not an importable
    package module, and its body is guarded by `if __name__ == "__main__"`,
    so loading it under a private module name runs no install step."""
    spec = importlib.util.spec_from_file_location("_makima_setup_under_test", _SETUP_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture(scope="module")
def setup_module_under_test():
    return _load_setup()


def _patch_seams(monkeypatch, setup, *, served, spawn_exc=None, spawned=None):
    """Substitute the two seams `start_warm_engine` imports lazily, by
    installing them on the already-imported engine modules it imports FROM
    (the function resolves them at call time, not at module import)."""
    from coordinator_core.ops.ceremony import detached_spawn
    from coordinator_core.warm import client

    def fake_spawn(repo_root, script_path, args=None):
        if spawn_exc is not None:
            raise spawn_exc
        if spawned is not None:
            spawned.append((repo_root, script_path))
        return True

    monkeypatch.setattr(detached_spawn, "spawn_detached", fake_spawn)
    monkeypatch.setattr(client, "try_warm_dispatch", lambda msg: served)
    # Substitute the module-global `time` in setup's own namespace rather than
    # the stdlib module's attributes: patching `time.monotonic` process-wide
    # would reach pytest's own timing for the duration of the test.
    monkeypatch.setattr(setup, "time", _FakeClock(), raising=True)


def test_pass_only_when_a_server_actually_served(monkeypatch, capsys, setup_module_under_test):
    setup = setup_module_under_test
    spawned: list = []
    _patch_seams(
        monkeypatch,
        setup,
        served={"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        spawned=spawned,
    )

    setup.start_warm_engine(Path("X:/engine-root"))

    out = capsys.readouterr()
    assert "PASS [warm engine]" in out.out
    assert spawned, "a spawn must actually be issued"


def test_unserved_ping_is_an_advisory_not_a_pass(monkeypatch, capsys, setup_module_under_test):
    """The defect this whole step exists to prevent: reporting success off a
    spawn rather than off a served response."""
    setup = setup_module_under_test
    _patch_seams(monkeypatch, setup, served=None)

    setup.start_warm_engine(Path("X:/engine-root"))

    out = capsys.readouterr()
    assert "PASS [warm engine]" not in out.out
    assert "[ADVISORY]" in out.err


def test_a_failing_spawn_never_fails_the_install(monkeypatch, capsys, setup_module_under_test):
    setup = setup_module_under_test
    _patch_seams(monkeypatch, setup, served=None, spawn_exc=OSError("no such interpreter"))

    setup.start_warm_engine(Path("X:/engine-root"))  # must not raise

    out = capsys.readouterr()
    assert "[ADVISORY]" in out.err
    assert "PASS [warm engine]" not in out.out


class _FakeClock:
    """Stands in for the `time` module inside `scripts/setup.py` only.

    Reading 1 sets the deadline, reading 2 is inside it so the poll body
    runs at least once (otherwise the served case could never be observed),
    and reading 3 is far past it so an unserved case terminates instead of
    spinning -- the deadline is a real bound, not a hope. `sleep` is a no-op
    so an unserved case costs no wall-clock.
    """

    def __init__(self) -> None:
        self._readings = iter([0.0, 1.0, 1000.0])

    def monotonic(self) -> float:
        try:
            return next(self._readings)
        except StopIteration:
            return 9999.0

    def sleep(self, _secs: float) -> None:
        return None
