"""Spawn-count bound on `coverage.gate`'s out-of-window endpoint resolution.

Guards the defect class where the gate's git spawn count scales with the
REVIEW-TRAIL CORPUS ON DISK rather than with the commit range being gated. On
this repo's own ~1700-record corpus that shape cost 1402 `git rev-parse`
spawns / ~30s per invocation, which exceeded the engine's 30s dispatch timeout
outright: `coverage.gate` stopped returning a verdict at all (`-32603 op timed
out after 30.0s`). A capability failure on the ceremony-close path, not a slow
test.

Root cause: `coverage._make_chain_range_resolver` (the open-review-loop
diagnosis resolver, injected into `review_coverage_core.classify_pending_records`)
owned an `_OutOfWindowCache` but — unlike `_reviewed_via_graph_walk` — never
ran the batched out-of-window pre-scan, so `_probe_out_of_window` fell back to
one `git rev-parse --verify` spawn per distinct out-of-window endpoint token,
per trail record. `coverage.py`'s own comment above the
`_diagnose_open_review_loop` call site already asserted the property this test
now enforces ("at most one lazy graph-build spawn ... ceiling two"); nothing
executable held it.

Why a spawn COUNT and not a wall-clock threshold: this box averages 50-70
concurrent LLM sessions, so any elapsed-time assertion is a flake generator.
Spawn count is deterministic under load. Prior art for the idiom:
`coordinator_core/ops/ceremony/tests/test_git_native.py`.

Negative-spec: this does NOT assert a total-spawn budget for the whole gate,
and must not be widened into one — the property under guard is specifically
that per-ENDPOINT-TOKEN probing is batched, i.e. that the count does not grow
with `_OUT_OF_WINDOW_RECORDS`. A future change that legitimately adds one
fixed spawn elsewhere in the gate should not touch this test.

Spec backlink: coverage.py::_out_of_window_hex_tokens
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List, Tuple

import pytest

from coordinator_core import coverage
from coordinator_core.win_portability import no_console_creationflags

# Declared, not excused: this file spawns a real process (git/python) because
# the property under test is that binary's own behaviour, which no fixture
# stands in for. The spawn ratchet's `_BASELINE` is shrink-only pre-existing
# residue and is explicitly not the route for a new file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

#: Portable CREATE_NO_WINDOW (a no-op dict off Windows) — the fixture's own
#: git spawns must not flash a console under a headless Windows runner.
_NO_CONSOLE = no_console_creationflags()


#: Distinct out-of-window trail records in the fixture corpus. Must be
#: comfortably larger than `_REV_PARSE_CEILING` for the assertion to
#: discriminate a batched implementation from a per-token one at all — with
#: the pre-fix code this is precisely the `git rev-parse` spawn count.
_OUT_OF_WINDOW_RECORDS = 12

#: Allowed `git rev-parse` spawns for the whole gate run. A CONSTANT, which is
#: the entire point: it does not scale with `_OUT_OF_WINDOW_RECORDS`. Headroom
#: of a couple of spawns for symbolic-ref resolution elsewhere in the gate.
_REV_PARSE_CEILING = 2


def _git(args: List[str], cwd: Path) -> None:
    subprocess.run(
        ["git"] + args, cwd=str(cwd), capture_output=True, encoding="utf-8",
        check=True, **_NO_CONSOLE,
    )


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo), capture_output=True, encoding="utf-8", check=True,
        **_NO_CONSOLE,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    return _head(repo)


@pytest.fixture()
def corpus_repo(tmp_path: Path) -> Tuple[Path, str]:
    """A repo whose gated window is TINY but whose trail corpus is not.

    Layout (oldest first):
        root, then `_OUT_OF_WINDOW_RECORDS` commits BEFORE the window base,
        then the base, then three in-window commits.

    Every trail record cites a single-commit range over one of the
    pre-base commits, so each record's endpoint token is out-of-window
    (an ancestor of the window base) and cannot be resolved from the
    in-window parent map — exactly the tokens `_probe_out_of_window`
    handles. Records are `verdict: "pending"` so they credit nothing (the
    in-window commits stay uncovered, which is what makes the gate reach
    `_diagnose_open_review_loop` at all) while still forcing
    `classify_pending_records` to resolve every one of their ranges.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    _commit(repo, "root")  # so every old commit below has a resolvable `^`
    old_shas = [_commit(repo, f"old {i}") for i in range(_OUT_OF_WINDOW_RECORDS)]
    base = _commit(repo, "window base")
    for i in range(3):
        _commit(repo, f"in-window {i}")

    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True)
    for i, sha in enumerate(old_shas):
        (trail_dir / f"record-{i:03d}.json").write_text(
            json.dumps(
                {
                    "sha_range": f"{sha}^..{sha}",
                    "reviewer": "code-reviewer",
                    "scope": "session",
                    "scope_kind": "diff",
                    "verdict": "pending",
                    "diff_loc": 1,
                    "session_id": "00000000-0000-0000-0000-000000000001",
                }
            ),
            encoding="utf-8",
        )
    return repo, base


