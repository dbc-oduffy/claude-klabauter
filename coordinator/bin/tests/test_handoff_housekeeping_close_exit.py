from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "handoff-housekeeping.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("handoff_housekeeping_close_exit", str(_SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _stub(mod, monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_path", lambda: str(tmp_path))
    monkeypatch.setattr(mod, "_stamp_archive_sweeps_liveness", lambda _root: None)
    import coordinator_core.lifecycle as lifecycle
    import coordinator_core.ops.fleet._common as fleet_common
    import coordinator_core.ops.fleet.memo_heal as memo_heal

    monkeypatch.setattr(lifecycle, "git_common_dir", lambda _p: tmp_path / ".git")
    monkeypatch.setattr(fleet_common, "main_worktree_root", lambda _c: tmp_path)
    monkeypatch.setattr(memo_heal, "_memo_heal_inbox", lambda *_a, **_k: {"exit_code": 0, "acted": []})


def test_a_failed_close_pass_exits_nonzero_and_still_reports_the_sweep(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _stub(mod, monkeypatch, tmp_path)
    import coordinator_core.housekeeping.cycle as cycle

    monkeypatch.setattr(
        cycle, "_handler",
        lambda _p, _c: {"exit_code": 0, "archived": ["a", "b"], "closed": 1,
                        "close_error": "ValueError: boom"},
    )

    assert mod.main(["--cap", "5"]) == 1
    captured = capsys.readouterr()
    assert "closed 1, archived 2" in captured.out
    assert "close pass failed: ValueError: boom" in captured.err


def test_a_clean_close_pass_still_exits_zero(tmp_path, monkeypatch):
    mod = _load_module()
    _stub(mod, monkeypatch, tmp_path)
    import coordinator_core.housekeeping.cycle as cycle

    monkeypatch.setattr(
        cycle, "_handler",
        lambda _p, _c: {"exit_code": 0, "archived": [], "closed": 0, "close_error": None},
    )

    assert mod.main(["--cap", "5"]) == 0


def test_dry_run_lists_the_would_release_set(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _stub(mod, monkeypatch, tmp_path)
    import coordinator_core.housekeeping.cycle as cycle
    import coordinator_core.ops.fleet.archive_terminal_handoffs as ath

    monkeypatch.setattr(ath, "plan_sweep", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(
        cycle, "plan_gate_releases",
        lambda _w, _c: {"would_release": ["state/handoffs/x.md"], "dead_blockers": [],
                        "unresolvable_blockers": [{"id": "y"}]},
    )

    assert mod.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would release 1 gated handoff(s)" in out
    assert "state/handoffs/x.md" in out
    assert "1 name a blocker that resolves to no handoff" in out


def test_dry_run_with_no_close_skips_the_release_plan(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    _stub(mod, monkeypatch, tmp_path)
    import coordinator_core.housekeeping.cycle as cycle
    import coordinator_core.ops.fleet.archive_terminal_handoffs as ath

    monkeypatch.setattr(ath, "plan_sweep", lambda *_a, **_k: ([], []))
    monkeypatch.setattr(cycle, "plan_gate_releases", lambda *_a: pytest.fail("planned"))

    assert mod.main(["--dry-run", "--no-close"]) == 0
    assert "would release" not in capsys.readouterr().out
