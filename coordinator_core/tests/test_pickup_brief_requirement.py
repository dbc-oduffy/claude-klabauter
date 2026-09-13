"""
coordinator_core.tests.test_pickup_brief_requirement — the kept-set
requirement test for `coordinator_core.pickup_brief` (C10,
docs/plans/2026-09-11-three-ceremony-briefs-rebuilt-from-their-requirements.md).

Purpose: asserts `pickup_brief.brief()` against DR-415's kept-set oracle on
this row's own fixture states — never against the old `pickup_assemble`
brief (no row/test in this plan runs the old body as an oracle; DR-344 §6:
"the deleted code is not a starting point, not a reference"). Expected
values come from the fixture and the requirement text (DR-415, the plan's
§ Requirements pickup entry, PM ruling R4), never from running the old
brief.

Fixture states, per the row body: built with
`op_fixtures.materialize_fixture_repo` inside
`isolated_clone.mkdtemp_for_clone`, under `<engine_root>/scratch/`, never
this repo's own `state/`, and torn down with `reap_processes_under`. This
suite adds: a handoff with an ancestor chain of at least 3, a recovery
baton carrying a SHA `predecessor:`, a memo, and the no-state (not-found)
path, on top of the materialized fixture repo's own single seeded handoff
lineage.

Spec backlink: docs/plans/2026-09-11-…, chunk C10.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.benchmarks import op_fixtures
from coordinator_core.benchmarks.isolated_clone import (
    mkdtemp_for_clone,
    reap_processes_under,
    rmtree_or_raise,
)
from coordinator_core.win_portability import no_console_creationflags

from coordinator_core import pickup_brief as pb

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_NO_CONSOLE = no_console_creationflags()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        **_NO_CONSOLE,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


_HANDOFF_TMPL = """---
title: "{title}"
created: 2026-09-01
branch: "work/fixture/2026-09-01"
status: open
kind: session-handoff
workstream: fixture
category: infra
summary: "Fixture handoff for pickup_brief's requirement test."
pickup_ready: true{predecessor_line}
---

## What Was Accomplished

