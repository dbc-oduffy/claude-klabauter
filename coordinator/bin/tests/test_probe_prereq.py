"""test_probe_prereq.py — pytest tests for coordinator/bin/probe-prereq.py.

Covers the one subcommand documented in probe-prereq.py's module
docstring: python3 (thin pass/fail passthrough over
coordinator_core.install.prereq_probe.probe_python()).

The module's original `bash-version` subcommand (and its `bash -c` spawn to
read `BASH_VERSINFO`) was removed 2026-07-29 — see the module docstring's
"Negative-spec (bash-version, removed 2026-07-29)" for the consumer trace
that found it unwired. Its tests were removed with it, not skipped.

All cases stub prereq_probe.probe_python — no real binaries are required.

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


def _run_cli(mod, args: list[str]):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(args)
    return rc, out.getvalue()


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


def test_git_lfs_enable_subcommand_is_gone(mod):
    with pytest.raises(SystemExit):
        mod.main(["git-lfs-enable"])
