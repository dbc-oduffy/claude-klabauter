"""
bin.tests.test_sweep_actioned_memos_blocked — failure-path tests for the wedged-memo
signal in bin/sweep-actioned-memos.py.

These tests exercise ONLY the failure path, which is the point: the happy path (memo gets
archived) was already covered, and the defect this file guards against was that a memo
which can NEVER be archived — because a DIFFERENT file already occupies its archive
destination — was reported under the benign "already-archived" reason and dropped from the
dry-run preview entirely. The operator then read "nothing to archive (inbox already
converged)" and exit 0 while the inbox was structurally wedged.

Guarded contract:
  1. The archive-dest conflict gets its own reason (``_REASON_DEST_CONFLICT``), NOT the
     AC12-pinned "already-archived" string that means the benign source-gone replay case.
  2. The dry-run preview surfaces blocked memos on stderr and exits non-zero.
  3. The act path surfaces the wedge on stderr and exits non-zero.

Spec backlink: state/audits/2026-07-22-silent-success-audit.md
Negative-spec: does NOT run git or archive anything — the CLI's sweep() is stubbed, so
these tests cover the reporting contract, not the archival mechanics.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.fleet.archive_actioned_memos import _REASON_DEST_CONFLICT

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_SCRIPT = _REPO_ROOT / "bin" / "sweep-actioned-memos.py"


@pytest.fixture(scope="module")
def cli():
    """Load bin/sweep-actioned-memos.py as a module (its name is not import-safe)."""
    spec = importlib.util.spec_from_file_location("_sweep_actioned_memos_cli", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dest_conflict_reason_is_not_already_archived() -> None:
    """The wedge reason must be distinguishable from the benign idempotent-replay skip.

    AC12 pins "already-archived" to the source-gone case. Reusing it for a real conflict is
    exactly how a stuck memo reported as converged.
    """
    assert _REASON_DEST_CONFLICT != "already-archived"


def test_dry_run_blocked_memo_warns_and_exits_nonzero(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "sweep",
        lambda root, dry_run: {
            "dry_run": True,
            "candidates": [],
            "blocked": ["cross-repo/inbox/wedged.md: archive destination ... DIFFERENT file"],
        },
    )
    rc = cli.main(["--dry-run"])
    captured = capsys.readouterr()

    assert rc == 1, "a blocked memo must not report exit 0"
    assert "BLOCKED" in captured.err
    assert "wedged.md" in captured.err, "the blocked memo must be named"
    assert "inbox already converged" not in captured.out


def test_dry_run_clean_inbox_still_reports_converged(cli, monkeypatch, capsys) -> None:
    """Guard against over-correction: a genuinely empty inbox is still a green no-op."""
    monkeypatch.setattr(
        cli,
        "sweep",
        lambda root, dry_run: {"dry_run": True, "candidates": [], "blocked": []},
    )
    rc = cli.main(["--dry-run"])
    captured = capsys.readouterr()

    assert rc == 0
    assert "inbox already converged" in captured.out
    assert "BLOCKED" not in captured.err


def test_act_path_wedged_memo_warns_and_exits_nonzero(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "sweep",
        lambda root, dry_run: {
            "dry_run": False,
            "archived": [],
            "skipped": [{"id": "cross-repo/inbox/wedged.md", "reason": _REASON_DEST_CONFLICT}],
            "failed": [],
        },
    )
    rc = cli.main([])
    captured = capsys.readouterr()

    assert rc == 1, "a wedged memo must not report exit 0"
    assert "BLOCKED" in captured.err
    assert "wedged.md" in captured.err


def test_act_path_benign_skip_stays_green(cli, monkeypatch, capsys) -> None:
    """A not-yet-terminal memo is a legitimate skip — it must NOT trip the wedge signal."""
    monkeypatch.setattr(
        cli,
        "sweep",
        lambda root, dry_run: {
            "dry_run": False,
            "archived": [],
            "skipped": [{"id": "cross-repo/inbox/live.md", "reason": "not-terminal"}],
            "failed": [],
        },
    )
    rc = cli.main([])
    captured = capsys.readouterr()

    assert rc == 0
    assert "BLOCKED" not in captured.err


def test_json_mode_wedged_memo_exits_nonzero(cli, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        cli,
        "sweep",
        lambda root, dry_run: {
            "dry_run": False,
            "archived": [],
            "skipped": [{"id": "cross-repo/inbox/wedged.md", "reason": _REASON_DEST_CONFLICT}],
            "failed": [],
        },
    )
    assert cli.main(["--json"]) == 1
    assert _REASON_DEST_CONFLICT in capsys.readouterr().out
