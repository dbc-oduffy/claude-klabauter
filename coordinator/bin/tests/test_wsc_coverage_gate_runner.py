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

import importlib.util
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core.workstream_complete.directives_review import (
    _record_membership_shas,
    chain_partition_uncovered_shas,
    chain_partition_verdict_discharged,
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


@pytest.fixture(autouse=True)
def _reset_module_process_caches():
    """Review-integrator finding N2: `_RANGE_SHAS_CACHE`, `_DAG_SHAS_CACHE`,
    and `_FOREIGN_SHAS_CACHE` are never-cleared module globals by design in
    production (spawn-per-call, one process per gate run) but a cross-test
    contamination hazard here — many tests below reuse the literal
    `from_handoff`/`sha_range` strings (e.g. `"state/handoffs/x.md"`) across
    dozens of scenarios with different monkeypatched git-layer doubles.
    Autouse so every test in this module gets a clean slate without having
    to remember to request it."""
    _mod._clear_process_caches()
    yield
    _mod._clear_process_caches()


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


@pytest.fixture
def temp_git_repo(tmp_path, monkeypatch):
    """A real, throwaway git repo — used to exercise real ancestry checks
    (`_git_is_ancestor`, `verify_trail_range_termination`) and the real
    `git log --no-walk --format=%H %ct` batched call `_resolve_chain_tip_sha`
    issues, without touching this repo's own tree."""
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_resolve_chain_tip_sha_excludes_bookkeeping_and_finds_newest(monkeypatch, temp_git_repo):
    """DAG chain_set = {aaa, bbb, ccc}; ccc is classified bookkeeping (excluded);
    bbb is the newest of the remaining CODE commits — must be chosen over aaa,
    never ccc."""
    sha_a = _make_commit(temp_git_repo, "a.txt", "commit A")
    sha_b = _make_commit(temp_git_repo, "b.txt", "commit B")
    sha_c = _make_commit(temp_git_repo, "c.txt", "commit C (bookkeeping)")

    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[sha_a, sha_b, sha_c], indeterminate=False,
        ),
    )
    monkeypatch.setattr(
        _mod,
        "_classify_bookkeeping_shas",
        lambda shas, repo_root, cache: (frozenset({sha_c}), frozenset(), None),
    )

    result = _mod._resolve_chain_tip_sha("state/handoffs/x.md")
    assert result == sha_b


def test_resolve_chain_tip_sha_indeterminate_dag_returns_none(monkeypatch, temp_git_repo):
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[], indeterminate=True,
        ),
    )
    assert _mod._resolve_chain_tip_sha("state/handoffs/x.md") is None


def test_resolve_chain_tip_sha_all_bookkeeping_falls_back_to_full_chain(monkeypatch, temp_git_repo):
    """Every chain commit is bookkeeping-only — report the newest of the full
    chain_set rather than 'no chain tip at all'."""
    sha_a = _make_commit(temp_git_repo, "a.txt", "bookkeeping commit A")
    sha_b = _make_commit(temp_git_repo, "b.txt", "bookkeeping commit B")

    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[sha_a, sha_b], indeterminate=False,
        ),
    )
    monkeypatch.setattr(
        _mod,
        "_classify_bookkeeping_shas",
        lambda shas, repo_root, cache: (frozenset({sha_a, sha_b}), frozenset(), None),
    )
    assert _mod._resolve_chain_tip_sha("state/handoffs/x.md") == sha_b


def test_machine_a_shape_stale_record_still_fails_corroboration_against_chain_own_tip(
    monkeypatch, temp_git_repo,
):
    """(a) Regression pin for the 2026-07-25 work/machine-a/2026-07-21 incident,
    re-verified against the CHAIN'S OWN newest commit (not raw HEAD): a trail
    record whose concrete tip stops short of the chain's own newest
    substantive commit must still fail corroboration."""
    sha_reviewed = _make_commit(temp_git_repo, "a.txt", "reviewed commit")
    sha_newest_in_chain = _make_commit(temp_git_repo, "b.txt", "newest chain commit (unreviewed)")

    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[sha_reviewed, sha_newest_in_chain], indeterminate=False,
        ),
    )
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas", lambda shas, repo_root, cache: (frozenset(), frozenset(), None),
    )

    chain_tip = _mod._resolve_chain_tip_sha("state/handoffs/x.md")
    assert chain_tip == sha_newest_in_chain

    records = [{"sha_range": f"{sha_reviewed}..{sha_reviewed}"}]
    corroborated = verify_trail_range_termination(records, chain_tip, _mod._git_is_ancestor)
    assert corroborated is False


def test_record_reaching_chain_own_tip_corroborates_despite_later_unrelated_peer_commits(
    monkeypatch, temp_git_repo,
):
    """(b) THE regression this fix closes: a trail record whose tip reaches
    the chain's own newest commit now PASSES even though unrelated peer
    commits landed on the shared branch after it — this is broken under the
    old raw-`git rev-parse HEAD` comparison and is the whole point of the fix.

    Concretely demonstrates BEFORE/AFTER using the real
    `verify_trail_range_termination` predicate and real git ancestry over a
    throwaway repo: the OLD chain_tip (raw HEAD, moved on by two unrelated
    peer commits) fails corroboration for the exact same record that the NEW
    chain_tip (this chain's own tip) corroborates.
    """
    sha_reviewed = _make_commit(temp_git_repo, "a.txt", "reviewed commit (chain's own tip)")
    # Unrelated peer-session commits landing on the shared branch AFTER this
    # chain's own tip — must not be demanded of this chain's own review.
    sha_peer_1 = _make_commit(temp_git_repo, "p1.txt", "unrelated peer commit 1")
    old_raw_head = _make_commit(temp_git_repo, "p2.txt", "unrelated peer commit 2")

    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[sha_reviewed], indeterminate=False,
        ),
    )
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas", lambda shas, repo_root, cache: (frozenset(), frozenset(), None),
    )

    new_chain_tip = _mod._resolve_chain_tip_sha("state/handoffs/x.md")
    assert new_chain_tip == sha_reviewed
    assert new_chain_tip != old_raw_head  # peer commits genuinely moved HEAD on

    records = [{"sha_range": f"{sha_reviewed}..{sha_reviewed}"}]

    # BEFORE (reproduces the defect): comparing against raw HEAD fails —
    # this is literally what the retired `_resolve_chain_tip_sha` compared
    # against.
    before_fix = verify_trail_range_termination(records, old_raw_head, _mod._git_is_ancestor)
    assert before_fix is False

    # AFTER (the fix): comparing against the chain's own tip corroborates.
    after_fix = verify_trail_range_termination(records, new_chain_tip, _mod._git_is_ancestor)
    assert after_fix is True


def test_unterminated_head_range_still_refused_against_chain_own_tip(monkeypatch, temp_git_repo):
    """(c) An unterminated `<sha>..HEAD` record is refused regardless of how
    the chain tip itself is resolved — `resolve_trail_range_tip`'s refusal is
    independent of this fix and must survive it unweakened."""
    sha_reviewed = _make_commit(temp_git_repo, "a.txt", "reviewed commit")

    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(
        _mod,
        "_derive_dag_chain_set",
        lambda from_handoff, repo_root, closing_session_id: SimpleNamespace(
            shas=[sha_reviewed], indeterminate=False,
        ),
    )
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas", lambda shas, repo_root, cache: (frozenset(), frozenset(), None),
    )
    chain_tip = _mod._resolve_chain_tip_sha("state/handoffs/x.md")

    records = [{"sha_range": f"{sha_reviewed}..HEAD"}]
    assert verify_trail_range_termination(records, chain_tip, _mod._git_is_ancestor) is False


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


def _discharging_record(verdict="ok", sha=_CHAIN_CODE_SHA):
    """A trail record whose range resolves (via the `_resolve_range_shas`
    double `_patch_chain_scoping` installs) to exactly `{sha}` — a
    within-chain member when `sha` is `_CHAIN_CODE_SHA`.

    `scope`/`session_id` (B1, 2026-08-06): every well-formed on-disk record
    carries a `scope` in `_FOREIGN_STRIPPED_SCOPES` — the write path
    enforces the closed three-value set — so this fixture matches that
    shape rather than the historical scope-less shape `_record_membership_
    shas` used to silently skip narrowing for. `_patch_chain_scoping`
    installs a matching `_resolve_foreign_session_shas` double that treats
    nothing as foreign, so this fixture's own commits still discharge."""
    return {"verdict": verdict, "sha_range": f"start..{sha}", "scope": "chain", "session_id": "own-sid"}


def _patch_chain_scoping(monkeypatch, chain_code_shas=None, chain_dag_shas=None, range_shas_map=None):
    """Isolates the C13 discharge tests from real git — `_resolve_chain_
    code_shas`, `_resolve_chain_dag_shas`, `_resolve_range_shas`, and (B1,
    2026-08-06) `_resolve_foreign_session_shas` are all isolated-for-test-
    monkeypatching seams (see their own docstrings). `chain_dag_shas`
    defaults to `chain_code_shas` (membership and coverage coincide unless a
    test explicitly names a same-chain bookkeeping sha not in `chain_code_
    shas`). `range_shas_map` defaults to resolving each `start..<sha>` range
    (the shape `_discharging_record` builds) to exactly `{sha}`, one entry
    per `chain_code_shas` member, so a default-shaped discharging record is
    always a within-chain member unless a test overrides one or both maps.
    `_resolve_foreign_session_shas` is stubbed to treat nothing as foreign —
    these tests exercise chain-scoping and discharge, not the B2/B3 foreign-
    narrowing behavior itself (see the dedicated `test_resolve_foreign_
    session_shas_*` tests for that), and B1's fix now means EVERY record
    with a `_FOREIGN_STRIPPED_SCOPES` `scope` (the shape `_discharging_
    record` now uses) reaches this narrowing in production."""
    chain_code_shas = list(_CHAIN_CODE_SHAS if chain_code_shas is None else chain_code_shas)
    chain_dag_shas = list(chain_code_shas if chain_dag_shas is None else chain_dag_shas)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_dag_shas))
    mapping = (
        {f"start..{sha}": {sha} for sha in chain_code_shas}
        if range_shas_map is None
        else range_shas_map
    )
    monkeypatch.setattr(_mod, "_resolve_range_shas", lambda sha_range: mapping.get(sha_range, set()))
    monkeypatch.setattr(_mod, "_resolve_foreign_session_shas", lambda sha_range, session_id: frozenset())


def _patch_brightline_persist_seam(monkeypatch, tmp_path, session_id="test-sid-brightline"):
    monkeypatch.setattr(_mod, "_run_review_brightline_gate", lambda argv: (0, _TIER_B_STDOUT, ""))
    # The chain-ancestry-waiver mint this PARTITION-MANDATORY branch used to
    # fold in here (`_run_review_coverage_gate(..., mint_chain_waivers=True)`)
    # is removed outright (state/kill-ledger.md K-005, 2026-08-16 — "waiver
    # system dies") — no stub needed; these tests exercise persistence and
    # discharge, not minting.
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(_mod, "_resolve_closing_session_id", lambda repo_root: session_id)
    # C13: these persistence-focused tests are not exercising the AC20
    # discharge refusal — supply a discharging trail record that IS a
    # within-chain member so they keep observing tier=B's pre-existing
    # communicate-only 0 exit. The dedicated discharge-refusal tests below
    # patch `_load_trail_records` themselves and do not call this helper.
    _patch_chain_scoping(monkeypatch)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [_discharging_record()])


