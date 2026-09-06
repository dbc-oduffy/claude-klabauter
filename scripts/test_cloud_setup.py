"""scripts/test_cloud_setup.py — the prime exit criterion's falsifier.

Covers the three dispositions that break scripts/cloud_setup.py (docs/plans/
2026-09-06-a-deterministic-cloud-install-for-the-engine.md § C4): a closed stdin,
every network step failing, and a scratch HOME that disagrees with the work
directory. Also pins the argv `cloud_setup.py` builds for `scripts/setup.py` and the
durable JSON install report.

Hermeticity, since this file lands in the repo's fast tier the moment it exists and
~50 concurrent sessions share this box: no real network call in any arm, no
un-stubbed subprocess, every filesystem write under `tmp_path`. `main()` is invoked
in-process (never as a real subprocess) so that `subprocess.run` itself can be
monkeypatched to a no-op stub rather than actually shelling out to `git`/`python3`.

The closed-stdin arm is the load-bearing one (measured 2026-09-06): a genuinely
closed stdin raises `RuntimeError: input(): lost sys.stdin`, not `EOFError`. This
module never calls `input()` itself, so the arm is a regression guard against that
ever becoming false, not a test of an existing guard clause — it simulates the
measured RuntimeError via a fake stdin object rather than truly closing the test
process's own fd 0, which would be destructive to pytest on a shared box.

Run: python3 -m pytest scripts/test_cloud_setup.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CLOUD_SETUP_PY_PATH = Path(__file__).resolve().parent / "cloud_setup.py"


def _load_cloud_setup_module():
    spec = importlib.util.spec_from_file_location(
        "_scripts_cloud_setup_under_test", _CLOUD_SETUP_PY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cloud_mod():
    # Fresh module per test (not module-scoped): tests monkeypatch module-level
    # globals (CLONES, INSTALL_REPORT_PATH) and must not leak state across arms.
    return _load_cloud_setup_module()


@pytest.fixture(autouse=True)
def _restore_process_env():
    """Undo `cloud_setup.set_engine_env`'s writes to the REAL process environment.

    The `cloud_mod` fixture isolates module globals but not `os.environ`, and
    `set_engine_env` deliberately mutates the live environment — that is its whole
    job (fact 2: the env-var block is unreadable from the setup script, so the
    script exports the values itself). Under pytest that write outlives the test.

    Measured 2026-09-06: without this,
    `test_setup.py::test_resolve_claude_klabauter_root_repo_root_default` fails roughly one
    run in three, resolving the engine root to THIS file's tmp_path — because
    `resolve_claude_klabauter_root` reads `COORDINATOR_ENGINE_ROOT` and a cloud arm had left
    one behind. It only fails when random ordering happens to run that test after
    this module, which is what made it look like a flake in a file this module does
    not touch. A cross-file leak from an autouse-free fixture, not flakiness.
    """
    import os

    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


class _ClosedStdin:
    """Simulates a genuinely closed stdin (0<&-): raises RuntimeError on read,
    matching the measured 2026-09-06 baseline (`input(): lost sys.stdin`), not
    EOFError (what /dev/null raises, which is the vacuous case this arm exists
    to avoid)."""

    def read(self, *a, **kw):
        raise RuntimeError("input(): lost sys.stdin")

    def readline(self, *a, **kw):
        raise RuntimeError("input(): lost sys.stdin")


def _make_scratch_clones(tmp_path: Path) -> dict:
    return {
        "coordinator-claude": {
            "url": "https://github.com/dbc-oduffy/coordinator-claude.git",
            "dest": str(tmp_path / "coordinator-claude"),
        },
        "klabauter": {
            "url": "https://github.com/dbc-oduffy/claude-klabauter.git",
            "dest": str(tmp_path / "klabauter"),
        },
    }


def _patch_linux_root(monkeypatch, cloud_mod):
    monkeypatch.setattr(cloud_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cloud_mod.os, "geteuid", lambda: 0, raising=False)


def _patch_network_steps_ok(monkeypatch, cloud_mod):
    """Stub every network/subprocess-touching step to a hermetic no-op success."""
    monkeypatch.setattr(cloud_mod, "clone_repo", lambda name: None)
    monkeypatch.setattr(cloud_mod, "run_coordinator_install_trampoline", lambda: None)

    def _fake_run_claude_klabauter_setup(report):
        report.container_optin_requested = True
        report.setup_exit_code = 0

    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _fake_run_claude_klabauter_setup)


def _patch_network_steps_all_raise(monkeypatch, cloud_mod):
    def _raise_clone(name):
        raise RuntimeError(f"stubbed network failure: clone {name}")

    def _raise_trampoline():
        raise RuntimeError("stubbed network failure: install trampoline")

    def _raise_setup(report):
        raise RuntimeError("stubbed network failure: run scripts/setup.py")

    monkeypatch.setattr(cloud_mod, "clone_repo", _raise_clone)
    monkeypatch.setattr(cloud_mod, "run_coordinator_install_trampoline", _raise_trampoline)
    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _raise_setup)


# ---------------------------------------------------------------------------
# Arm: closed stdin (0<&-) -- the load-bearing arm
# ---------------------------------------------------------------------------


def test_closed_stdin_exits_zero_with_no_prompt_text(monkeypatch, tmp_path, capsys, cloud_mod):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_ok(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(tmp_path))
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", tmp_path / "cloud-setup-report.json")
    monkeypatch.setattr(sys, "stdin", _ClosedStdin())

    rc = cloud_mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "?" not in out  # no prompt text was written to stdout
    assert "input(" not in out


# ---------------------------------------------------------------------------
# Arm: every network step stubbed to raise -- must still exit 0 and name each
# failed step in both the printed summary and the durable JSON report.
# ---------------------------------------------------------------------------


def test_every_network_step_failing_is_named_not_silent(monkeypatch, tmp_path, capsys, cloud_mod):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_all_raise(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(tmp_path))
    report_path = tmp_path / "cloud-setup-report.json"
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", report_path)

    rc = cloud_mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    for step_name in (
        "clone coordinator-claude",
        "clone klabauter",
        "coordinator-claude install orchestrator",
        "run scripts/setup.py",
    ):
        assert step_name in out
        assert "FAILED" in out

    assert report_path.exists()
    report = json.loads(report_path.read_text())
    steps_by_name = {s["name"]: s for s in report["steps"]}
    for step_name in (
        "clone coordinator-claude",
        "clone klabauter",
        "coordinator-claude install orchestrator",
        "run scripts/setup.py",
    ):
        assert step_name in steps_by_name, f"{step_name} missing from report"
        assert steps_by_name[step_name]["ok"] is False
        assert steps_by_name[step_name]["detail"]  # non-empty diagnostic


# Review: overengineering-reviewer — cut the scratch-HOME arm. `set_engine_env`
# is six lines and reads only `CLONES`; it cannot derive a path from `HOME` by
# construction (no `os.environ["HOME"]` read exists anywhere in the module),
# so the arm asserted a property the code cannot violate rather than pinning
# real behaviour.


# ---------------------------------------------------------------------------
# Arm: the argv cloud_setup.py builds for scripts/setup.py names
# --coordinator-root pointing at the CLONES coordinator-claude destination.
# ---------------------------------------------------------------------------


def test_run_claude_klabauter_setup_argv_names_coordinator_root(monkeypatch, tmp_path, cloud_mod):
    scratch_clones = _make_scratch_clones(tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)

    captured_argv = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "--i-assert-no-other-consumer"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        return _FakeCompletedProcess()

    monkeypatch.setattr(cloud_mod.subprocess, "run", _fake_run)

    report = cloud_mod.Report()
    cloud_mod.run_claude_klabauter_setup(report)

    argv = captured_argv["argv"]
    assert "--coordinator-root" in argv
    idx = argv.index("--coordinator-root")
    assert argv[idx + 1] == scratch_clones["coordinator-claude"]["dest"]
    assert report.container_optin_requested is True
    assert report.setup_exit_code == 0


def test_run_claude_klabauter_setup_records_nonzero_exit_code(monkeypatch, tmp_path, cloud_mod):
    """DR-411's audit case: a PEP-668 refusal (exit 96) must be recoverable from
    the report, not just the raised exception's message."""
    scratch_clones = _make_scratch_clones(tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)

    class _FakeCompletedProcess:
        returncode = 96
        stdout = ""
        stderr = "externally-managed-environment"

    monkeypatch.setattr(
        cloud_mod.subprocess, "run", lambda argv, **kwargs: _FakeCompletedProcess()
    )

    report = cloud_mod.Report()
    with pytest.raises(RuntimeError):
        cloud_mod.run_claude_klabauter_setup(report)

    assert report.container_optin_requested is True
    assert report.setup_exit_code == 96


# ---------------------------------------------------------------------------
# Non-Linux / non-root host precondition -- the arm that runs natively here.
# ---------------------------------------------------------------------------


def test_host_precondition_refuses_on_non_linux_and_records_nothing_executed(
    monkeypatch, tmp_path, cloud_mod
):
    # No monkeypatch of platform/geteuid: this arm exercises the real host, which
    # on this Windows box is exactly the case the precondition must refuse.
    report_path = tmp_path / "cloud-setup-report.json"
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", report_path)

    def _fail_if_called(*a, **kw):
        raise AssertionError("no step may run when the host precondition fails")

    monkeypatch.setattr(cloud_mod, "clone_repo", _fail_if_called)
    monkeypatch.setattr(cloud_mod, "run_coordinator_install_trampoline", _fail_if_called)
    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _fail_if_called)

    rc = cloud_mod.main()

    assert rc == 0
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert len(report["steps"]) == 1
    assert report["steps"][0]["name"] == "host precondition"
    assert report["steps"][0]["ok"] is False
