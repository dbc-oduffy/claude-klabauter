"""Tests for coordinator_core.ops.orphan_branch_sweep.

Golden oracle verified byte-parity against the DoE-side plugin-ecosystem test
suite (coordinator/tests/plugin-ecosystem/orphan-sweep.test.js —
CRITICAL/WARNING/OK classification with a stubbed `gh`) during the
2026-07-17 BIG_PORT port.

Port of: orphan-branch-sweep.sh (DoE b5a4192c, 2026-07-20)

Regression coverage: `test_unmerged_count_uses_separate_revs` locks in a P1 bug
found and fixed during this port — `git rev-list --count "<sha> ^<ref>"` (one
combined string) raises "ambiguous argument"; git requires the tip SHA and the
`^ref` exclusion as SEPARATE argv entries. The bash oracle already passed them
as two words (unquoted `${tip_sha} ^main`, which bash arg-splits at the space);
the first Python draft accidentally combined them into one f-string and silently
returned 0 (unmerged count), which the CRITICAL classifier reads as "cannot ever
be orphaned" — misclassifying every merged-with-trailing-commits branch as OK.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops import orphan_branch_sweep as obs
from coordinator_core.ops.orphan_branch_sweep import (
    main,
    _rev_list_count,
    _severity_rank,
    compute_descendant_tip,
    detect_unpushed_commits,
    list_unmerged_work_branches,
    verify_commit_in_review_window,
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )


def _init_repo(tmp_path: Path) -> Path:
    """Init a tmp git repo with a LOCAL identity that matches whatever email
    will ACTUALLY land on commits made in it during this test, not an
    arbitrary literal.

    CORRECTION (2026-07-21): coordinator_core/conftest.py's suite-root
    autouse ``_quarantine_real_home`` fixture forces ``GIT_AUTHOR_EMAIL`` /
    ``GIT_COMMITTER_EMAIL`` env vars for every test (so git-touching tests
    keep working without a real ``~/.gitconfig`` once ``HOME`` is
    quarantined) -- and a ``GIT_AUTHOR_EMAIL``/``GIT_COMMITTER_EMAIL`` env var
    wins over local ``git config user.email`` for actual commit authorship.
    ``orphan_branch_sweep.main()``'s per-branch author-identity filter (a
    genuine, intentional production feature -- only surface the CALLING
    USER's own ``work/*``/``feature/*`` branches) compares against
    ``git config user.email`` (this repo's LOCAL config, unaffected by the env
    override). Hardcoding a literal email here that differed from the
    conftest-forced identity silently made every branch fail that filter and
    ``main()`` emit nothing at all -- not a "branch sprawl" or
    live-repo-state problem (Cluster F's original suspicion), but a
    test-fixture identity collision between two independently-authored pieces
    of test infrastructure. Reading the SAME env var conftest.py sets (at
    call time, i.e. after its autouse fixture has already run for this test
    -- NOT a module-level constant, which would freeze in the pre-fixture
    value at collection time) keeps the local config and actual commit
    authorship in sync by construction, so this test's author-based
    assertions are deterministic regardless of what identity the surrounding
    test harness forces, and regardless of whether this file is ever run
    outside this suite's conftest (falls back to a literal when unset).
    """
    email = os.environ.get("GIT_AUTHOR_EMAIL", "test@example.com")
    name = os.environ.get("GIT_AUTHOR_NAME", "Test")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", email)
    _git(repo, "config", "user.name", name)
    (repo / "f.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _commit(repo: Path, name: str, msg: str) -> None:
    (repo / name).write_text(msg)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", msg)


# ---------------------------------------------------------------------------
# severity_rank
# ---------------------------------------------------------------------------


def test_severity_rank_ordering():
    assert _severity_rank("OK") == 0
    assert _severity_rank("WARNING") == 1
    assert _severity_rank("CRITICAL") == 2
    assert _severity_rank("garbage") == 0


# ---------------------------------------------------------------------------
# CLI surface: --help, unknown arg, not-a-git-repo
# ---------------------------------------------------------------------------


def test_help_exits_zero(capsys):
    rc = main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--format" in out


def test_unknown_arg_exits_one(capsys):
    rc = main(["--bogus"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Unknown argument" in err


def test_outside_git_repo_exits_zero_silent(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""


# ---------------------------------------------------------------------------
# rev-list --count argv shape (the P1 fix)
# ---------------------------------------------------------------------------


def test_unmerged_count_uses_separate_revs(tmp_path, monkeypatch):
    """`git rev-list --count <sha> ^<ref>` MUST be two argv entries — not one
    combined string ("<sha> ^<ref>"), which git rejects as an ambiguous
    argument and which subprocess.run (list-argv, no shell) would otherwise
    silently mis-pass as a single unresolvable token."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Two-arg form (correct) succeeds and returns a real count.
    count = _rev_list_count(tip, "^main")
    assert count == 0  # tip IS main's HEAD here — nothing unmerged

    _commit(repo, "g.txt", "second")
    tip2 = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "-b", "work/test/branch")
    _commit(repo, "h.txt", "third")
    branch_tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "-q", "main")

    assert _rev_list_count(branch_tip, "^main") == 1

    # The buggy one-string form must NOT silently succeed with a wrong count —
    # git rejects it outright, and _rev_list_count's own error handling
    # degrades that rejection to 0 (matching the surrounding classify logic's
    # existing `|| echo 0` fail-open posture) rather than raising.
    bad = _rev_list_count(f"{branch_tip} ^main")
    assert bad == 0


