"""
coordinator_core.ops.tests.test_testing_full_runner — scoped tests for the
"testing.full_runner" registered op (C5, DEC-9).

Purpose: exercises the structured-JSON op surface end-to-end — wire
registration (registry presence, classification, `_OP_KEY_SCOPE`, and NOT
`WORKTREE_SCOPED_OPS`), the JSON result shape, that it actually runs the C1/C2
engine against a hermetic fixture tree (shared factory,
`coordinator_core.testing.conftest.fixture_tree`, Finding 13), that
`overall_ok` correctly reflects suite results on both an all-pass and a
failing tree, and that the scope-"none" `target_root` param is resolved AND
path-guarded (a literal `..` traversal segment is rejected).

Port source: none — net-new (DR-059 harness authoring).
Spec backlink: pln-claude-klabauter-python-full-test-runner-f8ca5a § C5 (DEC-9)

Negative-spec:
    - Does NOT write any dummy `test_*.py` fixture file anywhere under
      `coordinator_core/` — every fixture file used here is materialized
      under pytest's `tmp_path` at runtime only via `fixture_tree` (Finding
      9). A committed failing dummy would permanently red claude-klabauter's own suite
      (`testpaths=["coordinator_core"]`, `python_files=["test_*.py"]`).
    - Does NOT exercise the js-prefix/js-suffix/py-nonnative families — this
      op's own logic is family-agnostic (it forwards straight to C1/C2, which
      already have their own per-family coverage in
      `coordinator_core/testing/test_*.py`); restricting these tests to the
      `py-native` family keeps them hermetic and independent of whether
      `node` happens to be on the runner's PATH.
"""

from __future__ import annotations

import asyncio

import pytest

import coordinator_core.ipc as ipc
import coordinator_core.ops  # noqa: F401 — triggers every op module's register_op(...) side-effect
from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.ops import testing_full_runner
from coordinator_core.ops.testing_full_runner import _testing_full_runner
from coordinator_core.testing.run import DEFAULT_TIMEOUT

pytest_plugins = ("coordinator_core.testing._fixtures",)


def _run(coro):
    if asyncio.iscoroutine(coro):
        return asyncio.new_event_loop().run_until_complete(coro)
    return coro


def _call(params: dict) -> dict:
    return _run(_testing_full_runner(params, repo_root=None))


# ---------------------------------------------------------------------------
# Wire registration — registry presence, classification, _OP_KEY_SCOPE
# ---------------------------------------------------------------------------


def test_op_is_registered():
    assert "testing.full_runner" in ipc._REGISTRY, (
        "'testing.full_runner' is not in coordinator_core.ipc._REGISTRY — "
        "coordinator_core/ops/__init__.py is missing the import that triggers "
        "this op module's register_op(...) side-effect."
    )
    assert callable(ipc._REGISTRY["testing.full_runner"])


def test_op_is_classified_mutating():
    # See coordinator_core/authz/classification.py's "testing.full_runner"
    # entry docstring: this op dispatches arbitrary subprocess suites, which
    # may write to disk as a side-effect of running (unlike coverage.gate's/
    # cartography.churn's read-only-git-query precedent) — DR-208
    # ambiguous-defaults-MUTATING.
    assert classify("testing.full_runner") is OpClass.MUTATING


def test_op_has_none_scope():
    assert "testing.full_runner" in ipc.OP_KEY_SCOPE, (
        "'testing.full_runner' is missing from ipc._OP_KEY_SCOPE — an op "
        "absent from _OP_KEY_SCOPE silently degrades to central scope "
        "(lesson 2026-07-06-compute-only-op-registration-needs-an-op)."
    )
    assert ipc.OP_KEY_SCOPE["testing.full_runner"] == "none", (
        "testing.full_runner takes an explicit target_root wire param and "
        "accesses no repo-specific state via repo_root — expected scope "
        f"'none', got {ipc.OP_KEY_SCOPE['testing.full_runner']!r}."
    )


def test_op_not_in_worktree_scoped_ops():
    assert "testing.full_runner" not in ipc.WORKTREE_SCOPED_OPS, (
        "testing.full_runner is scope 'none' (explicit target_root param) — "
        "it must NOT be injected with the caller's own dispatching-tree "
        "repo_root via WORKTREE_SCOPED_OPS."
    )


def test_dispatch_message_smoke(fixture_tree, exercise_suspended_op):
    tree = fixture_tree(venv_dirname=".venv")
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "testing.full_runner",
        "params": {"target_root": str(tree.repo_root), "families": ["py-native"]},
    }
    d = _run(ipc.dispatch_message(msg))

    assert "result" in d, f"dispatch_message must succeed; got error: {d.get('error')}"
    result = d["result"]
    assert result["overall_ok"] is True
    assert result["families"] == ["py-native"]


