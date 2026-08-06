"""Tests for coordinator_core.ops.check_arch_audit_staleness.

Golden oracle (Port of: check-arch-audit-staleness.sh, example-doctrine-repo b5a4192c, 2026-07-20),
snapshotted 2026-07-16 against a `CAAS_TEST_STATE_ROOT`-stubbed
coordinator_state_root() (a locally patched shim -- the real coordinator lib
resolution is not itself under test here, see test_resolve_state_root_*
below for that slice):

    Last targeted audit: 2026-07-06 (10 days ago, boundary)  -> FRESH  / 0
    Last targeted audit: (none)                               -> STALE  / 0
    Last targeted audit: 2026-07-01 (15 days ago)              -> STALE  / 0
    Last targeted audit: not-a-date                            -> STALE  / 0
    Last targeted audit: 2026-13-45 (shape-valid, invalid cal) -> UNKNOWN / 0
    ledger present, field absent                                -> UNKNOWN / 0
    ledger file absent                                           -> UNKNOWN / 0
    cwd outside any git repo                                     -> UNKNOWN / 0
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from coordinator_core.ops import check_arch_audit_staleness as caas


# ---------------------------------------------------------------------------
# _compute_staleness — pure predicate over a ledger file, parity-critical
# ---------------------------------------------------------------------------


def _write_ledger(tmp_path: Path, body: str) -> Path:
    ledger = tmp_path / "health-ledger.md"
    ledger.write_text(body, encoding="utf-8")
    return ledger


def test_fresh_at_boundary_10_days(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-06\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "FRESH"


def test_stale_over_10_days(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-01\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "STALE"


def test_stale_on_none_placeholder(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** (none)\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "STALE"


def test_stale_on_unparseable_free_text(tmp_path):
    """Negative-spec: shape-mismatched text is STALE, not UNKNOWN — reproduces
    the bash oracle's two-stage regex-then-date-parse split verbatim."""
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** not-a-date\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "STALE"


def test_unknown_on_shape_valid_invalid_calendar_date(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** 2026-13-45\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_missing_field(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last full audit:** 2026-07-12\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "UNKNOWN"


def test_unknown_on_missing_ledger(tmp_path):
    ledger = tmp_path / "health-ledger.md"
    assert not ledger.exists()
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "UNKNOWN"


def test_clamps_future_date_to_zero_distance(tmp_path):
    ledger = _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-20\n")
    assert caas._compute_staleness(ledger, today=date(2026, 7, 16)) == "FRESH"


# ---------------------------------------------------------------------------
# _resolve_state_root — env-override seam and meta-repo/sibling-repo routing
# ---------------------------------------------------------------------------


def test_resolve_state_root_honours_test_override(monkeypatch):
    monkeypatch.setenv("CAAS_TEST_STATE_ROOT", "/some/explicit/state")
    assert caas._resolve_state_root() == "/some/explicit/state"


def test_resolve_state_root_sibling_repo_uses_git_root_state(monkeypatch, tmp_path):
    monkeypatch.delenv("CAAS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(caas, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(caas, "_claude_home", lambda: "/nonexistent/claude/home")
    assert caas._resolve_state_root() == str(tmp_path / "state")


def test_resolve_state_root_meta_repo_routes_to_claude_klabauter(monkeypatch, tmp_path):
    monkeypatch.delenv("CAAS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(caas, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(caas, "_claude_home", lambda: str(tmp_path))
    monkeypatch.setattr(caas, "_claude_klabauter_root", lambda: "/claude-klabauter/root")
    assert caas._resolve_state_root() == str(Path("/claude-klabauter/root") / "state")


def test_resolve_state_root_meta_repo_unresolvable_claude_klabauter_returns_none(
    monkeypatch, tmp_path
):
    monkeypatch.delenv("CAAS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(caas, "_git_root", lambda: str(tmp_path))
    monkeypatch.setattr(caas, "_claude_home", lambda: str(tmp_path))
    monkeypatch.setattr(caas, "_claude_klabauter_root", lambda: None)
    assert caas._resolve_state_root() is None


def test_resolve_state_root_no_git_root_returns_none(monkeypatch):
    monkeypatch.delenv("CAAS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setattr(caas, "_git_root", lambda: None)
    assert caas._resolve_state_root() is None


# ---------------------------------------------------------------------------
# main() — end-to-end, exit code and stdout parity
# ---------------------------------------------------------------------------


def test_main_prints_stale_and_returns_zero(monkeypatch, tmp_path, capsys):
    _write_ledger(tmp_path, "**Last targeted audit:** (none)\n")
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: str(tmp_path))
    rc = caas.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "STALE"


def test_main_prints_unknown_when_state_root_unresolvable(monkeypatch, capsys):
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: None)
    rc = caas.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "UNKNOWN"


def test_main_always_exits_zero_even_on_fresh(monkeypatch, tmp_path, capsys):
    _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-06\n")
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: str(tmp_path))
    rc = caas.main([])
    assert rc == 0
    assert capsys.readouterr().out.strip() in {"FRESH", "STALE"}


# ---------------------------------------------------------------------------
# --root argv path (Finding 2, slicecheck-arch-weekly-staleness-root-thread)
# ---------------------------------------------------------------------------


def test_main_root_flag_bypasses_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-06\n")
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: 1 / 0)
    rc = caas.main(["--root", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "STALE"


def test_main_root_equals_form_bypasses_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-06\n")
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: 1 / 0)
    rc = caas.main([f"--root={tmp_path}"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "STALE"


def test_main_no_root_falls_through_to_resolve_state_root(monkeypatch, tmp_path, capsys):
    _write_ledger(tmp_path, "**Last targeted audit:** 2026-07-06\n")
    calls = []
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: (calls.append(1), str(tmp_path))[1])
    rc = caas.main([])
    assert rc == 0
    assert calls == [1]
    assert capsys.readouterr().out.strip() == "STALE"


def test_main_blank_root_treated_as_absent_not_ambient_cwd(monkeypatch, capsys):
    """Finding 1 (P1): --root "" must route to _resolve_state_root(), not
    fall through to Path("") which resolves against ambient process cwd."""
    monkeypatch.setattr(caas, "_resolve_state_root", lambda: None)
    rc = caas.main(["--root", ""])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "UNKNOWN"
