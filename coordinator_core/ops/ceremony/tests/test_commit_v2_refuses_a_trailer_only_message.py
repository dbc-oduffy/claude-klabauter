"""`ceremony.commit_v2` refuses a message with no subject line, BEFORE any
commit work -- issue #90 item 2.

Defect: this route (`commit_paths`' hand-rolled object-write plumbing) fires
no git hooks, so `apply_missing_trailers` below is the ONLY trailer-attach
point on it (see that call's own comment). A caller that supplied an empty
message, or a message that was itself only a trailer line (e.g.
"Session-Id: <uuid>"), used to sail through unrefused: `apply_missing_
trailers` appended more trailers on top and the resulting commit's message
was ONLY trailers -- no subject for `git log --oneline`, blame, bisect, or
chunk-id registration to read (measured: example-retrieval-repo commit a268182b).

These pin the refusal at the handler boundary: checked on the message AS
SUPPLIED, never on the message after trailers are appended (an already-
trailered message would misread its own appended block as a "subject").
No real git repo is needed -- the refusal fires before `main_worktree_root`
or any git read, so `repo_root` here is a throwaway path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import commit_v2


def _call(message, tmp_path: Path):
    params = {"paths": ["README.md"], "message": message}
    return commit_v2._handler(params, repo_root=tmp_path / ".git")


def test_an_empty_message_is_refused(tmp_path: Path):
    out = _call("", tmp_path)
    assert out["committed"] is False
    assert "message" in out["error"]


def test_a_whitespace_only_message_is_refused(tmp_path: Path):
    out = _call("   \n  \n", tmp_path)
    assert out["committed"] is False
    assert "message" in out["error"]


def test_a_trailer_only_message_is_refused(tmp_path: Path):
    out = _call("Session-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n", tmp_path)
    assert out["committed"] is False, out
    assert "no subject" in out["error"], out
    assert "trailer" in out["error"], out


def test_a_multi_line_trailer_only_message_is_still_refused(tmp_path: Path):
    """The trailer-only shape does not require a single line -- a message
    whose first non-blank line is a trailer is refused even when further
    trailer lines follow it, since there is still no subject anywhere."""
    out = _call(
        "Session-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n"
        "Deliverable-Id: some-deliverable\n",
        tmp_path,
    )
    assert out["committed"] is False, out
    assert "no subject" in out["error"], out


def test_a_normal_message_is_not_refused_by_this_check(tmp_path: Path, monkeypatch):
    """A real subject line must reach past this refusal -- proven by patching
    the next seam (`commit_paths`) to raise a marker exception rather than
    landing a real commit, mirroring `test_commit_v2_session_id_override.py`'s
    stop-after-capture pattern. Reaching `commit_paths` at all is the proof:
    this refusal would have returned a structured `_error` instead."""

    def _stop(*args, **kwargs):
        raise AssertionError("stop-after-refusal-check")

    monkeypatch.setattr(commit_v2, "commit_paths", _stop)

    with pytest.raises(AssertionError, match="stop-after-refusal-check"):
        _call("fix: the frobnicator\n", tmp_path)


def test_a_normal_message_with_a_trailing_trailer_block_is_not_refused(
    tmp_path: Path, monkeypatch
):
    """A properly-shaped message (subject, then a trailer block at the end)
    must not be caught by this refusal -- only the FIRST non-blank line
    decides, never a trailer block appearing later in the message."""

    def _stop(*args, **kwargs):
        raise AssertionError("stop-after-refusal-check")

    monkeypatch.setattr(commit_v2, "commit_paths", _stop)

    with pytest.raises(AssertionError, match="stop-after-refusal-check"):
        _call(
            "fix: the frobnicator\n\nSession-Id: 6ab7b0d8-1234-4a12-9abc-1234567890ab\n",
            tmp_path,
        )
