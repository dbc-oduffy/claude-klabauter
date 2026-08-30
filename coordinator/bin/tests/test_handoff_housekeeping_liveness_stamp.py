"""test_handoff_housekeeping_liveness_stamp.py — the ceremony archival path
stamps the `archive_sweeps` housekeeping-liveness key.

`archive_sweeps` names the archival JOB, and `sweep-terminal-handoffs.py` was
its only writer while `handoff-housekeeping` (the `/workday-complete` spine's
`d_step2_67_handoff_housekeeping` directive, over `housekeeping.cycle`) does
the same work on a real cadence and stamped nothing. A monitor reading the key
therefore reported the manual drain's cadence, never the ceremony path's: a
repo archiving healthily through the spine read as hours stale, and a repo
whose spine never ran read as fresh after one manual invocation. Reported by
doe-claude-em, `cross-repo/inbox/2026-08-30-doe-claude-em-boot-sweep-kill-left-
abandoned-session-unowned.md`.

The dry-run half mirrors the sibling CLI's own census rule: a plan is not a
sweep, so it must not stamp.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "handoff-housekeeping.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "handoff_housekeeping_under_test", str(_SCRIPT)
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class TestArchiveSweepsLivenessStamp:
    def test_mutating_run_stamps_archive_sweeps(self, tmp_path, monkeypatch):
        mod = _load_module()
        stamped: list[str] = []
        monkeypatch.setattr(
            mod, "_stamp_archive_sweeps_liveness", lambda root: stamped.append(root)
        )
        self._run_with_stubs(mod, monkeypatch, tmp_path, argv=["--cap", "5"])
        assert stamped == [str(tmp_path)], (
            "the ceremony archival path must stamp `archive_sweeps` — the key "
            "names the job, not `sweep-terminal-handoffs.py`"
        )

    def test_dry_run_does_not_stamp(self, tmp_path, monkeypatch):
        mod = _load_module()
        stamped: list[str] = []
        monkeypatch.setattr(
            mod, "_stamp_archive_sweeps_liveness", lambda root: stamped.append(root)
        )
        monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_path", lambda: str(tmp_path))
        import coordinator_core.lifecycle as lifecycle
        import coordinator_core.ops.fleet._common as fleet_common
        import coordinator_core.ops.fleet.archive_terminal_handoffs as ath

        monkeypatch.setattr(lifecycle, "git_common_dir", lambda _p: tmp_path / ".git")
        monkeypatch.setattr(fleet_common, "main_worktree_root", lambda _c: tmp_path)
        monkeypatch.setattr(ath, "plan_sweep", lambda *_a, **_k: ([], []))

        assert mod.main(["--dry-run"]) == 0
        assert stamped == [], "a plan is not a sweep — --dry-run must not stamp"

    @staticmethod
    def _run_with_stubs(mod, monkeypatch, tmp_path, argv):
        monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_path", lambda: str(tmp_path))
        import coordinator_core.lifecycle as lifecycle
        import coordinator_core.ops.fleet._common as fleet_common
        import coordinator_core.housekeeping.cycle as cycle

        monkeypatch.setattr(lifecycle, "git_common_dir", lambda _p: tmp_path / ".git")
        monkeypatch.setattr(fleet_common, "main_worktree_root", lambda _c: tmp_path)
        monkeypatch.setattr(
            cycle,
            "_handler",
            lambda _payload, _common: {"exit_code": 0, "archived": [], "closed": 0},
        )
        assert mod.main(argv) == 0
