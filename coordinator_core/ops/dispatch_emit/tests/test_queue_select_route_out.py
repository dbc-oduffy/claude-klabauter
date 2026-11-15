"""Schema-failing legacy rows with no derivable repo are routed out by a `_handback` mark."""
from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

from coordinator_core.ops.dispatch_emit.tests.test_queue_select import (
    _lesson_gate,
    _select,
    _write_legacy,
)


def _outbox(tmp_path: Path) -> Path:
    queue_dir = tmp_path / "state" / "cross-repo" / "outbox"
    queue_dir.mkdir(parents=True)
    return queue_dir


def _marks(tmp_path: Path) -> list[Path]:
    d = tmp_path / "state" / "queue-grind" / "fixture" / "_handback"
    return sorted(d.glob("*.json")) if d.is_dir() else []


def _run(tmp_path: Path, queue_dir: Path, **kw):
    return _select(queue=[queue_dir], repo_root=tmp_path, row_schema_gate=_lesson_gate(), **kw)


def test_first_select_writes_a_mark_and_second_declines_by_mark(tmp_path):
    queue_dir = _outbox(tmp_path)
    win_path = str(PureWindowsPath("C:/abs/x.md"))  # abs-path-ok: key=paths.data_root
    _write_legacy(queue_dir / "bare.yaml", path=win_path)
    first = _run(tmp_path, queue_dir, write_handback_marks=True)
    assert first.entries == () and [d.row_id for d in first.declined] == ["bare"]
    assert "not derivable" in first.declined[0].reason
    (mark,) = _marks(tmp_path)
    record = json.loads(mark.read_text(encoding="utf-8"))
    assert record["row"] == "bare" and record["type"] == "needs-judgment"
    assert "not derivable" in record["reason"]

    second = _run(tmp_path, queue_dir, write_handback_marks=True)
    (declined,) = second.declined
    assert declined.row_id == "bare" and "hand-back mark" in declined.reason
    assert len(_marks(tmp_path)) == 1


def test_default_off_writes_nothing(tmp_path):
    queue_dir = _outbox(tmp_path)
    _write_legacy(queue_dir / "bare.yaml", path="/abs/x.md")
    manifest = _run(tmp_path, queue_dir)
    assert [d.row_id for d in manifest.declined] == ["bare"]
    assert _marks(tmp_path) == []
    assert not (tmp_path / "state" / "queue-grind").exists()


def test_an_edited_row_is_revalidated_not_declined_by_a_stale_mark(tmp_path):
    queue_dir = _outbox(tmp_path)
    row = queue_dir / "bare.yaml"
    _write_legacy(row, path="/abs/x.md")
    _run(tmp_path, queue_dir, write_handback_marks=True)
    _write_legacy(row, repo="example-store-repo")
    manifest = _run(tmp_path, queue_dir, write_handback_marks=True)
    assert [e.row_id for e in manifest.entries] == ["bare"] and manifest.declined == ()


def test_a_derivable_row_and_a_non_handback_schema_failure_get_no_mark(tmp_path):
    queue_dir = tmp_path / "state" / "lessons"
    queue_dir.mkdir(parents=True)
    _write_legacy(queue_dir / "ok.yaml", repo="example-store-repo")
    _write_legacy(queue_dir / "badscope.yaml", repo="x", scope="galaxy")
    manifest = _run(tmp_path, queue_dir, write_handback_marks=True)
    assert [e.row_id for e in manifest.entries] == ["ok"]
    assert [d.row_id for d in manifest.declined] == ["badscope"]
    assert _marks(tmp_path) == []
