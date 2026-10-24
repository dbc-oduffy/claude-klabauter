
from __future__ import annotations

import os
import tempfile

import pytest

from coordinator_core.hooks import _em_write_budget as leaf
from coordinator_core.hooks import nudge_em_code_dispatch as _mod
from coordinator_core.hooks.nudge_em_code_dispatch import (
    _is_comment_or_docstring_only_diff,
    _is_semantic_bypass_edit,
    _is_single_token_rename,
    _is_whitespace_only_diff,
    op,
)


@pytest.fixture(autouse=True)
def _outside_f7_carveout_scope(monkeypatch):
    monkeypatch.setattr(_mod, "_is_bootstrap_or_out_of_repo", lambda file_path: False)
    monkeypatch.setattr(leaf, "_is_bootstrap_or_out_of_repo", lambda file_path: False)


def _set_sizing(monkeypatch, state: tuple[bool, bool]) -> None:
    monkeypatch.setattr(_mod, "_session_sizing_state", lambda payload, sid: state)
    monkeypatch.setattr(leaf, "_session_sizing_state", lambda payload, sid: state)


@pytest.fixture(autouse=True)
def _isolated_tempdir(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))


_REAL_SIZING_STATE = _mod._session_sizing_state


@pytest.fixture(autouse=True)
def _sized_by_default(monkeypatch):
    _set_sizing(monkeypatch, (True, False))


def _is_advisory(result) -> bool:
    if result is None:
        return False
    out = result["hookSpecificOutput"]
    return "permissionDecision" not in out and bool(out.get("additionalContext"))


def test_whitespace_only_diff_true() -> None:
    assert _is_whitespace_only_diff("x = 1\n", "x = 1\n\n") is True


def test_whitespace_only_diff_false_on_substantive_change() -> None:
    assert _is_whitespace_only_diff("x = 1", "x = 2") is False


def test_comment_only_diff_true() -> None:
    assert _is_comment_or_docstring_only_diff(
        "# old note\ndef f(): pass",
        "# new note, updated\ndef f(): pass",
    ) is True


def test_comment_only_diff_false_when_code_line_changes() -> None:
    assert _is_comment_or_docstring_only_diff(
        "# note\ndef f(): pass",
        "# note\ndef f(): return 1",
    ) is False


def test_comment_only_diff_false_when_no_change() -> None:
    assert _is_comment_or_docstring_only_diff("# note\n", "# note\n") is False


def test_single_token_rename_true() -> None:
    assert _is_single_token_rename("value = compute(x)", "value = compute(y)") is True


def test_single_token_rename_false_on_multi_token_change() -> None:
    assert _is_single_token_rename("value = compute(x)", "total = calculate(x)") is False


def test_single_token_rename_false_on_non_identifier_swap() -> None:
    assert _is_single_token_rename("x = 1", "x = 2") is False


def test_semantic_bypass_edit_covers_noop() -> None:
    assert _is_semantic_bypass_edit("same", "same") is True


def test_semantic_bypass_edit_false_on_substantive_change() -> None:
    assert _is_semantic_bypass_edit("def f():\n    return 1", "def f():\n    return compute(x)") is False


def test_edit_whitespace_only_suppresses_nudge() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "x = 1\n",
            "new_string": "x = 1\n\n",
        },
    }
    assert op(payload) is None


def test_edit_single_token_rename_suppresses_nudge() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "value = compute(x)",
            "new_string": "value = compute(y)",
        },
    }
    assert op(payload) is None


def test_edit_substantive_change_fires_and_brief_has_no_todo() -> None:
    payload = {
        "tool_name": "Edit",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "def f():\n    return 1",
            "new_string": "def f():\n    return compute_something(x, y)",
        },
    }
    assert _is_advisory(op(payload))


def test_multiedit_all_bypass_suppresses_nudge() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {"file_path": "/repo/pkg/module.py", "old_string": "# a", "new_string": "# b"},
            ],
        },
    }
    assert op(payload) is None


def test_multiedit_one_substantive_entry_fires() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {
                    "file_path": "/repo/pkg/module.py",
                    "old_string": "def f(): pass",
                    "new_string": "def f(): return compute(x, y, z)",
                },
            ],
        },
    }
    assert _is_advisory(op(payload))


def test_multiedit_malformed_entry_does_not_bypass() -> None:
    payload = {
        "tool_name": "MultiEdit",
        "session_id": "sess-1",
        "tool_input": {
            "edits": [
                {"file_path": "/repo/pkg/module.py", "old_string": "x = 1", "new_string": "x  =  1"},
                {"file_path": "/repo/pkg/module.py", "old_string": 123, "new_string": "x"},
            ],
        },
    }
    assert _is_advisory(op(payload))


