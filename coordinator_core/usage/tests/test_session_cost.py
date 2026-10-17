"""session_cost: incremental offset + running-sum cache, parity with agent_usage._messages."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coordinator_core.usage import agent_usage, session_cost
from coordinator_core.usage.price_table import estimate_usd

OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"


def row(mid, rid, out=1000, model=OPUS, inp=100):
    msg = {"model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                     "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}}
    if mid is not None:
        msg["id"] = mid
    ev = {"type": "assistant", "message": msg}
    if rid is not None:
        ev["requestId"] = rid
    return json.dumps(ev) + "\n"


def usd_of(model=OPUS, out=1000, inp=100):
    u = {"input_tokens": inp, "output_tokens": out,
         "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}
    return estimate_usd(model, u)


def write(path: Path, *rows: str, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="") as h:
        h.write("".join(rows))


def append(path: Path, *rows: str):
    write(path, *rows, mode="a")


@pytest.fixture
def sess(tmp_path):
    main = tmp_path / "proj" / "abc.jsonl"
    return main, tmp_path / "cache" / "abc.json"


def adv(sess, now=1000.0, budget=10**9):
    main, cache = sess
    return session_cost.advance(str(main), cache, now=now, byte_budget=budget)


def cache_of(sess):
    return json.loads(sess[1].read_text(encoding="utf-8"))


def test_parity_with_messages_and_all_three_file_kinds(sess):
    main, _ = sess
    sub = main.with_suffix("") / "subagents"
    write(main, row("m1", "r1"), row("m1", "r1", out=5000), row("m2", "r2", model=HAIKU), "not json\n")
    write(sub / "agent-a.jsonl", row("s1", "q1"), row(None, None), row(None, None))
    write(sub / "workflows" / "wf_x" / "agent-b.jsonl", row("w1", "z1"))
    write(sub / "workflows" / "wf_x" / "journal.jsonl", row("j1", "y1"))
    files = [main, sub / "agent-a.jsonl", sub / "workflows" / "wf_x" / "agent-b.jsonl"]
    expected = sum(estimate_usd(m["model"], m["usage"]) for f in files
                   for m in agent_usage._messages(str(f)))
    got = adv(sess)
    assert got["usd"] == pytest.approx(expected)
    assert got["complete"] is True
    assert got["files"] == 3
    assert session_cost.render(got) == f"~${expected:.2f}"


def test_incremental_reads_only_appended_range(sess):
    main, _ = sess
    write(main, row("m1", "r1"))
    adv(sess, now=1000.0)
    off1 = cache_of(sess)["files"]["abc.jsonl"]["offset"]
    assert off1 == main.stat().st_size
    append(main, row("m2", "r2"))
    got = adv(sess, now=1100.0)
    c = cache_of(sess)["files"]["abc.jsonl"]
    assert c["offset"] == main.stat().st_size > off1
    assert got["usd"] == pytest.approx(2 * usd_of())


def test_duplicate_after_append_boundary_replaces(sess):
    main, _ = sess
    write(main, row("m1", "r1", out=1000))
    adv(sess, now=1000.0)
    append(main, row("m1", "r1", out=3000))
    got = adv(sess, now=1100.0)
    assert got["usd"] == pytest.approx(usd_of(out=3000))


def test_torn_trailing_line_counts_once_when_completed(sess):
    main, _ = sess
    full = row("m2", "r2")
    write(main, row("m1", "r1"), full[:20])
    got = adv(sess, now=1000.0)
    assert got["usd"] == pytest.approx(usd_of())
    assert cache_of(sess)["files"]["abc.jsonl"]["offset"] == len(row("m1", "r1").encode())
    append(main, full[20:])
    got = adv(sess, now=1100.0)
    assert got["usd"] == pytest.approx(2 * usd_of())
    assert adv(sess, now=1200.0)["usd"] == pytest.approx(2 * usd_of())


def test_truncation_resets_and_reparses(sess):
    main, _ = sess
    write(main, row("m1", "r1"), row("m2", "r2"))
    adv(sess, now=1000.0)
    write(main, row("m3", "r3", out=2000))
    got = adv(sess, now=1100.0)
    assert got["usd"] == pytest.approx(usd_of(out=2000))


def test_byte_budget_backlog_converges(sess):
    main, _ = sess
    rows = [row(f"m{i}", f"r{i}") for i in range(6)]
    write(main, *rows)
    one = len(rows[0].encode())
    first = adv(sess, now=1000.0, budget=one * 2)
    assert first["complete"] is False
    assert session_cost.render(first).endswith("+")
    got = first
    for _ in range(10):
        got = adv(sess, now=1000.0, budget=one * 2)
        if got["complete"]:
            break
    assert got["complete"] is True
    assert got["usd"] == pytest.approx(6 * usd_of())
    assert not session_cost.render(got).endswith("+")


def test_throttle_unchanged_transcript_touches_no_directory_or_transcript(sess, monkeypatch):
    main, cache = sess
    write(main, row("m1", "r1"))
    adv(sess, now=1000.0)
    real_open = open

    def guarded_open(path, *a, **k):
        if Path(path) != cache:
            raise AssertionError(f"opened {path}")
        return real_open(path, *a, **k)

    def no_scandir(*a, **k):
        raise AssertionError("scandir")

    monkeypatch.setattr(os, "scandir", no_scandir)
    monkeypatch.setattr("builtins.open", guarded_open)
    got = adv(sess, now=1000.0 + session_cost._RESCAN_AFTER_SEC - 1)
    monkeypatch.undo()
    assert got["usd"] == pytest.approx(usd_of())
    assert cache_of(sess)["main_size"] == main.stat().st_size


def test_appended_turn_folds_inside_rescan_window_without_listing(sess, monkeypatch):
    main, _ = sess
    write(main, row("m1", "r1"))
    adv(sess, now=1000.0)
    append(main, row("m2", "r2"))

    def no_scandir(*a, **k):
        raise AssertionError("scandir")

    monkeypatch.setattr(os, "scandir", no_scandir)
    got = adv(sess, now=1000.0 + session_cost._RESCAN_AFTER_SEC - 1)
    monkeypatch.undo()
    assert got["usd"] == pytest.approx(2 * usd_of())
    assert got["complete"] is True
    assert cache_of(sess)["main_size"] == main.stat().st_size


def test_idless_rows_across_two_appends_counted_once_each(sess):
    main, _ = sess
    write(main, row(None, None))
    adv(sess, now=1000.0)
    append(main, row(None, None))
    got = adv(sess, now=1100.0)
    assert got["usd"] == pytest.approx(2 * usd_of())
    assert adv(sess, now=1200.0)["usd"] == pytest.approx(2 * usd_of())


def test_models_priced_at_own_rates_and_unknown_unpriced(sess):
    main, _ = sess
    write(main, row("a", "a", model=OPUS), row("b", "b", model=HAIKU),
          row("c", "c", model="mystery-1"))
    got = adv(sess)
    assert got["usd"] == pytest.approx(usd_of(OPUS) + usd_of(HAIKU))
    assert usd_of(OPUS) != usd_of(HAIKU)
    assert got["unpriced_tokens"] == 100 + 1000 + 50 + 10


def test_render_none_cases(tmp_path):
    assert session_cost.render(None) is None
    assert session_cost.render(session_cost.advance("", tmp_path / "c.json", now=1.0, byte_budget=10)) is None
    missing = session_cost.advance(str(tmp_path / "no" / "x.jsonl"), tmp_path / "c.json",
                                   now=1.0, byte_budget=10)
    assert session_cost.render(missing) is None
    assert session_cost.render({"usd": 1.234, "complete": True, "files": 1}) == "~$1.23"
    assert session_cost.render({"usd": 1.234, "complete": False, "files": 1}) == "~$1.23+"


def test_usage_line_keys():
    assert session_cost.usage_line("no usage here", 0) is None
    key, model, usage, speed = session_cost.usage_line(row("m", "r"), 7)
    assert key == ("m", "r") and model == OPUS and speed is None
    assert session_cost.usage_line(row(None, None), 7)[0] == ("line", 7)
