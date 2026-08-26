"""Install-time warm-engine start: `scripts/setup.py::start_warm_engine`.

Spec backlink: docs/plans/2026-08-16-one-engine-for-the-whole-box.md § C15/C23

WHY THIS IS LOAD-BEARING. `engine.warm.enabled = true` makes a warm engine
PERMITTED, not present. Before this step existed, an install printed
`PASS [registration] engine.warm.enabled = true` and left the box with no
resident engine at all -- indistinguishable, at install time, from a box
where a server can never come up. The claim this file pins is the one that
made the difference: PASS is printed only when a server actually SERVED a
request, never merely because a spawn was issued.

The poll itself now runs in a CHILD process (`_verification_child_program`,
eng-director F1) rather than in `start_warm_engine`'s own frame -- the
installer's own interpreter has already imported `coordinator_core` from the
unstamped live checkout, so a poll running in-process would deterministically
time out regardless of which root the server was spawned against. These
tests substitute that child process at its `subprocess.run` seam and drive
`start_warm_engine` off the JSON line it prints, rather than patching a
`time` module `start_warm_engine` no longer owns.

NEGATIVE-SPEC:
  - Does NOT assert a real server can be spawned here -- that is dogfooding,
    not a unit test; the spawn and verification-child seams are substituted.
  - Does NOT assert the install fails when warmth cannot start: it must not.
    An optional performance feature never fails an install (the raising-seam
    case below).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SETUP_PY = _REPO_ROOT / "scripts" / "setup.py"


def _load_setup():
    """Load `scripts/setup.py` by path -- it is a script, not an importable
    package module, and its body is guarded by `if __name__ == "__main__"`,
    so loading it under a private module name runs no install step."""
    spec = importlib.util.spec_from_file_location("_claude_klabauter_setup_under_test", _SETUP_PY)
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


def _patch_seams(monkeypatch, setup, engine_root, *, run_result=None, spawn_exc=None, spawned=None):
    """Substitute the three seams `start_warm_engine` resolves lazily: the
    published-root resolver, the spawn, and the verification child's own
    `subprocess.run`."""
    from coordinator_core.install import engine_root_for_install
    from coordinator_core.ops.ceremony import detached_spawn

    monkeypatch.setattr(
        engine_root_for_install,
        "resolve_engine_root_for_install",
        lambda: SimpleNamespace(kind="published", root=engine_root, remediation=None),
    )

    def fake_spawn(repo_root, script_path, args=None):
        if spawn_exc is not None:
            raise spawn_exc
        if spawned is not None:
            spawned.append((repo_root, script_path))
        return True

    monkeypatch.setattr(detached_spawn, "spawn_detached", fake_spawn)

    def fake_run(cmd, **kwargs):
        assert run_result is not None, "subprocess.run must not be reached past a spawn failure"
        return run_result

    monkeypatch.setattr(setup.subprocess, "run", fake_run)


def _run_result(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def test_pass_only_when_a_server_actually_served(monkeypatch, capsys, setup_module_under_test, tmp_path):
    setup = setup_module_under_test
    engine_root = tmp_path / "engine-root"
    resolved_file = engine_root / "coordinator_core" / "__init__.py"
    spawned: list = []
    _patch_seams(
        monkeypatch,
        setup,
        engine_root,
        run_result=_run_result(json.dumps({"served": True, "coordinator_core_file": str(resolved_file)}) + "\n"),
        spawned=spawned,
    )

    setup.start_warm_engine(tmp_path / "claude-klabauter-checkout")

    out = capsys.readouterr()
    assert "PASS [warm engine]" in out.out
    assert spawned, "a spawn must actually be issued"


def test_unserved_ping_is_an_advisory_not_a_pass(monkeypatch, capsys, setup_module_under_test, tmp_path):
    """The defect this whole step exists to prevent: reporting success off a
    spawn rather than off a served response."""
    setup = setup_module_under_test
    engine_root = tmp_path / "engine-root"
    _patch_seams(
        monkeypatch,
        setup,
        engine_root,
        run_result=_run_result(json.dumps({"served": False, "coordinator_core_file": None}) + "\n"),
    )

    setup.start_warm_engine(tmp_path / "claude-klabauter-checkout")

    out = capsys.readouterr()
    assert "PASS [warm engine]" not in out.out
    assert "[ADVISORY]" in out.err


def test_a_failing_spawn_never_fails_the_install(monkeypatch, capsys, setup_module_under_test, tmp_path):
    setup = setup_module_under_test
    engine_root = tmp_path / "engine-root"
    _patch_seams(
        monkeypatch,
        setup,
        engine_root,
        run_result=None,
        spawn_exc=OSError("no such interpreter"),
    )

    setup.start_warm_engine(tmp_path / "claude-klabauter-checkout")  # must not raise

    out = capsys.readouterr()
    assert "[ADVISORY]" in out.err
    assert "PASS [warm engine]" not in out.out
