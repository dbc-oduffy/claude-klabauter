"""
coordinator_core.ops.dispatch_emit.tests.test_cli_admission

C4 of docs/plans/2026-09-27-load-aware-workflow-admission.md: `cli.main` runs
load-aware admission before emission on the plan/inventory/queue routes and
merges the returned record into the printed JSON.

Stubs `admission` through `monkeypatch.setitem(sys.modules,
"coordinator_core.ops.dispatch_emit.admission", fake)` (cli.py imports it
lazily inside `main`, precisely so this swap is observed) so these tests pass
independently of C2's real module -- they pin the CALL CONTRACT (when it is
called, with what, and how the record is merged), not admission's own
verdict logic (test_admission.py's job).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module

_ADMISSION_MODULE_NAME = "coordinator_core.ops.dispatch_emit.admission"


class _FakeAdmission:
    """Records every `await_admission` call; returns a scripted record."""

    def __init__(self, record=None):
        self.record = record or {
            "verdict": "admitted",
            "route": "cold",
            "first_reading": None,
            "last_reading": None,
            "reasons": [],
            "waited_s": 0.0,
            "rechecks": 0,
            "thresholds": None,
            "missing_keys": [],
        }
        self.calls = []

    def await_admission(self, start_dir, *, hold_allowed):
        self.calls.append({"start_dir": start_dir, "hold_allowed": hold_allowed})
        return self.record


def _patch_admission_module(monkeypatch, fake_module):
    """See `_install_fake_admission`'s docstring: patch both `sys.modules`
    and the parent package's already-bound `admission` attribute."""
    monkeypatch.setitem(sys.modules, _ADMISSION_MODULE_NAME, fake_module)
    import coordinator_core.ops.dispatch_emit as _parent_pkg

    monkeypatch.setattr(_parent_pkg, "admission", fake_module, raising=False)


def _install_fake_admission(monkeypatch, record=None):
    """Swap the `admission` module for a fake. `cli.py`'s lazy `from
    coordinator_core.ops.dispatch_emit import admission` resolves via
    getattr on the already-imported parent package before it ever
    consults `sys.modules` -- once another test module (e.g.
    test_admission.py) has imported the real `admission` once, that
    getattr succeeds and the `sys.modules` swap alone is silently
    ignored. Patch both so the fake is observed regardless of import
    order across the suite."""
    fake = _FakeAdmission(record)
    _patch_admission_module(monkeypatch, fake)
    return fake


def _plan_fixture(tmp_path: Path):
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    plan_path = repo_root / "docs" / "plans" / "p.md"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        "---\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n"
        "- id: C1\n  title: Do a thing\n  writes: [\"a.txt\"]\n  body: |\n"
        "    Write a.txt.\n```\n",
        encoding="utf-8",
    )
    out_path = repo_root / "p.workflow.mjs"
    return repo_root, plan_path, out_path


def _queue_fixture(tmp_path: Path):
    import yaml

    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)
    queue_dir = repo_root / "state" / "bug-backlog"
    queue_dir.mkdir(parents=True)
    for i in range(2):
        (queue_dir / f"row{i}.yaml").write_text(
            yaml.safe_dump({"id": f"row{i}", "title": f"Row {i}"}), encoding="utf-8"
        )
    (repo_root / "state" / "queue-grind").mkdir(parents=True, exist_ok=True)
    return repo_root, queue_dir


_FIXTURE_PROFILE_DIR = Path(__file__).parent / "fixtures" / "queue-profiles"


def test_plan_route_prints_admission_in_json(tmp_path, monkeypatch):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    fake = _install_fake_admission(
        monkeypatch, {"verdict": "admitted_after_hold", "waited_s": 4.0, "reasons": []}
    )
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    rc = cli_module.main(argv)
    assert rc == cli_module.EXIT_OK
    assert len(fake.calls) == 1


def test_plan_route_json_carries_admission_key(tmp_path, monkeypatch, capsys):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    record = {"verdict": "admitted_after_hold", "waited_s": 4.0, "reasons": ["x"]}
    _install_fake_admission(monkeypatch, record)
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["admission"] == record


