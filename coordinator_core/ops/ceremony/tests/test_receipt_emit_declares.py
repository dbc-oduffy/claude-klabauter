"""
coordinator_core.ops.ceremony.tests.test_receipt_emit_declares — C2.

Purpose: assert emit_receipt() declares the receipt path it wrote via
session_scope.touch_written_path, following the same shape as
coordinator_core/subagent_sandbox/provision_report.py's touch_written_path call sites (raw
sid, after-write-only, phantom-live-peer graceful-absent).

Spec backlink: state/dispatch-briefs/2026-08-20-the-close-ceremony-commits-
what-the-session-wrote/C2.md
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony import receipt_emit
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope


def _init_git_repo(tmp_path: Path) -> Path:
    """Build a minimal walk-detectable git repo root -- plain files only,
    no `git init` spawn.  `git_common_dir` (session_core.sessions_dir's
    resolver) is walk-only over `.git` filesystem entries and never
    spawns, so a bare `.git` directory is sufficient fixture state.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    session_core.reset_sessions_dir_cache()
    return repo_root


def _make_ctx(ceremony: str = "wsc") -> PipelineContext:
    return PipelineContext(ceremony=ceremony, scope_mode="auto")


def _claimed_paths(repo_root: Path, sid: str) -> set[str]:
    """Reads the session's claim state through the same seam production
    code uses (`session_scope._read_touch_record_as_legacy_lines` over
    `touch-record.jsonl`) -- NOT a raw `touched.txt` read. The old
    `touched.txt` sink was fully retired 2026-08-26
    (`_read_touch_record_as_legacy_lines`'s own docstring, "THE COMPAT
    UNION IS GONE"); a direct `touched.txt` read here always sees an empty
    set post-retirement regardless of whether the write actually happened.
    """
    sdir = Path(session_core.session_dir(sid, str(repo_root)))
    sink_path = sdir / session_scope._TOUCH_RECORD_FILENAME
    lines, _degraded = session_scope._read_touch_record_as_legacy_lines(sink_path)
    return session_scope.project_self_scope(lines)


def test_emit_receipt_declares_written_path(tmp_path):
    repo_root = _init_git_repo(tmp_path)
    sid = "test-sid-declares-01"
    session_core.init(sid, "test goal", str(repo_root))

    ctx = _make_ctx()
    out_path, _op_tail = receipt_emit.emit_receipt(
        ctx, repo_root=repo_root, sid=sid, emitted_at="2026-08-20T00:00:00Z"
    )

    assert out_path.exists()
    rel_path = str(out_path.relative_to(repo_root)).replace("\\", "/")
    assert rel_path in _claimed_paths(repo_root, sid)


def test_emit_receipt_declares_nothing_on_failed_write(tmp_path, monkeypatch):
    repo_root = _init_git_repo(tmp_path)
    sid = "test-sid-declares-02"
    session_core.init(sid, "test goal", str(repo_root))

    ctx = _make_ctx()

    def _boom(path, data):
        raise OSError("simulated write failure")

    monkeypatch.setattr(receipt_emit, "_atomic_write_json", _boom)

    try:
        receipt_emit.emit_receipt(
            ctx, repo_root=repo_root, sid=sid, emitted_at="2026-08-20T00:00:00Z"
        )
    except OSError:
        pass
    else:
        raise AssertionError("expected emit_receipt to propagate the write failure")

    assert _claimed_paths(repo_root, sid) == set()


def test_emit_receipt_absent_session_dir_declares_silently(tmp_path):
    """Pinned behaviour: declaring against an absent session dir (the
    phantom-live-peer guard's precondition, session/scope.py:1818-1821)
    returns silently and adds no claim, rather than raising. Do NOT "fix"
    this as part of this chunk — see the brief.
    """
    repo_root = _init_git_repo(tmp_path)
    sid = "test-sid-declares-absent-03"
    # Deliberately do NOT call session_core.init — no session dir exists.

    ctx = _make_ctx()
    out_path, _op_tail = receipt_emit.emit_receipt(
        ctx, repo_root=repo_root, sid=sid, emitted_at="2026-08-20T00:00:00Z"
    )

    assert out_path.exists()
    sdir = Path(session_core.session_dir(sid, str(repo_root)))
    assert not sdir.is_dir()
