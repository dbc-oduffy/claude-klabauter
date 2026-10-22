"""Pins on the warm `plan.seam_check` call's per-plan and per-row cuts. Git is faked; no spawn."""

from __future__ import annotations

import json

from coordinator_core.ops import plan_seam_check as op
from coordinator_core.ops.dispatch_emit import memo_rows, spine_read
from coordinator_core.ops.tests.test_plan_seam_check import _plan, _root, _row, git  # noqa: F401


def _set(root):
    return [
        _plan(root, "a", _row("A1", "[a.py]") + _row("A2", "[shared.py]")),
        _plan(root, "b", _row("B1", "[b.py]") + _row("B2", "[shared.py]")),
        _plan(root, "c", _row("C1", "[c.py]", extra="  brief: run cross-repo-memo send now\n")),
    ]


def _reply(root, plans):
    reply = op._check_handler({"plans": plans, "phase": "prep", "named_set": False}, repo_root=root)
    reply.pop("checked_at_sha", None)
    return json.dumps(reply, sort_keys=True, default=str)


def test_memo_brief_without_either_literal_skips_the_regex(monkeypatch):
    class Boom:
        def search(self, _text):
            raise AssertionError("regex ran on a brief holding neither literal")

    monkeypatch.setattr(memo_rows, "_MEMO_SEND_BRIEF_RE", Boom())
    assert memo_rows._brief_names_memo_send("a long brief about memo handling and sending") is False


def test_memo_brief_with_a_literal_still_matches():
    assert memo_rows._brief_names_memo_send("then cross-repo-memo send the note") is True
    assert memo_rows._brief_names_memo_send("call memo.send here") is True
    assert memo_rows._brief_names_memo_send("cross-repo-memo.py send") is True
    assert memo_rows._brief_names_memo_send("a cross-repo-memo draft only") is False


def test_a_plan_text_is_split_once_across_every_reader(tmp_path, git, monkeypatch):
    root = _root(tmp_path)
    plans = _set(root)
    spine_read._FM_SPLITS.clear()
    calls: list = []
    real = spine_read.split_frontmatter

    def counting(text):
        calls.append(text)
        return real(text)

    monkeypatch.setattr(spine_read, "split_frontmatter", counting)
    op._check_handler({"plans": plans, "phase": "prep", "named_set": False}, repo_root=root)
    op._check_handler({"plans": plans, "phase": "prep", "named_set": False}, repo_root=root)
    assert len(calls) == len(set(calls)) == len(plans)


def test_a_plan_read_by_many_read_spine_calls_is_parsed_once(tmp_path, monkeypatch):
    root = _root(tmp_path)
    rel = _plan(root, "solo", _row("S1", "[s.py]"))
    spine_read._ROWS_MEMO.clear()
    parses: list = []
    real = spine_read._parse_rows

    def counting(source):
        parses.append(source)
        return real(source)

    monkeypatch.setattr(spine_read, "_parse_rows", counting)
    for _ in range(5):
        spine_read.read_spine(root / rel, schema_preflight=False)
    assert len(parses) == 1


def test_reply_equals_the_uncut_reply(tmp_path, git, monkeypatch):
    root = _root(tmp_path)
    plans = _set(root)
    cut = _reply(root, plans)
    monkeypatch.setattr(spine_read, "split_frontmatter_memo", spine_read.split_frontmatter)
    monkeypatch.setattr(op, "split_frontmatter_memo", spine_read.split_frontmatter)
    monkeypatch.setattr(
        memo_rows, "_brief_names_memo_send", lambda brief: memo_rows._MEMO_SEND_BRIEF_RE.search(brief) is not None
    )
    assert _reply(root, plans) == cut
