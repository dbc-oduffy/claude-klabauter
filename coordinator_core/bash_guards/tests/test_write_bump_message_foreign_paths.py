"""Tests for coordinator_core.bash_guards._write_bump_message -- the
"one reader, three repo paths" call site (task C4, docs/plans/2026-08-30-
the-engine-stops-naming-its-own-repo.md).

`_target_phrase` embeds `target_repo`/`raw_target`, and `render_em_message`/
`render_subagent_message` add `session_repo` on top -- three repo paths
rendered at one call site. Axis 3 splits them: `target_repo`/`raw_target`
are SUBJECT (the message's whole point is "you are writing there, not
here"), `session_repo` is the reader's OWN repo and is NOT-FOREIGN, so it is
untouched by the classification rule. This suite pins the declaring comment
at both call sites (so a future edit adding a fourth rendered path cannot
land silently, per the C3 "same shape" instruction) and the budget property
the module docstring's "BUDGET" section claims but does not itself test:
a backticked span inside the `_alternative_liveness` cue window costs
nothing against `MESSAGE_PROSE_CAP_BYTES`, regardless of how long the real
path is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import _write_bump_marker as marker
from coordinator_core.bash_guards import _write_bump_message as message
from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES, measure_envelope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(root: str, *args: str) -> None:
    import subprocess

    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(str(root), "init", "-q")
    _git(str(root), "config", "user.email", "t@example.com")
    _git(str(root), "config", "user.name", "Test")
    (root / "README.md").write_text("init\n", encoding="utf-8")
    _git(str(root), "add", "README.md")
    _git(str(root), "commit", "-q", "-m", "init")
    return root


_TARGET_REPO = "DoE-claude"
_SESSION_REPO = "claude-klabauter"
_SESSION_ID = "751ab9de-9319-4d63-b174-36145a4a3045"
_SANDBOX_ROOT = "state/subagent-share/751ab9de-9319-4d63-b174-36145a4a3045"

_SHORT_TARGET = "x-repo"
# test_write_bump_message.py's _LONG_RAW_MSYS_TOKEN, exercising the same
_LONG_TARGET = (
    "/c/Users/example-operator/AppData/Local/Temp/claude/X--claude-klabauter/a-very-long-"
    "synthetic-foreign-target-path-that-is-much-longer-than-x-repo.txt"
)


def _measure(text: str):
    envelope = {"hookSpecificOutput": {"permissionDecisionReason": text}}
    return measure_envelope(envelope)


def test_em_message_prose_bytes_stable_across_short_and_long_target(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    short_text = message.render_em_message(_SHORT_TARGET, _SESSION_REPO, gitdir, _SESSION_ID)
    long_text = message.render_em_message(_LONG_TARGET, _SESSION_REPO, gitdir, _SESSION_ID)
    short_measurement = _measure(short_text)
    long_measurement = _measure(long_text)
    assert short_measurement.prose_bytes == long_measurement.prose_bytes
    assert long_measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert long_measurement.over_cap is False


def test_subagent_message_prose_bytes_stable_across_short_and_long_target(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    short_text = message.render_subagent_message(
        _SHORT_TARGET, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    long_text = message.render_subagent_message(
        _LONG_TARGET, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    short_measurement = _measure(short_text)
    long_measurement = _measure(long_text)
    assert short_measurement.prose_bytes == long_measurement.prose_bytes
    assert long_measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert long_measurement.over_cap is False


# `session_repo` is NOT-FOREIGN -- it renders exactly once, in the untouched


def test_em_message_session_repo_renders_exactly_once(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert text.count(_SESSION_REPO) == 1
    assert f"(not `{_SESSION_REPO}`)" in text


def test_subagent_message_session_repo_renders_exactly_once(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert text.count(_SESSION_REPO) == 1
    assert f"(not `{_SESSION_REPO}`)" in text


# A3 (docs/plans/2026-09-26-commit-emit-plane-engine-findings.md, row B7):
# both foreign-class renderers point at `git ls-remote <remote>` to confirm
# the foreign repo's state before retrying, and both stay under the prose
# cap with the pointer included.


def test_em_message_carries_ls_remote_pointer_and_stays_under_cap(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_em_message(_TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID)
    assert "`git ls-remote <remote>`" in text
    measurement = _measure(text)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert measurement.over_cap is False


def test_subagent_message_carries_ls_remote_pointer_and_stays_under_cap(tmp_path):
    root = _init_repo(tmp_path)
    gitdir = marker.resolve_gitdir(str(root))
    text = message.render_subagent_message(
        _TARGET_REPO, _SESSION_REPO, gitdir, _SESSION_ID, _SANDBOX_ROOT
    )
    assert "`git ls-remote <remote>`" in text
    measurement = _measure(text)
    assert measurement.prose_bytes <= MESSAGE_PROSE_CAP_BYTES
    assert measurement.over_cap is False