def test_brightline_gate_persists_a_readable_verdict_record(monkeypatch, tmp_path):
    from coordinator_core.workstream_complete.chain_partition_verdict_store import (
        read_verdict_record,
    )

    _patch_brightline_persist_seam(monkeypatch, tmp_path)
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0  # tier=B is communicate-only, never a halt
    verdict = read_verdict_record(
        tmp_path, session_id="test-sid-brightline", expected_from_handoff="state/handoffs/x.md"
    )
    assert verdict == "PARTITION-MANDATORY"


def test_brightline_gate_action_line_no_longer_demands_manual_carry(monkeypatch, tmp_path, capsys):
    _patch_brightline_persist_seam(monkeypatch, tmp_path)
    _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert "persisted" in err
    assert "override" in err.lower()


def test_brightline_gate_persist_failure_is_non_fatal_and_loud(monkeypatch, tmp_path, capsys):
    """A disk-write failure while persisting the verdict must be reported
    loudly on stderr but must NEVER change this advisory gate's exit code
    (tier=B stays a communicate-only 0)."""
    _patch_brightline_persist_seam(monkeypatch, tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_mod, "write_verdict_record", _boom)
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "disk full" in err


def test_brightline_gate_persist_skipped_loudly_when_session_id_unresolvable(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(_mod, "_run_review_brightline_gate", lambda argv: (0, _TIER_B_STDOUT, ""))
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(_mod, "_resolve_closing_session_id", lambda repo_root: None)
    _patch_chain_scoping(monkeypatch)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [_discharging_record()])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "session id unresolvable" in err


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


def _patch_brightline_no_persist_seam(monkeypatch, tmp_path, stdout=_TIER_B_STDOUT):
    """Same shape as `_patch_brightline_persist_seam` but WITHOUT pre-seeding
    `_load_trail_records` — the discharge-refusal tests below own that
    fixture explicitly, since it is the exact thing under test here.
    Chain-scoping IS pre-seeded (fixed `chain_code_shas` + the matching
    `_resolve_range_shas` double) so these tests control discharge purely
    through the trail-record fixture they supply, not through git-resolution
    noise."""
    monkeypatch.setattr(_mod, "_run_review_brightline_gate", lambda argv: (0, stdout, ""))
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(_mod, "_resolve_closing_session_id", lambda repo_root: "test-sid-c13")
    _patch_chain_scoping(monkeypatch)


def test_brightline_gate_partition_mandatory_undischarged_refuses_the_cap(monkeypatch, tmp_path, capsys):
    """AC20/AC23: no review-trail records at all — the exact "told four,
    ran zero" shape the source memo reports — refuses the cap."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert "PARTITION-MANDATORY" in err


def test_brightline_gate_partition_mandatory_only_pending_and_waived_refuses_the_cap(
    monkeypatch, tmp_path, capsys,
):
    """AC20: a trail that exists but carries only pending/waived records is
    the SAME refusal as no trail at all — presence is not discharge."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _mod,
        "_load_trail_records",
        lambda: [{"verdict": "pending"}, {"verdict": "WAIVED"}, {"verdict": ""}],
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in capsys.readouterr().err


def test_brightline_gate_refusal_names_handoff_as_the_sanctioned_exit(monkeypatch, tmp_path, capsys):
    """AC21 — a message-content assertion, not decoration: the refusal MUST
    name /handoff as the exit, or an EM under context pressure at close has
    no visible alternative to proceed-and-annotate."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert "/handoff" in capsys.readouterr().err


def test_brightline_gate_partition_mandatory_discharged_still_caps_cleanly(monkeypatch, tmp_path):
    """Negative case, non-optional per the chunk brief: a PARTITION-MANDATORY
    chain WITH a real (non-pending, non-waived) verdict on the trail must
    still cap cleanly — a refusal that also fires on the discharged case is
    a new permanent red light, the exact failure mode this plan exists to
    remove."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _mod,
        "_load_trail_records",
        lambda: [{"verdict": "pending"}, _discharging_record("blocked")],
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0  # tier=B is communicate-only once discharged


# ---------------------------------------------------------------------------
# AC4/Seam 2/3 (plan 2026-08-15-chain-scope-review-gets-a-discharging-
# artifact.md, C2): `chain_slices` — C7's slate, persisted by C2 onto the
# SAME `chain_partition_verdict_store` record `_persist_brightline_verdict`
# already writes. Absent-vs-`[]` is load-bearing (module docstring, "the key
# is absent when the gate has not run for this close" — never `[]`, which
# means resolved-and-empty): the pair below pins both sides of that split.
# ---------------------------------------------------------------------------


def test_brightline_gate_persists_uncapped_chain_slices_on_undischarged_close(
    monkeypatch, tmp_path,
):
    """AC4 — an undischarged PARTITION-MANDATORY close persists a
    `chain_slices` slate on the SAME record `_persist_brightline_verdict`
    already writes: present (not absent), uncapped (survives past the
    ten-sha `cap = 10` prose limit that renders elsewhere in this same
    call), and carrying C7's per-entry shape
    (`sha`/`sha_range`/`recordable`/`certifies_review`/
    `chain_slices_caveat`)."""
    from coordinator_core.workstream_complete.chain_partition_verdict_store import (
        read_chain_slices,
    )

    many_shas = [f"{i:040x}" for i in range(1, 13)]  # 12 > cap = 10
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    _patch_chain_scoping(monkeypatch, chain_code_shas=many_shas)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])

    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1  # undischarged, own_shas non-empty — HALT

    slices = read_chain_slices(
        tmp_path, session_id="test-sid-c13", expected_from_handoff="state/handoffs/x.md",
    )
    assert slices is not None
    assert len(slices) == 12  # uncapped — every owed sha, not just the first 10
    entry = slices[0]
    assert entry["sha"] == many_shas[0]
    assert entry["sha_range"] == f"{many_shas[0]}^..{many_shas[0]}"
    assert entry["recordable"] is True  # own-session code commit
    assert entry["certifies_review"] is False  # no waiver record on disk
    assert "chain_slices_caveat" in entry


def test_brightline_gate_persists_empty_chain_slices_on_discharged_close(monkeypatch, tmp_path):
    """AC4 — a discharged PARTITION-MANDATORY close (a real, non-pending,
    non-waived verdict covers every chain code commit) persists
    `chain_slices == []`: PRESENT and empty, never absent. Absent means
    "the gate did not compute a slate for this close"; a clean close is the
    gate having run and resolved the owed set to nothing, which is a
    different, honest answer that must render as such on read-back
    (`write_verdict_record`'s own None-vs-`[]` contract)."""
    from coordinator_core.workstream_complete.chain_partition_verdict_store import (
        read_chain_slices,
    )

    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _mod,
        "_load_trail_records",
        lambda: [{"verdict": "pending"}, _discharging_record("blocked")],
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0  # tier=B is communicate-only once discharged

    slices = read_chain_slices(
        tmp_path, session_id="test-sid-c13", expected_from_handoff="state/handoffs/x.md",
    )
    assert slices == []


def test_brightline_gate_chain_slices_persist_failure_is_non_fatal_and_loud(
    monkeypatch, tmp_path, capsys,
):
    """The chunk brief's persist-failure contract, restated for the
    chain_slices leg specifically: a disk-write failure while persisting
    the slate must be reported loudly on stderr but must NEVER change the
    gate's own verdict or return value (module docstring, "Fail-closed
    contract" — `write_verdict_record` raises, the CALLER swallows it)."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(_mod, "write_verdict_record", _boom)
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1  # unchanged — same undischarged HALT as the un-mocked case
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "disk full" in err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err


# ---------------------------------------------------------------------------
# C4b (docs/plans/2026-08-11-review-trail-carries-execution-basis.md, AC4):
# `cmd_brightline_gate` now also calls the read-only reporting companion
# `directives_review.chain_partition_execution_basis_report` and surfaces its
# counts. Purely narration — the pair below pins BOTH halves of that
# contract: the line appears, and it never moves the verdict/exit code.
# ---------------------------------------------------------------------------

def test_brightline_gate_execution_basis_report_line_appears(monkeypatch, tmp_path, capsys):
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _mod,
        "_load_trail_records",
        lambda: [_discharging_record()],
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 0  # tier=B is communicate-only once discharged
    assert "EXECUTION-BASIS: chain discharged on 1 record(s):" in err
    assert "not recorded" in err


@pytest.mark.parametrize("execution_basis", [None, "executed", "read-only"])
def test_brightline_gate_execution_basis_field_never_moves_outcome(
    monkeypatch, tmp_path, execution_basis,
):
    """Same inputs, same verdict-shaping fields — only `execution_basis`
    varies. C4's companion is a VOICE, never a veto (its own docstring); this
    pin is the regression that matters per the chunk brief."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    record = _discharging_record()
    if execution_basis is not None:
        record["execution_basis"] = execution_basis
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [record])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0


def test_brightline_gate_partition_mandatory_unrelated_range_record_does_not_discharge(
    monkeypatch, tmp_path, capsys,
):
    """2026-08-06 chain-scoping regression pin: a terminal verdict on the
    trail whose resolved range names a commit OUTSIDE this chain's own
    `chain_code_shas` — e.g. a concurrent peer session's own review over its
    own range, on this fleet's ONE SHARED `work/{machine}/{date}` branch —
    must NOT discharge the refusal, regardless of where that record's range
    sits on the shared branch relative to this chain. Before the fix,
    `chain_partition_verdict_discharged` scoped by `is_ancestor(chain_tip_
    sha, tip)` alone — "tip is later on the shared branch" — which every
    concurrent peer session's record satisfies by construction; this test
    would have falsely discharged under that condition even though
    `_git_is_ancestor` is never even consulted by the fixed predicate
    (removed from its signature entirely)."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(
        _mod,
        "_load_trail_records",
        lambda: [_discharging_record("ok", sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef")],
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err


_TIER_B_SINGLE_REVIEWER_OK_STDOUT = (
    'BRIGHTLINE reviewers_required=1 reviewers_suggested=1 reviewers_low=1 '
    'plan_oracle=1 chain_oracle=1 session_oracle=1 '
    'verdict=single-reviewer-ok basis="plan_oracle=1(...) tier=B"\n'
)


def test_brightline_gate_single_reviewer_ok_never_refused_regardless_of_trail(monkeypatch, tmp_path):
    """The exception is narrow: an ordinary `single-reviewer-ok` verdict
    (never PARTITION-MANDATORY) must cap cleanly even with an EMPTY
    review-trail — C13 must not leak into the general permissive path C10
    set."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path, stdout=_TIER_B_SINGLE_REVIEWER_OK_STDOUT)
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Review: review-integrator — test gap flagged in brightline-discharge
# round4 (2026-08-06): `chain_owes_no_code_review` is the ONE branch in
# `cmd_brightline_gate`'s PARTITION-MANDATORY handling that GRANTS a pass
# with an EMPTY review-trail, and had no test at all before this pin.
# ---------------------------------------------------------------------------


def test_brightline_gate_zero_code_chain_vacuously_discharges(monkeypatch, tmp_path, capsys):
    """A chain whose DAG resolved but carries ZERO code-review obligations
    (every chain commit is ceremony bookkeeping / handoff-authoring-only)
    must cap cleanly with an empty review-trail — `chain_owes_no_code_
    review = not dag_resolution_failed and not chain_code_shas`. Distinct
    from `dag_resolution_failed` (indeterminate DAG derivation), which must
    NOT take this vacuous-pass branch (pinned separately below)."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_derive_dag_shas", lambda from_handoff: (str(tmp_path), []))
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: [])
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: [])
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "vacuously satisfied" in err


def test_brightline_gate_chain_owing_review_cannot_reach_the_vacuous_pass(monkeypatch, tmp_path, capsys):
    """The negative case the test gap named: a chain whose DAG resolved AND
    carries a real (non-empty) code-review obligation set must NOT take the
    `chain_owes_no_code_review` vacuous-pass branch, even though its DAG
    resolution succeeded exactly like the zero-code-chain case above — an
    empty review-trail against a non-empty `chain_code_shas` must still
    refuse the cap, and must never print the vacuous-pass NOTE line."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_derive_dag_shas", lambda from_handoff: (str(tmp_path), list(_CHAIN_CODE_SHAS)))
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert "vacuously satisfied" not in err


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