# ---------------------------------------------------------------------------
# JSON result shape + engine run against a hermetic fixture tree
# ---------------------------------------------------------------------------


def test_result_shape_and_all_pass(fixture_tree):
    tree = fixture_tree(venv_dirname=".venv")

    result = _call({"target_root": str(tree.repo_root), "families": ["py-native"]})

    assert set(result.keys()) == {"families", "passed", "failed", "suites", "overall_ok"}
    assert result["families"] == ["py-native"]
    assert result["passed"] == 1
    assert result["failed"] == 0
    assert result["overall_ok"] is True

    assert len(result["suites"]) == 1
    entry = result["suites"][0]
    assert set(entry.keys()) == {"family", "path", "exit_code", "duration"}
    assert entry["family"] == "py-native"
    assert entry["exit_code"] == 0
    assert isinstance(entry["duration"], float)
    assert str(tree.family_files["py-native"]) == entry["path"]


def test_venv_excluded_decoy_not_dispatched(fixture_tree):
    """The venv-planted decoy test_*.py (DEC-2) must never be discovered/run."""
    tree = fixture_tree(venv_dirname=".venv")

    result = _call({"target_root": str(tree.repo_root), "families": ["py-native"]})

    paths = [entry["path"] for entry in result["suites"]]
    assert str(tree.decoy_venv_test) not in paths
    assert len(result["suites"]) == 1


def test_overall_ok_false_on_failing_suite(fixture_tree):
    tree = fixture_tree(
        venv_dirname=".venv",
        extra_files=(("test_fail.py", "def test_bad():\n    assert False\n"),),
    )

    result = _call({"target_root": str(tree.repo_root), "families": ["py-native"]})

    assert result["overall_ok"] is False
    assert result["failed"] == 2  # both py-native files share the one batched exit_code (DEC-5)
    assert result["passed"] == 0
    assert all(entry["exit_code"] != 0 for entry in result["suites"])


# ---------------------------------------------------------------------------
# target_root resolution + path-guarding
# ---------------------------------------------------------------------------


def test_missing_target_root_is_rejected():
    result = _call({})
    assert "error" in result


def test_traversal_target_root_is_rejected(fixture_tree, tmp_path):
    tree = fixture_tree(venv_dirname=".venv")
    traversal = f"{tree.repo_root}/../{tree.repo_root.name}"

    result = _call({"target_root": traversal, "families": ["py-native"]})

    assert "error" in result, (
        "a target_root string containing a literal '..' traversal segment "
        "must be rejected outright, never silently resolved."
    )


def test_nonexistent_target_root_is_rejected(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = _call({"target_root": str(missing), "families": ["py-native"]})
    assert "error" in result


def test_file_target_root_is_rejected(tmp_path):
    a_file = tmp_path / "not-a-dir.txt"
    a_file.write_text("x", encoding="utf-8")
    result = _call({"target_root": str(a_file), "families": ["py-native"]})
    assert "error" in result


# ---------------------------------------------------------------------------
# Timeout ceiling — the acknowledged pytest carve-out is still bounded
# ---------------------------------------------------------------------------


def _record_run_suites_timeout(monkeypatch) -> list[int]:
    """Swap the op module's run_suites for a recorder of its timeout kwarg.

    No pytest subprocess is dispatched: the clamp is the whole contract here,
    and running a real suite to observe it would put minutes of load on a shared
    box to learn one integer.
    """
    seen: list[int] = []

    def _fake_run_suites(_suites, _root, *, timeout, jobs=None):
        seen.append(timeout)
        return []

    monkeypatch.setattr(testing_full_runner, "run_suites", _fake_run_suites)
    monkeypatch.setattr(testing_full_runner, "discover", lambda _root, families=None: [])
    return seen


def test_huge_timeout_is_clamped_to_the_runaway_ceiling(monkeypatch, tmp_path):
    seen = _record_run_suites_timeout(monkeypatch)

    _call({"target_root": str(tmp_path), "timeout": 10**9})

    assert seen == [testing_full_runner.MAX_TIMEOUT_SECONDS], (
        "testing.full_runner is exempt from the 2s process regime because it "
        "runs pytest — it is NOT exempt from a runaway bound, and a caller "
        "cannot raise the per-unit subprocess timeout past it"
    )


def test_timeout_below_the_ceiling_is_honoured(monkeypatch, tmp_path):
    seen = _record_run_suites_timeout(monkeypatch)

    _call({"target_root": str(tmp_path), "timeout": 30})

    assert seen == [30], (
        "the ceiling is a bound on over-asking, not a floor — a caller wanting "
        "a shorter per-unit timeout must still get it"
    )


def test_default_timeout_is_well_under_the_ceiling():
    assert DEFAULT_TIMEOUT < testing_full_runner.MAX_TIMEOUT_SECONDS, (
        "a ceiling at or below the default would silently make the default "
        "unreachable and turn every ordinary run into a clamped one"
    )
