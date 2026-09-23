"""Co-Authored-By is an ENGINE trailer, not caller boilerplate (PM ruling,
state/cross-repo/inbox/2026-09-23-example-game-repo-em-commit-trailers-owned-by-
engine.md; reworked same day after the first cut's `settings.json` source
was rejected as the operator's DEFAULT model, not the model that actually
authored the session).

Source: the committing SESSION's OWN transcript -- the most recent
`type == "assistant"` record's `message.model`, via the shared
`coordinator_core.transcript_tail.resolve_last_assistant_model` reader (also
used by `hooks/block_ungranted_opus_subagent.py`, so this is its second
caller, not a third reimplementation).

Covers: attach when missing; display-name derivation (including the
8-digit-date-suffix and unparseable-id cases); dedup against ANY existing
`Co-Authored-By:` line addressed to `noreply@anthropic.com` (including a
different display string, the migration shape); a different-address line
stays untouched; a transcript whose last record is not an assistant turn;
no session id / no transcript / no assistant record -> no trailer, no
failure.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import commit_trailers
from coordinator_core.git.commit_trailers import (
    _display_name_from_model_id,
    compute_missing_trailer_args,
    trailer_values_from_argv,
)
from coordinator_core.win_portability import no_console_creationflags

# compute_missing_trailer_args resolves the git-dir via a real `git
# rev-parse`, same as every sibling suite in this package.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "56565656-5656-4565-8565-565656565656"


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _reset_attribution_memo():
    """Both attribution memos are module-level dicts keyed by session id /
    (session_id, transcript_path) -- reset around every test so one test's
    fake `CLAUDE_HOME`/transcript never leaks into the next, and a real
    ambient transcript read by an earlier import never pollutes a
    "no source resolves" test."""
    commit_trailers._ATTRIBUTION_TRANSCRIPT_MEMO.clear()
    commit_trailers._ATTRIBUTION_VALUE_MEMO.clear()
    commit_trailers._ATTRIBUTION_MISSING_NOTED = False
    yield
    commit_trailers._ATTRIBUTION_TRANSCRIPT_MEMO.clear()
    commit_trailers._ATTRIBUTION_VALUE_MEMO.clear()
    commit_trailers._ATTRIBUTION_MISSING_NOTED = False


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init", "-q"], r)
    _git(["config", "user.email", "t@t.example"], r)
    _git(["config", "user.name", "t"], r)
    return r


def _write_transcript(
    claude_home: Path, session_id: str, records: list, project_slug: str = "proj"
) -> Path:
    proj_dir = claude_home / ".claude" / "projects" / project_slug
    proj_dir.mkdir(parents=True, exist_ok=True)
    path = proj_dir / f"{session_id}.jsonl"
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def claude_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "fake-claude-home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.setenv("CLAUDE_SESSION_ID", _SID)
    return home


def _assistant_record(model: str) -> dict:
    return {"type": "assistant", "message": {"model": model}}


def _msg_file(repo: Path, text: str) -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


def _co_authored_by_lines(argv) -> list:
    values = trailer_values_from_argv(argv)
    return [v for v in values if v.lower().startswith("co-authored-by:")]


# ---------------------------------------------------------------------------
# Display-name derivation (pure function, no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id,expected",
    [
        ("claude-opus-5-5", "Claude Opus 5.5"),
        ("claude-sonnet-5", "Claude Sonnet 5"),
        ("claude-haiku-4-5-20251001", "Claude Haiku 4.5"),
    ],
)
def test_display_name_derivation(model_id, expected):
    assert _display_name_from_model_id(model_id) == expected


@pytest.mark.parametrize(
    "model_id",
    [
        None,
        "",
        "gpt-4",
        "claude-",
        "claude-opus-5-pro",  # a non-numeric token AFTER the numeric run started
    ],
)
def test_display_name_derivation_unparseable_returns_none(model_id):
    assert _display_name_from_model_id(model_id) is None


# ---------------------------------------------------------------------------
# compute_missing_trailer_args, end to end against a real transcript file
# ---------------------------------------------------------------------------


def test_attaches_when_missing(repo, claude_home):
    _write_transcript(claude_home, _SID, [_assistant_record("claude-opus-5-5")])
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == [
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    ]


def test_no_duplicate_against_existing_anthropic_line_same_display(
    repo, claude_home
):
    _write_transcript(claude_home, _SID, [_assistant_record("claude-opus-5-5")])
    message = (
        "chore: already attributed\n\n"
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
    )
    argv = compute_missing_trailer_args(
        _msg_file(repo, message), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == []


def test_no_duplicate_against_migration_shape_different_display(repo, claude_home):
    """PM ruling: ANY existing Anthropic-addressed Co-Authored-By line -- even
    the old hand-typed '(1M context)' display -- counts as satisfied."""
    _write_transcript(claude_home, _SID, [_assistant_record("claude-opus-5-5")])
    message = (
        "chore: migration-era caller\n\n"
        "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>\n"
    )
    argv = compute_missing_trailer_args(
        _msg_file(repo, message), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == []


def test_different_address_line_stays_untouched(repo, claude_home):
    _write_transcript(claude_home, _SID, [_assistant_record("claude-sonnet-5")])
    different_author_line = "Co-Authored-By: A Human <human@example.com>"
    message = f"chore: pairing\n\n{different_author_line}\n"
    argv = compute_missing_trailer_args(
        _msg_file(repo, message), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == [
        "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
    ]

    from coordinator_core.git.commit_trailers import format_trailers_in_process

    values = trailer_values_from_argv(argv)
    rendered = format_trailers_in_process(message.encode("utf-8"), values).decode(
        "utf-8"
    )
    assert different_author_line in rendered
    assert "Claude Sonnet 5 <noreply@anthropic.com>" in rendered


def test_transcript_last_record_not_assistant_still_finds_earlier_one(
    repo, claude_home
):
    _write_transcript(
        claude_home,
        _SID,
        [
            _assistant_record("claude-haiku-4-5-20251001"),
            {"type": "user", "message": {"content": "thanks"}},
            {"type": "tool_result", "message": {}},
        ],
    )
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == [
        "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"
    ]


def test_no_session_id_no_trailer_no_failure(repo, claude_home, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    argv = compute_missing_trailer_args(_msg_file(repo, "chore: bare\n"), repo)
    assert _co_authored_by_lines(argv) == []


def test_no_transcript_no_trailer_no_failure(repo, claude_home):
    # claude_home's projects/ dir exists but carries no transcript for _SID.
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == []


def test_no_assistant_record_no_trailer_no_failure(repo, claude_home):
    _write_transcript(
        claude_home,
        _SID,
        [{"type": "user", "message": {"content": "hi"}}],
    )
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == []


def test_explicit_transcript_path_is_preferred_over_glob(repo, claude_home, tmp_path):
    # A transcript the session-id glob would find, carrying a DIFFERENT model
    # than one passed explicitly -- the explicit path must win.
    _write_transcript(claude_home, _SID, [_assistant_record("claude-sonnet-5")])
    explicit = tmp_path / "explicit.jsonl"
    explicit.write_text(
        json.dumps(_assistant_record("claude-opus-5-5")) + "\n", encoding="utf-8"
    )
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"),
        repo,
        session_id_override=_SID,
        transcript_path=str(explicit),
    )
    assert _co_authored_by_lines(argv) == [
        "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
    ]
