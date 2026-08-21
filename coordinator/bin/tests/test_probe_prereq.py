"""test_probe_prereq.py — pytest tests for coordinator/bin/probe-prereq.py.

Covers the two subcommands documented in probe-prereq.py's module
docstring: python3 (thin pass/fail passthrough over
coordinator_core.install.prereq_probe.probe_python()), and git-lfs-enable
(--check-only never mutates; otherwise mutates via `git lfs install` only
when `git lfs version` succeeds, and always exits 0 regardless of status).

The module's original `bash-version` subcommand (and its `bash -c` spawn to
read `BASH_VERSINFO`) was removed 2026-07-29 — see the module docstring's
"Negative-spec (bash-version, removed 2026-07-29)" for the consumer trace
that found it unwired. Its tests were removed with it, not skipped.

All cases stub the module's own `_run` helper (and, for git-lfs-enable,
subprocess.run directly for the mutation call) rather than spawning a real
git-lfs — no real binaries are required to run this suite.

Run: python -m pytest coordinator/bin/tests/test_probe_prereq.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("probe_prereq", _BIN_DIR / "probe-prereq.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _run_cli(mod, args: list[str]):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(args)
    return rc, out.getvalue()


# ---------------------------------------------------------------------------
# python3
# ---------------------------------------------------------------------------


def test_python3_pass_delegates_to_prereq_probe(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe, "probe_python", lambda: '{"name":"python","status":"pass","severity":"hard","detail":"Python 3.12.0","remediation":""}\n'
    )

    rc, out = _run_cli(mod, ["python3"])

    assert rc == 0
    row = json.loads(out)
    assert row["name"] == "python"
    assert row["status"] == "pass"


def test_python3_fail_on_appx_stub_delegates_status(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe,
        "probe_python",
        lambda: '{"name":"python","status":"fail","severity":"hard","detail":"App-Execution-Alias stub","remediation":"see docs"}\n',
    )

    rc, out = _run_cli(mod, ["python3"])

    assert rc == 1
    row = json.loads(out)
    assert row["status"] == "fail"
    assert "stub" in row["detail"]


# ---------------------------------------------------------------------------
# git-lfs-enable
# ---------------------------------------------------------------------------


def test_git_lfs_enable_check_only_never_mutates(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe,
        "probe_git_lfs",
        lambda: '{"name":"git_lfs","status":"warn","severity":"advisory","detail":"git-lfs present but not configured","remediation":"run: git lfs install"}\n',
    )

    def fail_run(*a, **k):
        raise AssertionError("subprocess.run must not be called under --check-only")

    monkeypatch.setattr(mod.subprocess, "run", fail_run)
    monkeypatch.setattr(mod, "_run", fail_run)

    rc, out = _run_cli(mod, ["git-lfs-enable", "--check-only"])

    assert rc == 0
    row = json.loads(out)
    assert row["status"] == "warn"


def test_git_lfs_enable_mutates_when_binary_functional(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    probe_calls = {"n": 0}

    def fake_probe():
        probe_calls["n"] += 1
        return '{"name":"git_lfs","status":"pass","severity":"advisory","detail":"post-mutation ready","remediation":""}\n'

    monkeypatch.setattr(prereq_probe, "probe_git_lfs", fake_probe)

    spawns = []

    def fake_run(argv, **kwargs):
        spawns.append(argv)
        return _FakeResult(0, stdout="git-lfs/3.5.1")

    monkeypatch.setattr(mod, "_run", fake_run)

    def fail_bare_run(*a, **k):
        raise AssertionError(
            "`git lfs install` must go through `_run`, which bounds it with a "
            "timeout and captures its output away from this command's JSON line; "
            "a bare subprocess.run does neither"
        )

    monkeypatch.setattr(mod.subprocess, "run", fail_bare_run)

    rc, out = _run_cli(mod, ["git-lfs-enable"])

    assert rc == 0
    assert spawns == [["git", "lfs", "version"], ["git", "lfs", "install"]]
    row = json.loads(out)
    assert row["status"] == "pass"


def test_git_lfs_install_is_bounded_like_its_neighbour(mod, monkeypatch):
    """Both git-lfs spawns carry the same finite timeout.

    `git lfs install` used to run bare on the line after a timeout-guarded
    `git lfs version` — adjacent spawns, asymmetric guarding. A hung git on a
    cold install path blocked the installer with nothing to report, and this
    command's stdout is a JSON line a caller parses, so the mutation's own
    output belonged in a capture rather than mixed into it.
    """
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe,
        "probe_git_lfs",
        lambda: '{"name":"git_lfs","status":"pass","severity":"advisory","detail":"ready","remediation":""}\n',
    )

    import inspect

    declared_timeout = inspect.signature(mod._run).parameters["timeout"].default
    assert 0 < declared_timeout < 60, (
        "`_run`'s timeout default is the bound both git-lfs spawns inherit; an "
        "absent or unbounded default makes that bound decorative"
    )

    timeouts = []

    def fake_run(argv, *, timeout=declared_timeout, **kwargs):
        timeouts.append(timeout)
        return _FakeResult(0, stdout="git-lfs/3.5.1")

    monkeypatch.setattr(mod, "_run", fake_run)

    rc, _ = _run_cli(mod, ["git-lfs-enable"])

    assert rc == 0
    assert timeouts == [declared_timeout, declared_timeout]


def test_git_lfs_enable_skips_mutation_when_binary_absent(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe,
        "probe_git_lfs",
        lambda: '{"name":"git_lfs","status":"warn","severity":"advisory","detail":"git-lfs not found","remediation":"install git-lfs"}\n',
    )
    monkeypatch.setattr(mod, "_run", lambda *a, **k: None)

    def fail_run(*a, **k):
        raise AssertionError("must not attempt `git lfs install` when the binary is absent")

    monkeypatch.setattr(mod.subprocess, "run", fail_run)

    rc, out = _run_cli(mod, ["git-lfs-enable"])

    assert rc == 0
    assert json.loads(out)["status"] == "warn"


def test_git_lfs_enable_always_exits_zero_even_on_warn(mod, monkeypatch):
    from coordinator_core.install import prereq_probe

    monkeypatch.setattr(
        prereq_probe,
        "probe_git_lfs",
        lambda: '{"name":"git_lfs","status":"warn","severity":"advisory","detail":"still not configured","remediation":"run: git lfs install"}\n',
    )
    monkeypatch.setattr(mod, "_run", lambda *a, **k: _FakeResult(1))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeResult(0))

    rc, out = _run_cli(mod, ["git-lfs-enable"])

    assert rc == 0
