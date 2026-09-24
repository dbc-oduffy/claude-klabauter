"""coordinator_core.execute_plan_assemble.apply — the closed dispatch table
and its four handlers.

Spec: docs/plans/2026-09-11-the-execute-plan-pre-execution-chain-emi.md, C2

Negative-spec covered here:
    - `_dispatch_pickup_assemble` calls `stamp_check` in-process: NO
      subprocess is spawned for d1.
    - `_CLI_DISPATCH`'s key set matches every `cli` value
      `pre_execution_directives()` ever emits, and each resolves through
      `apply_base.resolve_cli`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from coordinator_core.contract import apply_base
from coordinator_core.execute_plan_assemble import apply as apply_mod
from coordinator_core.execute_plan_assemble.pre_execution import (
    pre_execution_directives,
)

PLAN_PATH = "docs/plans/2026-09-11-the-execute-plan-pre-execution-chain-emi.md"


def _no_spawn_run(*args: Any, **kwargs: Any):
    raise AssertionError("subprocess.run was called — d1 must be in-process")


# ---------------------------------------------------------------------------
# Every emitted `cli` value resolves through the closed table.
# ---------------------------------------------------------------------------


def test_every_emitted_cli_is_a_dispatch_key_and_resolves():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    for directive in directives:
        assert directive["cli"] in apply_mod._CLI_DISPATCH
        # Raises on an unrecognized name; must not raise here.
        apply_base.resolve_cli(apply_mod._CLI_DISPATCH, directive["cli"])


def test_autonomous_directives_also_resolve():
    directives, _judgment_points = pre_execution_directives(PLAN_PATH, autonomous=True)
    for directive in directives:
        assert directive["cli"] in apply_mod._CLI_DISPATCH
        apply_base.resolve_cli(apply_mod._CLI_DISPATCH, directive["cli"])


# ---------------------------------------------------------------------------
# d1 — in-process stamp-check, verdict-to-raise mapping.
# ---------------------------------------------------------------------------


def test_d1_is_in_process_no_subprocess(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, {"verdict": "match"}))
    monkeypatch.setattr(apply_mod.subprocess, "run", _no_spawn_run)
    result = apply_mod._dispatch_pickup_assemble(["stamp-check", PLAN_PATH], tmp_path)
    assert result["gate"]["verdict"] == "match"


@pytest.mark.parametrize(
    "gate",
    [
        {"verdict": "match"},
        {"verdict": "stale-bookkeeping"},
        {"error": "carries no execution_authorized_sha"},
    ],
)
def test_d1_proceeds_on_non_stale_substantive_verdicts(monkeypatch, tmp_path: Path, gate):
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (1, gate))
    result = apply_mod._dispatch_pickup_assemble(["stamp-check", PLAN_PATH], tmp_path)
    assert result["gate"] == gate


def test_d1_raises_on_stale_substantive(monkeypatch, tmp_path: Path):
    gate = {"verdict": "stale-substantive", "next_move": "Surface to the PM before proceeding"}
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, gate))
    with pytest.raises(RuntimeError, match="stale-substantive"):
        apply_mod._dispatch_pickup_assemble(["stamp-check", PLAN_PATH], tmp_path)


def test_stale_substantive_blocks_d2_in_a_full_run(monkeypatch, tmp_path: Path):
    gate = {"verdict": "stale-substantive", "next_move": "ask the PM"}
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, gate))

    def _boom(*args: Any, **kwargs: Any):
        raise AssertionError("d2 must never dispatch after d1 raises")

    dispatch = dict(apply_mod._CLI_DISPATCH)
    dispatch["review-exec-auth-stamp"] = _boom
    directives, _judgment_points = pre_execution_directives(PLAN_PATH)
    d1_d2 = [d for d in directives if d["id"] in ("d1", "d2")]
    exit_code, report = apply_base.execute_directives(d1_d2, [], tmp_path, dispatch)
    assert exit_code == apply_base.APPLY_EXIT_PARTIAL_MUTATION
    assert report.get("landed", []) == []


# ---------------------------------------------------------------------------
# d3/d4 — judgment-gated; halted while undispositioned, d1/d2 still fire.
# ---------------------------------------------------------------------------


def test_undispositioned_judgment_points_halt_d3_and_d4_but_not_d1_d2(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, {"verdict": "match"}))

    def _boom_claim(*args: Any, **kwargs: Any):
        raise AssertionError("d3 must not dispatch while its judgment points are undispositioned")

    def _boom_emit(*args: Any, **kwargs: Any):
        raise AssertionError("d4 must not dispatch while its judgment points are undispositioned")

    dispatch = dict(apply_mod._CLI_DISPATCH)
    dispatch["review-exec-auth-stamp"] = lambda args, repo_root: {
        "cli": "review-exec-auth-stamp",
        "args": list(args),
    }
    dispatch["session-claim-cli"] = _boom_claim
    dispatch["emit-dispatch-workflow"] = _boom_emit

    directives, judgment_points = pre_execution_directives(PLAN_PATH)
    exit_code, report = apply_base.execute_directives(directives, judgment_points, tmp_path, dispatch)

    assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
    assert set(report.get("landed", [])) == {"d1", "d2"}


def test_declined_judgment_points_also_halt_d3_and_d4(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, {"verdict": "match"}))

    dispatch = dict(apply_mod._CLI_DISPATCH)
    dispatch["review-exec-auth-stamp"] = lambda args, repo_root: {
        "cli": "review-exec-auth-stamp",
        "args": list(args),
    }
    dispatch["session-claim-cli"] = lambda *a, **k: pytest.fail("d3 must not dispatch")
    dispatch["emit-dispatch-workflow"] = lambda *a, **k: pytest.fail("d4 must not dispatch")

    directives, judgment_points = pre_execution_directives(PLAN_PATH)
    # Every gate answered, but declining -- resolves nothing.
    decisions = {jp["id"]: {"disposition": "decline"} for jp in judgment_points}
    exit_code, report = apply_base.execute_directives(
        directives, judgment_points, tmp_path, dispatch, decisions=decisions
    )
    assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
    assert set(report.get("landed", [])) == {"d1", "d2"}


def test_all_gates_proceed_dispatches_d3_and_d4(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "stamp_check", lambda plan_path, repo_root=None: (0, {"verdict": "match"}))
    calls: list[str] = []

    dispatch = dict(apply_mod._CLI_DISPATCH)
    dispatch["review-exec-auth-stamp"] = lambda args, repo_root: calls.append("d2") or {
        "cli": "review-exec-auth-stamp"
    }
    dispatch["session-claim-cli"] = lambda args, repo_root: calls.append("d3") or {
        "cli": "session-claim-cli"
    }
    dispatch["emit-dispatch-workflow"] = lambda args, repo_root: calls.append("d4") or {
        "cli": "emit-dispatch-workflow"
    }

    directives, judgment_points = pre_execution_directives(PLAN_PATH)
    decisions = {jp["id"]: {"disposition": "proceed"} for jp in judgment_points}
    exit_code, report = apply_base.execute_directives(
        directives, judgment_points, tmp_path, dispatch, decisions=decisions
    )
    assert exit_code == apply_base.APPLY_EXIT_OK
    assert set(report.get("landed", [])) == {"d1", "d2", "d3", "d4"}
    assert calls == ["d2", "d3", "d4"]


# ---------------------------------------------------------------------------
# d4 — explicit, session-scoped --out; raises loud on an unresolvable
# DoE-claude root; never touches dispatch.emit.
# ---------------------------------------------------------------------------


def test_emit_leg_out_is_explicit_and_session_scoped(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "coordinator_doe_root", lambda: "/fake/doe-root")
    captured: dict[str, Any] = {}

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        return _FakeCompleted()

    monkeypatch.setattr(apply_mod.subprocess, "run", _fake_run)

    with apply_base.session_identity("sess-abc123"):
        result = apply_mod._dispatch_emit_dispatch_workflow(["--plan", PLAN_PATH], tmp_path)

    argv = captured["argv"]
    assert "--repo-root" in argv
    assert str(tmp_path) in argv
    assert "--out" in argv
    out_value = argv[argv.index("--out") + 1]
    assert "sess-abc123" in out_value
    assert Path(PLAN_PATH).stem in out_value
    assert result["out"] == out_value


def test_emit_leg_raises_when_doe_root_unresolvable(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(apply_mod, "coordinator_doe_root", lambda: None)
    with apply_base.session_identity("sess-xyz"):
        with pytest.raises(RuntimeError):
            apply_mod._dispatch_emit_dispatch_workflow(["--plan", PLAN_PATH], tmp_path)


def test_emit_leg_never_imports_or_calls_dispatch_emit_op():
    import ast

    source = Path(apply_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "coordinator_core.ops.dispatch_emit.emit"
        if isinstance(node, ast.Attribute):
            assert node.attr != "emit_script"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "emit_script"
