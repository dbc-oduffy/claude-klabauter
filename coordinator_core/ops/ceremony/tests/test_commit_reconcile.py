"""
coordinator_core.ops.ceremony.tests.test_commit_reconcile

Tests for `_reconcile_landed_despite_failure`, extracted from
`test_commit_pipeline.py` (lines 5369-5643 at the module's pre-delete HEAD)
alongside the C4 extraction of `commit_reconcile.py` out of the dying
`commit_pipeline.py` (docs/plans/2026-08-29-the-push-subsystem-leaves-and-
then-the-pipeline-can-go.md).

Coverage: the two predicate fixes (W3/W3b, `scoped_git_commit.py`) widened
what counts as landed, but could not help a `CommitOutcome` that says
`landed=False` in the first place. `_reconcile_landed_despite_failure` is
that repair, and these pin both halves of it: it must recover OUR commit,
and it must never adopt anyone else's. Live incident: 26ce6a671 (peer
1021e7bf, 2026-08-19).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_reconcile as commit_reconcile_mod

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    return repo


def _seed_file(repo: Path, rel_path: str, content: str) -> None:
    p = repo / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _rev_parse_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _seed_commit_with_token(repo: Path, token: str, rel_path: str) -> str:
    """Lands a real commit carrying `Commit-Token: <token>` and returns its
    sha -- the shape `_reconcile_landed_despite_failure` searches for."""
    _seed_file(repo, rel_path, "content\n")
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", f"subject\n\nCommit-Token: {token}"], repo)
    return _rev_parse_head(repo)


def test_reconcile_recovers_the_sha_of_a_commit_that_landed_despite_failure(tmp_path):
    """The repair: `git commit` reported failure (a timeout synthesizes
    `returncode=-1` in `git_native._git`) but the commit is really in
    `pre_sha..HEAD` under this call's own token, so the reconcile names it."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    token = "ff4eeab2dc164987a6012ace2f05597e"
    landed_sha = _seed_commit_with_token(repo, token, "notes/alpha.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, f"Commit-Token: {token}", pre_sha, ["notes/alpha.md"]
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec == f"{pre_sha}..HEAD"


def test_reconcile_never_adopts_a_peer_commit_in_the_same_window(tmp_path):
    """The safety property, and the one that matters on a shared branch: a
    peer commit landing in the SAME `pre_sha..HEAD` window carries a
    different token, so the reconcile must return None rather than claim it.
    Adopting it would report someone else's work as this call's own."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    _seed_commit_with_token(repo, "peertokenaaaaaaaaaaaaaaaaaaaaaaa", "notes/peer.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokenbbbbbbbbbbbbbbbbbbbbbbbb", pre_sha, ["notes/peer.md"]
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_returns_none_when_nothing_landed(tmp_path):
    """A genuine failure stays a failure -- the ordinary case, and the one
    that must not regress into a phantom success."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokencccccccccccccccccccccc", pre_sha, ["README.md"]
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_falls_back_to_a_bounded_window_without_a_pre_sha(tmp_path):
    """A missing `pre_sha` is a TIMED-OUT `git rev-parse HEAD`, not an absence
    of history -- and it fires under exactly the load that produces the defect
    the reconcile repairs, so declining there silences it when it is most
    needed (2026-08-19 investigation, suspect 1). With history shallower than
    `_RECONCILE_FALLBACK_WINDOW_COMMITS`, the `git rev-list --max-count` base
    probe cannot resolve a real base commit and the fallback searches the
    unbounded `HEAD` range instead (decline-safely, never a refusal) -- the
    token, not the range, is what makes the match safe."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    token = "dddddddddddddddddddddddddddddddd"
    landed_sha = _seed_commit_with_token(repo, token, "notes/alpha.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, f"Commit-Token: {token}", None, ["notes/alpha.md"]
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec == "HEAD"


def test_reconcile_fallback_window_still_never_adopts_a_peer_commit(tmp_path):
    """The safety property must survive the widening: with no `pre_sha` at all,
    a peer's commit sitting in the fallback window carries a different token, so
    the search still finds nothing. Widening the RANGE never widens what counts
    as ours."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_commit_with_token(repo, "peertokeneeeeeeeeeeeeeeeeeeeeeee", "notes/peer.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokenffffffffffffffffffffffff", None, ["notes/peer.md"]
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_fallback_resolves_a_real_bounded_base_when_history_exceeds_the_window(
    tmp_path, monkeypatch
):
    """The rev-list-bounded half of the no-`pre_sha` fallback: once history is
    deeper than `_RECONCILE_FALLBACK_WINDOW_COMMITS`, the fallback resolves a
    REAL `<base>..HEAD` range via an unfiltered `git rev-list --max-count`
    (a true walk bound, unlike a filtered `git log -n --grep`) instead of
    falling through to the unbounded-`HEAD` case the sibling test above
    covers. Window patched small so a handful of commits exercises it."""
    monkeypatch.setattr(commit_reconcile_mod, "_RECONCILE_FALLBACK_WINDOW_COMMITS", 3)
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    for i in range(5):
        _seed_file(repo, f"pad/{i}.md", "pad\n")
        _git(["add", "--", f"pad/{i}.md"], repo)
        _git(["commit", "-q", "-m", f"pad {i}"], repo)

    token = "eeee5555eeee5555eeee5555eeee5555"
    landed_sha = _seed_commit_with_token(repo, token, "notes/alpha.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, f"Commit-Token: {token}", None, ["notes/alpha.md"]
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec != "HEAD"
    assert found.range_spec.endswith("..HEAD")


# `test_reconcile_finds_a_commit_that_predates_its_own_pre_sha` (deleted): it
# pinned a WIDENED second `git log` pass on the `pre_sha`-present path,
# reached only when the bounded `pre_sha..HEAD` pass found nothing -- which
# includes the ordinary already-committed no-op, the commonest failure-path
# outcome there is, making that pass a near-full-history walk on the cheap
# common case (measured: a filtered `git log -n --grep` does not bound the
# walk, only the output -- see `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own
# comment). The shape it modelled -- this call's own commit landing OUTSIDE
# its own `pre_sha..HEAD` range -- was never an ordering fault inside
# `commit()`: `rev_parse_head()` genuinely always runs before
# `commit_scoped()`. The real cause was the warm-engine client re-executing
# an already-delivered mutation, so a SECOND execution read `pre_sha` AFTER a
# FIRST execution had already committed -- fixed at the root this session in
# `coordinator_core/warm/client.py`. With one execution per invocation,
# `pre_sha` is an ancestor of this call's own commit by construction, so the
# shape this test modelled can no longer occur, and the pass that defended
# against it is gone -- see `_reconcile_landed_despite_failure`'s own
# docstring for the full reasoning. See
# `test_reconcile_regression_pre_sha_path_issues_exactly_one_git_log` below
# for its replacement guard.


def test_reconcile_fallback_ignores_a_token_merely_quoted_in_a_message_body(tmp_path):
    """The one thing the fallback's wider-than-bounded search admits that the
    `pre_sha`-present path's plain substring match does not: a commit whose
    message QUOTES a token rather than carrying it as its own trailer --
    which this defect's own investigation notes do, repeatedly. The fallback
    anchors the match to a whole trailer line, so a quoted mention is not
    adopted.

    Without the anchor this test adopts the quoting commit and reports
    someone else's work as this call's own."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)

    token = "beef0000beef1111beef2222beef3333"
    _seed_file(repo, "notes/quoter.md", "content\n")
    _git(["add", "--", "notes/quoter.md"], repo)
    _git(
        [
            "commit", "-q", "-m",
            "investigation notes\n\nthe decline named `Commit-Token: %s` -- quoted, "
            "not ours" % token,
        ],
        repo,
    )

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, f"Commit-Token: {token}", None, ["notes/quoter.md"]
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_regression_pre_sha_path_issues_exactly_one_git_log(tmp_path, monkeypatch):
    """Regression guard for this finding: with `pre_sha` present, the reconcile
    must issue exactly ONE `git log` call and never fall through to a second,
    wider search -- the near-full-history walk this finding closed. Proven
    against the ordinary "nothing of ours landed" outcome, the commonest
    failure-path shape there is and the one the removed second pass used to
    run on every time."""
    from coordinator_core.ops.ceremony import git_native

    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    real_log_grep = git_native.log_grep
    calls: list = []

    def _spy(cwd, grep_pattern, *, extra_args=None):
        calls.append(extra_args)
        return real_log_grep(cwd, grep_pattern, extra_args=extra_args)

    monkeypatch.setattr(git_native, "log_grep", _spy)

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: nevercommittedaaaaaaaaaaaaaaaaaaaa", pre_sha, ["README.md"]
    )
    assert found.sha is None
    assert found.decline == "no-candidate"
    assert len(calls) == 1, "pre_sha-present path must issue exactly one git log, never a second"
