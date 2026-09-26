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
  write-trail's coverage was removed here (PM ruling 2026-08-23, kill
  review_trail.write) along with the subcommand itself.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
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

# would leak commits across tests. The spawn ratchet's `_BASELINE` is
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


def _git(*args, cwd, env=None):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env,
    )
    return proc.stdout.strip()


_commit_clock = {"epoch": 1_700_000_000}


def _make_commit(repo_dir, filename, message):
    import os as _os

    (repo_dir / filename).write_text(message)
    _git("add", filename, cwd=repo_dir)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    _git("commit", "-m", message, cwd=repo_dir, env=env)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


_TIER_B_STDOUT = (
    'BRIGHTLINE reviewers_required=4 reviewers_suggested=32 reviewers_low=4 '
    'plan_oracle=4 chain_oracle=32 session_oracle=10 '
    'verdict=PARTITION-MANDATORY basis="plan_oracle=4(...) tier=B"\n'
)

#: by WITHIN-CHAIN MEMBERSHIP — a trail record's resolved range must be a
_CHAIN_CODE_SHA = "c0ffee00c0ffee00c0ffee00c0ffee00c0ffee0"
_CHAIN_CODE_SHAS = [_CHAIN_CODE_SHA]


# UNDISCHARGED PARTITION-MANDATORY verdict. The narrow exception carved out
# `_TIER_B_STDOUT` fixture for the shared verdict=PARTITION-MANDATORY line.


_TIER_B_SINGLE_REVIEWER_OK_STDOUT = (
    'BRIGHTLINE reviewers_required=1 reviewers_suggested=1 reviewers_low=1 '
    'plan_oracle=1 chain_oracle=1 session_oracle=1 '
    'verdict=single-reviewer-ok basis="plan_oracle=1(...) tier=B"\n'
)


# `cmd_brightline_gate`'s PARTITION-MANDATORY handling that GRANTS a pass


# commits.md): the HALT's own UNCOVERED message told two lies — it called
# every entry a "chain code commit" (PLANNING commits stay in the
# undischargeable BY CONSTRUCTION, not because no one reviewed it). This is


# NON-EMPTY SUBSET of `chain_code_shas`. `chain_partition_verdict_


def test_record_membership_rejects_stored_head_range_before_consulting_resolver():

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

    def _resolver_must_not_be_called(sha_range: str):
        raise AssertionError(f"resolver must not be consulted for a non-code scope_kind: {sha_range!r}")

    record = {"verdict": "ok", "sha_range": "base..tip", "scope_kind": "integration"}
    assert _record_membership_shas(
        record, _resolver_must_not_be_called, {"aaaaaaa1"}, {"aaaaaaa1"},
    ) is None


def _commit_with_session_trailer(repo_dir, filename, message, session_id):
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
    import os as _os

    (repo_dir / filename).write_text(message)
    _git("add", filename, cwd=repo_dir)
    _commit_clock["epoch"] += 1
    date_str = f"{_commit_clock['epoch']} +0000"
    env = dict(_os.environ, GIT_AUTHOR_DATE=date_str, GIT_COMMITTER_DATE=date_str)
    full_message = f"{message}\n\nSession-Id: {session_id}\n--- end Step 2.67 blocks ---"
    _git("commit", "-m", full_message, cwd=repo_dir, env=env)
    return _git("rev-parse", "HEAD", cwd=repo_dir)


# is the single span — see its own docstring/`_OP_LATENCY_LABEL` for why.


_CARRIED_SID = "304997f1-4143-4beb-b2dc-657a16fef082"
_SERVER_ENV_SID = "f3865ac1-1111-2222-3333-444444444444"


def _spy_subprocess_run(monkeypatch):
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    return captured


def test_claim_child_env_carries_the_resolved_session_id(monkeypatch):
    from coordinator_core.session import core as session_core

    captured = _spy_subprocess_run(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _CARRIED_SID)

    rc, _out = _mod._run_session_claim_cli("some-plan")

    assert rc == 0
    assert captured["env"] is not None
    assert captured["env"]["COORDINATOR_SESSION_ID"] == _CARRIED_SID
    assert session_core.resolve_session_id() == _CARRIED_SID


def test_carried_identity_beats_the_ambient_environment_in_the_child(monkeypatch):
    """The regression pin. With a tier-0 identity bound (what the warm
    server binds per request) and a DIFFERENT sid in this process's own
    environment (the server's spawner), the child must be handed the
    carried one — before this fix it inherited the ambient one and stamped
    the claim record with an uninvolved session."""
    from coordinator_core.session import core as session_core

    captured = _spy_subprocess_run(monkeypatch)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _SERVER_ENV_SID)

    with session_core.session_identity_override(_CARRIED_SID):
        _mod._run_session_claim_cli("some-plan")

    assert captured["env"]["COORDINATOR_SESSION_ID"] == _CARRIED_SID


def test_unresolvable_identity_leaves_the_child_env_untouched(monkeypatch):
    captured = _spy_subprocess_run(monkeypatch)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)

    _mod._run_session_claim_cli("some-plan")

    assert captured["env"] is None
