"""test_wsc_coverage_gate_runner — pytest tests for wsc-coverage-gate-runner.py.

Spec backlink: docs/plans/2026-07-21-doe-skill-bash-to-claude-klabauter-python-port.md [DEAD-CITATION: plan file never committed to this repo]
  (M3 chunk WSC-2). Source: DoE-claude
  coordinator/skills/workstream-complete/SKILL.md §§ Step 2.4 "Plan-claim
  guard", Step 2.9 "Coverage gate (chain-end path)" + "Marker write".

Coverage:
  claim-plan:
    - rc=0 (claimed/re-entrant/stale-takeover) passes through as 0.
    - rc!=0 + "held by session" in combined output -> contention halt (rc 1).
    - rc!=0 + no "held by session" match -> infra-error halt (rc 1), distinct
      stderr framing (never misreported as a phantom peer).
  coverage-gate:
    - VERDICT=COVERED passes through the underlying exit code (0).
    - VERDICT=WARN (C10: replaces the retired UNCOVERED token) relays stderr,
      prints the coordinator:review-code remediation offer, and exits 0 —
      it never halts. This is the regression coverage for the AC16 defect:
      the dead UNCOVERED branch previously left the runner silent on a
      below-threshold run.
    - VERDICT=WARN + COORDINATOR_OVERRIDE_COVERAGE_GATE=1 still exits 0 and
      notes the override is a no-op (nothing left to override).
    - VERDICT=INDETERMINATE halts (exit 2) with no override present.
    - VERDICT=INDETERMINATE + COORDINATOR_OVERRIDE_COVERAGE_GATE=1 exits 0.
  write-trail:
    - forwards args and propagates the underlying exit code + stdout/stderr.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.workstream_complete.directives_review import (
    _record_membership_shas,
    verify_trail_range_termination,
)

# Declared, not excused: `_git`'s callers below spawn real `git` processes
# because the properties under test are real DAG-mode chain re-derivation
# and commit-clock/history plumbing (`_derive_dag_chain_set`,
# ceremony-bookkeeping exclusion) that no mock stands in for. Each test
# builds its own scratch repo via the per-test `_git`/`_make_commit`
# call sites rather than a shared module-scoped fixture, since these are
# mutation-heavy (fresh commit histories per scenario) and a shared repo
# would leak commits across tests. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for
# this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "wsc_coverage_gate_runner",
        _BIN_DIR / "wsc-coverage-gate-runner.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


@pytest.fixture
def clean_override_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_OVERRIDE_COVERAGE_GATE", raising=False)


# ---------------------------------------------------------------------------
# claim-plan
# ---------------------------------------------------------------------------

def _run_claim(monkeypatch, returncode, combined, slug="my-feature"):
    monkeypatch.setattr(
        _mod,
        "_run_session_claim_cli",
        lambda slug_arg: (returncode, combined),
    )
    return _mod.main(["claim-plan", slug])


def test_claim_plan_success_passes(monkeypatch):
    rc = _run_claim(monkeypatch, 0, "")
    assert rc == 0


def test_claim_plan_contention_halts(monkeypatch, capsys):
    rc = _run_claim(
        monkeypatch, 1,
        "cs_claim_plan: my-feature held by session abc123 (PID 999) — "
        "concurrent /pickup detected\n",
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "STOP: plan claim contention" in err
    assert "held by session" in err


def test_claim_plan_infra_error_halts_distinctly(monkeypatch, capsys):
    rc = _run_claim(monkeypatch, 1, "cs_claim_plan: unresolvable session id\n")
    assert rc == 1
    err = capsys.readouterr().err
    assert "STOP: plan claim infra error" in err
    assert "contention" not in err


# ---------------------------------------------------------------------------
# _resolve_chain_tip_sha — chain's OWN tip, not raw HEAD (2026-07-27 fix)
#
# Prior behavior: `_resolve_chain_tip_sha` was `git rev-parse HEAD` — on this
# fleet's documented single shared `work/*` branch (many concurrent sessions),
# HEAD accrues every peer session's unrelated commits between a chain's own
# tip and gate-run time, making the corroboration check structurally
# unsatisfiable outside a single-session repo. Fixed: re-derive the same
# DAG-mode chain_set the gate's own verdict computation used
# (`coordinator_core.coverage._derive_dag_chain_set`), exclude ceremony-
# bookkeeping commits (mirroring the verdict's own CODE-partition exclusion),
# and take the newest remaining commit as the comparison point.
# ---------------------------------------------------------------------------


def _git(*args, cwd, env=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env,
    )
    return proc.stdout.strip()


_commit_clock = {"epoch": 1_700_000_000}


def _make_commit(repo_dir, filename, message):
    """Commits within one test can land in the same wall-clock second — git's
    committer-date resolution is 1s — which would make `%ct`-based
    newest-commit selection order-nondeterministic. Force strictly
    increasing, one-second-apart commit timestamps via GIT_*_DATE so tests
    asserting 'the newest of these SHAs' are deterministic."""
    import os as _os

    (repo_dir / filename).write_text(message)
    _git("add", filename, cwd=repo_dir)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    _git("commit", "-m", message, cwd=repo_dir, env=env)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


# ---------------------------------------------------------------------------
# write-trail
# ---------------------------------------------------------------------------

def test_write_trail_forwards_and_propagates(monkeypatch, capsys):
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, '{"status": "ok"}\n', ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    rc = _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    assert captured_argv["argv"] == [
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
    ]


def test_write_trail_optional_scope_kind(monkeypatch):
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "waived",
        "--scope", "session",
        "--verdict", "waived",
        "--diff-loc", "0",
        "--scope-kind", "plan",
    ])
    assert "--scope-kind" in captured_argv["argv"]
    assert "plan" in captured_argv["argv"]


def test_write_trail_forwards_reviewer_evidence_when_given(monkeypatch):
    """`--reviewer-evidence` is forwarded verbatim when supplied."""
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
        "--reviewer-evidence", "state/subagent-share/sid/report.md",
    ])
    assert "--reviewer-evidence" in captured_argv["argv"]
    assert "state/subagent-share/sid/report.md" in captured_argv["argv"]


def test_write_trail_omits_reviewer_evidence_when_not_given(monkeypatch):
    """`--reviewer-evidence` is omitted entirely from the forwarded argv
    when not supplied — never forwarded as an empty string."""
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
    ])
    assert "--reviewer-evidence" not in captured_argv["argv"]


def test_write_trail_forwards_attestation_dispatch_id_when_given(monkeypatch):
    """`--attestation-dispatch-id` is forwarded verbatim when supplied."""
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
        "--attestation-dispatch-id", "dispatch-xyz",
    ])
    assert "--attestation-dispatch-id" in captured_argv["argv"]
    assert "dispatch-xyz" in captured_argv["argv"]


def test_write_trail_omits_attestation_dispatch_id_when_not_given(monkeypatch):
    """`--attestation-dispatch-id` is omitted entirely from the forwarded
    argv when not supplied — never forwarded as an empty string."""
    captured_argv = {}

    def _fake(argv):
        captured_argv["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "ok",
        "--diff-loc", "42",
    ])
    assert "--attestation-dispatch-id" not in captured_argv["argv"]


def test_write_trail_propagates_failure(monkeypatch, capsys):
    def _fake(argv):
        return 2, "", "review_trail.write: op-level refusal\n"

    monkeypatch.setattr(_mod, "_run_write_review_trail", _fake)
    rc = _mod.main([
        "write-trail",
        "--sha-range", "abc..def",
        "--reviewer", "code-reviewer",
        "--scope", "chain",
        "--verdict", "blocked",
        "--diff-loc", "10",
    ])
    assert rc == 2
    assert "op-level refusal" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# brightline-gate — chain_partition_verdict_store persistence (2026-08-04
# root-cause fix, cross-repo/inbox/2026-08-04-example-retrieval-repo-em-brightline-
# partition-mandatory-does-not-halt.md "mechanism 2"): the producer now
# writes the just-computed verdict to disk in addition to (never instead of)
# printing it, so the next `wsc.brief()`/`wsc.apply()` call can read it
# without an EM re-typing `decisions["chain_partition_verdict"]`.
# ---------------------------------------------------------------------------

_TIER_B_STDOUT = (
    'BRIGHTLINE reviewers_required=4 reviewers_suggested=32 reviewers_low=4 '
    'plan_oracle=4 chain_oracle=32 session_oracle=10 '
    'verdict=PARTITION-MANDATORY basis="plan_oracle=4(...) tier=B"\n'
)

#: Fixed chain code sha used across the C13 discharge tests below (2026-08-06
#: chain-scoping correction: `chain_partition_verdict_discharged` now scopes
#: by WITHIN-CHAIN MEMBERSHIP — a trail record's resolved range must be a
#: non-empty subset of the chain's own code-review obligation set to
#: discharge — never by tip ancestry on the shared branch).
_CHAIN_CODE_SHA = "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee0"
_CHAIN_CODE_SHAS = [_CHAIN_CODE_SHA]


# tier=A hard-stop branch (declared-but-unwalked-repo halt) and its
# `test_brightline_gate_tier_a_halt_unaffected_by_persistence` pin are
# removed outright (state/kill-ledger.md K-004, 2026-08-16, Verdict A —
# measured across 151 records: tier=A never fired). See
# docs/wiki/cost-budgets-and-the-kill-disposition.md.


# ---------------------------------------------------------------------------
# brightline-gate — C13 (docs/plans/2026-08-05-coverage-gate-planning-
# artifact-class.md, AC20/AC21/AC23): refuse the chain-terminal cap on an
# UNDISCHARGED PARTITION-MANDATORY verdict. The narrow exception carved out
# of tier=B/none's "never a hard stop" posture — see this test module's own
# `_TIER_B_STDOUT` fixture for the shared verdict=PARTITION-MANDATORY line.
# AC23 watched-to-fail evidence: with `coordinator/bin/wsc-coverage-gate-
# runner.py` and `coordinator_core/workstream_complete/directives_review.py`
# reverted to their pre-C13 content (`git stash push` scoped to those two
# files), `test_partition_mandatory_undischarged_refuses_the_cap` below FAILS
# (observed rc=0, "tier=B is communicate-only" — the chain-terminal close
# reaches a clean terminal stamp with zero discharging review-trail
# records). After restoring the C13 content, the same test PASSES (rc=1,
# HALT). Reported verbatim in the chunk's completion report.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC4/Seam 2/3 (plan 2026-08-15-chain-scope-review-gets-a-discharging-
# artifact.md, C2): `chain_slices` — C7's slate, persisted by C2 onto the
# SAME `chain_partition_verdict_store` record `_persist_brightline_verdict`
# already writes. Absent-vs-`[]` is load-bearing (module docstring, "the key
# is absent when the gate has not run for this close" — never `[]`, which
# means resolved-and-empty): the pair below pins both sides of that split.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# C4b (docs/plans/2026-08-11-review-trail-carries-execution-basis.md, AC4):
# `cmd_brightline_gate` now also calls the read-only reporting companion
# `directives_review.chain_partition_execution_basis_report` and surfaces its
# counts. Purely narration — the pair below pins BOTH halves of that
# contract: the line appears, and it never moves the verdict/exit code.
# ---------------------------------------------------------------------------


_TIER_B_SINGLE_REVIEWER_OK_STDOUT = (
    'BRIGHTLINE reviewers_required=1 reviewers_suggested=1 reviewers_low=1 '
    'plan_oracle=1 chain_oracle=1 session_oracle=1 '
    'verdict=single-reviewer-ok basis="plan_oracle=1(...) tier=B"\n'
)


# ---------------------------------------------------------------------------
# Review: review-integrator — test gap flagged in brightline-discharge
# round4 (2026-08-06): `chain_owes_no_code_review` is the ONE branch in
# `cmd_brightline_gate`'s PARTITION-MANDATORY handling that GRANTS a pass
# with an EMPTY review-trail, and had no test at all before this pin.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-08-07 (state/audits/2026-08-07-wsc-chain-gate-counts-doc-only-
# commits.md): the HALT's own UNCOVERED message told two lies — it called
# every entry a "chain code commit" (PLANNING commits stay in the
# obligation set by design but aren't code) and stayed silent when an
# uncovered commit is foreign to the closing session (frequently
# undischargeable BY CONSTRUCTION, not because no one reviewed it). This is
# a rendering-only fix: `chain_partition_uncovered_shas`'s output, the
# denominator, and the verdict are untouched — only the message's labeling
# of the SAME uncovered list.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC1 / AC13 (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md, C4):
# the vouch-free posture's positive and negative pins. AC1 uses REAL
# review-trail records written by the real writer (`write_review_trail_
# entry`) and read back through the real `_load_trail_records` — the
# original defect this plan corrects was in a read path, so a mocked read
# proves nothing. AC13 pins the one HALT case C1 deliberately did not
# touch: an `own_shas` entry in the undischarged uncovered set still halts,
# whether or not `foreign_shas` is also populated.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2026-08-10 narration fix (cross-repo/inbox/2026-08-10-doe-claude-em-
# brightline-unrecordable-narration-is-false.md): the foreign partition of the
# uncovered-set diagnostic must not assert unrecordability falsely, and must
# name the per-commit `<sha>^..<sha>` form that works.
#
# 2026-08-17 (cross-repo/inbox/2026-08-17-doe-claude-em-chain-ancestry-review-
# ran-clean.md): the waiver-conditioned half of that fix is gone. K-005 removed
# `_guard_foreign_session_range`'s Case 1 refusal, so EVERY foreign-attributed
# commit is recordable by an ordinary write over its own concrete range.
# 2026-08-18 (docs/plans/2026-08-18-chain-review-records-and-credits-
# predecessors.md § C3) replaces the dead waiver seam with `_resolve_
# attested_shas`, the RECORD-keyed DR-156 attestation read — this helper's
# `vouched` set is never actually consulted by these narration tests (`_load_
# trail_records` returns `[]` below, so `_record_membership_shas` never runs
# against a real record), but the monkeypatch target is kept accurate so a
# future change that starts calling it here fails loudly on a real signature
# rather than silently patching a name that no longer exists. The pins below
# are the negative-spec against the unrecordable wording returning.
# ---------------------------------------------------------------------------


# The mint-confirmation discriminator ("the chain-ancestry waiver mint
# could not be confirmed" / dead-child-vs-halting-gate narration) and its
# two pins (`test_brightline_gate_warns_when_the_mint_subprocess_failed_
# before_minting`, `test_brightline_gate_halting_gate_is_not_mistaken_for_a_
# failed_mint`) are removed outright along with the whole chain-ancestry-
# waiver mint mechanism (state/kill-ledger.md K-005, 2026-08-16 — "waiver
# system dies").


# ---------------------------------------------------------------------------
# chain_partition_verdict_discharged / chain_partition_uncovered_shas —
# 2026-08-06 chain-scoping correction. Live instrumentation against
# `state/handoffs/2026-08-06-eliminate-claude-klabauter-s-non-test-subprocess-spawn-
# population.md` (chain tip `72eee33c6`, 15 chain code shas) proved the
# original tip-reaching scoping condition (`tip == chain_tip_sha or
# is_ancestor(chain_tip_sha, tip)`, shared by both the retired "leg (a)" and
# "leg (b)") is not a chain-scoping check at all on this fleet's ONE SHARED
# `work/{machine}/{date}` branch: every record ANY concurrent peer session
# wrote later on the shared branch also satisfies it, regardless of whether
# it reviewed a single commit of the chain under evaluation. All 11 records
# that discharged the old condition against the live chain belonged to two
# unrelated peer sessions; zero belonged to this chain's own 17 records.
#
# Both legs are replaced by ONE within-chain-membership check: a record
# contributes to discharge only when its resolved range's sha set is a
# NON-EMPTY SUBSET of `chain_code_shas`. `chain_partition_verdict_
# discharged` no longer takes `chain_tip_sha`/`is_ancestor` at all — the
# tests below drive the fixed, four-argument signature directly. See
# `directives_review.chain_partition_verdict_discharged`'s own docstring
# for the full incident writeup and why the legacy leg collapsed into
# redundancy under the new scoping rather than surviving as a second leg.
#
# 2026-08-06 membership-vs-coverage split (review-integrator P1): unless a
# test is specifically exercising the difference, `chain_dag_shas` below is
# passed equal to `chain_code_shas` — every scenario that doesn't name a
# same-chain bookkeeping/handoff-authoring sha behaves identically whether
# membership is tested against the filtered or unfiltered set, since
# `chain_code_shas` is always a subset of `chain_dag_shas` in practice. See
# `test_record_spanning_code_and_same_chain_bookkeeping_commit_accepted_and_
# contributes_only_code_shas` below for the dedicated case where the two
# sets must actually differ.
# ---------------------------------------------------------------------------


def test_record_membership_rejects_stored_head_range_before_consulting_resolver():
    """2026-08-06 review-integrator finding B1: a `sha_range` whose tip is
    the literal symbolic ref `HEAD` re-resolves against whatever commit is
    HEAD at gate-run time, not write time — the verified 2026-07-25
    `work/machine-a/2026-07-21` incident (8 stale `<sha>..HEAD` records
    reading as COVERED 12 commits past the newest concrete-range record).
    `_record_membership_shas` now rejects this shape via
    `coverage._record_range_has_stored_head`, the SAME defence
    `resolve_trail_range_tip` and `coverage.build_reviewed_set` already
    apply to these exact records — checked on the raw string BEFORE
    `resolve_range_shas` is ever called. The resolver double below raises
    if invoked at all, proving the rejection happens pre-resolver, not
    merely "coincidentally returns nothing" the way the retired test
    above's `abc..HEAD` case did."""

    def _resolver_must_not_be_called(sha_range: str):
        raise AssertionError(f"resolver must not be consulted for a stored-HEAD range: {sha_range!r}")

    record = {"verdict": "ok", "sha_range": "abc..HEAD"}
    assert _record_membership_shas(
        record, _resolver_must_not_be_called, {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None


def test_record_membership_rejects_unsafe_range_before_consulting_resolver():
    """2026-08-06 review-integrator finding B2: `coverage.SAFE_RANGE` — the
    shared argument-injection validator ("blocks leading-dash argument
    injection, e.g. `--output=/x..y` reaching `git rev-list` as a flag") —
    is applied at every OTHER `git rev-list` call site in this codebase;
    `_record_membership_shas` now applies it too, before the resolver is
    ever consulted. Also covers the related shape gap: a bare-sha
    `sha_range` (no `..`/`...` separator) is rejected by the same check
    rather than becoming an unbounded `git rev-list <sha>` ancestry walk."""

    def _resolver_must_not_be_called(sha_range: str):
        raise AssertionError(f"resolver must not be consulted for an unsafe range: {sha_range!r}")

    injection_record = {"verdict": "ok", "sha_range": "--output=/x..y"}
    assert _record_membership_shas(
        injection_record, _resolver_must_not_be_called, {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None

    bare_sha_record = {"verdict": "ok", "sha_range": "aaaaaaa1"}
    assert _record_membership_shas(
        bare_sha_record, _resolver_must_not_be_called, {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None


def test_record_membership_skips_integration_scope_kind():
    """2026-08-06 review-integrator finding W1: a `scope_kind` of
    `"integration"` must not discharge this chain's `chain_code_shas` union
    — mirrors `coverage.build_reviewed_set`'s own Phase-1 classification,
    which `continue`s on `"integration"` entirely. Rejected before the
    resolver is even consulted."""

    def _resolver_must_not_be_called(sha_range: str):
        raise AssertionError(f"resolver must not be consulted for a non-code scope_kind: {sha_range!r}")

    record = {"verdict": "ok", "sha_range": "base..tip", "scope_kind": "integration"}
    assert _record_membership_shas(
        record, _resolver_must_not_be_called, {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None


# ---------------------------------------------------------------------------
# Review: review-integrator — test gap flagged in brightline-discharge
# round4 (2026-08-06): "no test bounds the intersection rule; every
# membership test asserts the permissive direction. Add tests that fail if
# intersection credits more than it should."
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Review: review-integrator — B2/B3 (2026-08-06, brightline-discharge
# round4). `_resolve_foreign_session_shas` must treat an untrailered commit,
# and a merge commit whose own trailer does not attribute it, as foreign —
# NOT silently creditable to any spanning record — closing the exclusion-
# based holes `session_attribution.trailer_foreign_shas`'s `git log
# --no-merges` left open. Exercised against a real throwaway git repo
# (`temp_git_repo`), same fixture shape the neighboring ancestry tests
# already use.
#
# Review R2 (2026-08-06): chain membership (`coverage._derive_dag_chain_set`
# step 3) itself runs `--no-merges`, so a merge commit is NEVER a member of
# `chain_code_shas`/`chain_dag_shas` — flagging one foreign here is pure
# defence-in-depth, not something an unsatisfiable-chain scenario can hinge
# on. The merge test below therefore pins "an untrailered merge is foreign",
# not "merge-ness alone makes a commit foreign" — its untrailered-ness is
# what the trailer-atom walk actually keys off, same as the plain
# untrailered-commit test above it.
# ---------------------------------------------------------------------------


def _commit_with_session_trailer(repo_dir, filename, message, session_id):
    """Same shape as `_make_commit`, but the commit message carries a real
    `Session-Id:` git trailer so `%(trailers:key=Session-Id,valueonly)`
    resolves it."""
    import os as _os

    (repo_dir / filename).write_text(message)
    _git("add", filename, cwd=repo_dir)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    full_message = f"{message}\n\nSession-Id: {session_id}"
    _git("commit", "-m", full_message, cwd=repo_dir, env=env)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


def _commit_with_unparseable_trailing_session_trailer(repo_dir, filename, message, session_id):
    """Same shape as `_commit_with_session_trailer`, but the `Session-Id:`
    line is followed by a non-trailer footer line — the authoring pattern
    review R1 (2026-08-06) names (`701b485ef`'s real body ends
    `Session-Id: ...\\n--- end Step 2.67 blocks ---\\n`). Git's trailer
    parser only reads a commit message's FINAL paragraph, so
    `%(trailers:key=Session-Id,valueonly)` cannot attribute this commit —
    but `--grep=^Session-Id: <sid>$` (the chain-membership predicate) still
    matches the message line directly."""
    import os as _os

    (repo_dir / filename).write_text(message)
    _git("add", filename, cwd=repo_dir)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    full_message = f"{message}\n\nSession-Id: {session_id}\n--- end Step 2.67 blocks ---"
    _git("commit", "-m", full_message, cwd=repo_dir, env=env)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


# brightline-gate's own chain-ancestry-waiver mint (2026-08-07,
# state/audits/2026-08-07-review-gate-scoping-predecessor-and-planning-
# artifacts.md, candidate fixes #1/#2) — along with the `--no-mint` CLI
# flag and its three ordering/default-on/opt-out pins
# (`test_brightline_gate_partition_mandatory_mints_before_reading_waivers`,
# `test_brightline_gate_default_mints`, `test_brightline_gate_no_mint_flag_
# omits_mint`) — is removed outright along with the whole chain-ancestry-
# waiver mechanism (state/kill-ledger.md K-005, 2026-08-16 — "waiver system
# dies").
# ---------------------------------------------------------------------------


# `coordinator_core.chain_ancestry_waivers` and its two direct-mechanism
# pins (`test_minted_chain_ancestry_waiver_moves_a_foreign_commit_from_
# uncovered_to_covered`, `test_stored_head_only_covering_record_stays_
# uncovered_even_when_waived`) are removed outright along with the whole
# chain-ancestry-waiver module (state/kill-ledger.md K-005, 2026-08-16 —
# "waiver system dies"; the module itself no longer exists on disk).


# ---------------------------------------------------------------------------
# op_latency instrumentation of the `--from-handoff` chain (state/kill-ledger.md
# K-004, 2026-08-16: "No stage of it is instrumented ... one timing span in
# cmd_brightline_gate makes this decidable"). `_run_review_brightline_gate`
# is the single span — see its own docstring/`_OP_LATENCY_LABEL` for why.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AC6 (docs/plans/2026-08-18-chain-review-records-and-credits-predecessors.md
# § C6): end-to-end credit, not just admission, and the two readers agree.
#
# Takes the exact command line the AC9 narration above prints (fills in its
# `<sha>`/`<dispatch-id>`/`<sidecar-path>` placeholders with a real fixture's
# values) and runs it — via `coordinator-write-review-trail.py`'s own `main`,
# never a hand-rolled call into `write_review_trail_entry` — against a real
# git repo whose sole uncovered commit is predecessor-authored. Only the
# transport hop (`cc_invoke.route_mutation`'s daemon/socket round trip) is
# replaced, with the SAME native op function
# (`review_trail_write.write_review_trail_entry`) this repo's own CLI
# ultimately calls — `_guard_foreign_session_range` and every other guard on
# the write path run for real, unmocked, exactly the "two surfaces
# disagreeing" defect class this chunk exists to catch (eng-director F7).
# ---------------------------------------------------------------------------

_AC6_OWN_SESSION = "ac6-own-session-e2e01"
_AC6_FOREIGN_SESSION = "ac6-foreign-session-e2e02"
_AC6_DISPATCH_ID = "code-reviewer@session-ac6own"


def _load_write_review_trail_cli():
    loader = importlib.machinery.SourceFileLoader(
        "wsc_coverage_gate_runner_ac6_write_trail_cli",
        str(_BIN_DIR / "coordinator-write-review-trail.py"),
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _ac6_ledger_row(repo, session_id, dispatch_id):
    ledger_dir = repo / ".git" / "coordinator-sessions" / session_id
    ledger_dir.mkdir(parents=True, exist_ok=True)
    (ledger_dir / "dispatched-agents.txt").write_text(
        f"{dispatch_id}\topus\tcode-reviewer\t1786451686\n", encoding="utf-8",
    )


def _ac6_write_pending_frozen_record(repo, session_id, sha_range):
    trail_dir = repo / "state" / "review-trail"
    trail_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "sha_range": sha_range,
        "reviewer": "code-reviewer",
        "scope": "session",
        "scope_kind": "diff",
        "verdict": "pending",
        "diff_loc": 1,
        "session_id": session_id,
        "workstream": None,
    }
    (trail_dir / f"2026-08-18-000000-{session_id[:8]}-pending.json").write_text(
        json.dumps(record), encoding="utf-8",
    )


def _ac6_write_sidecar(repo, session_id, filename, reviewed_range_sha):
    rel = f"state/subagent-share/{session_id}/{filename}"
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\nreviewer: code-reviewer\n"
        f'reviewed_range:\n  - "{reviewed_range_sha}^..{reviewed_range_sha}"\n'
        "---\n",
        encoding="utf-8",
    )
    return rel