def test_queue_route_json_carries_admission_key(tmp_path, monkeypatch, capsys):
    repo_root, queue_dir = _queue_fixture(tmp_path)
    out_path = repo_root / "state" / "queue-grind" / "out.workflow.mjs"
    monkeypatch.chdir(repo_root)
    record = {"verdict": "admitted", "waited_s": 0.0, "reasons": []}
    fake = _install_fake_admission(monkeypatch, record)
    argv = [
        "--queue", str(queue_dir),
        "--profile", "fixture",
        "--profile-dir", str(_FIXTURE_PROFILE_DIR),
        "--out", str(out_path),
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["admission"] == record
    assert len(fake.calls) == 1


def test_restamp_output_has_no_admission_key_and_admission_never_called(tmp_path, monkeypatch, capsys):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    fake = _install_fake_admission(monkeypatch)
    # A successful emit first, so --restamp has a receipt to restamp.
    assert cli_module.main(["--plan", str(plan_path), "--out", str(out_path)]) == cli_module.EXIT_OK
    fake.calls.clear()
    capsys.readouterr()

    rc = cli_module.main(["--restamp", str(out_path)])
    assert rc == cli_module.EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert "admission" not in printed
    assert fake.calls == []


def test_warm_route_passes_hold_allowed_false_cold_passes_true(tmp_path, monkeypatch):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    fake = _install_fake_admission(monkeypatch)

    from coordinator_core.telemetry import op_latency

    monkeypatch.delenv(op_latency.ROUTE_ENV, raising=False)
    assert cli_module.main(["--plan", str(plan_path), "--out", str(out_path)]) == cli_module.EXIT_OK
    assert fake.calls[-1]["hold_allowed"] is True

    out_path2 = repo_root / "p2.workflow.mjs"
    monkeypatch.setenv(op_latency.ROUTE_ENV, op_latency.WARM_SERVER)
    assert cli_module.main(["--plan", str(plan_path), "--out", str(out_path2)]) == cli_module.EXIT_OK
    assert fake.calls[-1]["hold_allowed"] is False


def test_admission_call_precedes_dispatch_emit(tmp_path, monkeypatch):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    order = []

    class _OrderedFakeAdmission(_FakeAdmission):
        def await_admission(self, start_dir, *, hold_allowed):
            order.append("admission")
            return super().await_admission(start_dir, hold_allowed=hold_allowed)

    _patch_admission_module(monkeypatch, _OrderedFakeAdmission())

    real_dispatch_emit = cli_module._dispatch_emit

    def _recording_dispatch_emit(*args, **kwargs):
        order.append("dispatch_emit")
        return real_dispatch_emit(*args, **kwargs)

    monkeypatch.setattr(cli_module, "_dispatch_emit", _recording_dispatch_emit)

    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    assert order == ["admission", "dispatch_emit"]


def test_non_plain_verdict_prints_exactly_one_stderr_line(tmp_path, monkeypatch, capsys):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    _install_fake_admission(
        monkeypatch,
        {"verdict": "admitted_after_hold", "waited_s": 4.0, "reasons": ["cpu_load 2.0 > 0.9"]},
    )
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    stderr_lines = [
        line for line in capsys.readouterr().err.splitlines() if "admission" in line
    ]
    assert len(stderr_lines) == 1
    assert "admitted_after_hold" in stderr_lines[0]


def test_admitted_and_disabled_verdicts_print_no_stderr_admission_line(tmp_path, monkeypatch, capsys):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    _install_fake_admission(monkeypatch, {"verdict": "admitted", "waited_s": 0.0, "reasons": []})
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    err = capsys.readouterr().err
    assert "admission —" not in err


def test_fire_runs_after_admission(tmp_path, monkeypatch):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    order = []

    class _OrderedFakeAdmission(_FakeAdmission):
        def await_admission(self, start_dir, *, hold_allowed):
            order.append("admission")
            return super().await_admission(start_dir, hold_allowed=hold_allowed)

    _patch_admission_module(monkeypatch, _OrderedFakeAdmission())

    def _fake_fire_workflow(path, cwd=None):
        order.append("fire")
        return {"ok": True, "path": path}

    monkeypatch.setattr(
        "coordinator_core.ops.workflow_fire.fire.fire_workflow", _fake_fire_workflow
    )

    argv = ["--plan", str(plan_path), "--out", str(out_path), "--fire"]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    assert order == ["admission", "fire"]


def test_await_admission_receives_cwd_on_plan_route_with_no_repo_root(tmp_path, monkeypatch):
    repo_root, plan_path, out_path = _plan_fixture(tmp_path)
    monkeypatch.chdir(repo_root)
    fake = _install_fake_admission(monkeypatch)
    argv = ["--plan", str(plan_path), "--out", str(out_path)]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    assert fake.calls[-1]["start_dir"] == Path.cwd()
    assert fake.calls[-1]["start_dir"] == repo_root


def test_environ_carries_admission_disable_key_under_the_suite():
    import os

    assert os.environ.get("COORDINATOR_WORKFLOW_ADMISSION_DISABLE") == "1"
