"""stamp_shipped_in stamps a scope-less handoff from a caller-held sha."""
from pathlib import Path

import pytest

from coordinator_core import archive_stamp


@pytest.fixture
def captured(monkeypatch, tmp_path):
    calls = []

    async def fake_handler(params, repo_root=None):
        calls.append(params)
        return {"exit_code": 0, "applied": True}

    monkeypatch.setattr(archive_stamp, "_resolve_repo_root_for", lambda p: (tmp_path, tmp_path))
    monkeypatch.setattr("coordinator_core.ops.handoff_stamp._handler", fake_handler)
    return calls


def _handoff(tmp_path: Path, scope: str) -> str:
    p = tmp_path / "h.md"
    p.write_text(f"---\ntitle: H\nstatus: claimed\n{scope}---\n# Body\n", encoding="utf-8")
    return str(p)


def test_scopeless_stamped_from_fallback_sha(captured, tmp_path):
    out = archive_stamp.stamp_shipped_in(
        _handoff(tmp_path, ""), kind="scope-derived", fallback_sha="abcdef1234"
    )
    assert out.exit_code == 0
    assert len(captured) == 1
    assert captured[0]["kind"] == "ship-commit"
    assert captured[0]["sha"].startswith("abcdef12")


def test_scopeless_without_fallback_still_refused(captured, tmp_path):
    out = archive_stamp.stamp_shipped_in(_handoff(tmp_path, ""), kind="scope-derived")
    assert out.exit_code == 0
    assert captured == []


def test_malformed_fallback_fails_loud(captured, tmp_path):
    out = archive_stamp.stamp_shipped_in(
        _handoff(tmp_path, ""), kind="scope-derived", fallback_sha="nothex"
    )
    assert out.exit_code == 1
    assert captured == []


def test_scoped_handoff_ignores_fallback(captured, monkeypatch, tmp_path):
    monkeypatch.setattr(archive_stamp, "_resolve_scope_sha", lambda *a, **k: None)
    out = archive_stamp.stamp_shipped_in(
        _handoff(tmp_path, "scope:\n  - src/a.py\n"),
        kind="scope-derived",
        fallback_sha="abcdef1234",
    )
    assert out.exit_code == 0
    assert captured == []


def test_cascade_ship_passes_held_sha_as_fallback(monkeypatch, tmp_path):
    from coordinator_core.ops import deliverable_cascade as cascade_mod

    seen = {}

    def spy(path, **kw):
        seen.update(kw)
        return archive_stamp.StampOutcome(exit_code=1, error="stop")

    monkeypatch.setattr(archive_stamp, "stamp_shipped_in", spy)
    ok, _ = cascade_mod._advance_one(
        tmp_path / "h.md", "dlv-x", "2026-10-13T00:00:00Z", tmp_path,
        source_kind="plan", fallback_sha="abcdef1234",
    )
    assert not ok
    assert seen["kind"] == "scope-derived" and seen["fallback_sha"] == "abcdef1234"


def test_ship_handoff_without_sha_uses_deliverable_id_commit(monkeypatch, tmp_path):
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    def git(*a):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=tmp_path,
            capture_output=True, text=True, check=True, **no_console_creationflags(),
        ).stdout.strip()

    git("init", "-q")
    (tmp_path / "a.txt").write_text("1")
    git("add", ".")
    git("commit", "-q", "-m", "work\n\nDeliverable-Id: dlv-ship-1")
    owned = git("rev-parse", "HEAD")
    (tmp_path / "b.txt").write_text("2")
    git("add", ".")
    git("commit", "-q", "-m", "peer\n\nDeliverable-Id: dlv-other")

    h = tmp_path / "h.md"
    h.write_text("---\ntitle: H\ndeliverable_id: dlv-ship-1\n---\nB\n", encoding="utf-8")
    monkeypatch.setattr(archive_stamp, "_resolve_repo_root_for", lambda p: (tmp_path, tmp_path))
    assert archive_stamp._deliverable_commit_for_scopeless(str(h)) == owned

    h.write_text("---\ntitle: H\ndeliverable_id: dlv-ship-1\nscope:\n  - a.txt\n---\nB\n", encoding="utf-8")
    assert archive_stamp._deliverable_commit_for_scopeless(str(h)) is None

    h.write_text("---\ntitle: H\n---\nB\n", encoding="utf-8")
    assert archive_stamp._deliverable_commit_for_scopeless(str(h)) is None


def test_cs_ship_handoff_threads_resolved_sha(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(archive_stamp, "_deliverable_commit_for_scopeless", lambda p: "abcdef1234")
    monkeypatch.setattr(
        archive_stamp, "_call_handoff_archive_transition",
        lambda p, params: sent.append(params) or {"exit_code": 0, "retained": True},
    )
    assert archive_stamp.cs_ship_handoff(str(tmp_path / "h.md")) == 0
    assert sent[0]["sha"] == "abcdef1234"