def test_brightline_gate_uncovered_message_labels_planning_only_set_as_planning(
    monkeypatch, tmp_path, capsys,
):
    """A PLANNING-only uncovered set (every commit classifies PLANNING, e.g.
    the sibling incident's seven `docs/plans/**`/`state/sizings/**`
    commits) must render as planning-artifact commit(s), never as "code
    commit(s)" — the exact mislabel the audit traced a sibling EM chasing
    the wrong fix over."""
    chain_code_shas = ["planonly1", "planonly2"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset(chain_code_shas), None),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "UNCOVERED: 2 of 2 chain code commit(s)" in err
    assert "2 planning-artifact commit(s) (owe a plan review, not a code review):" in err
    assert "code commit(s):" not in err


def test_brightline_gate_uncovered_message_names_all_foreign_set_unrecordable(
    monkeypatch, tmp_path, capsys,
):
    """An uncovered set that is entirely foreign to the closing session
    (every commit authored by a predecessor session — the sibling
    deadlock's actual shape) must narrate the unrecordable-by-construction
    case, must NOT print the ordinary REMEDY line — no per-commit write
    this session could make would ever discharge them — and, with no
    `own_shas` entry to halt on, communicates rather than halting (exit 0).
    The retired vouch-waiver sanctioned exit must be gone from the message
    entirely, while the rest of the uncovered-set diagnostic (the header,
    the code/foreign split, and the basis line) survives its removal."""
    chain_code_shas = ["foreign1", "foreign2"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset(), None),
    )

    # A3 (2026-08-08): `_partition_foreign_uncovered_shas` now resolves
    # foreign/own from a real git window walk, not per-sha
    # `_resolve_foreign_session_shas` calls — these fixture shas
    # ("foreign1"/"foreign2") are not real commits, so the partition
    # itself is stubbed directly here; its own git-window behaviour is
    # covered by the dedicated `test_partition_foreign_uncovered_shas_*`
    # tests.
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (list(shas), []),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "UNCOVERED: 2 of 2 chain code commit(s)" in err
    assert "2 of these 2 commit(s) are unrecordable by an ordinary review-trail write" in err
    assert "Sanctioned exits: a PM vouch waiver, or /handoff." not in err
    assert "REMEDY: record a per-commit" not in err
    assert 'basis: "' in err


# ---------------------------------------------------------------------------
# spec-dispatch PLANNING exemption, live-path wiring (2026-08-13 dispatch:
# "spec-dispatch route and the wsc-brightline gate disagree on plan
# review"). `coverage.run_coverage_gate`'s own conditional exemption
# (`coverage._spec_dispatch_exempt_planning_shas`) never reached THIS
# HALT — it reads `_resolve_chain_planning_shas`/`_classify_bookkeeping_
# shas` directly, not `run_coverage_gate`'s result. These pin the
# re-derivation via `_resolve_chain_spec_dispatch_exempt_shas`.
# ---------------------------------------------------------------------------


def test_brightline_gate_spec_dispatch_exempt_planning_discharges_and_caps_cleanly(
    monkeypatch, tmp_path, capsys,
):
    """A PLANNING commit that clears both exemption gates (its plan's
    `scope_mode: spec-dispatch`, and a qualifying code review elsewhere in
    the chain) must be fully discharged — subtracted from `uncovered`, not
    merely relabeled — so the chain caps cleanly, and the discharge reason
    is surfaced on stderr."""
    chain_code_shas = ["planonly1", "code1"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    _patch_chain_scoping(monkeypatch, chain_code_shas=chain_code_shas)
    monkeypatch.setattr(_mod, "_resolve_chain_planning_shas", lambda from_handoff: ["planonly1"])
    monkeypatch.setattr(
        _mod,
        "_resolve_chain_spec_dispatch_exempt_shas",
        lambda from_handoff, planning_shas: (
            frozenset(planning_shas),
            {sha: _mod.SPEC_DISPATCH_EXEMPT_REASON for sha in planning_shas},
        ),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [_discharging_record(sha="code1")])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 0, err  # both chain members discharged: code1 by trail, planonly1 by exemption
    assert "HALT: brightline verdict=PARTITION-MANDATORY" not in err
    assert "chain code commit(s) carry no discharging review-trail" not in err
    assert f"1 PLANNING commit(s) {_mod.SPEC_DISPATCH_EXEMPT_REASON}" in err
    assert "['planonly1']" in err


def test_brightline_gate_spec_dispatch_exemption_absent_still_halts(
    monkeypatch, tmp_path, capsys,
):
    """Negative case: no qualifying code review means no exemption, always
    — the same PLANNING commit stays uncovered and the chain still halts,
    with no spec-dispatch NOTE printed."""
    chain_code_shas = ["planonly1", "code1"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    _patch_chain_scoping(monkeypatch, chain_code_shas=chain_code_shas)
    monkeypatch.setattr(_mod, "_resolve_chain_planning_shas", lambda from_handoff: ["planonly1"])
    monkeypatch.setattr(
        _mod,
        "_resolve_chain_spec_dispatch_exempt_shas",
        lambda from_handoff, planning_shas: (frozenset(), {}),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [_discharging_record(sha="code1")])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert _mod.SPEC_DISPATCH_EXEMPT_REASON not in err


def test_resolve_chain_spec_dispatch_exempt_shas_wires_the_two_gates(monkeypatch, tmp_path):
    """Unit-level pin on the resolver itself, mirroring `coverage.py`'s own
    `test_spec_dispatch_exemption_applies_with_qualifying_code_review`: a
    PLANNING sha whose plan carries `scope_mode: spec-dispatch` and is
    backed by a qualifying (non-waived, non-pending, diff-shaped)
    code-reviewer trail record over a CODE chain member is exempt; import
    reuses `coverage._spec_dispatch_exempt_planning_shas` rather than a
    second implementation (per the dispatch brief's constraint)."""
    from coordinator_core import coverage as cov_mod

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _make_commit(repo, "seed.txt", "C0: initial")

    plan_rel = "docs/plans/2026-08-13-spec-dispatch-example.md"
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / plan_rel).write_text(
        "---\nscope_mode: spec-dispatch\ndeliverable_id: dlv-example-1\n"
        "scope:\n  - src_example.py\n---\n\n"
        "# author plan\n",
        encoding="utf-8",
    )
    _git("add", plan_rel, cwd=repo)
    _git("commit", "-m", "author plan", cwd=repo)
    plan_sha = _git("rev-parse", "HEAD", cwd=repo)

    # The plan's own `scope:` frontmatter is the CODE-side attribution join
    # key (`_plan_scope_paths` + `_path_matches_scope`) — a reviewed code
    # commit touching a path under the plan's `scope:` supplies the
    # compensating control, plan-scoped, not chain-wide. (The
    # `Deliverable-Id` trailer below is fixture noise only — it names the
    # SESSION's held-claim deliverable in real chains, never the plan's own
    # `deliverable_id`, so it is deliberately NOT the join key here.)
    (repo / "src_example.py").write_text("implement plan")
    _git("add", "src_example.py", cwd=repo)
    _git(
        "commit", "-m", "implement plan\n\nDeliverable-Id: dlv-example-1",
        cwd=repo,
    )
    code_sha = _git("rev-parse", "HEAD", cwd=repo)

    record_path = tmp_path / "code_record.json"
    record_path.write_text(
        json.dumps({
            "sha_range": f"{plan_sha}..{code_sha}",
            "reviewer": "code-reviewer",
            "scope": "chain",
            "verdict": "ok",
            "scope_kind": "diff",
            "session_id": "own-sid",
        }),
        encoding="utf-8",
    )

    dag_shas = [plan_sha, code_sha]
    monkeypatch.setattr(_mod, "_derive_dag_shas", lambda from_handoff: (str(repo), dag_shas))
    monkeypatch.setattr(_mod, "_list_review_trail_paths", lambda: [str(record_path)])

    exempt, reasons = _mod._resolve_chain_spec_dispatch_exempt_shas(
        "state/handoffs/x.md", [plan_sha],
    )
    assert exempt == frozenset([plan_sha])
    assert reasons[plan_sha] == cov_mod.SPEC_DISPATCH_EXEMPT_REASON


def test_resolve_chain_spec_dispatch_exempt_shas_empty_input_is_a_noop(monkeypatch):
    """No candidate PLANNING shas at all must never even attempt DAG
    derivation — the cheapest possible fail-safe, matching every other
    resolver in this module."""
    def _boom(from_handoff):
        raise AssertionError("must not resolve the DAG for an empty candidate set")

    monkeypatch.setattr(_mod, "_derive_dag_shas", _boom)
    exempt, reasons = _mod._resolve_chain_spec_dispatch_exempt_shas("state/handoffs/x.md", [])
    assert exempt == frozenset()
    assert reasons == {}


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


def test_brightline_gate_own_commit_covered_foreign_ancestor_uncovered_caps_cleanly(
    monkeypatch, temp_git_repo, capsys,
):
    """AC1: a chain whose own commit carries a real `ok` review-trail
    verdict (written by the real writer, read back by the real
    `_load_trail_records`, never mocked) and whose only remaining uncovered
    commit is a foreign chain ancestor — `own_shas` is therefore empty —
    reaches a clean exit 0 with no HALT, the exact shape the vouch-free
    posture must still cap."""
    from coordinator_core.ops.review_trail_write import write_review_trail_entry

    foreign_sha = _make_commit(temp_git_repo, "ancestor.txt", "foreign predecessor commit")
    own_sha = _make_commit(temp_git_repo, "own.txt", "own reviewed commit")

    state_root = temp_git_repo / "state"
    monkeypatch.setenv("REVIEW_TRAIL_OUTPUT_ROOT", str(state_root))
    monkeypatch.setenv("COORDINATOR_ROOT", str(state_root))

    write_review_trail_entry(
        sha_range=f"{foreign_sha}..{own_sha}",
        reviewer="code-reviewer",
        scope="chain",
        verdict="ok",
        diff_loc=5,
        scope_kind="diff",
        session_id="own-sid",
    )

    monkeypatch.setattr(_mod, "_run_review_brightline_gate", lambda argv: (0, _TIER_B_STDOUT, ""))
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(_mod, "_resolve_closing_session_id", lambda repo_root: "own-sid")
    chain_code_shas = [foreign_sha, own_sha]
    _patch_chain_scoping(
        monkeypatch,
        chain_code_shas=chain_code_shas,
        range_shas_map={f"{foreign_sha}..{own_sha}": {own_sha}},
    )
    monkeypatch.setattr(
        _mod, "_resolve_foreign_session_shas",
        lambda sha_range, session_id: frozenset({foreign_sha}) if sha_range.startswith(foreign_sha) else frozenset(),
    )
    # `_load_trail_records` is deliberately left un-mocked — this test's
    # whole point is exercising the real disk read.

    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 0, err
    assert "HALT:" not in err
    assert "NOTE: brightline verdict=PARTITION-MANDATORY" in err
    assert "UNCOVERED: 1 of 2 chain code commit(s)" in err


def test_brightline_gate_own_shas_in_uncovered_set_still_halts_even_with_foreign(
    monkeypatch, tmp_path, capsys,
):
    """AC13: a chain whose undischarged uncovered set contains BOTH an
    `own_shas` entry (the closing session's own code commit with no
    discharging verdict) and a foreign-session entry still prints HALT and
    exits 1 — the one case C1 deliberately did not touch, unaffected by the
    vouch-waiver text's removal."""
    chain_code_shas = ["ownundischarged", "foreignundischarged"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset(), None),
    )

    # A3: see the identical fixture-shape note above — stubbed directly,
    # git-window mechanics covered elsewhere.
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (
            [s for s in shas if s == "foreignundischarged"],
            [s for s in shas if s != "foreignundischarged"],
        ),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 1, err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert "the review this chain mandates has not been run" in err
    assert (
        "REMEDY: record a per-commit review-trail verdict for each of the "
        "remaining 1"
    ) in err


