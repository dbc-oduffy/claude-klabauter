"""Tests for release_stamp.writer.stamp_entry."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from coordinator_core.ops.release_stamp import writer
from coordinator_core.ops.release_stamp.model import ReleaseTag, StampError

TAG = ReleaseTag("v1.2.0", (1, 2, 0), "a" * 40, "b" * 40, "2026-10-01")
REL = "archive/completed/2026-10/e.md"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "archive/completed/2026-10").mkdir(parents=True)
    return tmp_path


def _write(root: Path, body: str, rel: str = REL) -> Path:
    p = root / rel
    p.write_text(body, encoding="utf-8", newline="")
    return p


PENDING = "---\ntitle: x\nstatus: pending-release\nowner: me\n---\nbody\n"


@pytest.fixture
def declared(monkeypatch):
    calls: list = []
    import coordinator_core.session.declared_writes as dw

    monkeypatch.setattr(dw, "declare_write", lambda p: calls.append(p))
    return calls


def test_four_fields_land_and_only_those_lines_change(root, declared):
    p = _write(root, PENDING)
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert skip is None
    assert rec == {
        "path": REL,
        "released_in": "v1.2.0",
        "released_at": "2026-10-01",
        "released_sha": "a" * 40,
    }
    new = p.read_text()
    assert new == (
        "---\ntitle: x\nstatus: released\nowner: me\n"
        f"released_in: v1.2.0\nreleased_at: 2026-10-01\nreleased_sha: {'a' * 40}\n---\nbody\n"
    )
    assert "\r" not in new
    assert declared == [REL]


def test_existing_empty_keys_replaced_not_duplicated(root, declared):
    p = _write(
        root,
        "---\nstatus: pending-release\nreleased_in:\nreleased_at: \nreleased_sha:\n---\n",
    )
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert skip is None
    text = p.read_text()
    for k in ("released_in", "released_at", "released_sha"):
        assert text.count(f"{k}:") == 1
    assert "released_in: v1.2.0\n" in text


def test_released_entry_untouched(root, declared):
    body = "---\nstatus: released\nreleased_in: v0.9.0\nreleased_at: 2026-01-01\nreleased_sha: c\n---\n"
    p = _write(root, body)
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert rec is None and skip == f"{REL}: status is released"
    assert p.read_text() == body
    assert declared == []


def test_other_status_skipped(root, declared):
    body = "---\nstatus: session-closed-workstream-open\n---\n"
    p = _write(root, body)
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert rec is None and skip == f"{REL}: status is session-closed-workstream-open"
    assert p.read_text() == body
    assert declared == []


def test_malformed_frontmatter_skipped(root, declared):
    _write(root, "---\nstatus: pending-release\n")
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert rec is None and "malformed frontmatter" in skip
    assert declared == []


def test_missing_file_is_skip(root, declared):
    rec, skip = writer.stamp_entry(root, REL, TAG)
    assert rec is None and "unreadable" in skip
    assert declared == []


def test_path_outside_entry_root_refused(root, declared):
    _write(root, PENDING, rel="docs.md")
    with pytest.raises(StampError) as ei:
        writer.stamp_entry(root, "docs.md", TAG)
    assert ei.value.reason == "bad-params"
    with pytest.raises(StampError):
        writer.stamp_entry(root, "archive/completed/../../docs.md", TAG)
    assert declared == []


def test_two_threads_one_stamp_one_skip(root, declared):
    _write(root, PENDING)
    results: list = []
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        results.append(writer.stamp_entry(root, REL, TAG))

    ts = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    stamps = [r for r in results if r[0] is not None]
    skips = [r for r in results if r[1] is not None]
    assert len(stamps) == 1 and len(skips) == 1
