"""bug_close: marker contract, row lookup, the pure closer and the locked row closer."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import bug_close

ID = "0123456789ab"
ID2 = "ba9876543210"
SHA = "f" * 40

ROW = (
    "created: 2026-10-26\n"
    "title: t\n"
    "body: b\n"
    "status: open\n"
    "surface: s\n"
    "severity: P1\n"
)


def _row(tmp_path, bug_id=ID, text=ROW, name=None):
    (tmp_path / ".git").mkdir(exist_ok=True)
    home = tmp_path / "state" / "bug-backlog"
    home.mkdir(parents=True, exist_ok=True)
    p = home / (name or f"2026-10-26-x-{bug_id}.yaml")
    p.write_bytes(text.encode("utf-8"))
    return p


def test_marker_round_trip_and_tolerance():
    assert bug_close.marker_line(ID) == f"post-commit: bug_close {ID}"
    assert bug_close.marker_ids(bug_close.marker_line(ID)) == (ID,)
    text = f"DONE\n- post-commit: bug_close {ID}\n> post-commit: bug_close {ID2}\n**post-commit: bug_close {ID}**\n"
    assert bug_close.marker_ids(text) == (ID, ID2)


def test_closing_bug_ids_requires_closure_intent(tmp_path):
    _row(tmp_path)
    for ok in (f"close bug {ID}", f"Closes {ID}", f"fix bug row {ID}", f"please close the bug row `{ID}` now"):
        assert bug_close.closing_bug_ids(tmp_path, ok) == (ID,), ok
    for bad in (f"unrelated to {ID}", f"distinct from {ID}", f"{ID} is still open", f"do not close bug {ID}",
                f"close bug {ID2}", f"can't close bug {ID}", f"cannot close {ID}", f"not yet close bug {ID}",
                f"don't yet close bug {ID}", f"without fixing bug {ID}"):
        assert bug_close.closing_bug_ids(tmp_path, bad) == (), bad


def test_marker_ids_ignore_prose():
    assert bug_close.marker_ids(f"close bug {ID} with the landing sha") == ()


@pytest.mark.parametrize("bad", ["", "abc", "0123456789AB", "0123456789abc", None])
def test_marker_line_bad_id_raises(bad):
    with pytest.raises(ValueError):
        bug_close.marker_line(bad)


def test_delivered_with_post_commit():
    m = bug_close.marker_line(ID)
    assert bug_close.delivered_with_post_commit(f"PARTIAL: x\n{m}\n")
    assert bug_close.delivered_with_post_commit(f"status: partial\n{m}\n")
    assert not bug_close.delivered_with_post_commit(f"DONE: x\n{m}\n")
    assert not bug_close.delivered_with_post_commit(f"PARTIAL: x\n{m}\ngate-blocker: outside-footprint: t\n")
    assert not bug_close.delivered_with_post_commit(f"BLOCKED: x\n{m}\n")
    assert not bug_close.delivered_with_post_commit(f"PARTIAL: x\nNot done: close {ID} with the landing sha\n")


def test_bug_row_rel(tmp_path):
    assert bug_close.bug_row_rel(tmp_path, ID) is None
    _row(tmp_path)
    assert bug_close.bug_row_rel(tmp_path, ID) == f"state/bug-backlog/2026-10-26-x-{ID}.yaml"
    _row(tmp_path, name=f"2026-10-27-y-{ID}.yaml")
    assert bug_close.bug_row_rel(tmp_path, ID) is None


def test_bug_row_rel_ignores_directory(tmp_path):
    (tmp_path / "state" / "bug-backlog" / f"2026-10-26-x-{ID}.yaml").mkdir(parents=True)
    assert bug_close.bug_row_rel(tmp_path, ID) is None


def test_named_bug_ids(tmp_path):
    _row(tmp_path)
    assert bug_close.named_bug_ids(tmp_path, f"{ID} closed; also {ID2} and {ID}") == (ID,)
    assert bug_close.named_bug_ids(tmp_path, "") == ()


def test_post_commit_clause_names_ids_and_marker():
    c = bug_close.post_commit_clause((ID, ID2))
    assert ID in c and ID2 in c and bug_close.marker_line(ID) in c
    assert "never undone work" in c


def test_closed_text_adds_three_keys_and_keeps_other_bytes():
    new = bug_close.closed_text(ROW, SHA, "2026-10-27")
    assert new == ROW.replace(
        "status: open\n", 'status: closed\nclosed_at: 2026-10-27\nresolution: "fixed at ffffffffff"\n'
    )


def test_closed_text_crlf_stays_crlf():
    old = ROW.replace("\n", "\r\n")
    new = bug_close.closed_text(old, SHA, "2026-10-27")
    assert new.count("\n") == new.count("\r\n")
    assert new.replace("\r\n", "\n") == bug_close.closed_text(ROW, SHA, "2026-10-27")


def test_closed_text_keeps_prior_resolution():
    old = ROW + 'resolution: "earlier note"\n'
    new = bug_close.closed_text(old, SHA, "2026-10-27")
    assert 'resolution: "fixed at ffffffffff; earlier note"\n' in new
    assert new.count("resolution:") == 1


def test_closed_text_already_closed_is_none():
    assert bug_close.closed_text(ROW.replace("open", "closed"), SHA, "2026-10-27") is None
    closed_other = ROW.replace("open", "closed") + 'resolution: "fixed at 1111111111"\n'
    assert bug_close.closed_text(closed_other, SHA, "2026-10-27") is None


def test_closed_text_multiline_prior_resolution_raises():
    with pytest.raises(ValueError):
        bug_close.closed_text(ROW + "resolution: |\n  a\n", SHA, "2026-10-27")


def test_close_rows_buckets(tmp_path):
    open_p = _row(tmp_path)
    done = ROW.replace("open", "closed") + 'resolution: "fixed at 1111111111"\n'
    done_p = _row(tmp_path, ID2, done)
    res = bug_close.close_rows(tmp_path, (ID, ID2, "aaaaaaaaaaaa"), SHA)
    assert res["closed"] == {ID: f"state/bug-backlog/2026-10-26-x-{ID}.yaml"}
    assert res["already"] == {ID2: f"state/bug-backlog/2026-10-26-x-{ID2}.yaml"}
    assert [r["id"] for r in res["refused"]] == ["aaaaaaaaaaaa"]
    assert 'resolution: "fixed at ffffffffff"' in open_p.read_text(encoding="utf-8")
    assert done_p.read_text(encoding="utf-8") == done
    again = bug_close.close_rows(tmp_path, (ID,), SHA)
    assert again["already"] and not again["closed"]


def test_close_rows_crlf_round_trips(tmp_path):
    p = _row(tmp_path, text=ROW.replace("\n", "\r\n"))
    assert bug_close.close_rows(tmp_path, (ID,), SHA)["closed"]
    data = p.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") and b"status: closed" in data


def test_close_rows_schema_invalid_is_refused_and_unchanged(tmp_path):
    bad = ROW.replace("severity: P1\n", "")
    p = _row(tmp_path, text=bad)
    res = bug_close.close_rows(tmp_path, (ID,), SHA)
    assert not res["closed"] and res["refused"][0]["id"] == ID
    assert p.read_text(encoding="utf-8") == bad


def test_close_rows_refuses_bad_sha(tmp_path):
    p = _row(tmp_path)
    res = bug_close.close_rows(tmp_path, (ID,), "nothex")
    assert res["refused"] and p.read_text(encoding="utf-8") == ROW
