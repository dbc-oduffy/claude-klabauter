"""
coordinator_core.tests.test_git_scope — regression suite for the foreign-repo
git probing seam.

Incident of record (2026-08-03, reported by coordinator-claude-em): a cross-repo premise
check reported two shas with the identical "NOT in their clone" sentence. One was
genuinely dangling; the other resolved cleanly on their branch and on origin. Two
mechanical causes, both pinned here:

  1. `git cat-file -e` exits 128 for EVERY failure mode — malformed name, absent
     object, not-a-repo, missing path — so a `returncode == 0` reading has no
     third branch available to it even in principle.
  2. `git -C <path>` changes only the working directory. An inherited `GIT_DIR`
     still wins over repository discovery, retargeting the probe at whichever
     repo the process was launched from. git exports `GIT_DIR` to every hook it
     runs, so anything downstream of one inherits it — this is what fired in the
     wild.

The tests below are the executable form of both claims. `test_git_dir_poison_*`
in particular fails against the pre-fix shape (bare `subprocess.run(["git", "-C",
...])`), which is the point: it is the only test here that cannot be satisfied by
accident.

Run: python3 -m pytest coordinator_core/tests/test_git_scope.py -q
"""

from __future__ import annotations

import subprocess

import pytest

# Real git spawns throughout: the module under test wraps `git cat-file -e`
# exit-code collapsing and `GIT_DIR` env-poisoning, both of which are git's
# own process/env behaviour — no mock reproduces `GIT_DIR` inheritance or
# real object-database exit codes. Each test builds its own receiver/sender
# repo, so isolation is not hoisted to module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.git_scope import (
    PROBE_NO,
    PROBE_UNKNOWN,
    PROBE_YES,
    REPO_SCOPING_ENV_VARS,
    foreign_repo_unusable_reason,
    git_predicate,
    scoped_git_env,
)

_ABSENT_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"[:40]


def _git(*args: str, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False
    )


def _init_repo_with_commit(path, filename: str = "seed.txt") -> str:
    """git-init a fixture repo AND land one commit, returning its full sha.

    Every probe here interrogates an object database; a repo with no commits can
    answer nothing, so a fixture that wants an EARNED verdict rather than a
    could-not-check needs real history.
    """
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / filename).write_text("seed\n", encoding="utf-8")
    _git("add", filename, cwd=path)
    _git("commit", "-qm", "seed", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).stdout.strip()


@pytest.fixture
def receiver(tmp_path):
    """A real repo standing in for the FOREIGN repository being probed."""
    root = tmp_path / "receiver"
    sha = _init_repo_with_commit(root)
    if not sha:
        pytest.skip("git unavailable — cannot build a fixture clone with history")
    return root, sha


@pytest.fixture
def sender(tmp_path):
    """A second real repo standing in for OUR repo — the one a poisoned
    environment would silently retarget the probe at."""
    root = tmp_path / "sender"
    sha = _init_repo_with_commit(root, filename="other.txt")
    if not sha:
        pytest.skip("git unavailable — cannot build the second fixture clone")
    return root, sha


# ---------------------------------------------------------------------------
# scoped_git_env
# ---------------------------------------------------------------------------

def test_scoped_env_strips_every_repo_scoping_var():
    base = {var: "poison" for var in REPO_SCOPING_ENV_VARS}
    base["PATH"] = "/usr/bin"
    base["GIT_AUTHOR_NAME"] = "keep me"

    result = scoped_git_env(base)

    assert not (set(result) & set(REPO_SCOPING_ENV_VARS)), (
        "a repo-scoping var survived — `git -C` does not override these, so any "
        f"survivor reopens the retargeting hole: {sorted(set(result) & set(REPO_SCOPING_ENV_VARS))}"
    )
    assert result["PATH"] == "/usr/bin", "stripping must not empty the environment"
    assert result["GIT_AUTHOR_NAME"] == "keep me", (
        "identity vars are not repo-scoping and must survive — stripping them "
        "would break committing callers"
    )


