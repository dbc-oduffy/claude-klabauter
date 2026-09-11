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


class TestHealInboxOnTheHousekeepingDoor:
    """C6: the door runs `memo.heal_inbox` before the handoff cycle.

    Lives here rather than beside the warm-serve test because that module's
    negative-spec forbids importing `handoff-housekeeping.py` at all — and
    these two assertions are precisely about what `main()` does when it runs.
    """

    def test_main_reports_a_restored_memo(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        self._stub_cycle(mod, monkeypatch, tmp_path)
        self._stub_heal(
            monkeypatch,
            result={
                "exit_code": 0,
                "acted": [{"action": "restored", "id": "2026-09-11-a-memo.md"}],
            },
        )

        assert mod.main(["--cap", "5"]) == 0
        assert "restored from anchor: 2026-09-11-a-memo.md" in capsys.readouterr().out, (
            "a restored memo is the one heal outcome the operator must see on the "
            "door's own stdout — the anchor restored it, nothing else will say so"
        )

    def test_a_heal_failure_still_runs_the_cycle(self, tmp_path, monkeypatch):
        mod = _load_module()
        ran = self._stub_cycle(mod, monkeypatch, tmp_path)

        def _raise(*_a, **_k):
            raise RuntimeError("heal exploded")

        self._stub_heal(monkeypatch, raises=_raise)

        exit_code = mod.main(["--cap", "5"])
        assert ran, (
            "a heal failure must never stop or skip the handoff cycle — the cycle "
            "is this door's first job and does not depend on the heal"
        )
        assert exit_code == 1, (
            "a heal failure is reported, not swallowed: it flips the exit code where "
            "the cycle's own outcome would otherwise have been zero"
        )

    def test_heal_inbox_partial_failure_flips_exit_code_and_reports_failed_count(
        self, tmp_path, monkeypatch, capsys
    ):
        mod = _load_module()
        ran = self._stub_cycle(mod, monkeypatch, tmp_path)
        self._stub_heal(
            monkeypatch,
            result={
                "exit_code": 2,
                "acted": [],
                "failed": [{"id": "2026-09-11-b-memo.md", "reason": "restore commit declined"}],
            },
        )

        exit_code = mod.main(["--cap", "5"])
        assert ran, "a DETERMINATE-PARTIAL heal must never stop or skip the handoff cycle"
        assert exit_code == 1, (
            "a non-zero, non-None heal exit_code is a heal failure and flips this "
            "door's own exit code the same way a raise does"
        )
        assert "1 memo(s) failed to heal" in capsys.readouterr().err, (
            "a per-item heal failure count is reported on stderr — the only place "
            "an operator sees a partially-failed heal on this door"
        )

    def test_dry_run_reports_candidates_in_candidate_voice(self, tmp_path, monkeypatch, capsys):
        mod = _load_module()
        monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_path", lambda: str(tmp_path))
        import coordinator_core.lifecycle as lifecycle
        import coordinator_core.ops.fleet._common as fleet_common
        import coordinator_core.ops.fleet.archive_terminal_handoffs as ath

        monkeypatch.setattr(lifecycle, "git_common_dir", lambda _p: tmp_path / ".git")
        monkeypatch.setattr(fleet_common, "main_worktree_root", lambda _c: tmp_path)
        monkeypatch.setattr(ath, "plan_sweep", lambda *_a, **_k: ([], []))
        self._stub_heal(
            monkeypatch,
            result={
                "dry_run": True,
                "candidates": [
                    {"action": "restore", "id": "2026-09-11-c-memo.md"},
                    {"action": "adopt", "id": "2026-09-11-d-memo.md"},
                    {"action": "retire", "id": "2026-09-11-e-anchor.md"},
                ],
            },
        )

        assert mod.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "would restore from anchor: 2026-09-11-c-memo.md" in out, (
            "a dry run reports a restore CANDIDATE — never the past-tense "
            "'restored from anchor' line, since nothing wrote or committed anything"
        )
        assert "restored from anchor:" not in out
        assert "heal: would adopt 1 memo(s)" in out
        assert "heal: would retire 1 anchor(s)" in out

    @staticmethod
    def _stub_heal(monkeypatch, result=None, raises=None):
        import coordinator_core.ops.fleet.memo_heal as memo_heal

        monkeypatch.setattr(
            memo_heal, "_memo_heal_inbox", raises or (lambda *_a, **_k: result)
        )

    @staticmethod
    def _stub_cycle(mod, monkeypatch, tmp_path):
        """Stubs everything the cycle half of `main()` needs, and returns a
        one-element list that becomes truthy once the cycle actually ran."""
        ran: list[bool] = []
        monkeypatch.setattr(mod, "_ensure_claude_klabauter_on_path", lambda: str(tmp_path))
        monkeypatch.setattr(mod, "_stamp_archive_sweeps_liveness", lambda _root: None)
        import coordinator_core.housekeeping.cycle as cycle
        import coordinator_core.lifecycle as lifecycle
        import coordinator_core.ops.fleet._common as fleet_common

        monkeypatch.setattr(lifecycle, "git_common_dir", lambda _p: tmp_path / ".git")
        monkeypatch.setattr(fleet_common, "main_worktree_root", lambda _c: tmp_path)

        def _handler(_payload, _common):
            ran.append(True)
            return {"exit_code": 0, "archived": [], "closed": 0}

        monkeypatch.setattr(cycle, "_handler", _handler)
        return ran
