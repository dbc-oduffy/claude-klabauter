"""review_reverify_findings: finding inversion keeps executor bytes; applied_rows filters rows."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops import review_findings_ledger
from coordinator_core.ops.review_reverify_findings import FindingRef, applied_rows, invert


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "Popen", boom)


def _row(i, before, after, **extra):
    return (FindingRef("s.md", i), {"id": i, "before": before, "after": after, **extra})


def test_single_finding_inverted_executor_lines_untouched():
    data = b"exec1\nnew_name = 1\nexec2\n"
    out = invert(data, [_row("f1", "old_name = 1\n", "new_name = 1\n")])
    assert out.data == b"exec1\nold_name = 1\nexec2\n"
    assert out.reverted == [FindingRef("s.md", "f1")]
    assert out.kept == []


def test_two_findings_reverse_order_with_overlap():
    # f2's before contains f1's after, so f2 must be undone first.
    rows = [_row("f1", "a\n", "B\n"), _row("f2", "B\n", "C\n")]
    out = invert(b"x\nC\nZ\n", rows)
    assert out.data == b"x\na\nZ\n"
    assert [r.id for r in out.reverted] == ["f1", "f2"]


def test_after_absent_kept():
    out = invert(b"hello\n", [_row("f1", "a", "gone")])
    assert out.data == b"hello\n"
    assert out.kept == [(FindingRef("s.md", "f1"), "after-absent")]


def test_after_twice_kept_ambiguous():
    out = invert(b"dup\ndup\n", [_row("f1", "x", "dup")])
    assert out.data == b"dup\ndup\n"
    assert out.kept == [(FindingRef("s.md", "f1"), "after-ambiguous")]


def test_empty_after_kept_no_after():
    out = invert(b"abc\n", [_row("f1", "x", "")])
    assert out.data == b"abc\n"
    assert out.kept == [(FindingRef("s.md", "f1"), "no-after")]


def test_crlf_file_with_lf_row_stays_crlf():
    data = b"exec\r\nnew\r\nmore\r\ntail\r\n"
    out = invert(data, [_row("f1", "old\nline\n", "new\nmore\n")])
    assert out.data == b"exec\r\nold\r\nline\r\ntail\r\n"
    assert out.reverted == [FindingRef("s.md", "f1")]


def test_undecodable_returns_unchanged_all_kept():
    data = b"\xff\xfe\x00bad"
    out = invert(data, [_row("f1", "a", "b"), _row("f2", "c", "d")])
    assert out.data == data
    assert out.reverted == []
    assert [reason for _, reason in out.kept] == ["undecodable", "undecodable"]


def test_applied_rows_filters(tmp_path, monkeypatch):
    inside = tmp_path / "inside.md"
    inside.write_text("x")
    nol = tmp_path / "noledger.md"
    nol.write_text("x")
    outside = tmp_path.parent / "outside-sidecar.md"
    rows = [
        {"id": "a", "file": "pkg/m.py", "status": "applied"},
        {"id": "b", "file": "pkg/m.py"},
        {"id": "c", "file": "pkg/m.py", "status": "em-rejected"},
        {"id": "d", "file": "pkg/other.py"},
        {"id": "e", "file": "./pkg\\m.py"},
    ]

    def fake(path):
        return (True, rows) if path.name in ("inside.md", "outside-sidecar.md") else None

    monkeypatch.setattr(review_findings_ledger, "read_ledger", fake)
    got = applied_rows(tmp_path, ["inside.md", str(outside), "noledger.md"], "pkg/m.py")
    assert [ref.id for ref, _ in got] == ["a", "b", "e"]
    assert all(ref.sidecar == "inside.md" for ref, _ in got)
