"""Tests for the ledger-unit EM turn counter."""

import json
import time

from coordinator_core.session import em_turn_count as etc


def _line(mid, content=None, sidechain=False, model="claude-x", crlf=False):
    rec = {
        "isSidechain": sidechain,
        "message": {
            "model": model,
            "id": mid,
            "role": "assistant",
            "content": content or [{"type": "text", "text": "hi"}],
        },
        "type": "assistant",
    }
    return json.dumps(rec, separators=(",", ":")).encode() + (b"\r\n" if crlf else b"\n")


def _fire(name):
    return [{"type": "tool_use", "name": "Workflow", "input": {"name": name}}]


def _write(p, *chunks):
    p.write_bytes(b"".join(chunks))
    return p


def test_split_message_counts_once(tmp_path):
    t = _write(tmp_path / "t.jsonl", _line("a"), _line("a"), _line("b"))
    assert etc.advance(t, etc.fresh_state())["turns"] == 2


def test_sidechain_and_synthetic_skipped(tmp_path):
    t = _write(
        tmp_path / "t.jsonl",
        _line("a", sidechain=True),
        _line("b", model="<synthetic>"),
        _line("c"),
        b'{"type":"user","message":{"role":"user"}}\n',
    )
    assert etc.advance(t, etc.fresh_state())["turns"] == 1


def test_crlf_safe(tmp_path):
    t = _write(tmp_path / "t.jsonl", _line("a", crlf=True), _line("b", crlf=True))
    assert etc.advance(t, etc.fresh_state())["turns"] == 2


def test_offset_resume_with_partial_trailing_line(tmp_path):
    t = tmp_path / "t.jsonl"
    full = _line("b")
    t.write_bytes(_line("a") + full[:20])
    s1 = etc.advance(t, etc.fresh_state())
    assert s1["turns"] == 1 and s1["offset"] == len(_line("a"))
    t.write_bytes(_line("a") + full + _line("c"))
    s2 = etc.advance(t, s1)
    assert s2["turns"] == 3
    assert etc.advance(t, s2)["turns"] == 3


def test_reads_only_bytes_after_offset(tmp_path):
    t = _write(tmp_path / "t.jsonl", _line("a"), _line("b"))
    s = etc.advance(t, etc.fresh_state(offset=len(_line("a"))))
    assert s["turns"] == 1


def test_fire_turn_only_for_engine_ask(tmp_path):
    t = _write(
        tmp_path / "t.jsonl",
        _line("a", _fire("other-flow")),
        _line("b"),
        _line("c", _fire("coordinator:engine-ask")),
        _line("d", _fire("engine-ask")),
    )
    s = etc.advance(t, etc.fresh_state())
    assert s["fire_turn"] == 3 and s["turns"] == 4


def test_non_workflow_tool_named_engine_ask_does_not_fire(tmp_path):
    c = [{"type": "tool_use", "name": "Bash", "input": {"name": "engine-ask"}}]
    t = _write(tmp_path / "t.jsonl", _line("a", c))
    assert etc.advance(t, etc.fresh_state())["fire_turn"] is None


def test_over_cap_jumps_to_tail_and_marks_partial(tmp_path):
    big = b"x" * (etc.READ_CAP + 10) + b"\n"
    t = _write(tmp_path / "t.jsonl", _line("a"), big)
    s = etc.advance(t, etc.fresh_state())
    assert s["partial"] is True and s["offset"] == t.stat().st_size and s["turns"] == 0
    with open(t, "ab") as fh:
        fh.write(_line("z"))
    assert etc.advance(t, s)["turns"] == 1


def test_malformed_state_resets_at_tail(tmp_path):
    t = _write(tmp_path / "t.jsonl", _line("a"), _line("b"))
    sp = tmp_path / "em-turns.json"
    sp.write_text("{not json", encoding="utf-8")
    s = etc.load_state(sp, t)
    assert s["offset"] == t.stat().st_size and s["turns"] == 0
    sp.write_text(json.dumps({"offset": "x"}), encoding="utf-8")
    assert etc.load_state(sp, t)["offset"] == t.stat().st_size


def test_absent_state_starts_at_zero_and_round_trips(tmp_path):
    t = _write(tmp_path / "t.jsonl", _line("a"))
    sp = tmp_path / "sub" / "em-turns.json"
    s = etc.load_state(sp, t)
    assert s["offset"] == 0
    s = etc.advance(t, s)
    etc.save_state(sp, s)
    assert etc.load_state(sp, t) == s


def test_save_state_returns_within_deadline_under_stalled_write(tmp_path, monkeypatch):
    import threading
    from pathlib import Path

    release = threading.Event()
    real = Path.write_text

    def _stall(self, *a, **k):
        if self.name.startswith("stalled-em-turns"):
            release.wait(10)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", _stall)
    sp = tmp_path / "stalled-em-turns.json"
    try:
        t0 = time.perf_counter()
        etc.save_state(sp, etc.fresh_state())
        etc.save_state(sp, etc.fresh_state())
        assert time.perf_counter() - t0 < 0.5
        assert not sp.exists()
    finally:
        release.set()
    ok = tmp_path / "ok-em-turns.json"
    etc.save_state(ok, etc.fresh_state())
    assert ok.exists()


def test_bars_are_the_ledger_values():
    assert etc.PRE_FIRE_TURNS_MAX == 10
    assert etc.POST_LANDING_TURNS_MAX == 5


def test_one_mib_delta_under_20ms_process_time(tmp_path):
    parts, i, total = [], 0, 0
    while total < etc.READ_CAP - 4096:
        parts.append(_line(f"m{i}"))
        total += len(parts[-1])
        i += 1
    t = _write(tmp_path / "t.jsonl", *parts)
    t0 = time.process_time()
    s = etc.advance(t, etc.fresh_state())
    dt = time.process_time() - t0
    assert s["turns"] == i and not s.get("partial")
    assert dt < 0.020, dt