# ---------------------------------------------------------------------------
# End-to-end classification via main() — mirrors the golden JS suite's 3 cases,
# using a stubbed `gh` on PATH exactly like the DoE-side parity harness.
# ---------------------------------------------------------------------------


def _write_gh_stub(bin_dir: Path, pr_map: dict) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    map_file = bin_dir / "pr-map.json"
    map_file.write_text(json.dumps(pr_map))
    stub = bin_dir / "gh"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "BRANCH=\"\"\n"
        "while [[ $# -gt 0 ]]; do\n"
        "  case \"$1\" in\n"
        "    --head) BRANCH=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f"python3 -c 'import json,sys; m=json.load(open(sys.argv[2])); "
        "pr=m.get(sys.argv[1]); print(json.dumps([pr]) if pr else \"[]\")' "
        f"\"$BRANCH\" \"{map_file}\"\n"
    )
    stub.chmod(0o755)


def _dated_commit(repo: Path, name: str, msg: str, ts: int) -> None:
    """Commit with an explicit author+committer date (UTC), avoiding any race
    with the real wall clock `now` the script reads at run time."""
    date_str = time.strftime("%Y-%m-%dT%H:%M:%S+0000", time.gmtime(ts))
    (repo / name).write_text(msg)
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "commit", "-q", "-m", msg, f"--date={date_str}"],
        cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GIT_AUTHOR_DATE": date_str, "GIT_COMMITTER_DATE": date_str},
    )


