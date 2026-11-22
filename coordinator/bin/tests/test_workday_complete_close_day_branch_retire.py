"""step9-dispatch retires the configured day branch after a successful default close and only then."""
from __future__ import annotations

import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_BIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI = os.path.join(_BIN_DIR, "workday-complete-close.py")


def _load_module():
    if _BIN_DIR not in sys.path:
        sys.path.insert(0, _BIN_DIR)
    loader = SourceFileLoader("workday_complete_close_retire_test", _CLI)
    spec = importlib.util.spec_from_file_location("workday_complete_close_retire_test", _CLI, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Args:
    def __init__(self, **kw) -> None:
        self.only_mode = False
        self.no_push = False
        self.dry_run = False
        self.commit_span = None
        self.for_date = None
        self.scope_summary = None
        for k, v in kw.items():
            setattr(self, k, v)


class _Step9:
    def __init__(self, rc: int) -> None:
        self.rc = rc

    def main(self, forward):
        return self.rc


def _wire(monkeypatch, tmp_path, *, step9_rc=0, branch="work/box/2026-11-21", retire=None):
    from coordinator_core import daily_branch
    from coordinator_core.ops.ceremony import day_branch_retire

    module = _load_module()
    calls: list[tuple[Path, str]] = []

    def _fake_retire(root, name, **kw):
        calls.append((Path(root), name))
        return retire(root, name) if retire else day_branch_retire.DayBranchRetireOutcome(
            deleted=True, containing_ref="refs/remotes/origin/main"
        )

    monkeypatch.setenv("COORDINATOR_ROOT", str(tmp_path))
    monkeypatch.setattr(module, "_load_step9_module", lambda: _Step9(step9_rc))
    monkeypatch.setattr(daily_branch, "read_configured_day_branch", lambda root: branch)
    monkeypatch.setattr(day_branch_retire, "retire_contained_day_branch", _fake_retire)
    return module, calls


def test_default_close_retires_the_configured_day_branch_and_prints_containing_ref(
    monkeypatch, tmp_path, capsys
) -> None:
    module, calls = _wire(monkeypatch, tmp_path)

    rc = module.cmd_step9_dispatch(_Args())

    assert rc == 0
    assert calls == [(tmp_path, "work/box/2026-11-21")]
    err = capsys.readouterr().err
    assert "deleted remote day branch work/box/2026-11-21" in err
    assert "refs/remotes/origin/main" in err


@pytest.mark.parametrize(
    "flags",
    [{"no_push": True}, {"dry_run": True}, {"for_date": "2026-11-20"}, {"commit_span": "a..b"}],
)
def test_no_push_dry_run_for_date_and_commit_span_delete_nothing(monkeypatch, tmp_path, flags) -> None:
    module, calls = _wire(monkeypatch, tmp_path)

    assert module.cmd_step9_dispatch(_Args(**flags)) == 0
    assert calls == []


def test_only_mode_deletes_nothing(monkeypatch, tmp_path) -> None:
    module, calls = _wire(monkeypatch, tmp_path)

    assert module.cmd_step9_dispatch(_Args(only_mode=True)) == 0
    assert calls == []


def test_failed_step9_deletes_nothing_and_keeps_its_exit_code(monkeypatch, tmp_path) -> None:
    module, calls = _wire(monkeypatch, tmp_path, step9_rc=3)

    assert module.cmd_step9_dispatch(_Args()) == 3
    assert calls == []


def test_no_configured_day_branch_skips(monkeypatch, tmp_path) -> None:
    module, calls = _wire(monkeypatch, tmp_path, branch=None)

    assert module.cmd_step9_dispatch(_Args()) == 0
    assert calls == []


def test_refusal_is_reported_and_exit_code_stays_zero(monkeypatch, tmp_path, capsys) -> None:
    from coordinator_core.ops.ceremony.day_branch_retire import DayBranchRetireOutcome

    module, calls = _wire(
        monkeypatch, tmp_path,
        retire=lambda root, name: DayBranchRetireOutcome(refused="contained-only-in-itself"),
    )

    assert module.cmd_step9_dispatch(_Args()) == 0
    assert len(calls) == 1
    assert "refused contained-only-in-itself" in capsys.readouterr().err


def test_retirer_exception_never_changes_the_exit_code(monkeypatch, tmp_path, capsys) -> None:
    def _boom(root, name):
        raise RuntimeError("boom")

    module, calls = _wire(monkeypatch, tmp_path, retire=_boom)

    assert module.cmd_step9_dispatch(_Args()) == 0
    assert "skipped" in capsys.readouterr().err