def test_scoped_env_defaults_to_os_environ(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/somewhere/else/.git")
    monkeypatch.setenv("COORDINATOR_TEST_MARKER", "present")

    result = scoped_git_env()

    assert "GIT_DIR" not in result
    assert result.get("COORDINATOR_TEST_MARKER") == "present"


# ---------------------------------------------------------------------------
# git_predicate — the tri-state, all three states
# ---------------------------------------------------------------------------

def test_predicate_yes_for_a_resolvable_object(receiver):
    root, sha = receiver

    verdict, reason = git_predicate(root, ["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"])

    assert verdict == PROBE_YES, f"a sha the repo can resolve must read YES, got {verdict} ({reason})"
    assert reason == "", "a determinate verdict carries no could-not-check reason"


def test_predicate_no_for_an_absent_object_in_a_reachable_repo(receiver):
    """State 2 of 3. The tri-state must not soften a TRUE positive into a hedge —
    that would destroy the signal from the other direction, which is exactly as
    bad as the false positive the fix targets."""
    root, _sha = receiver

    verdict, reason = git_predicate(
        root, ["rev-parse", "--verify", "--quiet", f"{_ABSENT_SHA}^{{commit}}"]
    )

    assert verdict == PROBE_NO, (
        f"a reachable repo that genuinely lacks the object must read NO, got {verdict} ({reason})"
    )


def test_predicate_unknown_when_the_target_is_not_a_repo(tmp_path):
    """State 3 of 3, and the one the old `returncode != 0` shape could not
    express: git exits 128, which is NOT the same claim as exit 1."""
    not_a_repo = tmp_path / "plain-directory"
    not_a_repo.mkdir()

    verdict, reason = git_predicate(
        not_a_repo, ["rev-parse", "--verify", "--quiet", f"{_ABSENT_SHA}^{{commit}}"]
    )

    assert verdict == PROBE_UNKNOWN, (
        "a path that is not a git repo answered nothing — rendering that as NO "
        "is the defect this module exists to prevent"
    )
    assert reason, "an UNKNOWN must name git's own reason, or it is unreadable to an operator"


def test_predicate_unknown_when_the_target_path_does_not_exist(tmp_path):
    verdict, _reason = git_predicate(
        tmp_path / "no-such-directory", ["rev-parse", "--verify", "--quiet", "HEAD"]
    )
    assert verdict == PROBE_UNKNOWN


def test_predicate_merge_base_is_ancestor_does_not_conflate_1_with_128(receiver, sender):
    """`merge-base --is-ancestor` has the same 1-vs-128 conflation as cat-file.
    Exit 1 (definitely not an ancestor) and exit 128 (the shas are not in this
    object database at all) must land on different verdicts."""
    r_root, r_sha = receiver
    s_root, s_sha = sender

    # Both shas present, unrelated histories -> a DEFINITE negative.
    _git("fetch", "-q", str(s_root), s_sha, cwd=r_root)
    definite, _ = git_predicate(r_root, ["merge-base", "--is-ancestor", s_sha, r_sha])

    # A sha no repo has ever seen -> UNANSWERABLE, not a negative.
    unanswerable, reason = git_predicate(
        r_root, ["merge-base", "--is-ancestor", _ABSENT_SHA, r_sha]
    )

    assert unanswerable == PROBE_UNKNOWN, (
        "an absent commit makes the ancestry question unanswerable; git says 128, "
        "and 128 is not 'no'"
    )
    assert reason
    assert definite != PROBE_UNKNOWN, (
        "with both shas present the question IS answerable — the fix must not "
        f"hedge a determinate answer (got {definite})"
    )


# ---------------------------------------------------------------------------
# The environment-poisoning regressions — the wild failure mode
# ---------------------------------------------------------------------------

def test_git_dir_poison_does_not_retarget_the_predicate(monkeypatch, receiver, sender):
    """Direct regression for the reported false positive.

    With `GIT_DIR` inherited, an unscoped `git -C <receiver>` answers about the
    SENDER's object database while every log line still names the receiver. A
    sha that exists ONLY in the receiver then reads as definitively absent —
    indistinguishable from a genuinely dangling one.
    """
    r_root, r_sha = receiver
    s_root, _s_sha = sender
    monkeypatch.setenv("GIT_DIR", str(s_root / ".git"))

    # Baseline: prove the poison is live and would have flipped the verdict.
    unscoped = subprocess.run(
        ["git", "-C", str(r_root), "rev-parse", "--verify", "--quiet", f"{r_sha}^{{commit}}"],
        capture_output=True, text=True, check=False,
    )
    assert unscoped.returncode != 0, (
        "fixture is not exercising the defect — the poisoned environment must "
        "make a bare `git -C` probe fail to find the receiver's own HEAD"
    )

    verdict, reason = git_predicate(
        r_root, ["rev-parse", "--verify", "--quiet", f"{r_sha}^{{commit}}"]
    )

    assert verdict == PROBE_YES, (
        "an inherited GIT_DIR retargeted the probe and produced a false absence "
        f"claim about the receiver ({verdict}: {reason})"
    )


def test_git_dir_poison_is_reported_as_unusable_not_as_a_verdict(monkeypatch, receiver, sender):
    """The confinement half of the fix. Stripping the environment inside this
    process is not enough on its own — `foreign_repo_unusable_reason` must also
    be able to SAY that a resolved git dir does not belong to the target, since
    a `.git` file or ceiling-directory interaction can retarget discovery
    without any environment variable at all."""
    r_root, _ = receiver
    s_root, _ = sender
    monkeypatch.setenv("GIT_DIR", str(s_root / ".git"))

    assert foreign_repo_unusable_reason(r_root) is None, (
        "the receiver IS a usable repo; the poisoned environment must be "
        "stripped before the confinement check, not tripped over by it"
    )


def test_relative_git_dir_poison_does_not_make_a_real_repo_look_broken(monkeypatch, receiver):
    """git exports `GIT_DIR` to hooks as the relative `"."` in the common case.
    Unscoped, that makes every probe fail outright ("fatal: not a git
    repository: '.'"), which the old shape rendered as an absence claim."""
    r_root, r_sha = receiver
    monkeypatch.setenv("GIT_DIR", ".")

    assert foreign_repo_unusable_reason(r_root) is None
    verdict, _ = git_predicate(r_root, ["rev-parse", "--verify", "--quiet", f"{r_sha}^{{commit}}"])
    assert verdict == PROBE_YES


@pytest.mark.parametrize("var", REPO_SCOPING_ENV_VARS)
def test_no_repo_scoping_var_leaks_into_a_probe(monkeypatch, receiver, var):
    """Every variable in the strip list is stripped in practice, not just in the
    tuple. GIT_DIR is the one that fires in the wild; the rest are here so a
    later edit cannot quietly reopen the hole through a narrower door."""
    r_root, r_sha = receiver
    monkeypatch.setenv(var, "/nonexistent/poison")

    assert foreign_repo_unusable_reason(r_root) is None, (
        f"{var} survived into the usability probe"
    )
    verdict, reason = git_predicate(
        r_root, ["rev-parse", "--verify", "--quiet", f"{r_sha}^{{commit}}"]
    )
    assert verdict == PROBE_YES, f"{var} survived into the predicate probe ({reason})"


# ---------------------------------------------------------------------------
# foreign_repo_unusable_reason — the pre-probe gate
# ---------------------------------------------------------------------------

def test_unusable_reason_is_none_for_a_real_repo(receiver):
    root, _ = receiver
    assert foreign_repo_unusable_reason(root) is None


def test_unusable_reason_names_a_plain_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    reason = foreign_repo_unusable_reason(plain)
    assert reason, "a non-repo must be reported as unusable, never silently probed"


def test_unusable_reason_names_a_missing_path(tmp_path):
    reason = foreign_repo_unusable_reason(tmp_path / "absent")
    assert reason


def test_unusable_reason_rejects_an_empty_path():
    assert foreign_repo_unusable_reason("")


def test_unusable_reason_catches_a_git_dir_outside_the_target_tree(tmp_path, receiver):
    """The confinement check proper: a directory whose `.git` FILE points at
    another repo's git dir resolves cleanly and answers confidently about the
    wrong repository. No environment variable involved — this is why stripping
    the environment alone is insufficient."""
    r_root, _ = receiver
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    (impostor / ".git").write_text(f"gitdir: {r_root / '.git'}\n", encoding="utf-8")

    probe = subprocess.run(
        ["git", "-C", str(impostor), "rev-parse", "--absolute-git-dir"],
        capture_output=True, text=True, check=False, env=scoped_git_env(),
    )
    if probe.returncode != 0:
        pytest.skip("this git rejects the gitdir-file fixture; confinement is untestable here")

    reason = foreign_repo_unusable_reason(impostor)

    assert reason, (
        "a git dir outside the target's own tree means the probe would answer "
        "about the wrong repository — that must be reported, not trusted"
    )
    assert "outside" in reason


def test_module_never_raises_when_git_is_missing(monkeypatch, tmp_path):
    """A broken probe must degrade, never break its caller's ceremony."""
    monkeypatch.setenv("PATH", str(tmp_path))
    assert foreign_repo_unusable_reason(tmp_path)
    verdict, reason = git_predicate(tmp_path, ["rev-parse", "--verify", "--quiet", "HEAD"])
    assert verdict == PROBE_UNKNOWN
    assert reason


def test_probe_verdict_constants_are_three_distinct_values():
    """NO and UNKNOWN are different CLAIMS. If a later refactor ever aliases
    them, every caller's rendering collapses back to the 2026-08-03 defect."""
    assert len({PROBE_YES, PROBE_NO, PROBE_UNKNOWN}) == 3