def test_end_to_end_critical_warning_ok(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    # All fixture timestamps anchor 10 days before test-run time and only ever
    # step FORWARD from that fixed base — never against the real wall clock —
    # so nothing here races the `now = int(time.time())` the script reads at
    # run time (a `merged_at_ts + N` scheme keyed off a live commit's own
    # timestamp flaked: sub-second test execution meant "post-merge" commits
    # could land in the FUTURE relative to the script's own `now`, silently
    # flipping orphan_after_merge to 0 depending on scheduler jitter).
    base_ts = int(time.time()) - 10 * 86400

    # cleanmerged: merged PR, no commits after merge -> OK
    _git(repo, "checkout", "-q", "-b", "work/test/cleanmerged")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-q", "work/test/cleanmerged", "-m", "merge cleanmerged")

    # orphaned-after-merge: merged PR, then extra commits post-merge -> CRITICAL
    _git(repo, "checkout", "-q", "-b", "work/test/orphaned-after-merge")
    _dated_commit(repo, "o1.txt", "o1", base_ts)
    _git(repo, "checkout", "-q", "main")
    merged_at_ts = base_ts + 30
    merged_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(merged_at_ts))
    _git(repo, "checkout", "-q", "work/test/orphaned-after-merge")
    for i in range(2):
        _dated_commit(repo, f"post{i}.txt", f"post{i}", merged_at_ts + 60 + i * 10)
    _git(repo, "checkout", "-q", "main")

    # no-pr-stale: no PR, old branch name, ahead of main -> WARNING
    stale_branch = "work/testmachine/2020-01-01"
    _git(repo, "checkout", "-q", "-b", stale_branch)
    _dated_commit(repo, "stale.txt", "stale", base_ts - 3 * 86400)
    _git(repo, "checkout", "-q", "main")

    gh_stub_dir = tmp_path / "stub-bin"
    _write_gh_stub(gh_stub_dir, {
        "work/test/cleanmerged": {"number": 1, "state": "MERGED", "mergedAt": merged_at_iso},
        "work/test/orphaned-after-merge": {"number": 2, "state": "MERGED", "mergedAt": merged_at_iso},
    })
    monkeypatch.setenv("PATH", f"{gh_stub_dir}{os.pathsep}{os.environ['PATH']}")

    rc = main(["--format", "json", "--severity-min", "ok", "--max-age-days", "365"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    by_branch = {ln["branch"]: ln for ln in lines}

    assert by_branch["work/test/orphaned-after-merge"]["severity"] == "CRITICAL"
    assert by_branch[stale_branch]["severity"] == "WARNING"
    assert by_branch.get("work/test/cleanmerged", {}).get("severity", "OK") == "OK"


# ---------------------------------------------------------------------------
# Faithful oracle-bug repro: current-branch `"* "` marker is not stripped by
# the bash-equivalent regex, so a qualifying branch that is ALSO the checked-
# out HEAD is silently dropped from seen_branches. See module docstring
# negative-spec. NOT a fix target — this is a NEGATIVE-SPEC regression test:
# if this starts passing (branch appears), the faithful-repro contract broke.
# ---------------------------------------------------------------------------


def test_current_branch_dropped_oracle_bug(tmp_path, monkeypatch, capsys):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/current/branch")
    _commit(repo, "g.txt", "c2")
    monkeypatch.chdir(repo)

    rc = main(["--format", "text", "--max-age-days", "365"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out == ""


# ---------------------------------------------------------------------------
# --severity-min filtering and --format text, positive (populated) cases.
#
# Ported from coordinator/tests/plugin-ecosystem/orphan-sweep.test.js:
#   "--severity-min critical suppresses WARNING entries" (its :303) and
#   "text format emits readable lines" (its :323) — the two JS-suite
#   assertions NOT already covered above by test_end_to_end_critical_warning_ok
#   (which only ever calls main() with --severity-min ok) or
#   test_current_branch_dropped_oracle_bug (--format text but always-empty
#   output, so it never exercises the per-severity line-prefix contract).
# See module docstring: byte-parity verified against this same JS suite
# during the 2026-07-17 BIG_PORT port.
# ---------------------------------------------------------------------------


def _plant_critical_and_warning_branches(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """Shared fixture for the two tests below: one CRITICAL branch (merged PR,
    commits added after mergedAt) and one WARNING branch (no PR, stale
    branch-name date, ahead of main). Returns (repo, stale_branch_name)."""
    repo = _init_repo(tmp_path)
    monkeypatch.chdir(repo)

    base_ts = int(time.time()) - 10 * 86400

    _git(repo, "checkout", "-q", "-b", "work/test/orphaned-after-merge")
    _dated_commit(repo, "o1.txt", "o1", base_ts)
    _git(repo, "checkout", "-q", "main")
    merged_at_ts = base_ts + 30
    merged_at_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(merged_at_ts))
    _git(repo, "checkout", "-q", "work/test/orphaned-after-merge")
    for i in range(2):
        _dated_commit(repo, f"post{i}.txt", f"post{i}", merged_at_ts + 60 + i * 10)
    _git(repo, "checkout", "-q", "main")

    stale_branch = "work/testmachine/2020-01-01"
    _git(repo, "checkout", "-q", "-b", stale_branch)
    _dated_commit(repo, "stale.txt", "stale", base_ts - 3 * 86400)
    _git(repo, "checkout", "-q", "main")

    gh_stub_dir = tmp_path / "stub-bin"
    _write_gh_stub(gh_stub_dir, {
        "work/test/orphaned-after-merge": {"number": 2, "state": "MERGED", "mergedAt": merged_at_iso},
    })
    monkeypatch.setenv("PATH", f"{gh_stub_dir}{os.pathsep}{os.environ['PATH']}")

    return repo, stale_branch


def test_severity_min_critical_suppresses_warning(tmp_path, monkeypatch, capsys):
    _plant_critical_and_warning_branches(tmp_path, monkeypatch)

    rc = main(["--format", "json", "--severity-min", "critical", "--max-age-days", "365"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [json.loads(ln) for ln in out.splitlines() if ln.strip()]

    warnings = [ln for ln in lines if ln["severity"] == "WARNING"]
    criticals = [ln for ln in lines if ln["severity"] == "CRITICAL"]
    assert warnings == [], f"--severity-min critical should suppress WARNINGs, got: {out}"
    assert any(ln["branch"] == "work/test/orphaned-after-merge" for ln in criticals), (
        f"Expected CRITICAL for orphaned-after-merge, got:\n{out}"
    )


def test_text_format_emits_readable_lines(tmp_path, monkeypatch, capsys):
    _plant_critical_and_warning_branches(tmp_path, monkeypatch)

    rc = main(["--format", "text", "--severity-min", "warning", "--max-age-days", "365"])
    assert rc == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]

    assert len(lines) >= 1, "Expected at least one output line in text format"
    for line in lines:
        assert line.startswith("CRITICAL") or line.startswith("WARNING") or line.startswith("OK"), (
            f"Unexpected text format line: {line}"
        )


# ---------------------------------------------------------------------------
# C1c quartet: compute_descendant_tip / detect_unpushed_commits /
# list_unmerged_work_branches / verify_commit_in_review_window.
#
# All git exercised here runs ONLY inside a throwaway repo under tmp_path
# (via _init_repo/_commit above) — never against the real working repo.
# ---------------------------------------------------------------------------


def test_compute_descendant_tip_single_line_history(tmp_path):
    repo = _init_repo(tmp_path)
    root = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "a.txt", "a")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = compute_descendant_tip([root, tip], cwd=repo)
    assert result == {"descendant_tip": tip, "diverged": False}

    # Idempotent: identical inputs, identical output on a second call.
    assert compute_descendant_tip([root, tip], cwd=repo) == result


def test_compute_descendant_tip_diverged_branches(tmp_path):
    repo = _init_repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "work/a/one")
    _commit(repo, "a.txt", "a")
    tip_a = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", base)
    _git(repo, "checkout", "-q", "-b", "work/b/two")
    _commit(repo, "b.txt", "b")
    tip_b = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = compute_descendant_tip([tip_a, tip_b], cwd=repo)
    assert result == {"descendant_tip": None, "diverged": True}


def test_compute_descendant_tip_empty_and_single_candidate(tmp_path):
    repo = _init_repo(tmp_path)
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    assert compute_descendant_tip([], cwd=repo) == {"descendant_tip": None, "diverged": False}
    assert compute_descendant_tip([tip], cwd=repo) == {"descendant_tip": tip, "diverged": False}


def test_detect_unpushed_commits_no_origin_counts_all(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/test/nofetch")
    _commit(repo, "a.txt", "a")
    _commit(repo, "b.txt", "b")

    # No origin ref: falls back to a full rev-list count on the branch —
    # includes the repo's initial commit, not just the 2 new ones.
    result = detect_unpushed_commits("work/test/nofetch", cwd=repo)
    assert result == {"branch": "work/test/nofetch", "ahead_count": 3, "has_origin": False}

    # Idempotent: repeated call over unchanged history returns the same result.
    assert detect_unpushed_commits("work/test/nofetch", cwd=repo) == result


def test_detect_unpushed_commits_with_origin_ref(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/test/withorigin")
    _commit(repo, "a.txt", "a")
    # Simulate an already-fetched origin/<branch> ref without a real remote:
    # write the ref directly (no network call, no `git fetch`).
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/work/test/withorigin", tip)

    result = detect_unpushed_commits("work/test/withorigin", cwd=repo)
    assert result == {"branch": "work/test/withorigin", "ahead_count": 0, "has_origin": True}

    _commit(repo, "b.txt", "b")
    result2 = detect_unpushed_commits("work/test/withorigin", cwd=repo)
    assert result2 == {"branch": "work/test/withorigin", "ahead_count": 1, "has_origin": True}


def test_detect_unpushed_commits_non_qualifying_branch(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "not-a-work-branch")
    _commit(repo, "a.txt", "a")

    result = detect_unpushed_commits("not-a-work-branch", cwd=repo)
    assert result == {"branch": "not-a-work-branch", "ahead_count": 0, "has_origin": False}


def test_detect_unpushed_commits_defaults_to_current_branch(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature/test/current")
    _commit(repo, "a.txt", "a")

    # No origin ref: full rev-list count includes the initial commit too.
    result = detect_unpushed_commits(None, cwd=repo)
    assert result["branch"] == "feature/test/current"
    assert result["ahead_count"] == 2
    assert result["has_origin"] is False


def test_list_unmerged_work_branches_scopes_by_machine_case_insensitive(tmp_path):
    repo = _init_repo(tmp_path)

    _git(repo, "checkout", "-q", "-b", "work/MachineOne/topic-a")
    _commit(repo, "a.txt", "a")
    _git(repo, "checkout", "-q", "main")

    _git(repo, "checkout", "-q", "-b", "work/machinetwo/topic-b")
    _commit(repo, "b.txt", "b")
    _git(repo, "checkout", "-q", "main")

    branches = list_unmerged_work_branches("machineone", "main", cwd=repo)
    assert branches == ["work/MachineOne/topic-a"]

    # Idempotent: no state mutated by the read, second call matches.
    assert list_unmerged_work_branches("machineone", "main", cwd=repo) == branches


def test_list_unmerged_work_branches_excludes_merged(tmp_path):
    repo = _init_repo(tmp_path)

    _git(repo, "checkout", "-q", "-b", "work/host/merged-one")
    _commit(repo, "a.txt", "a")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--no-ff", "-q", "work/host/merged-one", "-m", "merge it")

    _git(repo, "checkout", "-q", "-b", "work/host/unmerged-one")
    _commit(repo, "b.txt", "b")
    _git(repo, "checkout", "-q", "main")

    branches = list_unmerged_work_branches("host", "main", cwd=repo)
    assert branches == ["work/host/unmerged-one"]


def test_verify_commit_in_review_window_inclusive_upper_exclusive_lower(tmp_path):
    repo = _init_repo(tmp_path)
    lower = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "a.txt", "a")
    middle = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "b.txt", "b")
    upper = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Middle commit strictly between lower (exclusive) and upper (inclusive).
    assert verify_commit_in_review_window(middle, lower, upper, cwd=repo) == {"in_window": True}
    # Upper bound itself counts (inclusive-upper).
    assert verify_commit_in_review_window(upper, lower, upper, cwd=repo) == {"in_window": True}
    # Lower bound itself does NOT count (exclusive-lower).
    assert verify_commit_in_review_window(lower, lower, upper, cwd=repo) == {"in_window": False}

    # Idempotent: repeated identical call, identical result.
    assert verify_commit_in_review_window(middle, lower, upper, cwd=repo) == {"in_window": True}


def test_verify_commit_in_review_window_outside_range(tmp_path):
    repo = _init_repo(tmp_path)
    root = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "a.txt", "a")
    lower = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "b.txt", "b")
    upper = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # root predates lower -> not in window.
    assert verify_commit_in_review_window(root, lower, upper, cwd=repo) == {"in_window": False}


# ---------------------------------------------------------------------------
# Registered handler surface (async register_op wrappers) — same fixtures,
# invoked through the JSON-RPC-shaped entry points via asyncio.run, matching
# this repo's existing async-handler test convention.
# ---------------------------------------------------------------------------


def test_compute_descendant_tip_handler(tmp_path):
    repo = _init_repo(tmp_path)
    root = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "a.txt", "a")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = asyncio.run(
        obs._compute_descendant_tip_handler({"candidate_tips": [root, tip]}, repo_root=repo)
    )
    assert result == {"descendant_tip": tip, "diverged": False}


def test_detect_unpushed_commits_handler_params_repo_root_wins(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/test/handler")
    _commit(repo, "a.txt", "a")

    other_dir = tmp_path / "not-a-repo"
    other_dir.mkdir()

    result = asyncio.run(
        obs._detect_unpushed_commits_handler(
            {"repo_root": str(repo), "branch": "work/test/handler"},
            repo_root=other_dir,
        )
    )
    assert result == {"branch": "work/test/handler", "ahead_count": 2, "has_origin": False}


def test_list_unmerged_work_handler(tmp_path):
    repo = _init_repo(tmp_path)
    _git(repo, "checkout", "-q", "-b", "work/host/topic")
    _commit(repo, "a.txt", "a")
    _git(repo, "checkout", "-q", "main")

    result = asyncio.run(
        obs._list_unmerged_work_handler({"machine": "host", "main_ref": "main"}, repo_root=repo)
    )
    assert result == {"branches": ["work/host/topic"]}


def test_verify_commit_in_review_window_handler(tmp_path):
    repo = _init_repo(tmp_path)
    lower = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "a.txt", "a")
    upper = _git(repo, "rev-parse", "HEAD").stdout.strip()

    result = asyncio.run(
        obs._verify_commit_in_review_window_handler(
            {"commit_sha": upper, "lower_bound_sha": lower, "upper_bound_sha": upper},
            repo_root=repo,
        )
    )
    assert result == {"in_window": True}
