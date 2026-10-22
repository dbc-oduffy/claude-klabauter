"""compact_summary_constraints: the uncited-constraint warning, both sides."""

from __future__ import annotations

import json

from coordinator_core.hooks import compact_summary_constraints as csc

SUMMARY = (
    "This session is being continued.\n\nSummary:\n## Standing constraints\n"
    "- Never push `work/x`.\n"
    "- Never git add -A (PM turn 12).\n"
    "- Scratch under scratch/, never a drive root (see C:/u/.claude/rules/scratch.md and /rules/).\n"
    "- Fine line with no rule words.\n"
    "Continue the conversation ... do not acknowledge the summary\n"
)


def _transcript(tmp_path, summary=SUMMARY, pad=0):
    p = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "x" * pad}}]
    rows.append({"type": "user", "isCompactSummary": True,
                 "message": {"role": "user", "content": summary}})
    rows.append({"type": "assistant", "message": {"content": "ok"}})
    p.write_text("\n".join(json.dumps(r, separators=(",", ":")) for r in rows) + "\n",
                 encoding="utf-8")
    return str(p)


def test_flags_only_uncited_constraint(tmp_path):
    line = csc.warn_line({"transcript_path": _transcript(tmp_path)})
    assert line and "Never push `work/x`" in line
    assert "git add -A" not in line and "scratch" not in line and "Fine line" not in line
    assert "1 constraint" in line


def test_all_cited_gives_no_warning(tmp_path):
    p = _transcript(tmp_path, "Summary:\n- Never push (turn 3).\n")
    assert csc.warn_line({"transcript_path": p}) is None


def test_summary_found_past_big_prefix_within_tail_window(tmp_path):
    p = _transcript(tmp_path, pad=3 * csc._TAIL_BYTES)
    assert "Never push" in csc.warn_line({"transcript_path": p})


def test_missing_or_bad_input_is_none(tmp_path):
    assert csc.warn_line({}) is None
    assert csc.warn_line({"transcript_path": str(tmp_path / "nope")}) is None
    bad = tmp_path / "b.jsonl"
    bad.write_bytes(b'{"isCompactSummary":true\n')
    assert csc.warn_line({"transcript_path": str(bad)}) is None


def test_compact_leg_registered_for_compact_only():
    from coordinator_core.hooks import sessionstart_dispatch as d

    row = [r for r in d._LEGS if r[0] == "compact_summary_constraints"][0]
    assert row[1] == frozenset({"compact"})
