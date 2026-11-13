from __future__ import annotations

import pytest

from coordinator_core.ops import backlog_pass_ledger as led
from coordinator_core.session import record_homes


def test_empty_summary(tmp_path):
    s = led.summary(str(tmp_path), "distill")
    assert s["pass_id"] is None and s["runs"] == 0 and s["open"] is False
    assert s["whole_backlog_bar"] == 1_000_000


def test_pass_accumulates_until_deferred_zero_then_a_new_pass_starts(tmp_path):
    root = str(tmp_path)
    led.append(root, "distill", "r1", 200_000, 30, 20)
    led.append(root, "distill", "r2", 150_000, 20, 5)
    s = led.summary(root, "distill")
    assert (s["pass_id"], s["runs"], s["cumulative_tokens"], s["open"]) == (1, 2, 350_000, True)
    led.append(root, "distill", "r3", 50_000, 5, 0)
    s = led.summary(root, "distill")
    assert (s["pass_id"], s["runs"], s["cumulative_tokens"], s["open"]) == (1, 3, 400_000, False)
    led.append(root, "distill", "r4", 10, 1, 3)
    s = led.summary(root, "distill")
    assert (s["pass_id"], s["runs"], s["cumulative_tokens"], s["open"]) == (2, 1, 10, True)


def test_ceremonies_are_separate_ledgers(tmp_path):
    root = str(tmp_path)
    led.append(root, "distill", "r1", 1, 1, 1)
    assert led.summary(root, "learn-lessons")["runs"] == 0


def test_repeated_run_id_in_open_pass_is_not_double_counted(tmp_path):
    root = str(tmp_path)
    led.append(root, "distill", "r1", 100, 1, 1)
    led.append(root, "distill", "r1", 100, 1, 1)
    assert led.summary(root, "distill")["cumulative_tokens"] == 100


def test_writes_under_the_backlog_pass_home(tmp_path):
    led.append(str(tmp_path), "distill", "r1", 1, 1, 1)
    assert (tmp_path / "state" / "backlog-pass" / "distill.jsonl").is_file()


def test_undeclared_home_key_is_a_typed_error_naming_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(record_homes, "HOMES", record_homes.HOMES - {"backlog-pass"})
    with pytest.raises(led.LedgerHomeMissing, match="backlog-pass"):
        led.append(str(tmp_path), "distill", "r1", 1, 1, 1)
    assert not (tmp_path / "state").exists()


def test_corrupt_line_is_skipped(tmp_path):
    root = str(tmp_path)
    led.append(root, "distill", "r1", 7, 1, 1)
    p = tmp_path / "state" / "backlog-pass" / "distill.jsonl"
    p.write_text(p.read_text() + "garbage\n", encoding="utf-8")
    assert led.summary(root, "distill")["cumulative_tokens"] == 7
