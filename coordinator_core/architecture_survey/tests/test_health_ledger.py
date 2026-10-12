"""health_ledger.stamp_last_full_audit: only a full pass over a current index writes, one line only."""
from __future__ import annotations

from datetime import date

import pytest

from coordinator_core.architecture_survey import health_ledger

TODAY = date(2026, 10, 12)
LEDGER = "# Ledger\r\n\r\n**Last full audit:** 2026-08-06\r\n**Last targeted audit:** 2026-09-01\r\nfoo\r\n"


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("CAAS_TEST_STATE_ROOT", str(tmp_path))
    p = tmp_path / "health-ledger.md"
    p.write_bytes(LEDGER.encode())
    return p


@pytest.mark.parametrize("mode,state", [("targeted", "current"), ("refresh", "behind")])
def test_no_write(ledger, mode, state):
    before = ledger.read_bytes()
    d = health_ledger.stamp_last_full_audit(ledger.parent, mode=mode, index_state=state, today=TODAY)
    assert d.chosen == ("not-a-full-pass" if mode == "targeted" else "index-behind")
    assert ledger.read_bytes() == before


def test_full_pass_changes_one_line_and_keeps_crlf(ledger):
    d = health_ledger.stamp_last_full_audit(ledger.parent, mode="refresh", index_state="current", today=TODAY)
    assert d.chosen == "stamped"
    assert "2026-08-06" in d.evidence and "2026-10-12" in d.evidence
    assert ledger.read_bytes() == LEDGER.replace("2026-08-06", "2026-10-12").encode()


def test_missing_line_is_named_error(ledger):
    ledger.write_text("# nothing\n", encoding="utf-8")
    with pytest.raises(health_ledger.LedgerLineMissing):
        health_ledger.stamp_last_full_audit(ledger.parent, mode="bootstrap", index_state="current", today=TODAY)
    assert ledger.read_text(encoding="utf-8") == "# nothing\n"


def test_default_path_is_repo_state_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CAAS_TEST_STATE_ROOT", raising=False)
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "elsewhere"))
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "health-ledger.md").write_text("**Last full audit:** 2026-01-01\n", encoding="utf-8")
    health_ledger.stamp_last_full_audit(tmp_path, mode="bootstrap", index_state="current", today=TODAY)
    assert (tmp_path / "state" / "health-ledger.md").read_text(encoding="utf-8") == "**Last full audit:** 2026-10-12\n"


def test_dateless_line_with_text_keeps_separator(ledger):
    ledger.write_text("**Last full audit:** never\n", encoding="utf-8")
    health_ledger.stamp_last_full_audit(ledger.parent, mode="refresh", index_state="current", today=TODAY)
    assert ledger.read_text(encoding="utf-8") == "**Last full audit:** 2026-10-12 never\n"
