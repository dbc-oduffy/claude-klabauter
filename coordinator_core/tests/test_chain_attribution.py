"""
coordinator_core.tests.test_chain_attribution

Path-scoped tests for coordinator_core.chain_attribution (P2), exercised
against real git fixtures (temp repos with real commits), matching the idiom
coordinator_core/tests/test_session_attribution.py already uses for the
sibling P1 classifier.

Coverage:
  - bulk_commit_attribution_map: untrailered-vs-absent-vs-foreign-trailer
    three-way distinction; merge detection; multi-valued trailer ambiguity;
    GitLogFailed propagation on a git failure (never swallowed to empty).
  - bulk_grep_attributed_shas: --no-merges load-bearing; malformed session_id
    (_UUID_RE) returns empty rather than over-matching; git failure returns
    empty (fail-closed posture for the grep leg specifically).
  - foreign_shas_from_window: pure in-memory derivation, no git calls.
  - unattributed_foreign_shas: per-range convenience form, cache behaviour.
  - P1/P2 subset-relation smoke check (AC6): P1(range, sid) subset of
    P2(range, sid) for a small corpus of real commits in a fixture repo.

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a
task A1.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core import chain_attribution, session_attribution
from coordinator_core.session_attribution import GitLogFailed

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.spawns_process]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git"] + args, cwd=str(cwd), capture_output=True, check=True)


def _init_repo(root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], root)
    _git(["config", "user.email", "test@test.com"], root)
    _git(["config", "user.name", "Test"], root)
    _git(["config", "commit.gpgsign", "false"], root)
    return _commit(root, "init", files={".gitkeep": "init\n"})


def _commit(
    root: Path,
    message: str,
    *,
    files: Optional[dict] = None,
    date: Optional[str] = None,
) -> str:
    if files:
        for rel_path, content in files.items():
            path = root / rel_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    _git(["add", "-A"], root)
    env = None
    if date is not None:
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = date
        env["GIT_COMMITTER_DATE"] = date
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(root), capture_output=True, check=True, env=env,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def _run(args, cwd):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True,
    )
    return result.returncode, result.stdout, result.stderr


@pytest.fixture
def repo_root(tmp_path) -> Path:
    return tmp_path / "repo"


# ---------------------------------------------------------------------------
# bulk_commit_attribution_map
# ---------------------------------------------------------------------------


def test_untrailered_commit_is_present_with_none_not_absent(repo_root):
    init_sha = _init_repo(repo_root)
    untrailered_sha = _commit(
        repo_root, "no trailer here", files={"a.txt": "a\n"},
    )

    window = chain_attribution.bulk_commit_attribution_map(
        f"{init_sha}..HEAD", str(repo_root), _run,
    )
    assert untrailered_sha in window
    assert window[untrailered_sha].trailer_session_id is None
    assert window[untrailered_sha].is_merge is False
    assert window[untrailered_sha].trailer_ambiguous is False


def test_sha_outside_window_is_absent_from_map(repo_root):
    init_sha = _init_repo(repo_root)
    in_window_sha = _commit(repo_root, "in window", files={"a.txt": "a\n"})

    window = chain_attribution.bulk_commit_attribution_map(
        f"{init_sha}..HEAD", str(repo_root), _run,
    )
    assert init_sha not in window
    assert in_window_sha in window


def test_foreign_trailer_is_recorded_verbatim(repo_root):
    sid = "chain-attr-001"
    init_sha = _init_repo(repo_root)
    theirs_sha = _commit(
        repo_root, f"their work\n\nSession-Id: {sid}", files={"a.txt": "a\n"},
    )

    window = chain_attribution.bulk_commit_attribution_map(
        f"{init_sha}..HEAD", str(repo_root), _run,
    )
    assert window[theirs_sha].trailer_session_id == sid
    assert window[theirs_sha].trailer_ambiguous is False


def test_merge_commit_is_detected(repo_root):
    init_sha = _init_repo(repo_root)
    _commit(repo_root, "base", files={"base.txt": "base\n"})
    _git(["checkout", "-b", "feature"], repo_root)
    _commit(repo_root, "feature work", files={"feature.txt": "feature\n"})
    _git(["checkout", "main"], repo_root)
    _commit(repo_root, "main work", files={"main.txt": "main\n"})
    subprocess.run(
        ["git", "merge", "feature", "--no-edit"],
        cwd=str(repo_root), capture_output=True, check=True,
    )
    merge_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()

    window = chain_attribution.bulk_commit_attribution_map(
        f"{init_sha}..HEAD", str(repo_root), _run,
    )
    assert merge_sha in window, "walk must see merges — do not add --no-merges here"
    assert window[merge_sha].is_merge is True


def test_multi_valued_trailer_is_flagged_ambiguous(repo_root):
    init_sha = _init_repo(repo_root)
    ambiguous_sha = _commit(
        repo_root,
        "two trailers\n\nSession-Id: sid-one\nSession-Id: sid-two",
        files={"a.txt": "a\n"},
    )

    window = chain_attribution.bulk_commit_attribution_map(
        f"{init_sha}..HEAD", str(repo_root), _run,
    )
    assert window[ambiguous_sha].trailer_ambiguous is True


def test_gitlogfailed_propagates_not_swallowed(repo_root):
    _init_repo(repo_root)

    def _failing_run(args, cwd):
        return 1, "", "simulated git failure"

    with pytest.raises(GitLogFailed):
        chain_attribution.bulk_commit_attribution_map(
            "HEAD..HEAD", str(repo_root), _failing_run,
        )


# ---------------------------------------------------------------------------
# bulk_grep_attributed_shas
# ---------------------------------------------------------------------------


def test_grep_attributes_matching_session(repo_root):
    sid = "abcd1234-0000-0000-0000-000000000001"
    init_sha = _init_repo(repo_root)
    own_sha = _commit(
        repo_root, f"own work\n\nSession-Id: {sid}", files={"a.txt": "a\n"},
    )

    attributed = chain_attribution.bulk_grep_attributed_shas(
        f"{init_sha}..HEAD", sid, str(repo_root), _run,
    )
    assert own_sha in attributed


def test_grep_excludes_merges(repo_root):
    sid = "abcd1234-0000-0000-0000-000000000002"
    init_sha = _init_repo(repo_root)
    _commit(repo_root, "base", files={"base.txt": "base\n"})
    _git(["checkout", "-b", "feature2"], repo_root)
    _commit(repo_root, "feature work", files={"feature.txt": "feature\n"})
    _git(["checkout", "main"], repo_root)
    _commit(repo_root, "main work", files={"main.txt": "main\n"})
    subprocess.run(
        ["git", "merge", "feature2", "--no-edit", "-m", f"merge\n\nSession-Id: {sid}"],
        cwd=str(repo_root), capture_output=True, check=True,
    )
    merge_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()

    attributed = chain_attribution.bulk_grep_attributed_shas(
        f"{init_sha}..HEAD", sid, str(repo_root), _run,
    )
    assert merge_sha not in attributed, "grep leg must keep --no-merges"


def test_grep_rejects_malformed_session_id(repo_root):
    init_sha = _init_repo(repo_root)
    _commit(repo_root, "work", files={"a.txt": "a\n"})

    attributed = chain_attribution.bulk_grep_attributed_shas(
        f"{init_sha}..HEAD", ".*", str(repo_root), _run,
    )
    assert attributed == frozenset(), (
        "an unvalidated session_id like '.*' must not be interpolated into --grep"
    )


def test_grep_git_failure_returns_empty(repo_root):
    _init_repo(repo_root)
    sid = "abcd1234-0000-0000-0000-000000000003"

    def _failing_run(args, cwd):
        return 1, "", "simulated failure"

    attributed = chain_attribution.bulk_grep_attributed_shas(
        "HEAD..HEAD", sid, str(repo_root), _failing_run,
    )
    assert attributed == frozenset()


# ---------------------------------------------------------------------------
# foreign_shas_from_window — pure in-memory
# ---------------------------------------------------------------------------


def test_foreign_shas_from_window_pure_no_git():
    window = {
        "own-trailer": chain_attribution.CommitAttribution("own-trailer", "sid-mine", False),
        "foreign-trailer": chain_attribution.CommitAttribution("foreign-trailer", "sid-other", False),
        "untrailered-grep-hit": chain_attribution.CommitAttribution("untrailered-grep-hit", None, False),
        "untrailered-no-grep": chain_attribution.CommitAttribution("untrailered-no-grep", None, False),
        "merge": chain_attribution.CommitAttribution("merge", "sid-mine", True),
        "ambiguous": chain_attribution.CommitAttribution("ambiguous", "sid-mine", False, trailer_ambiguous=True),
    }
    grep_attributed = frozenset({"untrailered-grep-hit"})
    shas = list(window.keys()) + ["outside-window"]

    foreign = chain_attribution.foreign_shas_from_window(
        shas, "sid-mine", window, grep_attributed,
    )

    assert "own-trailer" not in foreign
    assert "foreign-trailer" in foreign
    assert "untrailered-grep-hit" not in foreign
    assert "untrailered-no-grep" in foreign
    assert "merge" in foreign
    assert "ambiguous" in foreign
    assert "outside-window" in foreign


# ---------------------------------------------------------------------------
# unattributed_foreign_shas — per-range convenience form
# ---------------------------------------------------------------------------


def test_unattributed_foreign_shas_caches_by_key(repo_root):
    sid = "abcd1234-0000-0000-0000-000000000004"
    init_sha = _init_repo(repo_root)
    _commit(repo_root, f"own work\n\nSession-Id: {sid}", files={"a.txt": "a\n"})

    cache: dict = {}
    call_count = {"n": 0}

    def _counting_run(args, cwd):
        call_count["n"] += 1
        return _run(args, cwd)

    range_str = f"{init_sha}..HEAD"
    first = chain_attribution.unattributed_foreign_shas(
        range_str, sid, str(repo_root), cache, _counting_run,
    )
    calls_after_first = call_count["n"]
    second = chain_attribution.unattributed_foreign_shas(
        range_str, sid, str(repo_root), cache, _counting_run,
    )

    assert first == second
    assert call_count["n"] == calls_after_first, "second call must be served from cache"


def test_unattributed_foreign_shas_gitlogfailed_propagates(repo_root):
    _init_repo(repo_root)

    def _failing_run(args, cwd):
        return 1, "", "simulated failure"

    with pytest.raises(GitLogFailed):
        chain_attribution.unattributed_foreign_shas(
            "HEAD..HEAD", "sid", str(repo_root), {}, _failing_run,
        )


# ---------------------------------------------------------------------------
# AC6 — P1(range, sid) subset of P2(range, sid)
# ---------------------------------------------------------------------------


def test_p1_foreign_set_is_subset_of_p2_foreign_set(repo_root):
    sid = "abcd1234-0000-0000-0000-000000000005"
    other_sid = "abcd1234-0000-0000-0000-000000000006"
    init_sha = _init_repo(repo_root)
    _commit(repo_root, f"own work\n\nSession-Id: {sid}", files={"a.txt": "a\n"})
    _commit(repo_root, f"their work\n\nSession-Id: {other_sid}", files={"b.txt": "b\n"})
    _commit(repo_root, "untrailered work", files={"c.txt": "c\n"})

    range_str = f"{init_sha}..HEAD"

    p1_foreign = session_attribution.trailer_foreign_shas(
        range_str, sid, str(repo_root), {}, _run,
    )
    p2_foreign = chain_attribution.unattributed_foreign_shas(
        range_str, sid, str(repo_root), {}, _run,
    )

    assert p1_foreign <= p2_foreign, (
        f"AC6 violated: P1 foreign set {p1_foreign} not a subset of "
        f"P2 foreign set {p2_foreign}"
    )