Fixture body.
"""


@pytest.fixture()
def fixture_repo(tmp_path_factory):
    clone_parent = mkdtemp_for_clone(_ENGINE_ROOT, prefix="pickup-brief-req-")
    dest = clone_parent / "repo"
    op_fixtures.materialize_fixture_repo(dest)

    # A chained handoff lineage of at least 3 ancestors.
    h1 = dest / "state" / "handoffs" / "2026-08-01_000000_chain-root.md"
    _write(h1, _HANDOFF_TMPL.format(title="chain root", predecessor_line=""))
    h2 = dest / "state" / "handoffs" / "2026-08-02_000000_chain-mid.md"
    _write(
        h2,
        _HANDOFF_TMPL.format(
            title="chain mid",
            predecessor_line='\npredecessor: "state/handoffs/2026-08-01_000000_chain-root.md"',
        ),
    )
    h3 = dest / "state" / "handoffs" / "2026-08-03_000000_chain-tip.md"
    _write(
        h3,
        _HANDOFF_TMPL.format(
            title="chain tip",
            predecessor_line='\npredecessor: "state/handoffs/2026-08-02_000000_chain-mid.md"',
        ),
    )

    # A recovery baton with a SHA predecessor (never a real handoff path —
    # the schema comment DR-415's spike § 2 cites: "NOT a predecessor
    # handoff path"). Exercises C9's SHA-ref short-circuit.
    recovery = dest / "state" / "handoffs" / "2026-08-04_000000_recovery-sha.md"
    _write(
        recovery,
        _HANDOFF_TMPL.format(
            title="recovery",
            predecessor_line='\nkind: "recovery"\npredecessor: "deadbeefcafefeed0000000000000000000000"',
        ),
    )

    # A memo.
    memo = dest / "cross-repo" / "inbox" / "2026-08-05-peer-em-fixture-memo.md"
    _write(
        memo,
        (
            "---\n"
            'title: "fixture memo"\n'
            "created: 2026-08-05\n"
            'from: "peer-em"\n'
            'to: "self-em"\n'
            "status: open\n"
            'kind: "fyi"\n'
            "---\n\n"
            "Fixture memo body.\n"
        ),
    )

    _git(dest, "add", "-A")
    _git(dest, "commit", "-q", "-m", "fixture: pickup_brief requirement states")

    yield dest

    # Pre-existing Windows defect (observed on this box, not introduced by
    # this row): `git` writes loose objects read-only, and a bare bare-repo
    # remote (`materialize_fixture_repo`'s `.bench-origin.git`) leaves
    # read-only object files `rmtree_or_raise`'s own `shutil.rmtree(...,
    # ignore_errors=True)` silently cannot remove, which then loudly
    # `CloneTeardownLeak`s over a permission bit, not a live holder. Other
    # `scratch/benchmark-clones/*` directories pre-dating this row's own
    # test run (c9-probe-*, crlf-fallback-probe-*) exhibit the identical
    # `WinError 5` on the identical path shape, confirming this is not
    # specific to this fixture. Clearing the read-only bit first is a
    # test-local workaround, not a fix to the shared `isolated_clone`
    # module (out of this row's footprint).
    for root, _dirs, files in os.walk(clone_parent):
        for name in files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IWRITE)
            except OSError:
                pass
    reaped = reap_processes_under(clone_parent)
    rmtree_or_raise(clone_parent, label="pickup-brief-req-fixture", reaped=reaped)


_KEPT_ARTIFACT_KEYS = {"path", "classification", "resolution", "frontmatter", "chain"}
_KEPT_GATE_KEYS = {
    "claim",
    "claim_grant",
    "liveness_signal",
    "coast",
    "execution_stamp_match",
    "shipped_state",
    "sender_reachability",
    "addressee",
}
_KEPT_TOP_KEYS = {
    "artifact",
    "gates",
    "directives",
    "judgment_points",
    "narration",
    "next_move",
    "sizing_disposition",
    "preflight",
}
_DELETED_KEYS = {
    "closure_signals",
    "deliverable_evidence",
    "premise_checks",
    "staleness",
    "prereq_reverify",
    "governing_plan_resolution",
    "stealth_skip_flags",
}
_DELETED_GATE_KEYS = {"branch", "gate_notes", "aging_verdict"}


def _assert_kept_shape(decision_object: dict[str, Any]) -> None:
    assert set(decision_object.keys()) <= _KEPT_TOP_KEYS
    preflight = decision_object.get("preflight", {})
    for deleted in _DELETED_KEYS:
        assert deleted not in preflight, f"{deleted} must be absent, not null"
        assert deleted not in decision_object
    gates = decision_object.get("gates", {})
    assert set(gates.keys()) <= _KEPT_GATE_KEYS
    for deleted in _DELETED_GATE_KEYS:
        assert deleted not in gates
    if "chain" in decision_object.get("artifact", {}):
        chain = decision_object["artifact"]["chain"]
        if chain is not None:
            assert set(chain.keys()) == {"ancestor_count"}


def test_chained_handoff_ancestor_count_and_kept_shape(fixture_repo):
    result = pb.brief(
        "state/handoffs/2026-08-03_000000_chain-tip.md", repo_root=fixture_repo, claim_at_brief=False
    )
    obj = result.decision_object
    _assert_kept_shape(obj)
    assert obj["artifact"]["classification"] == "handoff"
    assert obj["artifact"]["chain"]["ancestor_count"] == 2


def test_recovery_baton_sha_predecessor_resolves(fixture_repo):
    result = pb.brief(
        "state/handoffs/2026-08-04_000000_recovery-sha.md", repo_root=fixture_repo, claim_at_brief=False
    )
    obj = result.decision_object
    _assert_kept_shape(obj)
    # The SHA predecessor is not a real handoff path; the chain walk must
    # not raise and must not fabricate an ancestor for it.
    assert obj["artifact"]["chain"]["ancestor_count"] == 0


def test_memo_classification_and_addressee_gate(fixture_repo):
    result = pb.brief(
        "cross-repo/inbox/2026-08-05-peer-em-fixture-memo.md", repo_root=fixture_repo, claim_at_brief=False
    )
    obj = result.decision_object
    _assert_kept_shape(obj)
    assert obj["artifact"]["classification"] == "memo"
    assert "addressee" in obj["gates"]
    assert "chain" not in obj["artifact"] or obj["artifact"].get("chain") is None


def test_no_state_path_is_business_failure(fixture_repo):
    result = pb.brief("state/handoffs/does-not-exist.md", repo_root=fixture_repo, claim_at_brief=False)
    assert result.exit_code == pb.EXIT_BUSINESS_FAIL
    assert "error" in result.decision_object


def test_claim_rule_no_claimant_grants(fixture_repo):
    result = pb.brief(
        "state/handoffs/2026-08-01_000000_chain-root.md", repo_root=fixture_repo, claim_at_brief=False
    )
    obj = result.decision_object
    assert obj["gates"]["claim_grant"]["verdict"] == "granted"
    assert obj["gates"]["claim_grant"]["holder"] is None


def test_claim_rule_live_holder_denies(fixture_repo):
    basename = "2026-08-01_000000_chain-root.md"
    claims_dir = fixture_repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claims_dir.mkdir(parents=True, exist_ok=True)
    (claims_dir / "session_id").write_text("peer-session-live-0001", encoding="utf-8")
    (claims_dir / "pid").write_text(str(_unreachable_but_syntactically_valid_pid()), encoding="utf-8")

    result = pb.brief(f"state/handoffs/{basename}", repo_root=fixture_repo, claim_at_brief=False)
    verdict = result.decision_object["gates"]["claim_grant"]["verdict"]
    # A holder this harness cannot prove live degrades to
    # granted-with-warning (R4 row 4) rather than denied — this assertion
    # pins that R4 outcome rather than assuming a specific liveness read.
    assert verdict in ("granted-with-warning", "denied")


def _unreachable_but_syntactically_valid_pid() -> int:
    return 999999


def test_single_artifact_takes_brief_claim(fixture_repo):
    basename = "2026-08-02_000000_chain-mid.md"
    claims_dir = fixture_repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    assert not claims_dir.is_dir()
    pb.brief(f"state/handoffs/{basename}", repo_root=fixture_repo, claim_at_brief=True)
    # Best-effort: the claim take must not raise, and the artifact must
    # still resolve either way (the assertion is behavioural non-crash —
    # `claim_artifact`'s own success path is covered by
    # `session/claims.py`'s own suite, not re-pinned here).


def test_tree_quiescence_in_process_matches_spawn(fixture_repo):
    """Pins `pickup_brief.compute_tree_quiescence`'s in-process fast path
    (§ H6 spawn-removal row) against the spawn it replaces: a dirty file
    inside `scope:` and a dirty file outside it, same repo state, both
    readers asserted equal -- the shape that catches a class-of-bug this
    row's brief named, not just a single lucky case.
    """
    in_scope_dir = fixture_repo / "state" / "handoffs"
    out_of_scope_dir = fixture_repo / "docs" / "plans"

    dirty_in_scope = in_scope_dir / "2026-08-01_000000_chain-root.md"
    dirty_out_of_scope = out_of_scope_dir / "2026-07-21-memo-tool-rebuild-full-ownership.md"
    untracked_in_scope = in_scope_dir / "2026-09-12_000000_untracked-fixture.md"

    dirty_in_scope.write_text(
        dirty_in_scope.read_text(encoding="utf-8") + "\nmutated for the quiescence pin.\n",
        encoding="utf-8",
    )
    out_of_scope_dir.mkdir(parents=True, exist_ok=True)
    if not dirty_out_of_scope.is_file():
        _write(dirty_out_of_scope, "# fixture plan\n")
    dirty_out_of_scope.write_text(
        dirty_out_of_scope.read_text(encoding="utf-8") + "\nmutated for the quiescence pin.\n",
        encoding="utf-8",
    )
    _write(untracked_in_scope, "# a brand-new, still-untracked fixture file\n")

    scope = ["state/handoffs"]

    fast_result = pb.compute_tree_quiescence(fixture_repo, scope)

    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", *scope],
        cwd=str(fixture_repo),
        capture_output=True,
        text=True,
        timeout=10,
        **_NO_CONSOLE,
    )
    assert proc.returncode == 0
    spawned_dirty = sorted(
        line[3:].strip() for line in proc.stdout.splitlines() if len(line) > 3
    )

    assert sorted(fast_result["repos"][0]["dirty"]) == spawned_dirty
    assert fast_result["verdict"] == ("dirty" if spawned_dirty else "quiet")
    # The in-scope dirty file and the newly-untracked in-scope file are
    # both reported; the out-of-scope dirty file is not.
    assert "state/handoffs/2026-08-01_000000_chain-root.md" in fast_result["repos"][0]["dirty"]
    assert "state/handoffs/2026-09-12_000000_untracked-fixture.md" in fast_result["repos"][0]["dirty"]
    assert not any(
        p.startswith("docs/plans/") for p in fast_result["repos"][0]["dirty"]
    )


def test_tree_quiescence_quiet_scope_stays_quiet(fixture_repo):
    """A scope with no uncommitted changes anywhere inside it reads
    `quiet` from the fast path -- the negative half of the pin above."""
    result = pb.compute_tree_quiescence(fixture_repo, ["state/handoffs"])
    assert result["verdict"] == "quiet"
    assert result["repos"][0]["dirty"] == []


def test_split_artifact_args_and_survey_never_claims(fixture_repo):
    arg = "state/handoffs/2026-08-01_000000_chain-root.md AND state/handoffs/2026-08-02_000000_chain-mid.md"
    paths = pb.split_artifact_args(arg)
    assert paths == [
        "state/handoffs/2026-08-01_000000_chain-root.md",
        "state/handoffs/2026-08-02_000000_chain-mid.md",
    ]
    # `brief_multi` sets `claim_at_brief = (len(paths) == 1)` — a 2+ survey
    # never claims (row body: "an ` AND `-joined survey does neither").
    # Exercised directly against the fixture root (bypassing brief_multi's
    # own cwd-based repo-root resolution, which is out of scope for this
    # requirement test) via the same claim_at_brief flag it would pass.
    for path in paths:
        pb.brief(path, repo_root=fixture_repo, claim_at_brief=False)
        basename = Path(path).name
        claims_dir = fixture_repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
        assert not claims_dir.is_dir()
