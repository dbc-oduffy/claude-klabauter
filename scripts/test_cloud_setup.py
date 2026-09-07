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

`test_stubbed_run_produces_no_prompt_shaped_output` pins that a clean `main()` run
under fully-stubbed network steps writes no prompt-shaped text. It does not exercise
stdin -- this module never calls `input()` itself, and nothing on the stubbed path
reads `sys.stdin` -- so it no longer simulates a closed stdin (Finding 3,
`code-reviewer` 2026-09-06: the prior `_ClosedStdin` patch was provably vacuous,
since deleting it changed no assertion's outcome). The real closed-stdin contract on
the one subprocess this module hands a live stdin to is pinned directly instead, by
`test_run_claude_klabauter_setup_argv_names_coordinator_root`'s assertion that
`stdin=subprocess.DEVNULL` is present in the real `subprocess.run` kwargs.

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


def _make_scratch_clones(cloud_mod, tmp_path: Path) -> dict:
    # Review: code-reviewer (2026-09-06, Finding 6) -- derived from the real
    # CLONES dict rather than hand-copied, so a URL change in cloud_setup.py
    # cannot silently drift out of sync with what these tests exercise.
    return {
        name: {**spec, "dest": str(tmp_path / name)}
        for name, spec in cloud_mod.CLONES.items()
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


def test_stubbed_run_produces_no_prompt_shaped_output(monkeypatch, tmp_path, capsys, cloud_mod):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_ok(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", tmp_path / "cloud-setup-report.json")

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
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
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
    scratch_clones = _make_scratch_clones(cloud_mod, tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)

    captured_argv = {}
    captured_kwargs = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "--i-assert-no-other-consumer"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        captured_kwargs.update(kwargs)
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
    # Review: code-reviewer (2026-09-06, 15fa79cc) -- stdin=subprocess.DEVNULL
    # had zero coverage: this test's own _fake_run previously discarded kwargs,
    # so deleting the argument would not have failed anything.
    assert captured_kwargs.get("stdin") is cloud_mod.subprocess.DEVNULL


def test_run_coordinator_install_trampoline_argv_has_non_interactive(
    monkeypatch, tmp_path, cloud_mod
):
    scratch_clones = _make_scratch_clones(cloud_mod, tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)
    orchestrator = (
        Path(scratch_clones["klabauter"]["dest"]) / "coordinator_core" / "install" / "maximalist.py"
    )
    orchestrator.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.touch()

    captured_argv = {}
    captured_kwargs = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        captured_kwargs.update(kwargs)
        return _FakeCompletedProcess()

    monkeypatch.setattr(cloud_mod.subprocess, "run", _fake_run)

    cloud_mod.run_coordinator_install_trampoline()

    argv = captured_argv["argv"]
    assert "--non-interactive" in argv
    # Review: review-integrator (2026-09-06) -- asserted on the real argv passed
    # to subprocess.run, not a re-stub of run_coordinator_install_trampoline
    # itself, per code-reviewer's deferred coverage-gap finding (aabbbb3784):
    # every other trampoline test monkeypatches the function out, so an
    # accidental removal of "--non-interactive" from this argv list would not
    # have been caught by this suite.
    assert captured_kwargs.get("stdin") is cloud_mod.subprocess.DEVNULL


def test_run_claude_klabauter_setup_records_nonzero_exit_code(monkeypatch, tmp_path, cloud_mod):
    """DR-411's audit case: a PEP-668 refusal (exit 96) must be recoverable from
    the report, not just the raised exception's message."""
    scratch_clones = _make_scratch_clones(cloud_mod, tmp_path)
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


# ---------------------------------------------------------------------------
# Arm: a non-ASCII byte in a step's recorded detail must not raise past
# `_print_summary`, which `run_step`'s exception net does not wrap (Finding 1,
# `code-reviewer` 2026-09-06).
# ---------------------------------------------------------------------------


class _AsciiOnlyStdout:
    """A stand-in for a stdout stream on a minimal-locale host (LANG=C, no
    UTF-8): `.encoding` reports `ascii`, and writing a non-ASCII character
    raises `UnicodeEncodeError`, matching what a real `print()` would do
    there."""

    encoding = "ascii"

    def __init__(self):
        self.written: list[str] = []

    def write(self, s: str) -> int:
        s.encode("ascii")  # raises UnicodeEncodeError on any non-ASCII byte
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        pass


def test_non_ascii_step_detail_under_ascii_stdout_still_exits_zero(
    monkeypatch, tmp_path, cloud_mod
):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_ok(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", tmp_path / "cloud-setup-report.json")

    def _raise_with_non_ascii_detail(report):
        raise RuntimeError("clone failed: dépôt introuvable")

    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _raise_with_non_ascii_detail)

    fake_stdout = _AsciiOnlyStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    rc = cloud_mod.main()

    assert rc == 0
    joined = "".join(fake_stdout.written)
    assert "run scripts/setup.py" in joined
    assert "FAILED" in joined
