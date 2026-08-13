"""test_close_origin_stub_on_ship.py — pytest suite for close-origin-stub-on-ship.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into collectable top-level test_* functions; assertion intent
preserved 1:1.

Native-Python successor to the retired close-origin-stub-on-ship.test.sh
(coordinator-claude 394c8b64, 2026-07-19; 2026-07-19 Windows de-bash campaign, Wave 1b —
B-facade repoint). The retired bash suite stubbed the `cc_invoke` shell function to
exercise the veneer's fail-loud ladder hermetically, with no live claude-klabauter checkout. This
port achieves the same hermeticity by pre-populating `sys.modules["cc_invoke"]`
with a fake module BEFORE importing the subject: `close-origin-stub-on-ship.py`
does `from cc_invoke import route_mutation, RouteMutationError` after its own
`sys.path.insert(0, lib_dir)`, but Python's import machinery checks
`sys.modules` first — a pre-seeded entry short-circuits the file search
entirely, so no live CLAUDE_KLABAUTER_ROOT / coordinator_core.invoke subprocess is ever
spawned. Each test loads a FRESH copy of the subject module (importlib, a
new module object per test) so the fake `route_mutation` can vary per test
without cross-test leakage.

Test coverage (T1/T2 dropped — no jq dependency or bash-lib source step in
the Python entry; params are constructed as a native dict, and route_mutation
resolution is a Python import, not a subshell `command -v` probe):
  T3  route_mutation raises RuntimeError (transport failure) — exit 1,
      message names the transport failure
  T4  route_mutation raises RouteMutationError (op-level refusal) — exit 1,
      message names the op refusal
  T5  route_mutation returns success — exit 0, summary printed (closed
      count, stub path/id, pairs_resolved, message)
  T6  usage error (no args) — exit 2, message on stderr, route_mutation
      never invoked regardless of the fake's behavior
  T7  --sha plumbing — accepted on the op-success path, exit 0
  T8  params shape — route_mutation receives plan_path/handoff_path/sha as
      None when the corresponding flag is absent, populated when present

Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Wave 1b)
Spec backlink: cross-repo/inbox/2026-07-08-example-cockpit-repo-em-spinoff-roadmap-lifecycle-never-closed.md
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
SUBJECT_PATH = os.path.join(SCRIPT_DIR, "close-origin-stub-on-ship.py")


class _FakeRouteMutationError(RuntimeError):
    """Stand-in for cc_invoke.RouteMutationError — carries .result like the real one."""

    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


_ABSENT = object()


def _install_fake_cc_invoke(route_mutation_fn):
    """Seed sys.modules["cc_invoke"] with a fake module exposing route_mutation
    and RouteMutationError. Must run BEFORE the subject module is imported —
    the subject's `from cc_invoke import ...` resolves against sys.modules
    first, so this fully short-circuits any real file/CLAUDE_KLABAUTER_ROOT lookup.

    Returns the prior sys.modules entry (or `_ABSENT`) — hand it to
    `_restore_cc_invoke` in a `finally`.

    Negative spec: `sys.modules["cc_invoke"]` is process-global, and 30+
    `coordinator/bin/` scripts import `cc_invoke` by bare name. A fake left
    installed past the test that seeded it makes every later such import in the
    same worker resolve against a module carrying only these two attributes;
    the missing name surfaces as an ImportError at the victim's fixture setup,
    which pytest reports as an ERROR in an unrelated file. Never install
    without a paired restore."""
    fake = types.ModuleType("cc_invoke")
    fake.route_mutation = route_mutation_fn
    fake.RouteMutationError = _FakeRouteMutationError
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
    sys.modules.pop("close-origin-stub-on-ship", None)
    spec = importlib.util.spec_from_file_location("close-origin-stub-on-ship", SUBJECT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_main(route_mutation_fn, argv):
    """Load a fresh subject with the given fake route_mutation, call main(argv),
    and capture (exit_code, stdout, stderr)."""
    prior_cc_invoke = _install_fake_cc_invoke(route_mutation_fn)
    out, err = io.StringIO(), io.StringIO()
    try:
        subject = _load_subject_fresh()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = subject.main(argv)
    finally:
        _restore_cc_invoke(prior_cc_invoke)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def _require_subject():
    assert os.path.isfile(SUBJECT_PATH), f"subject not found: {SUBJECT_PATH}"


def test_transport_failure_runtime_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise RuntimeError("simulated transport failure (rc=127)")

    code, out, err = _run_main(_route_mutation, ["--plan", "docs/plans/foo.md"])
    assert code == 1
    assert "transport" in err


def test_op_level_refusal_route_mutation_error():
    def _route_mutation(op, params, repo_root, legacy_fn):
        raise _FakeRouteMutationError(
            "op refused: exit_code=1", {"exit_code": 1, "failed": ["bad join"]}
        )

    code, out, err = _run_main(_route_mutation, ["--handoff", "state/handoffs/foo.md"])
    assert code == 1
    assert "op reported failure" in err


def test_op_success_summary_printed():
    captured_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        captured_params["op"] = op
        captured_params["params"] = params
        return {
            "exit_code": 0,
            "closed": [{"stub_path": "state/handoffs/x.md", "stub_id": "lvv-09"}],
            "skipped": [],
            "pairs_resolved": 1,
            "message": "closed 1 stub",
        }

    code, out, err = _run_main(_route_mutation, ["--plan", "docs/plans/foo.md"])
    assert code == 0
    assert captured_params.get("op") == "handoff.close_origin_stub"
    assert "closed=1" in out
    assert "skipped=0" in out
    assert "lvv-09" in out
    assert "closed 1 stub" in out


def test_usage_error_no_args():
    reached = {"called": False}

    def _route_mutation(op, params, repo_root, legacy_fn):
        reached["called"] = True
        return {"exit_code": 0}

    code, out, err = _run_main(_route_mutation, [])
    assert code == 2
    assert "at least one of --plan / --handoff" in err
    assert reached["called"] is False


def test_sha_plumbing():
    sha_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        sha_params["params"] = params
        return {"exit_code": 0, "closed": [], "skipped": [], "pairs_resolved": 0, "message": ""}

    code, out, err = _run_main(
        _route_mutation, ["--plan", "docs/plans/foo.md", "--sha", "deadbeef01"]
    )
    assert code == 0
    assert sha_params["params"].get("sha") == "deadbeef01"


def test_params_shape_null_when_absent_populated_when_present():
    shape_params = {}

    def _route_mutation(op, params, repo_root, legacy_fn):
        shape_params["params"] = params
        return {"exit_code": 0, "closed": [], "skipped": [], "pairs_resolved": 0, "message": ""}

    code, out, err = _run_main(_route_mutation, ["--handoff", "state/handoffs/foo.md"])
    assert code == 0
    p = shape_params["params"]
    assert p.get("plan_path") is None
    assert p.get("handoff_path") == "state/handoffs/foo.md"
    assert p.get("sha") is None
