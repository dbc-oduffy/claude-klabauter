"""Pin: every suite gate reaches ``suite_authority``, never ``check_tier_u_grant`` directly.

Two legs. (1) AST scan of ``coordinator_core/`` and ``coordinator/bin/`` for call sites of
``check_tier_u_grant`` -- bare-name and attribute calls on any receiver -- failing on any
caller outside the allowlist. (2) A caller-kind x tier matrix driving ``guard.check`` with the
real ``suite_authority`` and monkeypatched rungs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from coordinator_core import env_locality
from coordinator_core.bash_guards import check_test_suite_invocation as guard

pytestmark = pytest.mark.cadence

_REPO = Path(__file__).resolve().parents[2]
_SCAN_ROOTS = ("coordinator_core", "coordinator/bin")
_TARGET = "check_tier_u_grant"
_TRIPWIRE = "EVERY-SUITE-GATE-REACHES-SUITE-AUTHORITY"

# (repo-relative path, enclosing function or None for "whole file")
_ALLOWED = {
    ("coordinator_core/session/grant.py", None),
    ("coordinator_core/session/suite_authority.py", None),
    ("coordinator_core/session/grant_scope.py", None),
    ("coordinator_core/bash_guards/check_test_suite_invocation.py", "_tier_u_grant"),
}


def _callee_name(node: ast.Call):
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _calls_in(func) -> set:
    return {_callee_name(n) for n in ast.walk(func) if isinstance(n, ast.Call)}


def _violations():
    found = []
    for root in _SCAN_ROOTS:
        for path in sorted((_REPO / root).rglob("*.py")):
            rel = path.relative_to(_REPO).as_posix()
            if "/tests/" in rel or path.name.startswith("test_"):
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            if (rel, None) in _ALLOWED:
                continue
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                own = [
                    n for n in ast.walk(fn)
                    if isinstance(n, ast.Call) and _callee_name(n) == _TARGET
                ]
                if not own:
                    continue
                if (rel, fn.name) in _ALLOWED:
                    continue
                # A gate that first consults suite_authority may read the grant
                # record afterwards to explain a denial.
                if "suite_authority" in _calls_in(fn):
                    continue
                found.append(f"{rel}:{own[0].lineno} in {fn.name}()")
    return found


def test_no_suite_gate_calls_check_tier_u_grant_directly():
    bad = _violations()
    assert not bad, f"{_TRIPWIRE}: route through suite_authority(): " + "; ".join(bad)


def test_scan_sees_attribute_and_bare_calls():
    tree = ast.parse("def f():\n    a.check_tier_u_grant()\n    check_tier_u_grant()\n")
    assert [_callee_name(n) for n in ast.walk(tree) if isinstance(n, ast.Call)] == [_TARGET, _TARGET]


# ---------------------------------------------------------------- matrix

_FAST = "python -m pytest -m 'not slow and not cross_repo'"
_FULL = "python -m pytest"
_AGENT = "a0123456789abcdef"
_CLOUD_ENV = {"CLAUDE_CODE_REMOTE": "true"}


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'testpaths = ["coordinator_core", "coordinator/tests"]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd: str(tmp_path))
    monkeypatch.delenv(guard._OVERRIDE_ENV_VAR, raising=False)
    monkeypatch.setattr(guard, "_mutex_holder", lambda: None)
    return tmp_path


def _rung(monkeypatch, call):
    monkeypatch.setattr(env_locality, "machine_rung", lambda *a, **k: SimpleNamespace(call=call))


def _run(cmd, repo, *, env=None, agent_id=None):
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": "sess1",
        "cwd": str(repo),
    }
    if env is not None:
        payload["env"] = env
    if agent_id:
        payload["agent_id"] = agent_id
    result = guard.check(payload)
    assert result is not None, "suite command must not pass silently"
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _authority_passed(reason: str) -> bool:
    # Past the authority leg the only remaining denial is the mutex-wrapper one.
    return "with-suite-mutex" in reason


@pytest.mark.parametrize("cmd", [_FAST, _FULL], ids=["fast", "full"])
def test_cloud_em_authorized_without_grant(repo, monkeypatch, cmd):
    _rung(monkeypatch, "cloud")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
    assert _authority_passed(_run(cmd, repo, env=_CLOUD_ENV))


@pytest.mark.parametrize("cmd", [_FAST, _FULL], ids=["fast", "full"])
def test_box_em_denied_without_grant(repo, monkeypatch, cmd):
    _rung(monkeypatch, "attended")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
    assert not _authority_passed(_run(cmd, repo, env={}))


@pytest.mark.parametrize("cmd", [_FAST, _FULL], ids=["fast", "full"])
def test_box_em_authorized_with_grant(repo, monkeypatch, cmd):
    _rung(monkeypatch, "attended")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, {}))
    assert _authority_passed(_run(cmd, repo, env={}))


@pytest.mark.parametrize("cmd", [_FAST, _FULL], ids=["fast", "full"])
def test_cloud_subagent_denied_by_identity(repo, monkeypatch, cmd):
    _rung(monkeypatch, "cloud")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, {}))
    assert not _authority_passed(_run(cmd, repo, env=_CLOUD_ENV, agent_id=_AGENT))


def test_forged_marker_on_attended_machine_denied(repo, monkeypatch):
    _rung(monkeypatch, "attended")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (False, None))
    assert not _authority_passed(_run(_FULL, repo, env=_CLOUD_ENV))


@pytest.mark.parametrize(
    "cli",
    [
        "python coordinator/bin/validate-fast-and-packageability.py",
        "python coordinator/bin/workday-complete-step1-validate.py",
    ],
)
def test_cloud_subagent_validate_cli_denied_by_identity(repo, monkeypatch, cli):
    _rung(monkeypatch, "cloud")
    monkeypatch.setattr(guard, "_tier_u_grant", lambda cwd: (True, {}))
    reason = _run(cli, repo, env=_CLOUD_ENV, agent_id=_AGENT)
    assert not _authority_passed(reason)
