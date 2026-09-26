
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.ceremony import receipt_emit
from coordinator_core.ops.ceremony.pipeline_context import PipelineContext
from coordinator_core.session import core as session_core
from coordinator_core.session import scope as session_scope


def _init_git_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / ".git").mkdir()
    session_core.reset_sessions_dir_cache()
    return repo_root


def _make_ctx(ceremony: str = "wsc") -> PipelineContext:
    return PipelineContext(ceremony=ceremony, scope_mode="auto")


def _claimed_paths(repo_root: Path, sid: str) -> set[str]:
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
    repo_root = _init_git_repo(tmp_path)
    sid = "test-sid-declares-absent-03"

    ctx = _make_ctx()
    out_path, _op_tail = receipt_emit.emit_receipt(
        ctx, repo_root=repo_root, sid=sid, emitted_at="2026-08-20T00:00:00Z"
    )

    assert out_path.exists()
    sdir = Path(session_core.session_dir(sid, str(repo_root)))
    assert not sdir.is_dir()