def test_out_of_window_endpoint_probing_does_not_spawn_per_record(
    corpus_repo: Tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git rev-parse` spawns stay at a CONSTANT ceiling regardless of how many
    distinct out-of-window endpoint tokens the trail corpus cites.

    Fails on the pre-fix code with exactly `_OUT_OF_WINDOW_RECORDS`
    `git rev-parse` spawns (one per record, from `_probe_out_of_window`'s
    per-token fallback); passes on the batched pre-scan.
    """
    repo, base = corpus_repo

    spawned: List[Tuple[str, ...]] = []
    real_run = coverage._run

    def counting_run(cmd, cwd=None, input_text=None):
        spawned.append(tuple(str(c) for c in cmd))
        return real_run(cmd, cwd=cwd, input_text=input_text)

    monkeypatch.setattr(coverage, "_run", counting_run)

    result = coverage.run_coverage_gate(
        range_arg=f"{base}..HEAD", repo_root=str(repo)
    )

    # Non-vacuity: the gate must actually have reached the open-review-loop
    # diagnosis, which is the only caller of the resolver under guard. Without
    # these two, a gate that short-circuited early would "pass" trivially.
    assert result.uncovered_shas, "fixture must leave in-window commits uncovered"
    assert any(
        note.startswith(coverage._OPEN_LOOP_NOTE_PREFIX) for note in result.notes
    ), f"open-review-loop diagnosis did not run; notes={result.notes}"

    rev_parse = [cmd for cmd in spawned if cmd[:2] == ("git", "rev-parse")]
    assert len(rev_parse) <= _REV_PARSE_CEILING, (
        f"{len(rev_parse)} `git rev-parse` spawns for "
        f"{_OUT_OF_WINDOW_RECORDS} out-of-window trail records — endpoint "
        f"probing is scaling with the review-trail corpus instead of being "
        f"batched into one `git cat-file --batch-check`. See "
        f"coverage._out_of_window_hex_tokens. Spawns: {rev_parse}"
    )


# ---------------------------------------------------------------------------
# DAG mode (`graph_range=None`) — `_make_chain_range_resolver`'s own resolver,
# not the default corpus-scaled fallback `classify_pending_records` used to
# receive on this path. See coverage.py's `_diagnose_open_review_loop` call
# site comment ("ceiling two") and `_build_chain_seed_parent_map`.
#
# 2026-08-15 audit (state/audits/2026-08-15-wsc-close-time-attribution.md):
# `_make_chain_range_resolver` returned None whenever `graph_range` was absent
# (exactly DAG mode, the mode WSC's chain-end gate runs in), so
# `classify_pending_records` fell back to its DEFAULT resolver — one
# `git rev-list` per DISTINCT range across the WHOLE trail corpus (2,328
# spawns / 96.3% of a gate invocation's git spawns, measured). This test
# exercises `_make_chain_range_resolver` + `classify_pending_records`
# directly, in DAG mode, over a pending-record corpus that scales — proving
# the spawn count does not.
# ---------------------------------------------------------------------------

from coordinator_core.benchmarks import budget
from coordinator_core.ops.review_coverage_core import classify_pending_records

#: Distinct pending trail records in the DAG-mode fixture corpus. Comfortably
#: larger than the manifest's `spawn_count_budget` for this test to
#: discriminate the fixed one-graph-build-spawn resolver from the
#: corpus-scaled per-range default at all.
_DAG_PENDING_RECORDS = 30


@pytest.fixture()
def dag_corpus_repo(tmp_path: Path) -> Tuple[Path, List[str], List[str]]:
    """A linear repo whose DAG-mode `chain_set` is a handful of commits, but
    whose pending trail corpus cites `_DAG_PENDING_RECORDS` DISTINCT ranges —
    each `<ancestor>^..<ancestor>` for a commit inside the same linear
    history, so every endpoint is resolvable from the ONE
    `git rev-list --parents --stdin` walk `_build_chain_seed_parent_map`
    seeds on `chain_set`, with zero further spawns.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)

    _commit(repo, "root")
    shas = [_commit(repo, f"c{i}") for i in range(_DAG_PENDING_RECORDS)]
    # chain_set: the newest 5 commits — small, deliberately not the whole
    # history, so the fixture cannot pass merely because chain_set == corpus.
    chain_set = shas[-5:]
    return repo, chain_set, shas


def test_dag_mode_resolver_does_not_spawn_per_pending_record(
    dag_corpus_repo: Tuple[Path, List[str], List[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """DAG mode's `_make_chain_range_resolver` (graph_range=None) costs a
    CONSTANT git-spawn count regardless of how many distinct pending-record
    ranges the trail corpus cites — the manifest's
    `coverage.diagnose_open_review_loop_dag_mode.spawn_count_budget.
    many_pending_records`, enforced here at exact equality.

    Fails on the pre-fix code (`_make_chain_range_resolver` returning None in
    DAG mode) with `_DAG_PENDING_RECORDS` `git rev-list` spawns — one per
    distinct range, from `classify_pending_records`' default resolver.
    """
    repo, chain_set, shas = dag_corpus_repo
    cwd = str(repo)

    all_records: List[Tuple[str, dict]] = [
        (
            "trail.jsonl",
            {
                "sha_range": f"{sha}^..{sha}",
                "reviewer": "code-reviewer",
                "scope": "session",
                "scope_kind": "diff",
                "verdict": "pending",
                "diff_loc": 1,
                "session_id": "00000000-0000-0000-0000-000000000001",
            },
        )
        for sha in shas
    ]

    spawned: List[Tuple[str, ...]] = []
    real_run = coverage._run
    real_subprocess_run = coverage.subprocess.run

    def counting_run(cmd, cwd=None, input_text=None):
        spawned.append(tuple(str(c) for c in cmd))
        return real_run(cmd, cwd=cwd, input_text=input_text)

    def counting_subprocess_run(cmd, *a, **kw):
        spawned.append(tuple(str(c) for c in cmd))
        return real_subprocess_run(cmd, *a, **kw)

    monkeypatch.setattr(coverage, "_run", counting_run)
    monkeypatch.setattr(coverage.subprocess, "run", counting_subprocess_run)

    resolver = coverage._make_chain_range_resolver(
        cwd,
        set(chain_set),
        graph_range=None,
        prescan_ranges={rec["sha_range"] for _p, rec in all_records},
    )
    assert resolver is not None, "non-empty chain_set must yield a DAG-mode resolver"

    pending_entries = classify_pending_records(all_records, resolve_range=resolver, cwd=cwd)
    assert len(pending_entries) == _DAG_PENDING_RECORDS, (
        "fixture must actually classify every pending record"
    )

    git_spawns = [cmd for cmd in spawned if cmd[:1] == ("git",)]
    manifest = budget.load_manifest()
    budgeted = manifest["overrides"]["coverage.diagnose_open_review_loop_dag_mode"][
        "spawn_count_budget"
    ]["many_pending_records"]
    assert len(git_spawns) == budgeted, (
        f"{len(git_spawns)} git spawns resolving {_DAG_PENDING_RECORDS} DAG-mode "
        f"pending records (manifest budgets {budgeted}) — DAG mode's resolver is "
        f"scaling with the review-trail corpus again. See "
        f"coverage._make_chain_range_resolver / _build_chain_seed_parent_map. "
        f"Spawns: {git_spawns}"
    )
