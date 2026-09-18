"""`ceremony.commit_v2` accepts an explicit `session_id` and threads it into
`apply_missing_trailers` as `session_id_override`.

The SEMANTICS of the override (precedence over the env ladder, byte-identity
of the resulting trailer) are `apply_missing_trailers`'/`compute_missing_
trailer_args`'s own and are pinned in `git/tests/test_commit_trailers*.py` --
what commit_v2 added is plumbing plus a shape refusal, so that is what these
assert (same posture as `test_commit_v2_prefer_deliberate_stage.py`).

Reported by coordinator-claude#52a: dispatched git-commit-agent commits were
landing with no Session-Id trailer under the env-ladder alone (1 of 68 on one
run), blinding the review-brightline gate. A caller that already knows its
own session id needed a way to say so explicitly.
"""

from __future__ import annotations

import pytest

from coordinator_core.ops.ceremony import commit_v2

VALID_UUID = "0b4efa23-3132-4861-9b79-4bbfa64c0e17"


def _spy_apply_missing_trailers(monkeypatch):
    """Capture the kwargs commit_v2 hands `apply_missing_trailers`, and stop
    there -- mirrors `test_commit_v2_prefer_deliberate_stage.py`'s
    `_spy(commit_paths)` pattern, one seam earlier."""
    seen: dict = {}

    def fake_apply_missing_trailers(*args, **kwargs):
        seen.update(kwargs)
        raise AssertionError("stop-after-capture")

    monkeypatch.setattr(
        commit_v2, "apply_missing_trailers", fake_apply_missing_trailers
    )
    return seen


def _call(repo_root, params):
    return commit_v2._handler(params, repo_root=repo_root)


def test_valid_session_id_reaches_apply_missing_trailers(monkeypatch, tmp_path):
    seen = _spy_apply_missing_trailers(monkeypatch)
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(
            tmp_path / ".git",
            {"paths": ["a.md"], "message": "m", "session_id": VALID_UUID},
        )
    assert seen["session_id_override"] == VALID_UUID


def test_absent_session_id_is_unchanged(monkeypatch, tmp_path):
    """Negative spec -- absent `session_id`, `session_id_override` is `None`,
    which is `apply_missing_trailers`' own documented "fall back to the env
    ladder" value; behaviour is byte-identical to before this parameter
    existed."""
    seen = _spy_apply_missing_trailers(monkeypatch)
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(tmp_path / ".git", {"paths": ["a.md"], "message": "m"})
    assert seen["session_id_override"] is None


@pytest.mark.parametrize(
    "bad_session_id",
    [
        "not-a-uuid",
        "0b4efa23-3132-4861-9b79-4bbfa64c0e1",  # one hex short
        "0b4efa23313248619b794bbfa64c0e17",  # missing hyphens
        "0b4efa23-3132-4861-9b79-4bbfa64c0e1g",  # non-hex char
        123,
        "",
    ],
)
def test_malformed_session_id_is_refused_nothing_committed(
    monkeypatch, tmp_path, bad_session_id
):
    """A malformed `session_id` refuses the whole call, naming the key --
    same shape as every other structured `_error()` refusal in this op --
    and never reaches `apply_missing_trailers` or `commit_paths`."""

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("must not be called on a malformed session_id")

    monkeypatch.setattr(
        commit_v2, "apply_missing_trailers", _must_not_be_called
    )
    monkeypatch.setattr(commit_v2, "commit_paths", _must_not_be_called)

    result = _call(
        tmp_path / ".git",
        {"paths": ["a.md"], "message": "m", "session_id": bad_session_id},
    )
    assert result["committed"] is False
    assert result["sha"] is None
    assert "session_id" in result["error"]


def test_case_insensitive_uuid_is_accepted(monkeypatch, tmp_path):
    """The validation is case-insensitive, matching `_UUID_RE`'s own
    `[0-9a-fA-F]` classes."""
    seen = _spy_apply_missing_trailers(monkeypatch)
    upper = VALID_UUID.upper()
    with pytest.raises(AssertionError, match="stop-after-capture"):
        _call(
            tmp_path / ".git",
            {"paths": ["a.md"], "message": "m", "session_id": upper},
        )
    assert seen["session_id_override"] == upper
