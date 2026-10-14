from __future__ import annotations

import json
from pathlib import Path

from coordinator_core.ops import baton_pm_turns as PT
from coordinator_core.session_baton import store


def _session(tmp_path: Path, monkeypatch, sid: str) -> Path:
    sdir = tmp_path / "coordinator-sessions" / sid
    sdir.mkdir(parents=True)
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "coordinator-sessions" / s)
    return sdir


def test_turns_append_in_order_verbatim_and_uncapped(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-pm")
    launch = "  build the thing\n\n" + "x" * 20000 + "  "
    assert PT.append_turn(launch, session_id="sid-pm")["turn"] == 0
    assert PT.append_turn("yes", session_id="sid-pm")["turn"] == 1
    assert PT.append_turn("yes", session_id="sid-pm")["turn"] == 2

    out = PT.read_turns(session_id="sid-pm")
    assert out["ok"] is True and out["count"] == 3
    assert out["text"] == launch
    assert not (sdir / store.BATON_FILENAME).exists()


def test_selectors(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-sel")
    for p in ("zero", "one", "two", "three"):
        PT.append_turn(p, session_id="sid-sel")

    assert PT.read_turns(session_id="sid-sel", turn=2)["text"] == "two"
    assert PT.read_turns(session_id="sid-sel", turn=-1)["text"] == "three"
    rng = PT.read_turns(session_id="sid-sel", start=1, end=2)
    assert [t["prompt"] for t in rng["turns"]] == ["one", "two"]
    assert rng["text"].startswith("## PM turn 1 (")
    assert len(PT.read_turns(session_id="sid-sel", all_turns=True)["turns"]) == 4
    assert PT.read_turns(session_id="sid-sel", start=3)["text"] == "three"
    miss = PT.read_turns(session_id="sid-sel", turn=9)
    assert miss["ok"] is False and miss["count"] == 4


def test_torn_line_keeps_its_index_and_is_skipped(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-torn")
    PT.append_turn("zero", session_id="sid-torn")
    with open(sdir / PT.PM_TURNS_FILENAME, "ab") as fh:
        fh.write(b'{"turn": 1, "ts": "x", "prom')
    assert PT.append_turn("two", session_id="sid-torn")["turn"] == 2

    out = PT.read_turns(session_id="sid-torn", all_turns=True)
    assert [t["turn"] for t in out["turns"]] == [0, 2]
    assert PT.read_turns(session_id="sid-torn", turn=2)["text"] == "two"


def test_entries_are_never_rewritten(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-ao")
    PT.append_turn("first", session_id="sid-ao")
    before = (sdir / PT.PM_TURNS_FILENAME).read_bytes()
    PT.append_turn("second", session_id="sid-ao")
    after = (sdir / PT.PM_TURNS_FILENAME).read_bytes()
    assert after.startswith(before)
    assert json.loads(after.splitlines()[1])["prompt"] == "second"


def test_missing_session_dir_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "baton_dir", lambda s, cwd=None: tmp_path / "absent" / s)
    out = PT.append_turn("hello", session_id="sid-none")
    assert out["ok"] is False and "no session directory" in out["reason"]
    assert not (tmp_path / "absent").exists()


def test_rejects_empty_prompt_and_bad_selector(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bad")
    assert PT.append_turn("   ", session_id="sid-bad")["ok"] is False
    out = PT._pm_turns({"session_id": "sid-bad", "turn": "2"})
    assert out["ok"] is False and "integer" in out["reason"]


def test_recent_window_after_turn_bounds_newest_first_with_no_total_budget(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-win")
    for n in range(6):
        PT.append_turn(f"{n}" * 5000, session_id="sid-win")
    win = PT.recent_window(session_id="sid-win", after_turn=2)
    assert [e["turn"] for e in win] == [5, 4, 3]
    assert "truncated" not in win[0]
    assert [e["turn"] for e in PT.recent_window(session_id="sid-win")] == [5, 4, 3, 2, 1, 0]


def test_recent_window_cuts_an_oversized_single_turn(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-big")
    PT.append_turn("small", session_id="sid-big")
    PT.append_turn("z" * (PT.SIZING_WINDOW_CHARS + 100), session_id="sid-big")
    win = PT.recent_window(session_id="sid-big")
    assert [e["turn"] for e in win] == [1, 0]
    assert win[0]["text"] == "z" * PT.SIZING_WINDOW_CHARS and win[0]["truncated"] is True
    assert "truncated" not in win[1]


def test_recent_window_defaults_to_the_session_mark_and_mark_never_lowers(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-mark")
    for n in range(4):
        PT.append_turn(f"t{n}", session_id="sid-mark")
    assert PT.mark_sized_through(2, session_id="sid-mark") is True
    assert PT.mark_sized_through(1, session_id="sid-mark") is True
    assert store.read_baton("sid-mark")["sized_through_turn"] == 2
    assert [e["turn"] for e in PT.recent_window(session_id="sid-mark")] == [3]


def test_bound_window_embeds_the_newest_quoted_turn_only(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bw")
    quoted = "please make the sizing embed one turn only"
    PT.append_turn(quoted, session_id="sid-bw")
    PT.append_turn("later unrelated chatter", session_id="sid-bw")
    PT.append_turn("newest unrelated chatter", session_id="sid-bw")
    win = PT.bound_window(f'the PM said "{quoted}"', session_id="sid-bw")
    assert [e["turn"] for e in win] == [0] and win[0]["text"] == quoted


def test_bound_window_falls_back_to_the_newest_turn_past_the_mark(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bf")
    for n in range(5):
        PT.append_turn(f"turn number {n}", session_id="sid-bf")
    assert [e["turn"] for e in PT.bound_window("a paraphrase", session_id="sid-bf")] == [4]
    assert [e["turn"] for e in PT.bound_window(None, session_id="sid-bf")] == [4]
    PT.mark_sized_through(4, session_id="sid-bf")
    assert PT.bound_window("a paraphrase", session_id="sid-bf") == []


def test_bound_window_head_cuts_an_oversized_turn(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bc")
    PT.append_turn("z" * (PT.SIZING_WINDOW_CHARS + 50), session_id="sid-bc")
    win = PT.bound_window(None, session_id="sid-bc")
    assert win[0]["text"] == "z" * PT.SIZING_WINDOW_CHARS and win[0]["truncated"] is True


def test_unicode_line_separators_stay_inside_one_turn(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-u")
    prompt = "a b c\u0085d"
    PT.append_turn(prompt, session_id="sid-u")
    assert PT.read_turns(session_id="sid-u")["text"] == prompt


def test_torn_multibyte_tail_is_skipped_not_raised(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-mb")
    PT.append_turn("zero", session_id="sid-mb")
    with open(sdir / PT.PM_TURNS_FILENAME, "ab") as fh:
        fh.write(b'{"turn": 1, "ts": "x", "prompt": "caf\xc3')
    assert [e["turn"] for e in PT.recent_window(session_id="sid-mb")] == [0]
    assert PT.append_turn("two", session_id="sid-mb")["turn"] == 2


def test_referent_round_trips(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-rf")
    ref = {"excerpt": " which one?\n", "transcript_uuid": "u-1"}
    out = PT.append_turn("the second", session_id="sid-rf", referent=ref)
    assert out["ok"] is True and out["referent_recorded"] is True
    got = PT.read_turns(session_id="sid-rf", turn=out["turn"])["turns"][0]
    assert got["referent"] == ref


def test_referent_excerpt_keeps_tail_and_flags_truncation(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-rt")
    excerpt = "a" * 500 + "b" * 2000
    out = PT.append_turn("p", session_id="sid-rt", referent={"excerpt": excerpt})
    got = PT.read_turns(session_id="sid-rt", turn=out["turn"])["turns"][0]["referent"]
    assert got["excerpt"] == "b" * 2000 and got["excerpt_truncated"] is True


def test_malformed_referent_still_appends_prompt(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-bad")
    bad = ["str", {"other": "x"}, {"excerpt": ""}, {"excerpt": 3}, {},
           {"excerpt": "ok", "extra": "x"}]
    for n, ref in enumerate(bad):
        out = PT.append_turn(f"p{n}", session_id="sid-bad", referent=ref)
        assert out["ok"] is True and out["turn"] == n
        assert out["referent_recorded"] is False and out["reason"]
    turns = PT.read_turns(session_id="sid-bad", all_turns=True)["turns"]
    assert len(turns) == len(bad)
    assert all("referent" not in t for t in turns)


def test_absent_referent_shape_is_unchanged(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-ab")
    out = PT.append_turn("hello", session_id="sid-ab", referent=None)
    assert set(out) == {"ok", "turn", "log_path", "session_id", "reason"}
    line = json.loads((sdir / PT.PM_TURNS_FILENAME).read_text().splitlines()[0])
    assert set(line) == {"turn", "ts", "prompt"}


def test_harness_notices_are_refused_without_consuming_an_index(tmp_path, monkeypatch):
    sdir = _session(tmp_path, monkeypatch, "sid-notice")
    for i, seed in enumerate(PT._HARNESS_NOTICE_PREFIXES):
        assert PT.append_turn("real", session_id="sid-notice")["turn"] == i
        refused = PT.append_turn("  " + seed + " body", session_id="sid-notice")
        assert refused == {"ok": False, "turn": None, "log_path": None,
                           "session_id": "sid-notice",
                           "reason": "harness notice; not recorded"}
    lines = (sdir / PT.PM_TURNS_FILENAME).read_text().splitlines()
    assert len(lines) == len(PT._HARNESS_NOTICE_PREFIXES)
    assert PT.append_turn("next", session_id="sid-notice")["turn"] == len(lines)


def test_prompt_containing_a_notice_prefix_mid_text_is_recorded(tmp_path, monkeypatch):
    _session(tmp_path, monkeypatch, "sid-mid")
    text = "see <task-notification> and [Cross-session idle notice] here"
    assert PT.append_turn(text, session_id="sid-mid")["turn"] == 0
    assert PT.read_turns(session_id="sid-mid")["text"] == text
    assert PT.read_turns(session_id="sid-mid")["count"] == 1
