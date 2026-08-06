"""test_emit_cadence.py — pytest suite for emit-cadence.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into collectable top-level test_* functions; assertion intent
preserved 1:1.

Port of: test-emit-cadence-facade.sh (9ec1a670, 2026-07-19) — B-facade repoint,
2026-07-19 Windows de-bash campaign, Wave 1b. The retired bash suite forced the
legacy branch via COORDINATOR_FORCE_LEGACY=1 (a legacy-facade-only lever
cc_invoke.py's two-state route() does not honor — see cc_invoke.py's negative-spec).
This port achieves the equivalent hermetic coverage by pre-populating
`sys.modules["cc_invoke"]` with a fake module BEFORE importing the subject:
emit-cadence.py does `from cc_invoke import RouteMutationError, route_mutation` after its
own `sys.path.insert(0, lib_dir)`, but Python's import machinery checks sys.modules first
-- a pre-seeded entry short-circuits the file search entirely, so no live CLAUDE_KLABAUTER_ROOT /
coordinator_core.invoke subprocess is ever spawned and no real emission fires. Each test
loads a FRESH copy of the subject module (importlib, a new module object per test) so the
fake `route_mutation` can vary per test without cross-test leakage.

Test coverage:
  T1  gate off (COORDINATOR_EMISSION_CADENCE_LIVE=0/false/off, case-insensitive) — exit 0,
      skip note on stderr, route_mutation never invoked (gate check precedes op routing)
  T2  gate on (unset) — route_mutation invoked with op="emit.cadence", params={}
  T3  legacy_cadence path (State-1 seam absent) — the legacy_fn raises _SeamAbsentError,
      which route_mutation propagates unchanged (fakes call legacy_fn() directly, same as
      the real route()'s State-1 branch) — exit 1, "claude-klabauter control plane" on stderr
  T4  route_mutation raises RouteMutationError (op-level refusal) — exit 1, op detail
      surfaced on stderr
  T5  route_mutation raises a bare RuntimeError (native transport failure) — exit 3
  T6  route_mutation returns normally (op success) — exit 0
  T7  route_mutation raises StructuralPinError (structural contract-pin
      failure) — exit 4, remediation detail passed through unmangled,
      will-not-self-heal note on stderr; proves the except ladder checks
      StructuralPinError BEFORE the plain-RuntimeError branch (it subclasses
      RuntimeError, so wrong ordering would misroute it to exit 3)

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Wave 1b)
Spec backlink: docs/plans/2026-07-11-emission-cadence-trigger-rewire.md § C2 / D3
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import types

import pytest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "emit-cadence.py")


class _FakeRouteMutationError(RuntimeError):
    """Stand-in for cc_invoke.RouteMutationError — carries .result like the real one."""

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


class _FakeStructuralPinError(RuntimeError):
    """Stand-in for cc_invoke.StructuralPinError — a RuntimeError subclass,
    so the T5b/T7 pairing proves the subject's except ladder orders this
    branch BEFORE the plain-RuntimeError branch (else it would never be
    reached)."""


_ABSENT = object()


def _install_fake_cc_invoke(route_mutation_fn):
    """Seed sys.modules["cc_invoke"] with a fake module exposing route_mutation
    and RouteMutationError. Must run BEFORE the subject module is imported —
    the subject's `from cc_invoke import ...` resolves against sys.modules
    first, so this fully short-circuits any real file/CLAUDE_KLABAUTER_ROOT lookup and
    guarantees no live emission fires.

    Returns the prior sys.modules entry (or `_ABSENT`) — hand it to
    `_restore_cc_invoke` in a `finally`.

    Negative spec: `sys.modules["cc_invoke"]` is process-global, and 30+
    `coordinator/bin/` scripts import `cc_invoke` by bare name. A fake left
    installed past the test that seeded it makes every later such import in the
    same worker resolve against a module carrying only these three attributes;
    the missing name surfaces as an ImportError at the victim's fixture setup,
    which pytest reports as an ERROR in an unrelated file. Never install
    without a paired restore."""
    fake = types.ModuleType("cc_invoke")
    fake.route_mutation = route_mutation_fn
    fake.RouteMutationError = _FakeRouteMutationError
    fake.StructuralPinError = _FakeStructuralPinError
    prior = sys.modules.get("cc_invoke", _ABSENT)
    sys.modules["cc_invoke"] = fake
    return prior


def _restore_cc_invoke(prior) -> None:
    """Undo `_install_fake_cc_invoke`, restoring absence as absence."""
    if prior is _ABSENT:
        sys.modules.pop("cc_invoke", None)
    else:
        sys.modules["cc_invoke"] = prior


def _load_subject_fresh():
    """Import a brand-new copy of the subject module (bypassing any cached
    entry) so per-test route_mutation fakes never leak across tests."""
    sys.modules.pop("emit-cadence", None)
    spec = importlib.util.spec_from_file_location("emit-cadence", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_mutation_fn, env_overrides: dict | None = None):
    """Load a fresh subject with the given fake route_mutation under the given
    env overrides, call main(), and capture (exit_code, stdout, stderr)."""
    prior_cc_invoke = _install_fake_cc_invoke(route_mutation_fn)
    out, err = io.StringIO(), io.StringIO()
    env_key = "COORDINATOR_EMISSION_CADENCE_LIVE"
    had_env = env_key in os.environ
    prior = os.environ.get(env_key)
    try:
        subject = _load_subject_fresh()
        if env_overrides and env_key in env_overrides:
            os.environ[env_key] = env_overrides[env_key]
        elif env_key in os.environ:
            del os.environ[env_key]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main()
    finally:
        if had_env:
            os.environ[env_key] = prior
        elif env_key in os.environ:
            del os.environ[env_key]
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


@pytest.mark.parametrize("off_val", ["0", "false", "off", "FALSE", "OFF"])
def test_gate_off_skips_op_routing(off_val):
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(
        _route_mutation, {"COORDINATOR_EMISSION_CADENCE_LIVE": off_val}
    )
    assert code == 0
    assert "is off" in err
    assert reached["called"] is False


def test_gate_on_invokes_op_with_correct_op_and_params():
    captured = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        captured["op"] = op
        captured["params"] = params
        captured["repo_root"] = repo_root
        return {"exit_code": 0}

    code, out, err = _run_main(_route_mutation)
    assert code == 0
    assert captured.get("op") == "emit.cadence"
    assert captured.get("params") == {}


def test_legacy_path_seam_absent_propagates():
    def _route_mutation(op, params, repo_root, legacy_fn):
        legacy_fn()  # mirrors route()'s State-1 branch: call and propagate

    code, out, err = _run_main(_route_mutation)
    assert code == 1
    assert "claude-klabauter control plane" in err
    assert "No emission fired" in err


def test_op_level_refusal_route_mutation_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1", {"exit_code": 1, "error": "backlog.record failed"}
        )

    code, out, err = _run_main(_route_mutation)
    assert code == 1
    assert "op refused" in err


def test_transport_failure_bare_runtime_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, out, err = _run_main(_route_mutation)
    assert code == 3
    assert "transport failure" in err


def test_op_success():
    def _route_mutation(op, params, repo_root, legacy_fn):
        return {"exit_code": 0, "message": "cadence fired"}

    code, out, err = _run_main(_route_mutation)
    assert code == 0


def test_structural_contract_pin_failure_not_exit_3():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeStructuralPinError("CONTRACT_VERSION drift: expected 2.20.0, found 2.18.0")

    code, out, err = _run_main(_route_mutation)
    assert code == 4, "must not be misrouted to the exit-3 plain-RuntimeError branch"
    assert "CONTRACT_VERSION drift" in err
    assert "will NOT" in err