def test_write_whitespace_only_against_disk_content_suppresses_nudge(tmp_path) -> None:
    target = tmp_path / "module.py"
    target.write_text("x = 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "x = 1\n\n",
        },
    }
    assert op(payload) is None


def test_write_substantive_change_against_disk_content_fires(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_mod, "_is_scratch_or_tmp", lambda p: False)
    target = tmp_path / "module.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "def f():\n    return compute_something(x, y)\n",
        },
    }
    assert _is_advisory(op(payload))


def test_write_brand_new_file_never_bypasses(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(_mod, "_is_scratch_or_tmp", lambda p: False)
    target = tmp_path / "does-not-exist-yet.py"
    payload = {
        "tool_name": "Write",
        "session_id": "sess-1",
        "tool_input": {
            "file_path": str(target),
            "content": "x = 1\n",
        },
    }
    assert _is_advisory(op(payload))


def test_op_is_registered() -> None:
    import coordinator_core.hooks  # noqa: F401 — triggers registration side-effects
    from coordinator_core.ipc import get_op_handler

    assert get_op_handler("hooks.nudge_em_code_dispatch") is not None


def _payload(sid: str = "sess-sizing-1") -> dict:
    return {
        "tool_name": "Edit",
        "session_id": sid,
        "tool_input": {
            "file_path": "/repo/pkg/module.py",
            "old_string": "def f(): pass",
            "new_string": "def f(): return compute(a, b, c)",
        },
    }


def _unsized(monkeypatch) -> None:
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    _set_sizing(monkeypatch, (False, False))
    # No repo root: the XS-lane budget stands down and the sizing branches decide.
    monkeypatch.setattr(sizing, "_repo_root", lambda payload: None)


def _advisory(result) -> str:
    out = result["hookSpecificOutput"]
    assert out.get("permissionDecision") in (None, "allow")
    return out["additionalContext"]


def _level(monkeypatch, level: str) -> None:
    import coordinator_core.machine_profile as mp
    monkeypatch.setattr(mp, "guard_level", lambda name: level)


def test_unsized_advises_and_repeats_at_warn(monkeypatch) -> None:
    _unsized(monkeypatch)
    _level(monkeypatch, "warn")
    first = _advisory(op(_payload()))
    second = _advisory(op(_payload()))
    assert "No sizing" in first
    assert first == second


def test_unsized_denies_at_strict(monkeypatch) -> None:
    _unsized(monkeypatch)
    _level(monkeypatch, "strict")
    out = op(_payload())["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "coordinator:engine-ask" in out["permissionDecisionReason"]


def test_sized_advises_at_strict(monkeypatch) -> None:
    _level(monkeypatch, "strict")
    assert "route through the warp run" in _advisory(op(_payload()))


def test_sized_advises_and_repeats(monkeypatch) -> None:
    first = _advisory(op(_payload()))
    second = _advisory(op(_payload()))
    assert "route through the warp run" in first
    assert 'args: {"sizing": ' in first
    assert first == second


def test_plan_routed_sizing_without_a_plan_advises_and_repeats(monkeypatch) -> None:
    _set_sizing(monkeypatch, (True, True))
    first = _advisory(op(_payload()))
    second = _advisory(op(_payload()))
    assert "routed to a plan" in first and 'args: {"sizing": ' in first
    assert "coordinator:executor" not in first
    assert first == second


def test_unsized_advisory_names_the_ask_fire_not_the_express_lane(monkeypatch) -> None:
    _level(monkeypatch, "warn")
    _unsized(monkeypatch)
    reason = _advisory(op(_payload()))
    assert 'name: "coordinator:engine-ask", args: {"ask": ' in reason and "express-lane" not in reason


@pytest.mark.parametrize("route,plans,pending", [
    ("plan", [], True), ("spec-dispatch", [], True),
    ("plan", ["docs/plans/p.md"], False), ("dispatch", [], False),
])
def test_session_sizing_state_reads_the_route(monkeypatch, route, plans, pending) -> None:
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    monkeypatch.setattr(sizing, "_repo_root", lambda p: "/repo")
    monkeypatch.setattr(sizing, "_session_touched_sizing_files", lambda s, r: ["state/sizings/a.yaml"])
    monkeypatch.setattr(sizing, "_load_sizing_object", lambda r, rel: {"route": route})
    monkeypatch.setattr(sizing, "_session_touched_plan_files", lambda s, r: plans)
    assert _REAL_SIZING_STATE({"cwd": "/repo"}, "s") == (True, pending)


def test_subagent_allowed(monkeypatch) -> None:
    _unsized(monkeypatch)
    p = _payload()
    p["agent_id"] = "a1"
    assert op(p) is None


def test_doc_file_allowed(monkeypatch) -> None:
    _unsized(monkeypatch)
    p = _payload()
    p["tool_input"]["file_path"] = "/repo/docs/x.md"
    assert op(p) is None


def test_scratch_path_allowed(monkeypatch) -> None:
    _unsized(monkeypatch)
    p = _payload()
    p["tool_input"]["file_path"] = "/repo/scratch/probe.py"
    assert op(p) is None


def test_autonomous_allowed_when_sized(monkeypatch) -> None:
    monkeypatch.setattr(_mod, "resolve_mode", lambda k, sid: True)
    assert op(_payload()) is None


def test_autonomous_does_not_skip_sizing(monkeypatch) -> None:
    _unsized(monkeypatch)
    _level(monkeypatch, "strict")
    monkeypatch.setattr(_mod, "resolve_mode", lambda k, sid: True)
    assert op(_payload())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_empty_sentinel_still_advises(monkeypatch) -> None:
    open(_mod.dispatch_nudge_sentinel_path("sess-sizing-1"), "w", encoding="utf-8").close()
    assert _is_advisory(op(_payload()))


def _repo_with_reason(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    with open(_mod.dispatch_nudge_sentinel_path("sess-sizing-1"), "w", encoding="utf-8") as fh:
        fh.write("hotfix, executor unavailable\n")
    p = _payload()
    p["cwd"] = str(repo)
    return repo, p


def _sidecar(repo, status: str, agent: str = "coordinator:executor") -> None:
    from coordinator_core.session.machinery_paths import share_dir

    d = share_dir(str(repo), "sess-sizing-1")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "coordinator-executor.a1.md"), "w", encoding="utf-8") as fh:
        fh.write(f"---\nstatus: {status}\nagent_type: {agent}\n---\n")


def test_reason_without_grant_or_failure_advises_and_keeps_sentinel(tmp_path) -> None:
    _repo, p = _repo_with_reason(tmp_path)
    assert _is_advisory(op(p))
    assert os.path.exists(_mod.dispatch_nudge_sentinel_path("sess-sizing-1"))


def test_non_failed_executor_with_reason_advises(tmp_path) -> None:
    repo, p = _repo_with_reason(tmp_path)
    _sidecar(repo, "complete")
    assert _is_advisory(op(p))
    assert os.path.exists(_mod.dispatch_nudge_sentinel_path("sess-sizing-1"))


def test_pm_grant_is_honoured(tmp_path) -> None:
    from coordinator_core.session import guard_unlock_sentinel as gus

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    gus.sentinel_path("sess-sizing-1", "nudge-em-code-dispatch").write_text("")
    p = _payload()
    p["cwd"] = str(repo)
    assert op(p) is None
    assert _is_advisory(op(p))


def test_reasoned_sentinel_allows_and_logs_one_line(monkeypatch, tmp_path) -> None:
    import json

    from coordinator_core.hooks.dispatch_escape import ESCAPE_LOG_NAME

    repo, p = _repo_with_reason(tmp_path)
    _sidecar(repo, "blocked")
    assert op(p) is None
    logs = list((repo / ".coordinator-local").rglob(ESCAPE_LOG_NAME))
    assert len(logs) == 1
    lines = logs[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["reason"] == "hotfix, executor unavailable"
    assert entry["session_id"] == "sess-sizing-1"
    assert entry["path"] == "/repo/pkg/module.py"
    assert "ts" in entry


def test_launched_run_counts_as_sized_after_touched_release(monkeypatch) -> None:
    from coordinator_core.hooks import flag_em_poll_in_flight as poll
    from coordinator_core.hooks import nudge_unrouted_sizing as sizing

    monkeypatch.setattr(sizing, "_repo_root", lambda p: "/repo")
    monkeypatch.setattr(sizing, "_session_touched_sizing_files", lambda sid, root: [])
    monkeypatch.setattr(poll, "session_workflow_run_ids", lambda sid: ["ask-1"])
    assert _REAL_SIZING_STATE({"cwd": "/repo"}, "s") == (True, False)
    monkeypatch.setattr(poll, "session_workflow_run_ids", lambda sid: [])
    assert _REAL_SIZING_STATE({"cwd": "/repo"}, "s") == (False, False)
