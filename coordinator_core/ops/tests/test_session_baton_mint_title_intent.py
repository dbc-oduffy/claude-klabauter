"""
coordinator_core.ops.tests.test_session_baton_mint_title_intent — the
sentinel-semantics ACs for `title`/`intent` on the "session_baton.mint" op.

Spec backlink: docs/plans/2026-08-20-a-baton-you-can-jot-in.md § C1.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops import session_baton_mint as mint_mod
from coordinator_core.session_baton import store

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _ensure_session_dir(repo: Path, sid: str) -> Path:
    sdir = repo / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    return sdir


def _mint(**params):
    return mint_mod._handler(dict(params))


# ---------------------------------------------------------------------------
# AC1 — accepted, and non-string rejected with a named error
# ---------------------------------------------------------------------------


def test_title_and_intent_accepted_and_persisted(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-1")
    result = _mint(
        session_id="sid-ti-1", title="First pass", intent="fix the thing", cwd=str(repo)
    )

    assert result["exit_code"] == 0
    assert result["error"] is None

    on_disk = store.read_baton("sid-ti-1", cwd=str(repo))
    assert on_disk["title"] == "First pass"
    assert on_disk["intent"] == "fix the thing"


def test_non_string_title_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-bad-title")
    result = _mint(session_id="sid-ti-bad-title", title=123, cwd=str(repo))

    assert result["exit_code"] == 1
    assert result["error"] is not None
    assert "title" in result["error"]


def test_non_string_intent_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-bad-intent")
    result = _mint(session_id="sid-ti-bad-intent", intent=["not", "a", "string"], cwd=str(repo))

    assert result["exit_code"] == 1
    assert result["error"] is not None
    assert "intent" in result["error"]


# ---------------------------------------------------------------------------
# AC2 — an omitted param is never threaded into merge_baton; it must not
# null an existing stored value (the naive-implementation trap).
# ---------------------------------------------------------------------------


def test_omitted_title_leaves_existing_title_untouched(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-2")

    first = _mint(session_id="sid-ti-2", title="Keep me", cwd=str(repo))
    assert first["exit_code"] == 0

    second = _mint(session_id="sid-ti-2", prompt="unrelated call", cwd=str(repo))
    assert second["exit_code"] == 0

    on_disk = store.read_baton("sid-ti-2", cwd=str(repo))
    assert on_disk["title"] == "Keep me"


def test_omitted_intent_leaves_existing_intent_untouched(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-3")

    first = _mint(session_id="sid-ti-3", intent="stay put", cwd=str(repo))
    assert first["exit_code"] == 0

    second = _mint(session_id="sid-ti-3", cwd=str(repo))
    assert second["exit_code"] == 0

    on_disk = store.read_baton("sid-ti-3", cwd=str(repo))
    assert on_disk["intent"] == "stay put"


# ---------------------------------------------------------------------------
# AC3 — unlike first_prompt, title/intent are overwritable on every call.
# ---------------------------------------------------------------------------


def test_intent_is_overwritable_on_second_call(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-4")

    _mint(session_id="sid-ti-4", intent="v1 understanding", cwd=str(repo))
    second = _mint(session_id="sid-ti-4", intent="v2 understanding", cwd=str(repo))
    assert second["exit_code"] == 0

    on_disk = store.read_baton("sid-ti-4", cwd=str(repo))
    assert on_disk["intent"] == "v2 understanding"


def test_title_is_overwritable_on_second_call(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-5")

    _mint(session_id="sid-ti-5", title="Draft title", cwd=str(repo))
    second = _mint(session_id="sid-ti-5", title="Final title", cwd=str(repo))
    assert second["exit_code"] == 0

    on_disk = store.read_baton("sid-ti-5", cwd=str(repo))
    assert on_disk["title"] == "Final title"


# ---------------------------------------------------------------------------
# AC4 — first_prompt's capture-once guarantee is unaffected by title/intent
# arriving alongside it.
# ---------------------------------------------------------------------------


def test_first_prompt_capture_once_survives_title_intent_call(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-6")

    first = _mint(session_id="sid-ti-6", prompt="original prompt", cwd=str(repo))
    assert first["first_prompt"] == "original prompt"

    second = _mint(
        session_id="sid-ti-6",
        prompt="a different prompt",
        title="new title",
        intent="new intent",
        cwd=str(repo),
    )
    assert second["exit_code"] == 0
    assert second["first_prompt"] == "original prompt"

    on_disk = store.read_baton("sid-ti-6", cwd=str(repo))
    assert on_disk["first_prompt"] == "original prompt"
    assert on_disk["title"] == "new title"
    assert on_disk["intent"] == "new intent"


# ---------------------------------------------------------------------------
# AC5 — created keeps its existing meaning: True only when this call minted
# the record. A title-only call against an existing baton returns False.
# ---------------------------------------------------------------------------


def test_title_only_call_on_existing_baton_reports_created_false(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-7")

    first = _mint(session_id="sid-ti-7", prompt="hello", cwd=str(repo))
    assert first["created"] is True

    second = _mint(session_id="sid-ti-7", title="added later", cwd=str(repo))
    assert second["created"] is False


def test_title_only_call_mints_new_record_reports_created_true(tmp_path):
    repo = _make_repo(tmp_path)
    _ensure_session_dir(repo, "sid-ti-8")

    result = _mint(session_id="sid-ti-8", title="only a title", cwd=str(repo))
    assert result["created"] is True

    on_disk = store.read_baton("sid-ti-8", cwd=str(repo))
    assert on_disk["title"] == "only a title"
