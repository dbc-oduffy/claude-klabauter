"""The main EM re-verifying an executor's work: second pytest run and scratch .py writes deny."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from coordinator_core.bash_guards import guard_post_executor_verification as g
from coordinator_core.hooks import nudge_em_code_dispatch as nd

SID = "sess-verify-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(nd, "_is_bootstrap_or_out_of_repo", lambda p: False)
    monkeypatch.setattr(nd, "_session_sizing_state", lambda payload, sid: (True, False))
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: False)
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "repo" / "pkg").mkdir()
    (tmp_path / "repo" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "repo" / "tools").mkdir()
    g._PKG_CACHE.clear()


@pytest.fixture
def repo(tmp_path):
    return tmp_path / "repo"


def _executor_ran(repo, agent_type="coordinator:executor") -> None:
    d = repo / ".git" / "coordinator-sessions" / SID
    d.mkdir(parents=True)
    (d / "dispatched-agents.txt").write_text(f"abcdef0123456789\tsonnet\t{agent_type}\t1\n")


def _bash(repo, cmd: str) -> dict:
    return {"tool_name": "Bash", "session_id": SID, "cwd": str(repo), "tool_input": {"command": cmd}}


def _write(repo, path: str, tool="Write") -> dict:
    return {
        "tool_name": tool,
        "session_id": SID,
        "cwd": str(repo),
        "tool_input": {"file_path": path, "content": "print(1)\n"},
    }


def _denied(out) -> bool:
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_no_executor_yet_pytest_is_free(repo) -> None:
    for _ in range(3):
        assert g.check(_bash(repo, "python -m pytest a.py")) is None


def test_first_pytest_allowed_second_denied(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "python -m pytest a.py")) is None
    out = g.check(_bash(repo, "pytest b.py -q"))
    assert _denied(out)
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert '"coordinator:engine-ask", args: {"sizing": ' in reason
    assert "No self-escape" in reason and "honoured once." in reason


def test_grep_pytest_not_counted(repo) -> None:
    _executor_ran(repo)
    for _ in range(3):
        assert g.check(_bash(repo, "grep -rn pytest coordinator_core")) is None
    assert g.check(_bash(repo, "pytest a.py")) is None


def test_non_executor_dispatch_does_not_arm(repo) -> None:
    _executor_ran(repo, "coordinator:test-runner")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert g.check(_bash(repo, "pytest a.py")) is None


def test_subagent_exempt(repo) -> None:
    _executor_ran(repo)
    p = _bash(repo, "pytest a.py")
    p["agent_id"] = "a1"
    assert g.check(p) is None and g.check(p) is None


def test_autonomous_still_routes_bash_verification(repo, monkeypatch) -> None:
    _executor_ran(repo)
    monkeypatch.setattr("coordinator_core.session.mode_resolution.resolve_mode", lambda k, sid: True)
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_autonomous_scratch_write_exempt(repo, monkeypatch) -> None:
    _executor_ran(repo)
    monkeypatch.setattr(nd, "resolve_mode", lambda k, sid: True)
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None


def _sidecar(repo, status: str) -> None:
    from coordinator_core.session.machinery_paths import share_dir

    d = share_dir(str(repo), SID)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "coordinator-executor.a1.md"), "w") as fh:
        fh.write(f"---\nstatus: {status}\nagent_type: coordinator:executor\n---\n")


def _armed_with_reason(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    with open(nd.dispatch_nudge_sentinel_path(SID), "w") as fh:
        fh.write("need a wider run\n")


def test_reason_without_grant_or_failure_denied_not_consumed(repo) -> None:
    _armed_with_reason(repo)
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_non_failed_executor_with_reason_denied(repo) -> None:
    _armed_with_reason(repo)
    _sidecar(repo, "complete")
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


def test_pm_grant_honoured(repo) -> None:
    from coordinator_core.session import guard_unlock_sentinel as gus

    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    gus.sentinel_path(SID, g.GUARD_NAME).write_text("")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_reasoned_sentinel_allows_and_logs(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert _denied(g.check(_bash(repo, "pytest a.py")))
    with open(nd.dispatch_nudge_sentinel_path(SID), "w") as fh:
        fh.write("need a wider run\n")
    _sidecar(repo, "blocked")
    assert g.check(_bash(repo, "pytest a.py")) is None
    logs = list((repo / ".coordinator-local").rglob(nd.ESCAPE_LOG_NAME))
    assert len(logs) == 1
    assert json.loads(logs[0].read_text().splitlines()[0])["reason"] == "need a wider run"
    assert not os.path.exists(nd.dispatch_nudge_sentinel_path(SID))
    assert _denied(g.check(_bash(repo, "pytest a.py")))


def test_scratch_py_write_denied_after_executor(repo) -> None:
    _executor_ran(repo)
    out = nd.op(_write(repo, str(repo / "scratch" / "probe.py")))
    assert _denied(out)
    assert "warp run's test stage" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_scratch_py_write_allowed_before_executor(repo) -> None:
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None


def test_scratch_md_write_allowed_after_executor(repo) -> None:
    _executor_ran(repo)
    assert nd.op(_write(repo, str(repo / "scratch" / "notes.md"))) is None


def test_scratch_py_sentinel_allows_and_logs(repo) -> None:
    _executor_ran(repo)
    with open(nd.dispatch_nudge_sentinel_path(SID), "w") as fh:
        fh.write("one-off probe\n")
    _sidecar(repo, "blocked")
    assert nd.op(_write(repo, str(repo / "scratch" / "probe.py"))) is None
    assert len(list((repo / ".coordinator-local").rglob(nd.ESCAPE_LOG_NAME))) == 1
    assert _denied(nd.op(_write(repo, str(repo / "scratch" / "probe.py"))))


def test_free_first_run_does_not_spend_the_sentinel(repo) -> None:
    _executor_ran(repo)
    with open(nd.dispatch_nudge_sentinel_path(SID), "w") as fh:
        fh.write("saved for later\n")
    assert g.check(_bash(repo, "pytest a.py")) is None
    assert os.path.exists(nd.dispatch_nudge_sentinel_path(SID))


@pytest.mark.parametrize("cmd", [
    "SP=/s; with-suite-mutex -- timeout 900 python3 -m pytest tests/ -q -k ledger > $SP/a 2>&1",
    "n=$(timeout 120 python3 -m pytest t.py --collect-only -q | wc -l)",
    "for f in a.py b.py; do n=$(timeout 120 python3 -m pytest $f -q | tail -1); echo $n; done",
    "with-suite-mutex -- timeout 600 bash -c 'for i in 1 2; do python3 -m pytest a.py -q; done'",
    "nice -n 5 nohup time pytest a.py",
])
def test_wrapped_pytest_is_counted(cmd) -> None:
    assert g.runs_pytest(cmd)


@pytest.mark.parametrize("cmd", [
    "timeout 5 grep -rn pytest x",
    "for f in a; do grep pytest $f; done",
    "x=$(git log | grep pytest)",
    "bash -c 'echo pytest'",
])
def test_wrapped_non_pytest_not_counted(cmd) -> None:
    assert not g.runs_pytest(cmd)


@pytest.mark.parametrize("cmd", [
    "python3 -c 'from pkg.mod import f; print(f())'",
    "python3 -c 'import os, pkg'",
    "timeout 60 python3 -c 'import pkg.x as m; m.run()'",
    "n=$(python3 -c 'from pkg import a' | wc -l)",
    "python3 -I /elsewhere/scratchpad/census.py",
    "python3 -I ../scratch/probe.py",
    "bash -c 'python3 -c \"import pkg\"'",
    "ls scratch && python3 - <<'PY'\nimport importlib.util\nm = load(\"pkg/mod.py\", \"a\")\nPY",
    "python3 <<PY\nfrom pkg import a\nPY",
    "python3 -c 'spec_from_file_location(\"m\", \"./pkg/mod.py\")'",
])
def test_probe_forms_counted(repo, cmd) -> None:
    assert g.runs_verification(cmd, str(repo), str(repo))


@pytest.mark.parametrize("cmd", [
    "python3 -c 'import os, json; print(1)'",
    "python3 -c 'import numpy'",
    "python3 tools/x.py",
    "python3 -I ./pkg/run.py --flag",
    "python3 -m json.tool f.json",
    "python3 /opt/engine/scripts/rag_work_target_index.py --status",
    "ls pkg",
    "python3 - <<'PY'\nimport json\nprint(json.load(open(\"pkg/data.json\")))\nPY",
    "cat <<EOF > notes.txt\nimport pkg\nEOF",
])
def test_non_probe_forms_not_counted(repo, cmd) -> None:
    assert not g.runs_verification(cmd, str(repo), str(repo))


def test_probe_shares_pytest_counter(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "pytest a.py")) is None
    out = g.check(_bash(repo, "python3 -c 'import pkg'"))
    assert _denied(out)
    assert "honoured once." in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_probe_first_free_then_probe_denied(repo) -> None:
    _executor_ran(repo)
    assert g.check(_bash(repo, "python3 -c 'import os'")) is None
    assert g.check(_bash(repo, "python3 /elsewhere/scratchpad/census.py")) is None
    assert _denied(g.check(_bash(repo, "python3 /elsewhere/scratchpad/census.py")))
