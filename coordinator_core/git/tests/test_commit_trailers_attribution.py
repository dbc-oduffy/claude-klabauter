"""Co-Authored-By is an ENGINE trailer, not caller boilerplate (PM ruling,
state/cross-repo/inbox/2026-09-23-example-game-repo-em-commit-trailers-owned-by-
engine.md), and it is the constant `Claude <noreply@anthropic.com>` --
model-free, so a dispatched commit agent cannot stamp its own model
(klabauter#67) and a repo forbidding model identifiers is never violated.

Covers: attach when missing; no duplicate against the constant; replacement
of any other Anthropic-addressed line (model-named, via both the file and
the message entry points); a different-address line stays untouched;
attached with no session id at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git.commit_trailers import (
    ATTRIBUTION_TRAILER_VALUE,
    apply_missing_trailers,
    compute_missing_trailer_args,
    format_trailers_in_process,
    trailer_values_from_argv,
)
from coordinator_core.win_portability import no_console_creationflags

# compute_missing_trailer_args resolves the git-dir via a real `git
# rev-parse`, same as every sibling suite in this package.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SID = "56565656-5656-4565-8565-565656565656"
_LINE = f"Co-Authored-By: {ATTRIBUTION_TRAILER_VALUE}"


@pytest.fixture
def repo(tmp_path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=str(r),
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )
    return r


def _msg_file(repo: Path, text: str) -> Path:
    p = repo / "MSG"
    p.write_text(text, encoding="utf-8")
    return p


def _co_authored_by_lines(argv) -> list:
    return [
        v for v in trailer_values_from_argv(argv) if v.lower().startswith("co-authored-by:")
    ]


def test_constant_is_model_free():
    assert ATTRIBUTION_TRAILER_VALUE == "Claude <noreply@anthropic.com>"


def test_attaches_when_missing(repo):
    argv = compute_missing_trailer_args(
        _msg_file(repo, "chore: bare\n"), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == [_LINE]


def test_attaches_without_a_session_id(repo, monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    argv = compute_missing_trailer_args(_msg_file(repo, "chore: bare\n"), repo)
    assert _co_authored_by_lines(argv) == [_LINE]


def test_no_duplicate_against_the_constant(repo):
    msg = _msg_file(repo, f"chore: already attributed\n\n{_LINE}\n")
    argv = compute_missing_trailer_args(msg, repo, session_id_override=_SID)
    assert _co_authored_by_lines(argv) == []
    assert msg.read_text(encoding="utf-8") == f"chore: already attributed\n\n{_LINE}\n"


@pytest.mark.parametrize(
    "model_line",
    [
        "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>",
        "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>",
    ],
)
def test_model_named_line_is_replaced(repo, model_line):
    msg = _msg_file(
        repo, f"chore: agent-written\n\n{model_line}\nSession-Id: {_SID}\n"
    )
    argv = compute_missing_trailer_args(msg, repo, session_id_override=_SID)
    assert _co_authored_by_lines(argv) == [_LINE]
    assert msg.read_text(encoding="utf-8") == f"chore: agent-written\n\nSession-Id: {_SID}\n"


def test_apply_missing_trailers_replaces_model_named_line(repo):
    out = apply_missing_trailers(
        "chore: agent-written\n\n"
        "Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>\n",
        repo,
        session_id_override=_SID,
    )
    assert "Haiku" not in out
    assert out.count("<noreply@anthropic.com>") == 1
    assert _LINE in out


def test_different_address_line_stays_untouched(repo):
    human = "Co-Authored-By: A Human <human@example.com>"
    message = f"chore: pairing\n\n{human}\n"
    argv = compute_missing_trailer_args(
        _msg_file(repo, message), repo, session_id_override=_SID
    )
    assert _co_authored_by_lines(argv) == [_LINE]

    rendered = format_trailers_in_process(
        message.encode("utf-8"), trailer_values_from_argv(argv)
    ).decode("utf-8")
    assert human in rendered
    assert _LINE in rendered
