"""Tests for sizing_match: baton to sizing matching and the `baton:` stamp."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.roadmap import sizing_match as sm


def _git(root: Path, *args: str) -> None:
    res = sm.run_git(["-C", str(root), *args])
    assert res.returncode == 0, res.stderr


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / "state/handoffs").mkdir(parents=True)
    return tmp_path


def _baton(repo: Path, name: str, did: str = "", title: str = "") -> str:
    fm = ["---"]
    if title:
        fm.append(f'title: "{title}"')
    if did:
        fm.append(f'deliverable_id: "{did}"')
    fm.append("---\nbody\n")
    rel = f"state/handoffs/{name}.md"
    (repo / rel).write_text("\n".join(fm), encoding="utf-8")
    return rel


def _sizing(repo: Path, name: str, body: str, newline: str = "\n") -> str:
    rel = f"state/sizings/{name}.yaml"
    (repo / rel).write_bytes(body.replace("\n", newline).encode("utf-8"))
    return rel


def test_deliverable_hit(repo):
    b = _baton(repo, "2026-10-22-thing", did="dlv-aaa")
    s = _sizing(repo, "2026-10-22-other-name", "name: x\nstatus: routed\ndeliverable_id: dlv-aaa\n")
    r = sm.match_sizings(repo, [b])
    assert r == [{"baton": b, "sizing": s, "basis": "deliverable_id", "candidates": [s]}]


def test_title_fallback_strips_date_and_runid(repo):
    b = _baton(repo, "2026-10-22-liveness-reader-closeout")
    s = _sizing(repo, "2026-10-22-liveness-reader-closeout-1a2b3c", "name: x\nstatus: routed\n")
    r = sm.match_sizings(repo, [b])[0]
    assert (r["sizing"], r["basis"]) == (s, "title")


def test_title_fallback_refuses_contradicting_deliverable_id(repo):
    b = _baton(repo, "2026-10-22-liveness-reader-closeout", did="dlv-aaa")
    _sizing(repo, "2026-10-22-liveness-reader-closeout", "status: routed\ndeliverable_id: dlv-bbb\n")
    r = sm.match_sizings(repo, [b])[0]
    assert (r["sizing"], r["basis"]) == (None, "none")


def test_title_fallback_accepts_sizing_without_deliverable_id_for_did_baton(repo):
    b = _baton(repo, "2026-10-22-liveness-reader-closeout", did="dlv-aaa")
    s = _sizing(repo, "2026-10-22-liveness-reader-closeout", "status: routed\n")
    r = sm.match_sizings(repo, [b])[0]
    assert (r["sizing"], r["basis"]) == (s, "title")


def test_ambiguous_two_deliverable_hits(repo):
    b = _baton(repo, "2026-10-22-thing", did="dlv-aaa")
    _sizing(repo, "2026-10-22-a", "status: routed\ndeliverable_id: dlv-aaa\n")
    _sizing(repo, "2026-10-22-b", "status: sized\ndeliverable_id: dlv-aaa\n")
    r = sm.match_sizings(repo, [b])[0]
    assert r["basis"] == "ambiguous" and r["sizing"] is None and len(r["candidates"]) == 2


def test_foreign_baton_rejected(repo):
    b = _baton(repo, "2026-10-22-thing", did="dlv-aaa")
    _sizing(repo, "2026-10-22-a", 'status: routed\nbaton: "state/handoffs/other.md"\ndeliverable_id: dlv-aaa\n')
    r = sm.match_sizings(repo, [b])[0]
    assert r["basis"] == "none" and r["sizing"] is None


def test_terminal_status_rejected(repo):
    b = _baton(repo, "2026-10-22-thing", did="dlv-aaa")
    _sizing(repo, "2026-10-22-a", "status: shipped  # done\ndeliverable_id: dlv-aaa\n")
    assert sm.match_sizings(repo, [b])[0]["basis"] == "none"


def test_untracked_sizing_found_and_tracked_too(repo):
    b = _baton(repo, "2026-10-22-thing", did="dlv-aaa")
    s = _sizing(repo, "2026-10-22-a", "status: routed\ndeliverable_id: dlv-aaa\n")
    _git(repo, "add", s)
    assert sm.match_sizings(repo, [b])[0]["sizing"] == s


def test_batch_of_three_is_one_git_spawn(repo, monkeypatch):
    batons = [_baton(repo, f"2026-10-22-t{i}", did=f"dlv-{i}") for i in range(3)]
    for i in range(3):
        _sizing(repo, f"2026-10-22-s{i}", f"status: routed\ndeliverable_id: dlv-{i}\n")
    calls = []
    real = sm.run_git

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(sm, "run_git", counting)
    out = sm.match_sizings(repo, batons)
    assert len(calls) == 1
    assert [o["basis"] for o in out] == ["deliverable_id"] * 3


def test_stamp_inserts_after_status_and_is_idempotent(repo):
    s = _sizing(repo, "2026-10-22-a", "name: x\nstatus: routed  # c\nintent: y\n")
    r = sm.stamp_sizing_baton(repo, s, "state/handoffs/b.md")
    assert r == {"sizing": s, "stamped": True, "conflict": None}
    text = (repo / s).read_text()
    assert text == 'name: x\nstatus: routed  # c\nbaton: "state/handoffs/b.md"\nintent: y\n'
    r2 = sm.stamp_sizing_baton(repo, s, "state/handoffs/b.md")
    assert r2["stamped"] is False and r2["conflict"] is None
    assert (repo / s).read_text() == text


def test_stamp_conflict_leaves_file(repo):
    s = _sizing(repo, "2026-10-22-a", 'status: routed\nbaton: "state/handoffs/z.md"\n')
    before = (repo / s).read_bytes()
    r = sm.stamp_sizing_baton(repo, s, "state/handoffs/b.md")
    assert r["stamped"] is False and r["conflict"] == "state/handoffs/z.md"
    assert (repo / s).read_bytes() == before


def test_stamp_crlf_preserved(repo):
    s = _sizing(repo, "2026-10-22-a", "name: x\nstatus: routed\nintent: y\n", newline="\r\n")
    sm.stamp_sizing_baton(repo, s, "state/handoffs/b.md")
    raw = (repo / s).read_bytes()
    assert raw == b'name: x\r\nstatus: routed\r\nbaton: "state/handoffs/b.md"\r\nintent: y\r\n'
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_slug_from_title_word_boundary():
    assert sm.slug_from_title("Hello, World!") == "hello-world"
    long = "alpha beta gamma delta epsilon zeta eta theta iota"
    out = sm.slug_from_title(long)
    assert len(out) <= 40 and not out.endswith("-") and long.replace(" ", "-").startswith(out)
