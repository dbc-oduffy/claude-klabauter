"""
coordinator_core.tests.test_coverage_reviewed_set — regression tests for the
SURVIVING classification/filtering logic in coverage.py.

`build_reviewed_set` (the per-record git rev-list orchestration this file used
to pin — batched-vs-per-record correctness, the single-graph-walk perf lever,
endpoint resolution, out-of-window/foreign-endpoint classification, spawn
budgets) was deleted in its entirety — see
docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md § C5.
The reviewed_set is now a FILE (`coordinator_core.review_trail.reviewed_set`),
not a computation coverage.py performs at gate-run time; that module's own
equivalence tests (coordinator_core/review_trail/tests/
test_reviewed_set_equivalence.py) are the durable home for the properties
that used to require reproducing the deleted orchestration end-to-end.

What survives here, and is what this file now tests directly rather than
through the deleted orchestrator: bookkeeping classification
(`_classify_bookkeeping_shas`), the kind-aware plan-vs-code credit collapse
(`_credit_from_kind_partition`), the stored-literal-HEAD read-side exclusion
(`_record_range_has_stored_head`), the verdict filter (`_verdict_counts`), the
unrecognized-scope_kind WARN aggregation (`emit_unrecognized_kind_warning`),
and the foreign-session scope set (`_FOREIGN_STRIPPED_SCOPES`) — plus two
pre-existing tests of `workstream_complete.directives_review`'s
`_record_membership_shas` spawn-avoidance short-circuit, which never touched
`build_reviewed_set` and are unrelated to this chunk's deletion.

Spec backlink: pln-pcore-03-beachhead-coordinator-core-fecdbb § C3
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import List

import pytest
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    **no_console_creationflags(),
)


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
    **no_console_creationflags(),
)
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_path_commit(repo: Path, rel_path: str, message: str) -> str:
    full = repo / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(f"{message}\n", encoding="utf-8")
    _git(["add", rel_path], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo),
        capture_output=True, encoding="utf-8", check=True,
    **no_console_creationflags(),
).stdout.strip()


def _write_trail_record(path: Path, sha: str) -> None:
    record = {
        "sha_range": f"{sha}^..{sha}",
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": "diff",
        "verdict": "ok",
        "diff_loc": 1,
        "session_id": "00000000-0000-0000-0000-000000000001",
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def test_parse_trail_file_reads_suffixed_filename(tmp_path: Path) -> None:
    from coordinator_core.coverage import _parse_trail_file

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    sha = _make_commit(repo, "C1: work")

    suffixed_path = tmp_path / "2026-07-27-140000-abc12345-2.json"
    _write_trail_record(suffixed_path, sha)

    records = _parse_trail_file(str(suffixed_path))

    assert len(records) == 1
    assert records[0]["sha_range"] == f"{sha}^..{sha}"


def test_verdict_counts_excludes_pending() -> None:
    from coordinator_core.coverage import _verdict_counts

    assert _verdict_counts({"verdict": "pending"}) is False


@pytest.mark.parametrize("verdict", ["ok", "warn", "blocked", "waived", None])
def test_verdict_counts_includes_non_pending(verdict) -> None:
    from coordinator_core.coverage import _verdict_counts

    rec = {} if verdict is None else {"verdict": verdict}
    assert _verdict_counts(rec) is True


@pytest.mark.parametrize(
    "sha_range",
    [
        "0227ea17..HEAD",
        "0227ea17...HEAD",
        "HEAD~2..0227ea17",
        "HEAD^..0227ea17",
    ],
)
def test_record_range_has_stored_head_true(sha_range: str) -> None:
    from coordinator_core.coverage import _record_range_has_stored_head

    assert _record_range_has_stored_head(sha_range) is True


@pytest.mark.parametrize(
    "sha_range",
    [
        "0227ea17..abc123ff",
        "abc123ff...0227ea17",
        "origin/main..0227ea17",
    ],
)
def test_record_range_has_stored_head_false(sha_range: str) -> None:
    from coordinator_core.coverage import _record_range_has_stored_head

    assert _record_range_has_stored_head(sha_range) is False


# _FOREIGN_STRIPPED_SCOPES — the whole admission story for foreign-session

def test_foreign_stripped_scopes_is_the_three_narrowed_scopes() -> None:
    """`_FOREIGN_STRIPPED_SCOPES` is the whole admission story: a foreign
    commit under one of these scopes is stripped unconditionally. K-010
    removed the per-record attestation exemption that briefly qualified
    that; nothing may re-add a scope value here to smuggle one back.
    """
    from coordinator_core.coverage import _FOREIGN_STRIPPED_SCOPES

    assert _FOREIGN_STRIPPED_SCOPES == frozenset({"session", "chain", "workstream-close-auto"})


def test_emit_unrecognized_kind_warning_no_op_when_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from coordinator_core.coverage import emit_unrecognized_kind_warning

    emit_unrecognized_kind_warning({})
    assert capsys.readouterr().err == ""


def test_emit_unrecognized_kind_warning_aggregates_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from coordinator_core.coverage import emit_unrecognized_kind_warning

    emit_unrecognized_kind_warning({"chunk": 3, "inline": 1})

    err = capsys.readouterr().err
    warn_lines = [line for line in err.splitlines() if "unrecognized scope_kind" in line]
    assert len(warn_lines) == 1, (
        f"expected exactly one aggregated WARN line, got {len(warn_lines)}: {warn_lines}"
    )
    assert "4" in warn_lines[0] and "chunk" in warn_lines[0] and "inline" in warn_lines[0]


def test_credit_from_kind_partition_plan_credits_planning_artifact_commit(
    tmp_path: Path,
) -> None:
    from coordinator_core.coverage import _credit_from_kind_partition

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")

    credited = _credit_from_kind_partition({"plan": {plan_sha}}, str(repo))

    assert plan_sha in credited, (
        "a plan-kind bucket crediting a planning-artifact-only commit must "
        "surface it (AC5)"
    )


def test_credit_from_kind_partition_plan_never_credits_code_commit(tmp_path: Path) -> None:
    from coordinator_core.coverage import _credit_from_kind_partition

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")
    code_sha = _make_path_commit(repo, "src/example.py", "code change")

    credited = _credit_from_kind_partition({"plan": {plan_sha, code_sha}}, str(repo))

    assert plan_sha in credited, "the planning-artifact commit must still be credited"
    assert code_sha not in credited, (
        "AC6: a plan-kind bucket must NEVER credit a code commit, even when "
        "it shares the bucket with a genuinely planning commit"
    )


def test_credit_from_kind_partition_plan_bookkeeping_only_commit_uncredited(
    tmp_path: Path,
) -> None:
    """A "plan"-kind bucket may also contain a BOOKKEEPING-only commit (not
    code, not planning) — e.g. a state/ ceremony-exhaust commit. EXHAUST wins
    on overlap (see `_classify_bookkeeping_shas`), so this commit is neither
    exhaust-credited (only "diff"-kind buckets get unconditional credit) nor
    planning-credited (it touches no planning-artifact path at all) — it must
    simply not appear in the credited set.
    """
    from coordinator_core.coverage import _credit_from_kind_partition

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")
    bookkeeping_sha = _make_path_commit(
        repo, "state/some-ledger.jsonl", "bookkeeping-only commit"
    )

    credited = _credit_from_kind_partition(
        {"plan": {plan_sha, bookkeeping_sha}}, str(repo)
    )

    assert plan_sha in credited, "the planning-artifact commit must still be credited"
    assert bookkeeping_sha not in credited, (
        "a bookkeeping-only commit within a plan bucket must not be credited "
        "— it is neither planning nor unconditionally-credited diff"
    )


def test_credit_from_kind_partition_planning_commit_uncredited_without_a_plan_bucket(
    tmp_path: Path,
) -> None:
    """AC9 (non-vacuous): a planning-artifact commit is NOT auto-credited just
    because it is classifiable PLANNING — it must actually appear in a
    "plan"-kind bucket. Absent one, it stays uncredited, exactly like any
    other unreviewed commit.
    """
    from coordinator_core.coverage import _credit_from_kind_partition

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    plan_sha = _make_path_commit(repo, "docs/plans/2026-08-06-example.md", "author plan")

    credited = _credit_from_kind_partition({}, str(repo))

    assert plan_sha not in credited, (
        "AC9: a planning-artifact commit with no plan-kind bucket crediting "
        "it must remain uncovered — planning status is not itself credit"
    )


def test_credit_from_kind_partition_diff_kind_credits_unconditionally(
    tmp_path: Path,
) -> None:
    from coordinator_core.coverage import _credit_from_kind_partition

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _make_commit(repo, "C0: initial")
    code_sha = _make_path_commit(repo, "src/example.py", "code change")

    credited = _credit_from_kind_partition({"diff": {code_sha}}, str(repo))

    assert code_sha in credited


def test_credit_from_kind_partition_integration_kind_credits_nothing() -> None:
    """Anti-scope: "integration" is not an unrestricted-credit kind and has no
    planning-classification path either — any kind outside
    `_UNRESTRICTED_CREDIT_KINDS`/"plan" credits nothing, fail-closed.
    """
    from coordinator_core.coverage import _credit_from_kind_partition

    credited = _credit_from_kind_partition(
        {"integration": {"deadbeef00000000000000000000000000000000"}}, "."
    )
    assert credited == set()


def test_single_commit_range_outside_chain_set_resolves_without_a_spawn():
    from coordinator_core.workstream_complete.directives_review import (
        _record_membership_shas,
    )

    in_chain = "a" * 40
    off_chain = "b" * 40
    calls: list[str] = []

    def _resolver(rng: str):
        calls.append(rng)
        return {off_chain}

    for spelling in (f"{off_chain}^..{off_chain}", f"{off_chain}~1..{off_chain}",
                     f"{off_chain[:9]}^..{off_chain[:9]}"):
        membership = _record_membership_shas(
            {
                "sha_range": spelling,
                "reviewer": "code-reviewer",
                "scope": "chain",
                "scope_kind": "diff",
                "verdict": "ok",
            },
            resolve_range_shas=_resolver,
            chain_dag_sha_set={in_chain},
            chain_code_sha_set={in_chain},
        )
        assert membership is None, (
            f"{spelling!r} names a commit outside the chain DAG set and must "
            f"contribute nothing -- got {membership!r}"
        )

    assert calls == [], (
        "the resolver must never be called for a single-commit range whose own "
        "sha is outside the chain DAG set -- each such call is a git subprocess "
        f"whose result is already determined; got {calls!r}"
    )


def test_single_commit_range_inside_chain_set_still_resolves_normally():
    from coordinator_core.workstream_complete.directives_review import (
        _record_membership_shas,
    )

    in_chain = "c" * 40
    calls: list[str] = []

    def _resolver(rng: str):
        calls.append(rng)
        return {in_chain}

    membership = _record_membership_shas(
        {
            "sha_range": f"{in_chain}^..{in_chain}",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "scope_kind": "diff",
            "verdict": "ok",
        },
        resolve_range_shas=_resolver,
        chain_dag_sha_set={in_chain},
        chain_code_sha_set={in_chain},
    )

    assert calls, "an in-chain record must still be resolved for real"
    assert membership == {in_chain}, membership