def test_brightline_gate_uncovered_message_partitions_mixed_set(monkeypatch, tmp_path, capsys):
    """A mixed uncovered set — one own-session code commit, one foreign
    code commit, one own-session planning commit — must partition BOTH
    axes correctly: classification counts stay accurate, only the foreign
    subset is called unrecordable, and REMEDY names only the remaining
    (genuinely undischarged, own-session) count."""
    chain_code_shas = ["codeown", "codeforeign", "planown"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset({"planown"}), None),
    )

    # A3: see the identical fixture-shape note above — stubbed directly,
    # git-window mechanics covered elsewhere.
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (
            [s for s in shas if s == "codeforeign"],
            [s for s in shas if s != "codeforeign"],
        ),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "UNCOVERED: 3 of 3 chain code commit(s)" in err
    assert "2 code commit(s):" in err
    assert "1 planning-artifact commit(s) (owe a plan review, not a code review):" in err
    assert "1 of these 3 commit(s) are unrecordable by an ordinary review-trail write" in err
    assert "REMEDY: record a per-commit review-trail verdict for each of the remaining 2" in err


# ---------------------------------------------------------------------------
# 2026-08-10 narration fix (cross-repo/inbox/2026-08-10-doe-claude-em-
# brightline-unrecordable-narration-is-false.md): the foreign partition of the
# uncovered-set diagnostic must consult the chain-ancestry waiver store before
# asserting unrecordability. A waived foreign sha IS recordable — the write
# guard honours the waiver this same runner mints — and the message must name
# the per-commit `<sha>^..<sha>` form that works. Degradation direction is
# inverted from this file's usual narrowing posture: an empty/failed waiver
# lookup falls back to the old unrecordable wording, never to a false
# recordable claim.
# ---------------------------------------------------------------------------


def _patch_foreign_narration_case(monkeypatch, tmp_path, chain_code_shas, foreign, vouched):
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset(), None),
    )
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (
            [s for s in shas if s in foreign],
            [s for s in shas if s not in foreign],
        ),
    )
    monkeypatch.setattr(_mod, "_resolve_vouched_shas", lambda session_id: frozenset(vouched))
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])


def test_brightline_gate_all_foreign_waived_narrates_recordable_with_per_commit_range(
    monkeypatch, tmp_path, capsys,
):
    """The refuted case from the memo: every uncovered commit is foreign but
    every one carries a gate-minted chain-ancestry waiver for this chain, so
    each IS recordable. The message must say so and must name the concrete-
    endpoint per-commit form; the false unrecordable claim must be gone."""
    chain_code_shas = ["foreign1", "foreign2"]
    _patch_foreign_narration_case(
        monkeypatch, tmp_path, chain_code_shas,
        foreign=set(chain_code_shas), vouched=set(chain_code_shas),
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "2 of these 2 commit(s) are foreign-session" in err
    assert "recordable via a chain-ancestry waiver" in err
    assert 'coordinator-write-review-trail --sha-range "<sha>^..<sha>" --scope chain' in err
    assert "unrecordable by an ordinary review-trail write" not in err


def test_brightline_gate_all_foreign_unwaived_keeps_unrecordable_wording(
    monkeypatch, tmp_path, capsys,
):
    """No waiver for any foreign sha — today's unrecordable wording is
    correct and must survive verbatim, with no recordable claim."""
    chain_code_shas = ["foreign1", "foreign2"]
    _patch_foreign_narration_case(
        monkeypatch, tmp_path, chain_code_shas,
        foreign=set(chain_code_shas), vouched=set(),
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "2 of these 2 commit(s) are unrecordable by an ordinary review-trail write" in err
    assert "recordable via a chain-ancestry waiver" not in err


def test_brightline_gate_mixed_waiver_partitions_foreign_set_both_ways(
    monkeypatch, tmp_path, capsys,
):
    """One waived foreign sha and one unwaived: both sub-partitions render,
    each with its own count, and neither claim swallows the other."""
    chain_code_shas = ["foreignwaived", "foreignbare"]
    _patch_foreign_narration_case(
        monkeypatch, tmp_path, chain_code_shas,
        foreign=set(chain_code_shas), vouched={"foreignwaived"},
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "1 of these 2 commit(s) are foreign-session" in err
    assert "1 of these 2 commit(s) are unrecordable by an ordinary review-trail write" in err


def test_brightline_gate_waiver_lookup_failure_degrades_to_unrecordable(
    monkeypatch, tmp_path, capsys,
):
    """Inverted fail-safe: an empty waiver-lookup result (the shape
    `_resolve_vouched_shas` returns for an unreadable store, a missing repo
    root, or any raising reader) must degrade to the pre-existing
    unrecordable wording — never invent recordability."""
    chain_code_shas = ["foreign1"]
    _patch_foreign_narration_case(
        monkeypatch, tmp_path, chain_code_shas,
        foreign=set(chain_code_shas), vouched=set(),
    )
    monkeypatch.setattr(_mod, "_resolve_vouched_shas", lambda session_id: frozenset())
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "1 of these 1 commit(s) are unrecordable by an ordinary review-trail write" in err
    assert "recordable via a chain-ancestry waiver" not in err


# The mint-confirmation discriminator ("the chain-ancestry waiver mint
# could not be confirmed" / dead-child-vs-halting-gate narration) and its
# two pins (`test_brightline_gate_warns_when_the_mint_subprocess_failed_
# before_minting`, `test_brightline_gate_halting_gate_is_not_mistaken_for_a_
# failed_mint`) are removed outright along with the whole chain-ancestry-
# waiver mint mechanism (state/kill-ledger.md K-005, 2026-08-16 — "waiver
# system dies").


def test_brightline_gate_waiver_partition_does_not_move_denominator_or_verdict(
    monkeypatch, tmp_path, capsys,
):
    """Rendering-only pin: crediting waivers in the narration must not change
    `uncovered`/`chain_code_shas` counts, the HALT/NOTE branch, or the return
    code relative to the unwaived rendering of the same set."""
    chain_code_shas = ["foreign1", "foreign2"]
    _patch_foreign_narration_case(
        monkeypatch, tmp_path, chain_code_shas,
        foreign=set(chain_code_shas), vouched=set(chain_code_shas),
    )
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    err = capsys.readouterr().err
    assert rc == 0
    assert "UNCOVERED: 2 of 2 chain code commit(s)" in err
    assert "HALT:" not in err
    assert "REMEDY: record a per-commit" not in err


def test_brightline_gate_uncovered_denominator_and_verdict_unchanged_by_labeling(
    monkeypatch, tmp_path, capsys,
):
    """Regression pin: the rendering split must never touch the verdict
    token or the `N of M` denominator — a reader diffing this change must
    see only strings (and their supporting per-sha classification) move,
    never the halt decision or the counts it's built from. Exercised over
    the same mixed-classification, partially-foreign shape as the
    partition test above, so a future change that quietly narrows
    `chain_code_shas` or flips the verdict on a classification/foreign
    split would fail here even if it left the per-bucket labels intact."""
    chain_code_shas = ["codeown", "codeforeign", "planown"]
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: list(chain_code_shas))
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset({"planown"}), None),
    )

    # A3: see the identical fixture-shape note above — stubbed directly,
    # git-window mechanics covered elsewhere.
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (
            [s for s in shas if s == "codeforeign"],
            [s for s in shas if s != "codeforeign"],
        ),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert (
        "UNCOVERED: 3 of 3 chain code commit(s) carry no discharging "
        "review-trail verdict (no record's range names them):"
    ) in err


def test_brightline_gate_dag_resolution_failure_does_not_take_vacuous_pass(monkeypatch, tmp_path, capsys):
    """`dag_resolution_failed` (DAG derivation itself indeterminate) is a
    DIFFERENT condition from a legitimately-empty `chain_code_shas` — it
    must not be mistaken for "this chain owes no code review". With
    `_derive_dag_shas` returning `None` and no chain-code/dag information
    resolvable, the gate must still refuse the cap on an empty trail,
    never take the vacuous-pass branch."""
    _patch_brightline_no_persist_seam(monkeypatch, tmp_path)
    monkeypatch.setattr(_mod, "_derive_dag_shas", lambda from_handoff: None)
    monkeypatch.setattr(_mod, "_resolve_chain_code_shas", lambda from_handoff: [])
    monkeypatch.setattr(_mod, "_resolve_chain_dag_shas", lambda from_handoff: [])
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])
    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "HALT: brightline verdict=PARTITION-MANDATORY" in err
    assert "vacuously satisfied" not in err


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


def _range_shas_lookup(mapping):
    """Builds a `resolve_range_shas` callable over a fixed
    {sha_range: covered_shas} mapping — the test double for
    `_resolve_range_shas`'s real `git rev-list` call."""
    return lambda sha_range: mapping.get(sha_range, set())


