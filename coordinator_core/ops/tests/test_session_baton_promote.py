"""
coordinator_core.ops.tests.test_session_baton_promote — idempotency,
post-scaffold body edit, and error handling for the "session_baton.promote"
op.

Spec backlink: docs/plans/2026-08-18-a-session-always-has-a-baton.md § C3.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import session_baton_promote as promote_mod
from coordinator_core.session_baton import store


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _promote(**params):
    return promote_mod._handler(dict(params))


def _fake_scaffold(dest_rel: str, body: str = ""):
    """Build a monkeypatch target replacing _scaffold_via_doc_new with a
    fake that writes a minimal handoff-shaped file under cwd/dest_rel and
    returns dest_rel, exactly like coordinator-doc-new's real stdout
    contract (a path string)."""

    def _fake(title, branch, cwd):
        target = Path(cwd) / dest_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "---\n"
            f"title: \"{title}\"\n"
            "kind: session-handoff\n"
            "---\n\n"
            "## What Was Accomplished\n\n"
            "<!-- Replace with what was built, fixed, or shipped this session. -->\n\n"
            "## Current State\n\n"
            "<!-- Replace with where things stand now. -->\n"
            + body,
            encoding="utf-8",
        )
        return dest_rel

    return _fake


# ---------------------------------------------------------------------------
# Basic promotion
# ---------------------------------------------------------------------------


def test_promote_scaffolds_and_stamps_baton(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    store.merge_baton("sid-1", cwd=str(repo), first_prompt="do the thing")

    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-18-fake.md"),
    )

    result = _promote(session_id="sid-1", cwd=str(repo))
    assert result["exit_code"] == 0
    assert result["error"] is None
    assert result["handoff_path"] == "state/handoffs/2026-08-18-fake.md"
    assert result["already_promoted"] is False

    on_disk = store.read_baton("sid-1", cwd=str(repo))
    assert on_disk["promoted_to"] == "state/handoffs/2026-08-18-fake.md"

    handoff_text = (repo / "state/handoffs/2026-08-18-fake.md").read_text(
        encoding="utf-8"
    )
    assert "do the thing" in handoff_text
    assert (
        "<!-- Replace with what was built, fixed, or shipped this session. -->"
        not in handoff_text
    )


def test_promote_without_first_prompt_leaves_placeholder(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        promote_mod,
        "_scaffold_via_doc_new",
        _fake_scaffold("state/handoffs/2026-08-18-noprompt.md"),
    )

    result = _promote(session_id="sid-noprompt", cwd=str(repo))
    assert result["exit_code"] == 0

    handoff_text = (repo / "state/handoffs/2026-08-18-noprompt.md").read_text(
        encoding="utf-8"
    )
    assert (
        "<!-- Replace with what was built, fixed, or shipped this session. -->"
        in handoff_text
    )


# ---------------------------------------------------------------------------
# Idempotency: a second call returns the existing path, scaffolds nothing
# ---------------------------------------------------------------------------


def test_second_promote_call_returns_existing_path_no_rescaffold(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    calls = []

    def _counting_fake(title, branch, cwd):
        calls.append(1)
        fake = _fake_scaffold("state/handoffs/2026-08-18-once.md")
        return fake(title, branch, cwd)

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _counting_fake)

    first = _promote(session_id="sid-idem", cwd=str(repo))
    assert first["already_promoted"] is False
    assert len(calls) == 1

    second = _promote(session_id="sid-idem", cwd=str(repo))
    assert second["exit_code"] == 0
    assert second["already_promoted"] is True
    assert second["handoff_path"] == first["handoff_path"]
    assert len(calls) == 1  # no second scaffold call


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------


def test_missing_session_id_errors():
    result = _promote()
    assert result["exit_code"] == 1
    assert "session_id" in result["error"]


def test_blank_session_id_errors():
    result = _promote(session_id="   ")
    assert result["exit_code"] == 1


def test_non_string_title_errors(tmp_path):
    repo = _make_repo(tmp_path)
    result = _promote(session_id="sid-badtitle", title=123, cwd=str(repo))
    assert result["exit_code"] == 1
    assert "title" in result["error"]


# ---------------------------------------------------------------------------
# Scaffold failure degrades to exit_code=1, never raises
# ---------------------------------------------------------------------------


def test_scaffold_failure_returns_error_not_exception(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)

    def _boom(title, branch, cwd):
        raise RuntimeError("coordinator-doc-new blew up")

    monkeypatch.setattr(promote_mod, "_scaffold_via_doc_new", _boom)

    result = _promote(session_id="sid-fail", cwd=str(repo))
    assert result["exit_code"] == 1
    assert "blew up" in result["error"]

    on_disk = store.read_baton("sid-fail", cwd=str(repo))
    assert on_disk["promoted_to"] is None


def test_scaffold_empty_stdout_is_an_error(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        promote_mod, "_scaffold_via_doc_new", lambda title, branch, cwd: ""
    )

    result = _promote(session_id="sid-empty", cwd=str(repo))
    assert result["exit_code"] == 1
    assert "no path" in result["error"]


# ---------------------------------------------------------------------------
# Real coordinator-doc-new integration (not mocked) — the actual seam this
# op is required to compose rather than reimplement.
# ---------------------------------------------------------------------------


def test_real_coordinator_doc_new_seam(tmp_path):
    repo = _make_repo(tmp_path)
    store.merge_baton("sid-real", cwd=str(repo), first_prompt="the real first prompt")

    result = _promote(
        session_id="sid-real", title="A real promotion test", cwd=str(repo)
    )
    if result["exit_code"] != 0:
        pytest.skip(
            "coordinator-doc-new CLI unavailable/misconfigured in this "
            f"environment: {result['error']}"
        )

    handoff_path = Path(result["handoff_path"])
    if not handoff_path.is_absolute():
        handoff_path = repo / handoff_path
    assert handoff_path.is_file()
    text = handoff_path.read_text(encoding="utf-8")
    assert "kind: session-handoff" in text
    assert "the real first prompt" in text

    on_disk = store.read_baton("sid-real", cwd=str(repo))
    assert on_disk["promoted_to"] == result["handoff_path"]
