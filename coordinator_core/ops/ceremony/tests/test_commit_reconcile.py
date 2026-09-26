
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import coordinator_core.ops.ceremony.commit_reconcile as commit_reconcile_mod
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args, cwd) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags())


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
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _seed_commit_with_token(repo: Path, token: str, rel_path: str) -> str:
    _seed_file(repo, rel_path, "content\n")
    _git(["add", "--", rel_path], repo)
    _git(["commit", "-q", "-m", f"subject\n\nCommit-Token: {token}"], repo)
    return _rev_parse_head(repo)


def test_reconcile_recovers_the_sha_of_a_commit_that_landed_despite_failure(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    token = "ff4eeab2dc164987a6012ace2f05597e"
    landed_sha = _seed_commit_with_token(repo, token, "notes/alpha.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, f"Commit-Token: {token}", pre_sha
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec == f"{pre_sha}..HEAD"


def test_reconcile_never_adopts_a_peer_commit_in_the_same_window(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    _seed_commit_with_token(repo, "peertokenaaaaaaaaaaaaaaaaaaaaaaa", "notes/peer.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokenbbbbbbbbbbbbbbbbbbbbbbbb", pre_sha
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_returns_none_when_nothing_landed(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokencccccccccccccccccccccc", pre_sha
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
        repo, f"Commit-Token: {token}", None
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec == "HEAD"


def test_reconcile_fallback_window_still_never_adopts_a_peer_commit(tmp_path):
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _seed_commit_with_token(repo, "peertokeneeeeeeeeeeeeeeeeeeeeeee", "notes/peer.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo, "Commit-Token: ourtokenffffffffffffffffffffffff", None
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
        repo, f"Commit-Token: {token}", None
    )
    assert found.sha == landed_sha
    assert found.decline == ""
    assert found.range_spec != "HEAD"
    assert found.range_spec.endswith("..HEAD")


# walk, only the output -- see `_RECONCILE_FALLBACK_WINDOW_COMMITS`'s own


def test_reconcile_fallback_ignores_a_token_merely_quoted_in_a_message_body(tmp_path):
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
        repo, f"Commit-Token: {token}", None
    )
    assert found.sha is None
    assert found.decline == "no-candidate"


def test_reconcile_finds_our_commit_even_when_the_caller_named_an_untouched_path(tmp_path):
    """Pins the C2 fix (`docs/plans/2026-09-11-the-publisher-refuse-to-push-
    defect-re-verified.md`): the live production consumer
    (`coordinator-safe-commit.py :: _reconcile_after_indeterminate`) passes
    `args.paths` -- the operator's raw CLI pathspec -- which is NOT
    guaranteed to be the set of paths the commit actually touched (unchanged,
    gitignored, or a directory pathspec covering an untouched file all reach
    this call). Before the fix, the search was narrowed to a `git log --
    <pathspec>` filter derived from that untrustworthy path list, so naming
    only a path the commit never wrote made a genuinely-landed commit under
    a DIFFERENT path invisible, reported as `no-candidate` for a commit that
    in fact exists in history. The fix drops the pathspec/`commit_paths`
    parameter entirely -- the `Commit-Token:` trailer alone already bounds
    correctness (see the function's own SAFETY paragraph), so there is no
    longer any path argument for a caller to get wrong."""
    repo = _init_repo(tmp_path)
    _seed_file(repo, "README.md", "seed")
    _git(["add", "--", "README.md"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    pre_sha = _rev_parse_head(repo)

    token = "aaaa1111aaaa1111aaaa1111aaaa1111"
    landed_sha = _seed_commit_with_token(repo, token, "notes/actually-touched.md")

    found = commit_reconcile_mod._reconcile_landed_despite_failure(
        repo,
        f"Commit-Token: {token}",
        pre_sha,
    )
    assert found.sha == landed_sha
    assert found.decline == ""


def test_reconcile_regression_pre_sha_path_issues_exactly_one_git_log(tmp_path, monkeypatch):
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
        repo, "Commit-Token: nevercommittedaaaaaaaaaaaaaaaaaaaa", pre_sha
    )
    assert found.sha is None
    assert found.decline == "no-candidate"
    assert len(calls) == 1, "pre_sha-present path must issue exactly one git log, never a second"
