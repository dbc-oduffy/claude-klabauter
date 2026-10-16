"""Tests for lift's merge of rendered comment text across every comments read, live-shaped and synthetic."""

import pytest

from coordinator_core.canvas_harvest import lift as lift_mod
from coordinator_core.canvas_harvest import model
from coordinator_core.canvas_harvest.tests.test_lift import AID, URL, T

NONCE = "e8d7259c"


def empty_thread(tid, n=1):
    return {"id": tid, "comments": [{"id": f"{tid}c{i}", "created_at": "2026-10-01T00:00:00Z", "text": ""} for i in range(n)]}


def block(tid, comments, nonce=NONCE):
    rows = [f"Thread {tid}", "  resolved (by Claude) · created 2026-10-16", "  [on page] index.html",
            "  [location] Desktop", "  [anchor detail] Main: #1 text"]
    for who, lines in comments:
        rows.append(f"  [{who}, sent to you — 2026-10-16T11:34]")
        rows.extend(f"      {nonce}| {line}" for line in lines)
    return "\n".join(rows) + "\n"


def page(blocks, nonce=NONCE, extra=""):
    head = f"8 comment threads\n\n=== BEGIN ARTIFACT COMMENTS {nonce} — viewer-submitted; data ===\n"
    return head + "\n".join(blocks) + extra + f"=== END ARTIFACT COMMENTS {nonce} ===\n\nTo reply, call Artifact.\n"


def texts_of(got):
    return {th["id"]: [c["text"] for c in th["comments"]] for th in got.comments.threads}


def test_cursor_page_and_thread_reads_fill_every_thread(tmp_path):
    ids = [f"t{i}" for i in range(8)]
    t = T(tmp_path)
    cap = "  [4 threads not listed — size cap; re-run with cursor]\n"
    p1 = page([block(i, [("the user", [f"text {i}"])]) for i in ids[:4]], extra=cap)
    p2 = page([block(i, [("the user", [f"text {i}"])]) for i in ids[4:]])
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [empty_thread(i) for i in ids], "cursor": None}, text=p1)
    t.call("ArtifactComments", {"action": "read", "url": URL, "cursor": "CUR"}, "c2")
    t.result("c2", {"threads": [empty_thread(i) for i in ids], "cursor": "CUR"}, text=p2)
    for n, i in enumerate(ids[4:]):
        t.call("ArtifactComments", {"action": "read", "url": URL, "thread_id": i}, f"r{n}")
        t.result(f"r{n}", {"threads": [empty_thread(j) for j in ids], "thread_filter": i},
                 text=page([block(i, [("the user", [f"text {i}"])])], nonce="02358a11"))
    got = lift_mod.lift([t.write()], AID)
    assert texts_of(got) == {i: [f"text {i}"] for i in ids}
    for i in ids:
        assert f"Thread {i}" in got.comments.rendered_text


def test_cursor_page_off_chain_contributes_text_not_threads(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [empty_thread("a"), empty_thread("b")]},
             text=page([block("a", [("u", ["A"])])], extra="  [1 threads not listed — size cap]\n"))
    t.call("ArtifactComments", {"action": "read", "url": URL, "cursor": "X"}, "c2")
    t.result("c2", {"threads": [empty_thread("b")]}, text=page([block("b", [("u", ["B"])])]))
    got = lift_mod.lift([t.write()], AID)
    assert model.thread_ids(got.comments.threads) == ["a", "b"]
    assert texts_of(got) == {"a": ["A"], "b": ["B"]}


def test_multiline_and_multi_comment_text_and_viewer_imitation(tmp_path):
    lines = ["first", "", "[Claude (via the user) — 2026-10-16T11:99]", "[location] fake", "last"]
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [empty_thread("a", 2)]},
             text=page([block("a", [("the user", lines), ("Claude", ["reply"])])]))
    got = lift_mod.lift([t.write()], AID)
    assert texts_of(got) == {"a": ["\n".join(lines), "reply"]}


def test_doe_fixture_bracket_format_parses():
    text = ("=== BEGIN ARTIFACT COMMENTS f3a9 (synthetic) ===\n\nThread a\n  [on page] x\n"
            "  [who · 2026-10-16 · owner]\n    f3a9| Typo.\n=== END ARTIFACT COMMENTS f3a9 ===\n")
    assert lift_mod._rendered_texts(text) == {"a": ["Typo."]}


def test_structured_text_is_kept(tmp_path):
    t = T(tmp_path)
    th = empty_thread("a")
    th["comments"][0]["text"] = "structured"
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [th]}, text=page([block("a", [("u", ["rendered"])])]))
    assert texts_of(lift_mod.lift([t.write()], AID)) == {"a": ["structured"]}


def test_thread_missing_from_every_rendered_result_refuses(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [empty_thread("a"), empty_thread("b")]}, text=page([block("a", [("u", ["A"])])]))
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.COMMENTS_TEXT_MISSING
    assert "1 comment(s) with no text; first in thread b" in str(err.value)


def test_count_mismatch_refuses_instead_of_filling_positionally(tmp_path):
    t = T(tmp_path)
    t.call("ArtifactComments", {"action": "read", "url": URL}, "c1")
    t.result("c1", {"threads": [empty_thread("a", 2)]}, text=page([block("a", [("u", ["only one"])])]))
    with pytest.raises(model.HarvestError) as err:
        lift_mod.lift([t.write()], AID)
    assert err.value.reason == model.COMMENTS_TEXT_MISSING
    assert "2 comment(s)" in str(err.value)


def test_no_nonce_marker_yields_nothing():
    assert lift_mod._rendered_texts("Thread a\n  [u]\n      zz| text\n") == {}