def test_collect_uncovered_shas_unions_multiple_in_chain_records():
    """Multiple in-chain, well-formed, non-pending/waived records union
    their resolved ranges to cover the full chain code-sha set."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2", "ccccccc3"]
    records = [
        {"verdict": "ok", "sha_range": "base1..r1"},
        {"verdict": "ok", "sha_range": "base2..r2"},
        {"verdict": "ok", "sha_range": "base3..r3"},
    ]
    resolve_range_shas = _range_shas_lookup(
        {"base1..r1": {"aaaaaaa1"}, "base2..r2": {"bbbbbbb2"}, "base3..r3": {"ccccccc3"}}
    )
    assert chain_partition_uncovered_shas(records, chain_code_shas, chain_code_shas, resolve_range_shas) == []
    assert chain_partition_verdict_discharged(records, chain_code_shas, chain_code_shas, resolve_range_shas)


def test_collect_uncovered_shas_names_the_uncovered_sha_when_one_record_missing():
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2", "ccccccc3"]
    records = [
        {"verdict": "ok", "sha_range": "base1..r1"},
        {"verdict": "ok", "sha_range": "base2..r2"},
    ]
    resolve_range_shas = _range_shas_lookup({"base1..r1": {"aaaaaaa1"}, "base2..r2": {"bbbbbbb2"}})
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["ccccccc3"]
    assert not chain_partition_verdict_discharged(records, chain_code_shas, chain_code_shas, resolve_range_shas)


def test_collect_uncovered_shas_pending_or_waived_record_does_not_contribute():
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2", "ccccccc3"]
    records = [
        {"verdict": "ok", "sha_range": "base1..r1"},
        {"verdict": "ok", "sha_range": "base2..r2"},
        {"verdict": "pending", "sha_range": "base3..r3"},
    ]
    resolve_range_shas = _range_shas_lookup(
        {"base1..r1": {"aaaaaaa1"}, "base2..r2": {"bbbbbbb2"}, "base3..r3": {"ccccccc3"}}
    )
    # r3's range is a genuine in-chain member too — verdict alone must
    # exclude it.
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["ccccccc3"]


def test_collect_uncovered_shas_resolver_empty_result_does_not_contribute():
    """A record whose `sha_range` resolves to an EMPTY set (the test double
    for a `git rev-list` failure `_resolve_range_shas` could not positively
    resolve) contributes nothing to the union — fail-safe-toward-refusal,
    never a silent guess. Uses a well-formed, non-`HEAD` range — the
    dedicated `..HEAD` rejection is pinned separately below at the
    `_record_membership_shas` level, where the real defence lives (2026-
    08-06 review-integrator finding B1: this test previously used
    `"abc..HEAD"` here and its docstring claimed to pin the `..HEAD`
    defence, but the test double's empty-set return was doing all the
    work — `git rev-list abc..HEAD` does not actually fail, so nothing
    here ever exercised the real resolver's live-HEAD-resolution hazard)."""
    chain_code_shas = ["aaaaaaa1", "ccccccc3"]
    records = [
        {"verdict": "ok", "sha_range": "base1..r1"},
        {"verdict": "ok", "sha_range": "base2..r2"},
    ]
    resolve_range_shas = _range_shas_lookup({"base1..r1": {"aaaaaaa1"}, "base2..r2": set()})
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["ccccccc3"]


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


def test_record_membership_plan_scope_kind_credits_nothing_without_planning_set():
    """2026-08-07 correction (`state/audits/2026-08-07-wsc-chain-gate-counts-
    doc-only-commits.md` Q4's "second gap"): `scope_kind: "plan"` is no
    longer an unconditional reject — a caller that supplies
    `chain_planning_sha_set` can credit a plan review against a PLANNING
    commit (see the sibling test below). A caller that omits it (the
    default, `None`) sees the same net effect as before this correction:
    the record credits nothing — but it now falls through the same
    membership machinery a `"diff"` record does (SAFE_RANGE, stored-HEAD,
    resolver, foreign-narrowing) rather than being rejected before the
    resolver is even consulted, so the resolver IS called here."""
    record = {"verdict": "ok", "sha_range": "base..tip", "scope_kind": "plan"}
    assert _record_membership_shas(
        record, _range_shas_lookup({"base..tip": {"aaaaaaa1"}}), {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None


def test_record_membership_plan_scope_kind_credits_only_planning_shas():
    """The positive leg of the 2026-08-07 correction: a `scope_kind: "plan"`
    record's contribution is `raw & chain_planning_sha_set` — capped at the
    PLANNING-classified subset of `chain_code_shas`, never the full
    `chain_code_shas` set. `bbbbbbb2` is a plain CODE commit in the same
    chain; the plan record's range names both `aaaaaaa1` (PLANNING) and
    `bbbbbbb2` (CODE), but only `aaaaaaa1` is credited."""
    record = {"verdict": "ok", "sha_range": "base..tip", "scope_kind": "plan"}
    result = _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1", "bbbbbbb2"}}),
        {"aaaaaaa1", "bbbbbbb2"},
        {"aaaaaaa1", "bbbbbbb2"},
        chain_planning_sha_set={"aaaaaaa1"},
    )
    assert result == {"aaaaaaa1"}


def test_record_membership_narrows_session_scoped_record_to_own_session_shas():
    """2026-08-06 review-integrator finding W2: a `scope="session"` record
    only credits commits belonging to ITS OWN session — mirrors
    `coverage.build_reviewed_set`'s `shas - trailer_foreign_shas(...)`
    narrowing for `_FOREIGN_STRIPPED_SCOPES` (`session`/`chain`/
    `workstream-close-auto`). `bbbbbbb2` is stripped as foreign even though
    it is a chain-DAG member and the record's raw range names it."""
    chain_dag_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    chain_code_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    record = {"verdict": "ok", "sha_range": "base..tip", "scope": "session", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        assert sha_range == "base..tip"
        assert session_id == "own-sid"
        return {"bbbbbbb2"}  # bbbbbbb2 belongs to a DIFFERENT session

    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1", "bbbbbbb2"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
        narrow_foreign_shas=narrow_foreign_shas,
    ) == {"aaaaaaa1"}


def test_record_membership_credits_a_commit_the_writing_session_owns():
    """`state/audits/2026-08-07-wsc-chain-gate-counts-doc-only-commits.md`
    reproduction: a session/chain-scoped record naming a commit that
    carries the WRITING session's own `Session-Id` trailer must be
    credited, not stripped as foreign. `narrow_foreign_shas` here mirrors
    `wsc-coverage-gate-runner._resolve_foreign_session_shas`'s own contract
    — it returns the commits whose trailer does NOT match `session_id` —
    and correctly reports the empty set for a self-owned single-commit
    range, so the record must discharge `aaaaaaa1`."""
    chain_dag_sha_set = {"aaaaaaa1"}
    chain_code_sha_set = {"aaaaaaa1"}
    record = {"verdict": "ok", "sha_range": "aaaaaaa1^..aaaaaaa1", "scope": "session", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        assert session_id == "own-sid"
        return set()  # every commit in this range carries the WRITING session's own trailer

    assert _record_membership_shas(
        record,
        _range_shas_lookup({"aaaaaaa1^..aaaaaaa1": {"aaaaaaa1"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
        narrow_foreign_shas=narrow_foreign_shas,
    ) == {"aaaaaaa1"}


def test_record_membership_chain_scope_does_not_raise():
    """`chain` is a documented valid `--scope` enum value (SKILL.md's
    `decisions["review"]` closed-enum table) and is a member of
    `coverage._FOREIGN_STRIPPED_SCOPES` exactly like `session` — this
    pins that a `scope="chain"` record flows through the SAME narrowing
    path as `scope="session"` with no `KeyError` or other exception, and
    correctly discharges a commit the narrowing confirms is not foreign."""
    chain_dag_sha_set = {"aaaaaaa1"}
    chain_code_sha_set = {"aaaaaaa1"}
    record = {"verdict": "ok", "sha_range": "aaaaaaa1^..aaaaaaa1", "scope": "chain", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        return set()

    result = _record_membership_shas(
        record,
        _range_shas_lookup({"aaaaaaa1^..aaaaaaa1": {"aaaaaaa1"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
        narrow_foreign_shas=narrow_foreign_shas,
    )
    assert result == {"aaaaaaa1"}


def test_record_membership_foreign_narrowing_failure_rejects_record():
    """`narrow_foreign_shas` raising (the same fail-closed posture
    `coverage.build_reviewed_set`'s `_ForeignSessionLookupError` handling
    already applies to this exact narrowing) rejects the record entirely —
    never a silent fallback to crediting it unnarrowed."""
    record = {"verdict": "ok", "sha_range": "base..tip", "scope": "session", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        raise RuntimeError("git log failed")

    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1"}}),
        {"aaaaaaa1"},
        {"aaaaaaa1"},
        narrow_foreign_shas=narrow_foreign_shas,
    ) is None


def test_record_membership_vouched_foreign_sha_is_not_narrowed():
    """2026-08-06 read-side vouch-honouring fix: a foreign-attributed commit
    that carries a PM vouch (or a matching gate-minted chain-ancestry
    waiver) must NOT be narrowed out of the record's coverage — the write
    side's `ForeignSessionRangeRefused` guard names the vouch as the
    sanctioned remedy for exactly this case, and the read side must honour
    it. `bbbbbbb2` is foreign-attributed (per `narrow_foreign_shas`) but
    also vouched (per `vouched_shas`) — it is credited."""
    chain_dag_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    chain_code_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    record = {"verdict": "ok", "sha_range": "base..tip", "scope": "chain", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        return {"bbbbbbb2"}

    def vouched_shas(session_id):
        assert session_id == "own-sid"
        return {"bbbbbbb2"}

    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1", "bbbbbbb2"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
        narrow_foreign_shas=narrow_foreign_shas,
        vouched_shas=vouched_shas,
    ) == {"aaaaaaa1", "bbbbbbb2"}


def test_record_membership_same_record_without_vouch_still_narrows():
    """The pinned regression: the SAME record, SAME foreign narrowing,
    with no vouch present (`vouched_shas` returns an empty set) — the
    existing narrowing behaviour stays exactly as before this fix."""
    chain_dag_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    chain_code_sha_set = {"aaaaaaa1", "bbbbbbb2"}
    record = {"verdict": "ok", "sha_range": "base..tip", "scope": "chain", "session_id": "own-sid"}

    def narrow_foreign_shas(sha_range, session_id):
        return {"bbbbbbb2"}

    def vouched_shas(session_id):
        return set()

    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1", "bbbbbbb2"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
        narrow_foreign_shas=narrow_foreign_shas,
        vouched_shas=vouched_shas,
    ) == {"aaaaaaa1"}


def test_record_membership_rejects_unrecognized_scope_instead_of_skipping_narrowing():
    """Review: review-integrator — B1 (2026-08-06, brightline-discharge
    round4). An on-disk record whose `scope` is anything other than
    `session`/`chain`/`workstream-close-auto` (e.g. an archived record
    carrying a comma-joined file list as `scope`) used to SKIP narrowing
    entirely and receive full-width credit — a fail-OPEN bypass of the
    whole trust filter under intersection membership, where narrowing IS
    the defence. It must now reject the record outright, exactly like an
    unrecognized `scope_kind` does."""

    def _narrow_must_not_be_called(sha_range, session_id):
        raise AssertionError("narrow_foreign_shas must not be consulted for a rejected record")

    record = {
        "verdict": "ok",
        "sha_range": "base..tip",
        "scope": "state/handoffs/x.md,state/handoffs/y.md",  # comma-joined file list, not a valid scope
        "session_id": "own-sid",
    }
    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1"}}),
        {"aaaaaaa1"},
        {"aaaaaaa1"},
        narrow_foreign_shas=_narrow_must_not_be_called,
    ) is None


def test_record_membership_missing_scope_is_rejected_when_narrowing_is_active():
    """A legacy/absent `scope` key is likewise not one of the three
    recognized values — same fail-closed posture as an explicitly
    unrecognized string, since `narrow_foreign_shas is not None` means
    narrowing is the operative trust boundary for this call."""

    def _narrow_must_not_be_called(sha_range, session_id):
        raise AssertionError("narrow_foreign_shas must not be consulted for a rejected record")

    record = {"verdict": "ok", "sha_range": "base..tip", "session_id": "own-sid"}
    assert _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"aaaaaaa1"}}),
        {"aaaaaaa1"},
        {"aaaaaaa1"},
        narrow_foreign_shas=_narrow_must_not_be_called,
    ) is None


def test_record_membership_recognized_scope_still_narrows_when_narrowing_active():
    """Regression pin alongside B1: the three recognized `_FOREIGN_STRIPPED_
    SCOPES` values must still reach narrowing exactly as before — B1's fix
    only closes the unrecognized-scope bypass, it does not touch the
    well-formed path."""
    calls = []

    def narrow_foreign_shas(sha_range, session_id):
        calls.append((sha_range, session_id))
        return set()

    for scope in ("session", "chain", "workstream-close-auto"):
        record = {"verdict": "ok", "sha_range": "base..tip", "scope": scope, "session_id": "own-sid"}
        assert _record_membership_shas(
            record,
            _range_shas_lookup({"base..tip": {"aaaaaaa1"}}),
            {"aaaaaaa1"},
            {"aaaaaaa1"},
            narrow_foreign_shas=narrow_foreign_shas,
        ) == {"aaaaaaa1"}
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# Review: review-integrator — test gap flagged in brightline-discharge
# round4 (2026-08-06): "no test bounds the intersection rule; every
# membership test asserts the permissive direction. Add tests that fail if
# intersection credits more than it should."
# ---------------------------------------------------------------------------


def test_record_membership_intersection_credits_only_the_shared_commits_never_the_whole_range():
    """A record's raw resolved range spans MANY commits, only SOME of which
    are chain-DAG members. Membership passes on the non-empty intersection,
    but the CREDITED set must be exactly `raw & chain_code_sha_set` — never
    the full raw range, and never the full `chain_code_sha_set` either.
    `zzzzzzz9` (a peer/foreign commit, not a chain-DAG member at all) and
    `yyyyyyy8` (a chain-DAG member but NOT a chain-code member, e.g.
    ceremony bookkeeping) must both be excluded from the credited set."""
    chain_dag_sha_set = {"aaaaaaa1", "yyyyyyy8"}
    chain_code_sha_set = {"aaaaaaa1"}
    record = {"verdict": "ok", "sha_range": "base..tip"}
    raw_range = {"aaaaaaa1", "yyyyyyy8", "zzzzzzz9"}

    result = _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": raw_range}),
        chain_dag_sha_set,
        chain_code_sha_set,
    )
    assert result == {"aaaaaaa1"}
    assert "zzzzzzz9" not in result
    assert "yyyyyyy8" not in result


def test_record_membership_intersection_with_zero_chain_code_overlap_credits_nothing():
    """A record whose raw range touches this chain's UNFILTERED DAG (so
    membership passes) but shares NO commit with the FILTERED `chain_code_
    sha_set` (e.g. it only names a same-chain bookkeeping commit) must
    contribute the EMPTY set — membership passing is not itself coverage."""
    chain_dag_sha_set = {"bookkeeping1"}
    chain_code_sha_set = {"aaaaaaa1"}
    record = {"verdict": "ok", "sha_range": "base..tip"}

    result = _record_membership_shas(
        record,
        _range_shas_lookup({"base..tip": {"bookkeeping1"}}),
        chain_dag_sha_set,
        chain_code_sha_set,
    )
    assert result == set()


def test_collect_discharging_range_shas_wide_record_does_not_over_credit_beyond_intersection():
    """End-to-end bound (via `_collect_discharging_range_shas`'s public
    siblings): a single WIDE trail record whose resolved range spans every
    `chain_code_sha` plus a large number of non-chain commits must credit
    EXACTLY `chain_code_shas` — no more, and the returned uncovered list
    must be empty only because every chain-code sha is actually named, not
    because the wide range was trusted wholesale. (Review R3: only the
    FIRST assertion below pins the over-credit bound; see the comment above
    the second block for what that half actually exercises.)"""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2"]
    chain_dag_shas = chain_code_shas
    wide_raw_range = set(chain_code_shas) | {f"peer{i}" for i in range(50)}
    records = [{"verdict": "ok", "sha_range": "base..tip"}]
    resolve_range_shas = _range_shas_lookup({"base..tip": wide_raw_range})

    assert chain_partition_uncovered_shas(records, chain_code_shas, chain_dag_shas, resolve_range_shas) == []
    assert chain_partition_verdict_discharged(records, chain_code_shas, chain_dag_shas, resolve_range_shas)

    # Review R3 (2026-08-06): this second half exercises the MEMBERSHIP leg,
    # not the over-credit bound named above — `unrelated_chain_code_shas`
    # doubles as both `chain_code_shas` and `chain_dag_shas`, so the record's
    # `raw & chain_dag_sha_set` intersection is already empty and
    # `_record_membership_shas` returns `None` before any credit arithmetic
    # runs at all. It still asserts something true (a wide record cannot
    # manufacture coverage for a chain it never touches), just not the
    # "credits no more than the intersection" claim the first half already
    # covers — kept as a membership-rejection regression pin under its own
    # name rather than deleted, since the property it does pin is real.
    unrelated_chain_code_shas = ["not-in-any-range"]
    assert chain_partition_uncovered_shas(
        records, unrelated_chain_code_shas, unrelated_chain_code_shas, resolve_range_shas,
    ) == ["not-in-any-range"]


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


def test_resolve_foreign_session_shas_treats_untrailered_commit_as_foreign(monkeypatch, temp_git_repo):
    """B3: a commit with NO `Session-Id` trailer at all must be treated as
    foreign to every session — `trailer_foreign_shas`'s exclusion-based
    posture left it creditable to any spanning record; this narrowing must
    now positively attribute a commit to `own_session_id` before treating it
    as non-foreign.

    Review F1 (2026-08-06): `git log`'s default ordering is TOPOLOGICAL for
    a straight-line chain — a commit is never printed before its own parent,
    regardless of commit date (verified live: backdating a child's
    committer date below its parent's does not reorder `git log`'s output
    for a two-commit linear range; git's walk still enqueues the parent only
    after visiting the child). So the only way to make `untrailered` the
    LAST line of `_git_run_for_session_attribution`'s output — the position
    where a whole-output `.strip()` used to eat the trailing `\\x1f` and
    cause the F1 bypass — is to make it the OLDEST commit in the range by
    construction: authored BEFORE `own`, not after. Placing the subject
    newest (as this test previously did, and as B3's scenario more
    naturally reads) cannot exercise that position at all."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    base = _make_commit(temp_git_repo, "base.txt", "base")
    untrailered = _make_commit(temp_git_repo, "untrailered.txt", "no trailer at all")
    own = _commit_with_session_trailer(temp_git_repo, "own.txt", "own commit", "own-sid")

    foreign = _mod._resolve_foreign_session_shas(f"{base}..{own}", "own-sid")
    assert untrailered in foreign
    assert own not in foreign


def test_resolve_foreign_session_shas_treats_untrailered_merge_commit_as_foreign(monkeypatch, temp_git_repo):
    """B2: an UNTRAILERED merge commit within `sha_range` must be treated as
    foreign — `session_attribution.trailer_foreign_shas`'s `git log
    --no-merges` never even enumerates a merge commit, so it was never
    flagged foreign regardless of which session's range spans it. This
    narrowing walks WITH merges included and requires positive attribution.

    Review R2 (2026-08-06): this pins "an untrailered merge is foreign", not
    "a merge is foreign unconditionally" — the merge here carries no
    `Session-Id` trailer at all, so its untrailered-ness is what actually
    drives the assertion (same mechanism the plain untrailered-commit test
    above exercises). Chain membership itself runs `--no-merges`
    (`coverage._derive_dag_chain_set` step 3), so no merge commit can ever
    be a chain-DAG/chain-code member in the first place — this is
    defence-in-depth, not something a real unsatisfiable-chain case turns
    on.

    Review F1 (2026-08-06): the reviewer asked that this test also be
    restructured so its subject isn't newest. It is NOT true in general that
    a merge can never be `git log`'s last line: whether a merge is a range's
    last-printed line depends only on how the range's own base was chosen
    relative to the merge's two parents, not on any property of merges as
    such. When the range's base is on the merge's FIRST-parent side (as
    `base..merge_sha` below is — `base` is an ancestor of `main`, the merge's
    first parent), the merge's non-fast-forward second parent (`side` here)
    always carries at least one commit not already reachable from `base` — a
    merge with no such unique second-parent content is refused by git as
    "Already up to date," not created at all — so that unique commit is
    always IN the printed range and OLDER than the merge, putting the merge
    ahead of it, not last. Review F-A (2026-08-06, code-reviewer): this
    repo's own history disproves the stronger "structurally impossible"
    claim this docstring previously made — a `side..merge` range (second
    parent as the base) makes the merge the ONLY, and therefore last,
    printed line; at least five such `--no-ff` merges exist in this repo's
    last 40 commits. The `base..merge_sha` assertions below are left as they
    were (they do not, and were never claimed to, exercise the F1 last-line
    position — the sibling untrailered-commit test above does that); a
    second assertion is added over `side_tip..merge_sha` to pin the
    last-line position this test's docstring previously claimed was
    unreachable."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    import os as _os

    base = _make_commit(temp_git_repo, "base.txt", "base")
    main_branch = _git("symbolic-ref", "--short", "HEAD", cwd=temp_git_repo)
    own = _commit_with_session_trailer(temp_git_repo, "own.txt", "own commit", "own-sid")
    # `side` branches AFTER `own` and `main` does not advance past `own`
    # before the merge — the exact shape review F-A's counterexample names:
    # the merge's first parent (`own`) is an ancestor of its second
    # (`side_tip`), so `side_tip..merge_sha` excludes the first-parent path
    # entirely and the merge is that range's only printed line.
    _git("checkout", "-b", "side", cwd=temp_git_repo)
    side_tip = _make_commit(temp_git_repo, "side.txt", "side commit")
    _git("checkout", main_branch, cwd=temp_git_repo)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    _git("merge", "--no-ff", "-m", "merge side into main", "side", cwd=temp_git_repo, env=env)
    merge_sha = _git("rev-parse", "HEAD", cwd=temp_git_repo)

    foreign = _mod._resolve_foreign_session_shas(f"{base}..{merge_sha}", "own-sid")
    assert merge_sha in foreign
    assert own not in foreign

    # Pin the F1 last-line position itself (review F-A remedy): with the
    # second parent as the range's base, the merge is the range's ONLY
    # (hence last) printed line.
    log_lines = _git("log", "--format=%H", f"{side_tip}..{merge_sha}", cwd=temp_git_repo)
    assert log_lines.strip() == merge_sha

    foreign_last_line = _mod._resolve_foreign_session_shas(f"{side_tip}..{merge_sha}", "own-sid")
    assert merge_sha in foreign_last_line

    foreign_last_line = _mod._resolve_foreign_session_shas(f"{side_tip}..{merge_sha}", "own-sid")
    assert merge_sha in foreign_last_line


def test_resolve_foreign_session_shas_own_trailer_still_not_foreign(monkeypatch, temp_git_repo):
    """Regression pin: a commit whose OWN `Session-Id` trailer matches
    `own_session_id` must still NOT be foreign — B2/B3's inclusive rewrite
    must not regress the well-formed, positively-attributed case."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    base = _make_commit(temp_git_repo, "base.txt", "base")
    own = _commit_with_session_trailer(temp_git_repo, "own.txt", "own commit", "own-sid")

    foreign = _mod._resolve_foreign_session_shas(f"{base}..{own}", "own-sid")
    assert own not in foreign


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


def test_resolve_foreign_session_shas_credits_chain_member_with_unparseable_trailer(monkeypatch, temp_git_repo):
    """Regression pin for R1 (2026-08-06): a commit whose `Session-Id:` line
    is followed by a non-trailer footer line is a CHAIN MEMBER by
    `--grep=^Session-Id: <sid>$` (the same predicate `coverage._derive_dag_
    chain_set` step 3 uses), but its trailer block is unparseable by
    `%(trailers:key=Session-Id,valueonly)`. Before the R1 fix this made the
    commit permanently foreign — creditable by no record at all, making a
    PARTITION-MANDATORY verdict on a chain containing it unsatisfiable.
    `_resolve_foreign_session_shas` must subtract the grep-attributed shas
    back out of `foreign`, so this commit is NOT foreign.

    Review F2 (2026-08-06): the original shape of this test used a range
    holding exactly ONE commit (`base..broken`), so `broken` was
    unconditionally the LAST `git log` line and F1's bypass carried it to
    the correct answer for the wrong reason — the assertion held whether or
    not the R1 subtraction ran at all, so the pin was vacuous (confirmed by
    neutralising `_grep_attributed_session_shas` to always return
    `frozenset()`: the test still passed). A well-formed, positively-
    attributed commit is appended AFTER `broken` so `broken` is no longer
    the last log line, and the range extends to that trailing commit — the
    R1 subtraction is now the only thing that can put `broken` outside
    `foreign`.

    Review F3 (2026-08-06): `_grep_attributed_session_shas` now shape-
    validates `session_id` against `coverage._UUID_RE` before interpolating
    it into `--grep=`, so a non-UUID-shaped id like the file's usual
    `"own-sid"` placeholder would silently take the guard's empty-set path
    and this pin would go back to passing for the wrong reason. A
    UUID-shaped id is used here instead so the grep leg genuinely fires."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    session_id = "deadbeef-1234-4abc-8def-0123456789ab"
    base = _make_commit(temp_git_repo, "base.txt", "base")
    broken = _commit_with_unparseable_trailing_session_trailer(
        temp_git_repo, "broken.txt", "own commit with trailing footer", session_id,
    )
    tip = _commit_with_session_trailer(temp_git_repo, "tip.txt", "trailing well-formed commit", session_id)

    foreign = _mod._resolve_foreign_session_shas(f"{base}..{tip}", session_id)
    assert broken not in foreign
    assert tip not in foreign


def test_grep_attributed_session_shas_rejects_non_uuid_session_id(monkeypatch, temp_git_repo):
    """F3 (2026-08-06, code-reviewer F-B): the injection guard on
    `_grep_attributed_session_shas` had zero test coverage — the reviewer
    ran the full suite with `_UUID_RE` disabled and all 72 tests still
    passed. Pins the guard directly: a `session_id` shaped like a regex
    metacharacter sequence (`.*`, matches every commit unboundedly; `^`,
    matches the start of every line) must be rejected BEFORE interpolation
    into `--grep=`, yielding the caller's existing foreign-by-default
    posture (empty frozenset — no subtraction), never a wildcard match
    against real commits in `sha_range`."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    base = _make_commit(temp_git_repo, "base.txt", "base")
    tip = _commit_with_session_trailer(temp_git_repo, "tip.txt", "some commit", "some-other-sid")

    for malformed in (".*", "^"):
        assert _mod._grep_attributed_session_shas(f"{base}..{tip}", malformed) == frozenset()


def test_grep_attributed_session_shas_resolves_well_formed_uuid(monkeypatch, temp_git_repo):
    """F3 companion pin: a well-formed UUID-shaped `session_id` must still
    resolve normally through the guard — the shape check narrows, it does
    not blanket-disable the grep leg for every caller."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    session_id = "deadbeef-1234-4abc-8def-0123456789ab"
    base = _make_commit(temp_git_repo, "base.txt", "base")
    tip = _commit_with_session_trailer(temp_git_repo, "tip.txt", "own commit", session_id)

    assert _mod._grep_attributed_session_shas(f"{base}..{tip}", session_id) == frozenset({tip})


def test_partition_foreign_uncovered_shas_no_session_id_returns_all_own(monkeypatch, temp_git_repo):
    """A3: `session_id=None` (closing session unresolvable) must degrade
    every sha to `own` without spawning at all — this diagnostic must never
    assert a commit is foreign when it cannot positively confirm who the
    closing session even is."""
    sha_a = _make_commit(temp_git_repo, "a.txt", "commit A")
    calls = []
    monkeypatch.setattr(
        _mod, "_git_run_for_session_attribution",
        lambda *a, **k: (calls.append(a) or (1, "", "should not spawn")),
    )
    assert _mod._partition_foreign_uncovered_shas([sha_a], None) == ([], [sha_a])
    assert calls == []


def test_partition_foreign_uncovered_shas_empty_shas_is_a_noop(monkeypatch):
    """An empty `shas` list degrades to `([], [])` without resolving a repo
    root or spawning."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert _mod._partition_foreign_uncovered_shas([], "own-sid") == ([], [])


def test_partition_foreign_uncovered_shas_all_trailered_skips_grep_spawn(monkeypatch, temp_git_repo):
    """A3's 2N-not-N fix: when every sha in the window carries an
    unambiguous trailer (own or foreign), the grep leg must never spawn —
    only ONE `git log` call total for the whole batch, not one per sha and
    not a second grep call it does not need."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    own_sid = "deadbeef-1234-4abc-8def-0123456789ab"
    other_sid = "aaaaaaaa-1234-4abc-8def-0123456789ab"
    own = _commit_with_session_trailer(temp_git_repo, "own.txt", "own commit", own_sid)
    other = _commit_with_session_trailer(temp_git_repo, "other.txt", "other commit", other_sid)

    real_run = _mod._git_run_for_session_attribution
    spawn_count = {"n": 0}

    def _counted(*args, **kwargs):
        spawn_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(_mod, "_git_run_for_session_attribution", _counted)

    foreign, own_list = _mod._partition_foreign_uncovered_shas([own, other], own_sid)

    assert foreign == [other]
    assert own_list == [own]
    assert spawn_count["n"] == 1


def test_partition_foreign_uncovered_shas_untrailered_needs_grep_spawn(monkeypatch, temp_git_repo):
    """An untrailered-but-grep-attributed commit needs the second (grep)
    spawn to resolve — exactly 2 spawns total for the whole batch, matching
    the "2 spawns per element" pre-fix measurement collapsed to "2 spawns
    total" post-fix."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    own_sid = "deadbeef-1234-4abc-8def-0123456789ab"
    base = _make_commit(temp_git_repo, "base.txt", "base")
    grep_only = _make_commit(temp_git_repo, "grep_only.txt", f"Session-Id: {own_sid}\nfooter line")
    untrailered_foreign = _make_commit(temp_git_repo, "untrailered.txt", "no session line at all")

    real_run = _mod._git_run_for_session_attribution
    spawn_count = {"n": 0}

    def _counted(*args, **kwargs):
        spawn_count["n"] += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(_mod, "_git_run_for_session_attribution", _counted)

    foreign, own_list = _mod._partition_foreign_uncovered_shas(
        [grep_only, untrailered_foreign], own_sid,
    )

    assert grep_only in own_list
    assert untrailered_foreign in foreign
    assert spawn_count["n"] == 2
    _ = base  # anchors the range git resolves from; not asserted on directly


def test_partition_foreign_uncovered_shas_merge_commit_is_foreign(monkeypatch, temp_git_repo):
    """A merge commit is unconditionally foreign, regardless of any
    Session-Id trailer it carries (fail-closed P2 posture,
    `chain_attribution.foreign_shas_from_window`)."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    own_sid = "deadbeef-1234-4abc-8def-0123456789ab"
    base = _make_commit(temp_git_repo, "base.txt", "base")
    _git("checkout", "-b", "side", cwd=temp_git_repo)
    side = _make_commit(temp_git_repo, "side.txt", "side commit")
    _git("checkout", "-", cwd=temp_git_repo)
    other = _commit_with_session_trailer(temp_git_repo, "other.txt", "other commit", own_sid)
    _git("merge", "--no-ff", "-m", f"merge\n\nSession-Id: {own_sid}", side, cwd=temp_git_repo)
    merge_sha = _git("rev-parse", "HEAD", cwd=temp_git_repo)

    foreign, _own_list = _mod._partition_foreign_uncovered_shas([merge_sha], own_sid)

    assert foreign == [merge_sha]
    _ = base, other  # anchors for a realistic repo shape


def test_partition_foreign_uncovered_shas_repo_root_unresolvable_degrades_whole_batch(monkeypatch):
    """An unresolvable repo root must degrade the WHOLE batch to `own`,
    never per-sha — this is the A3 posture change from the prior per-sha
    degrade."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: None)
    assert _mod._partition_foreign_uncovered_shas(["a" * 40, "b" * 40], "own-sid") == (
        [], ["a" * 40, "b" * 40],
    )


def test_partition_foreign_uncovered_shas_window_build_failure_degrades_whole_batch(monkeypatch, temp_git_repo):
    """A failed window build (non-zero `git log` rc, or an unresolvable
    root inside the helper) must degrade the WHOLE batch to `own` — never a
    false foreign claim on the shas the window walk never confirmed."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    monkeypatch.setattr(_mod, "_resolve_uncovered_commit_attribution_window", lambda shas, repo_root: None)
    shas = ["a" * 40, "b" * 40]
    assert _mod._partition_foreign_uncovered_shas(shas, "own-sid") == ([], shas)


def test_partition_foreign_uncovered_shas_sha_absent_from_window_degrades_whole_batch(monkeypatch, temp_git_repo):
    """A sha named in `shas` but not returned by the window walk is a
    coverage-gap in the window itself — absence must never be read as
    'untrailered' (§ Anti-scope 25 / § Anti-scope 8's three-way
    distinction). The WHOLE batch degrades to `own`, not just the missing
    sha."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    own_sid = "deadbeef-1234-4abc-8def-0123456789ab"
    own = _commit_with_session_trailer(temp_git_repo, "own.txt", "own commit", own_sid)
    missing_sha = "c" * 40

    real_window = _mod._resolve_uncovered_commit_attribution_window

    def _incomplete(shas, repo_root):
        window = real_window(shas, repo_root)
        window.pop(missing_sha, None)
        return window

    monkeypatch.setattr(_mod, "_resolve_uncovered_commit_attribution_window", _incomplete)

    result = _mod._partition_foreign_uncovered_shas([own, missing_sha], own_sid)
    assert result == ([], [own, missing_sha])


def test_partition_foreign_uncovered_shas_grep_failure_degrades_whole_batch(monkeypatch, temp_git_repo):
    """A grep-leg failure (non-zero rc), when the grep leg was actually
    needed, must degrade the WHOLE batch to `own` — not just the
    untrailered sha the grep leg would have resolved."""
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(temp_git_repo))
    own_sid = "deadbeef-1234-4abc-8def-0123456789ab"
    base = _make_commit(temp_git_repo, "base.txt", "base")
    trailered = _commit_with_session_trailer(temp_git_repo, "trailered.txt", "own commit", own_sid)
    untrailered = _make_commit(temp_git_repo, "untrailered.txt", "no trailer at all")

    monkeypatch.setattr(_mod, "_resolve_uncovered_grep_attribution", lambda shas, sid, repo_root: None)

    result = _mod._partition_foreign_uncovered_shas([trailered, untrailered], own_sid)
    assert result == ([], [trailered, untrailered])
    _ = base


def test_resolve_vouched_shas_always_returns_empty_no_waiver_source_remains(monkeypatch):
    """`_resolve_vouched_shas` formerly wrapped the gate-minted chain-
    ancestry-waiver store (`_chain_ancestry_waived_shas` /
    `_VOUCHED_SHAS_CACHE`); that whole mechanism is removed outright
    (state/kill-ledger.md K-005, 2026-08-16 — "waiver system dies"). No
    waiver source remains, so the resolver is unconditional: it must return
    an empty frozenset regardless of `session_id`, with no caching and no
    git/repo-root consultation at all."""
    monkeypatch.setattr(
        _mod, "_resolve_repo_root",
        lambda: (_ for _ in ()).throw(AssertionError("must not resolve repo root")),
    )
    assert _mod._resolve_vouched_shas("own-sid") == frozenset()
    assert _mod._resolve_vouched_shas(None) == frozenset()


def test_peer_record_outside_chain_does_not_discharge_despite_being_later_on_shared_branch():
    """THE fix's regression pin: a record from another (concurrent peer)
    session whose reviewed commits lie ENTIRELY outside this chain's DAG
    (not merely outside its filtered code-obligation set) must NOT
    discharge — regardless of where that record sits on the shared branch
    relative to the chain tip. The fixed `chain_partition_verdict_
    discharged` signature no longer even accepts a `chain_tip_sha`/
    `is_ancestor` pair, so "later on the shared branch" cannot possibly
    factor into the verdict any more — this is the structural guarantee the
    live re-verification demanded, not merely a behavioral assertion. A
    peer session's own commits are members of neither `chain_code_shas` NOR
    `chain_dag_shas`, so the peer-rejection guarantee holds under the
    membership-vs-coverage split too — contrast the same-chain bookkeeping
    case below, which IS a `chain_dag_shas` member and must be accepted."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2"]
    records = [
        {"verdict": "ok", "sha_range": "peer-start..peer-tip"},  # a peer session's own range
    ]
    resolve_range_shas = _range_shas_lookup({"peer-start..peer-tip": {"ccccccc3"}})  # peer's own commit
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["aaaaaaa1", "bbbbbbb2"]
    assert not chain_partition_verdict_discharged(records, chain_code_shas, chain_code_shas, resolve_range_shas)


def test_whole_chain_contiguous_record_discharges():
    """A single record whose range names every chain code sha discharges on
    its own — the union-of-one case that made the retired tip-reaching
    "leg (a)" redundant under this scoping."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2", "ccccccc3"]
    records = [{"verdict": "ok", "sha_range": "base..tip"}]
    resolve_range_shas = _range_shas_lookup({"base..tip": {"aaaaaaa1", "bbbbbbb2", "ccccccc3"}})
    assert chain_partition_uncovered_shas(records, chain_code_shas, chain_code_shas, resolve_range_shas) == []
    assert chain_partition_verdict_discharged(records, chain_code_shas, chain_code_shas, resolve_range_shas)


def test_per_commit_in_chain_record_contributes_exactly_its_own_sha():
    """A genuine per-commit record (`X^..X`, one chain-member commit)
    contributes exactly that one sha — never more, never less."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2"]
    records = [{"verdict": "ok", "sha_range": "aaaaaaa1^..aaaaaaa1"}]
    resolve_range_shas = _range_shas_lookup({"aaaaaaa1^..aaaaaaa1": {"aaaaaaa1"}})
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["bbbbbbb2"]


def test_record_straddling_chain_and_non_chain_commits_accepted_and_partially_credited():
    """2026-08-06 subset-to-intersection correction (review-integrator
    finding B4/F4): a record whose range spans BOTH a chain-member commit
    and a commit OUTSIDE the chain's DAG entirely (an honest multi-commit
    review range that straddles a concurrent PEER'S commit — the norm, not
    the exception, on this fleet's interleaving-sessions branch model) is
    now ACCEPTED, not rejected wholesale — the subset requirement this test
    used to pin made every honest multi-commit range that straddled a peer
    commit fail, which is why 91 of 1213 on-disk records failed membership
    under the retired rule. The record contributes EXACTLY its chain-code
    intersection (`raw & chain_code_shas`); `outofchainsha` confers no
    credit either way, whether the record is accepted or rejected, since it
    is not itself a member of `chain_code_shas`. Contrast the
    genuinely-all-foreign case pinned by
    `test_peer_record_outside_chain_does_not_discharge_despite_being_later_
    on_shared_branch` above, which correctly stays rejected (empty
    intersection)."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2"]
    records = [{"verdict": "ok", "sha_range": "x..y"}]
    resolve_range_shas = _range_shas_lookup({"x..y": {"aaaaaaa1", "outofchainsha"}})
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_code_shas, resolve_range_shas,
    ) == ["bbbbbbb2"]
    assert not chain_partition_verdict_discharged(records, chain_code_shas, chain_code_shas, resolve_range_shas)


def test_record_spanning_code_and_same_chain_bookkeeping_commit_accepted_and_contributes_only_code_shas():
    """The gap the reviewer named explicitly (2026-08-06 P1): an honest
    whole-chain review range that spans BOTH an in-chain code commit and an
    in-chain bookkeeping/handoff-authoring commit — a commit `chain_code_
    shas` deliberately excludes but that a real `base..tip` review range
    still names — must be ACCEPTED (not rejected the way a genuinely foreign
    peer commit is), and must contribute EXACTLY its code-bearing shas, no
    coverage credit for the bookkeeping commit. `bookkeepingsha` is a member
    of `chain_dag_shas` (the chain's own DAG, unfiltered) but NOT of
    `chain_code_shas` (the filtered code-obligation set) — that is exactly
    the membership-vs-coverage split this fix introduces."""
    chain_code_shas = ["aaaaaaa1", "bbbbbbb2"]
    chain_dag_shas = ["aaaaaaa1", "bbbbbbb2", "bookkeepingsha"]
    records = [{"verdict": "ok", "sha_range": "base..tip"}]
    resolve_range_shas = _range_shas_lookup(
        {"base..tip": {"aaaaaaa1", "bbbbbbb2", "bookkeepingsha"}}
    )
    assert chain_partition_uncovered_shas(
        records, chain_code_shas, chain_dag_shas, resolve_range_shas,
    ) == []
    assert chain_partition_verdict_discharged(
        records, chain_code_shas, chain_dag_shas, resolve_range_shas,
    )


def test_union_leg_empty_chain_code_shas_never_discharges_on_its_own():
    """`chain_code_shas=[]` must never discharge — an empty obligation set
    is indistinguishable from "the chain's code shas could not be
    resolved", so it fails safe toward refusal rather than vacuously
    succeeding."""
    records = [{"verdict": "ok", "sha_range": "base1..r1"}]
    resolve_range_shas = _range_shas_lookup({"base1..r1": {"aaaaaaa1"}})
    assert not chain_partition_verdict_discharged(records, [], ["aaaaaaa1"], resolve_range_shas)


def test_discharge_never_fires_when_chain_code_shas_or_dag_shas_or_resolver_is_none():
    """Fail-safe toward refusal, unconditionally: any of `chain_code_shas`,
    `chain_dag_shas`, or the resolver missing (e.g. the caller could not
    resolve the chain's code/DAG shas, or has no range-resolver available)
    must never discharge — there is no fallback path here that manufactures
    a verdict from partial evidence."""
    records = [_discharging_record("ok")]
    assert not chain_partition_verdict_discharged(records, None, _CHAIN_CODE_SHAS, _range_shas_lookup({}))
    assert not chain_partition_verdict_discharged(records, _CHAIN_CODE_SHAS, None, _range_shas_lookup({}))
    assert not chain_partition_verdict_discharged(records, _CHAIN_CODE_SHAS, _CHAIN_CODE_SHAS, None)


def test_resolve_chain_code_shas_excludes_a_handoff_authoring_only_commit(monkeypatch):
    """A commit that introduces ONLY a file under state/handoffs/ (the
    ceremony's own baton-write) owes no code review and must not appear in
    the union leg's obligation set — see `_resolve_chain_code_shas`'s own
    docstring for why `_resolve_chain_tip_sha` deliberately keeps such a
    commit as a tip *candidate* while this function excludes it from the
    code-review obligation set."""
    repo_root = "/fake/repo"
    candidates = ["codesha1", "handoffsha2"]
    monkeypatch.setattr(_mod, "_resolve_dag_candidates", lambda from_handoff: (repo_root, candidates))
    monkeypatch.setattr(
        _mod,
        "_commit_touched_paths",
        lambda shas, cwd, cache: (
            {
                "codesha1": frozenset({"coordinator_core/coverage.py"}),
                "handoffsha2": frozenset({"state/handoffs/2026-08-06-x.md"}),
            },
            None,
        ),
    )
    result = _mod._resolve_chain_code_shas("state/handoffs/x.md")
    assert result == ["codesha1"]


def test_resolve_chain_code_shas_fails_safe_to_empty_when_dag_unresolvable(monkeypatch):
    monkeypatch.setattr(_mod, "_resolve_dag_candidates", lambda from_handoff: None)
    assert _mod._resolve_chain_code_shas("state/handoffs/x.md") == []


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


def test_all_planning_foreign_uncovered_chain_prints_communicate_only_note(
    monkeypatch, tmp_path, capsys,
):
    """Supersedes the 22b2537b2 / e8fc10284f67 behaviour (formerly
    test_all_planning_foreign_uncovered_chain_does_not_print_mint_remedy,
    then formerly test_all_planning_foreign_uncovered_chain_prints_mint_
    remedy): the `REMEDY (foreign/predecessor commits)` block — which
    instructed operators to mint a chain-ancestry waiver that cannot
    actually discharge a foreign/ancestor commit on its own — was deleted
    outright in e8fc10284f67. With `own_shas` empty (every uncovered
    commit here is foreign) and every uncovered commit a planning
    artifact, the gate is now communicate-only: rc=0, a `NOTE:` explaining
    no discharging verdict exists but nothing here is recordable by this
    session, and the planning-artifact breakdown of the uncovered set
    still fully present in the diagnostic."""
    monkeypatch.setattr(_mod, "_run_review_brightline_gate", lambda argv: (0, _TIER_B_STDOUT, ""))
    monkeypatch.setattr(_mod, "_resolve_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(_mod, "_resolve_closing_session_id", lambda repo_root: "test-sid-planning-foreign")
    chain_code_shas = ["planforeign1", "planforeign2"]
    _patch_chain_scoping(monkeypatch, chain_code_shas=chain_code_shas)
    monkeypatch.setattr(
        _mod, "_classify_bookkeeping_shas",
        lambda shas, cwd, cache: (frozenset(), frozenset(chain_code_shas), None),
    )

    # A3: see the identical fixture-shape note above — stubbed directly,
    # git-window mechanics covered elsewhere.
    monkeypatch.setattr(
        _mod, "_partition_foreign_uncovered_shas",
        lambda shas, session_id: (list(shas), []),
    )
    monkeypatch.setattr(_mod, "_load_trail_records", lambda: [])

    rc = _mod.main(["brightline-gate", "--from-handoff", "state/handoffs/x.md"])

    err = capsys.readouterr().err
    assert rc == 0
    assert "NOTE: brightline verdict=PARTITION-MANDATORY" in err
    assert "communicate-only gate here, not a halt." in err
    assert "2 planning-artifact commit(s) (owe a plan review, not a code review):" in err
    assert "REMEDY (foreign/predecessor commits)" not in err


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


def test_run_review_brightline_gate_records_a_started_and_completed_row(monkeypatch):
    calls = []

    def fake_started(**kwargs):
        calls.append(("started", kwargs))

    def fake_latency(**kwargs):
        calls.append(("complete", kwargs))

    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_started", fake_started
    )
    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_latency", fake_latency
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_TIER_B_STDOUT, stderr=""),
    )

    rc, stdout, stderr = _mod._run_review_brightline_gate(["state/handoffs/x.md"])

    assert rc == 0
    assert stdout == _TIER_B_STDOUT
    kinds = [kind for kind, _ in calls]
    assert kinds == ["started", "complete"], calls
    started_kwargs = calls[0][1]
    complete_kwargs = calls[1][1]
    assert started_kwargs["op"] == _mod._OP_LATENCY_LABEL
    assert complete_kwargs["op"] == _mod._OP_LATENCY_LABEL
    assert complete_kwargs["outcome"] == "ok"
    assert complete_kwargs["corr_id"] == started_kwargs["corr_id"]


def test_run_review_brightline_gate_records_error_outcome_on_nonzero_rc(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_started",
        lambda **kwargs: calls.append(("started", kwargs)),
    )
    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_latency",
        lambda **kwargs: calls.append(("complete", kwargs)),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    rc, _stdout, _stderr = _mod._run_review_brightline_gate(["state/handoffs/x.md"])

    assert rc == 1
    complete_kwargs = calls[1][1]
    assert complete_kwargs["outcome"] == "error"


def test_run_review_brightline_gate_telemetry_failure_never_breaks_the_gate(monkeypatch):
    def raising_started(**kwargs):
        raise RuntimeError("telemetry sink unwritable")

    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_started", raising_started
    )
    monkeypatch.setattr(
        "coordinator_core.telemetry.op_latency.record_op_latency",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("telemetry sink unwritable")),
    )
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=_TIER_B_STDOUT, stderr=""),
    )

    rc, stdout, _stderr = _mod._run_review_brightline_gate(["state/handoffs/x.md"])

    assert rc == 0
    assert stdout == _TIER_B_STDOUT


