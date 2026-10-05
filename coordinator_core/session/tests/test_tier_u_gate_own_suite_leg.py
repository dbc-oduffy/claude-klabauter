"""Own-suite leg of enforce_tier_u_gate: a pytest-run process resolving this
repo's own on-disk tier is refused before the grant is consulted. No spawn of
the tier; gate-tier (no cadence/spawns_process marker)."""

from __future__ import annotations

import subprocess
import unittest.mock as mock

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.session import tier_u_gate
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_FAST = "pytest coordinator_core/sub/test_x.py"


def _repo(tmp_path, *, fast=None, full=None):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, **no_console_passthrough_kwargs())
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["coordinator_core"]\n'
    )
    lines = ["---"]
    if fast is not None:
        lines.append(f'fast_test_cmd: "{fast}"')
    if full is not None:
        lines.append(f'full_test_cmd: "{full}"')
    lines.append("---\n")
    (tmp_path / "coordinator.local.md").write_text("\n".join(lines), encoding="utf-8")
    return tmp_path


@pytest.fixture
def grant_calls(monkeypatch):
    calls = []

    def _spy(**kw):
        calls.append(kw)
        return True, None

    monkeypatch.setattr(tier_u_gate, "check_tier_u_grant", _spy)
    return calls


def _gate_f(repo, cmd):
    with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
         mock.patch.object(
             guard, "_configured_test_cmds",
             lambda root: [guard.ConfiguredCmd("fast_test_cmd", _FAST, 0)],
         ):
        return tier_u_gate.enforce_tier_u_gate(cmd, repo_root=str(repo), session_id="s1")


def test_tier_f_own_fast_cmd_refused_before_grant(tmp_path, monkeypatch, grant_calls):
    repo = _repo(tmp_path, fast=_FAST)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    result = _gate_f(repo, _FAST)
    assert result.proceed is False
    assert "PYTEST_CURRENT_TEST" in result.refusal_message
    assert "fast_test_cmd" in result.refusal_message
    assert "COORDINATOR_FAST_TEST_CMD" in result.refusal_message
    assert grant_calls == []


def test_tier_f_space_prefix_extension_refused(tmp_path, monkeypatch, grant_calls):
    repo = _repo(tmp_path, fast=_FAST)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    result = _gate_f(repo, _FAST + " coordinator_core/other/test_z.py")
    assert result.proceed is False
    assert grant_calls == []


def test_tier_u_full_cmd_refused_before_grant(tmp_path, monkeypatch, grant_calls):
    repo = _repo(tmp_path, full="pytest")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    result = tier_u_gate.enforce_tier_u_gate("pytest", repo_root=str(repo), session_id="s1")
    assert result.proceed is False
    assert "full_test_cmd" in result.refusal_message
    assert grant_calls == []


def test_without_pytest_env_grant_is_consulted(tmp_path, monkeypatch, grant_calls):
    repo = _repo(tmp_path, fast=_FAST)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    result = _gate_f(repo, _FAST)
    assert result.proceed is True
    assert len(grant_calls) == 1


def test_no_on_disk_key_leaves_stub_injection_alone(tmp_path, monkeypatch, grant_calls):
    repo = _repo(tmp_path)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    assert tier_u_gate._resolving_own_tier_from_inside_its_suite(_FAST, str(repo)) is None
    result = _gate_f(repo, _FAST)
    assert result.proceed is True
    assert len(grant_calls) == 1


def test_non_prefix_command_not_matched(tmp_path, monkeypatch):
    repo = _repo(tmp_path, fast=_FAST)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "x::y (call)")
    assert tier_u_gate._resolving_own_tier_from_inside_its_suite(_FAST + "x", str(repo)) is None
    assert tier_u_gate._resolving_own_tier_from_inside_its_suite("echo hi", str(repo)) is None
    assert tier_u_gate._resolving_own_tier_from_inside_its_suite(_FAST, str(repo)) == "fast_test_cmd"
