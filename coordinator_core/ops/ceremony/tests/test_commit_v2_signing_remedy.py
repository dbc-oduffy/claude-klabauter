"""klabauter#34 / DR-308: a signing failure must never block a commit from
landing (`docs/decisions/DR-308-signed-commit-enforcement-is-declined-as.md`
-- signing stays, enforcement does not).

`ceremony.commit_v2` used to enrich a `SigningFailed`-shaped `CommitRefused`
with an operator remedy -- `_SIGNING_FAILURE_PREFIX`/`_SIGNING_REMEDY` -- on
the theory that `commit_paths` could refuse a commit over a signing failure.
It cannot: `commit_paths` (`coordinator_core/git/commit.py`) and the
`commit_signing` module it now shares that contract with
(`coordinator_core/git/commit_signing.py::write_signed_commit_object`) both
land the commit unsigned and report why via `CommitOutcome.sign_warning`
(pinned end to end, including the real-signing-failure shape, by
`coordinator_core/git/tests/test_commit_gpgsign.py`). No raise site this
handler's `except (CommitRefused, FilterUnsupported)` can reach carries a
signing-shaped message any more, so the enrichment branch was dead code and
was deleted rather than kept for a shape that cannot occur.

This file pins the narrower fact that survives the deletion: an ordinary
`CommitRefused` -- signing-shaped text included -- passes through this
handler completely unmodified, with no remedy text appended to it. Pattern
follows `test_commit_v2_prefer_deliberate_stage.py`: `commit_paths` is
monkeypatched to raise directly, so no real git repo is needed --
`main_worktree_root` is a pure path computation over `repo_root`.
"""

from __future__ import annotations

from coordinator_core.git.commit import CommitRefused, FilterUnsupported
from coordinator_core.ops.ceremony import commit_v2


def _call(repo_root, params):
    return commit_v2._handler(params, repo_root=repo_root)


def _raise(exc: Exception):
    def _fake_commit_paths(*args, **kwargs):
        raise exc
    return _fake_commit_paths


def test_a_signing_shaped_message_is_never_enriched(monkeypatch, tmp_path):
    """Text that would have matched the old `_SIGNING_FAILURE_PREFIX` still
    passes through byte-for-byte -- no remedy is appended, because nothing
    raises this shape any more and the carve-out that used to react to it
    is gone."""
    signing_shaped_message = (
        "signed commit refused -- `git commit-tree -S` failed: "
        "gpg: signing failed: No secret key. "
        "Nothing was written and HEAD is unmoved."
    )
    monkeypatch.setattr(
        commit_v2, "commit_paths", _raise(CommitRefused(signing_shaped_message))
    )

    result = _call(tmp_path / ".git", {"paths": ["a.md"], "message": "m"})

    assert result["committed"] is False
    assert result["sha"] is None
    assert result["error"] == signing_shaped_message
    assert "operator" not in result["error"]
    assert "--no-verify" not in result["error"]
    assert "--no-gpg-sign" not in result["error"]


def test_non_signing_refusal_is_passed_through_unmodified(monkeypatch, tmp_path):
    other_message = "cannot read a/b.txt: [Errno 2] No such file or directory"
    monkeypatch.setattr(
        commit_v2, "commit_paths", _raise(CommitRefused(other_message))
    )

    result = _call(tmp_path / ".git", {"paths": ["a/b.txt"], "message": "m"})

    assert result["error"] == other_message


def test_filter_unsupported_is_passed_through_unmodified(monkeypatch, tmp_path):
    other_message = "eol=crlf path carries CR bytes and no fallback resolved it"
    monkeypatch.setattr(
        commit_v2, "commit_paths", _raise(FilterUnsupported(other_message))
    )

    result = _call(tmp_path / ".git", {"paths": ["a.md"], "message": "m"})

    assert result["error"] == other_message
