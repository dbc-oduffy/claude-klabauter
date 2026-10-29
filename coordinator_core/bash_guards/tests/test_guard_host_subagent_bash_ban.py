"""coordinator_core.bash_guards.tests.test_guard_host_subagent_bash_ban --
behavioural pin for `guard_host_subagent_bash_ban.check`: the fail-open and
scope contracts, not the happy path.

A guard on the Bash path is one bug from bricking every dispatched agent, so
every malformed, missing or unreadable input must ALLOW, the EM must never be
denied, PowerShell must never be denied, and a host that has not opted in must
be unaffected. Each fixture builds a throwaway `coordinator.local.md` under
`tmp_path`; the guard resolves config by walking up from the payload `cwd`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from coordinator_core.bash_guards import guard_host_subagent_bash_ban as guard
from coordinator_core.bash_guards.dispatch import evaluate_payload_json
from coordinator_core.guard_messages.testing import envelope_of


def _enveloped(check):
    return lambda *args, **kwargs: envelope_of(check(*args, **kwargs))

_DENY_FRONTMATTER = "---\nproject_type: general\nsubagent_bash_policy: deny\n---\n\nbody\n"
_NO_POLICY_FRONTMATTER = "---\nproject_type: general\n---\n\nbody\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "coordinator.local.md").write_text(_DENY_FRONTMATTER, encoding="utf-8")
    return tmp_path


def _payload(repo_root: Path, *, tool: str = "Bash", agent_id: Any = "agent-abc123", command: str = "ls -la") -> Dict[str, Any]:
    body: Dict[str, Any] = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(repo_root)}
    if agent_id is not None:
        body["agent_id"] = agent_id
    return body


def _write_config(root: Path, text: str) -> None:
    (root / "coordinator.local.md").write_text(text, encoding="utf-8")


def _reason(result: Dict[str, Any]) -> str:
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_denies_a_dispatched_agents_bash_call_on_an_opted_in_host(repo: Path) -> None:
    result = _enveloped(guard.check)(_payload(repo))
    assert result is not None
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "BLOCKED" in _reason(result)
    assert "PowerShell" in _reason(result), "a deny must name the sanctioned alternative"


def test_the_em_is_never_denied(repo: Path) -> None:
    assert _enveloped(guard.check)(_payload(repo, agent_id=None)) is None


def test_a_blank_agent_id_is_treated_as_the_em(repo: Path) -> None:
    assert _enveloped(guard.check)(_payload(repo, agent_id="   ")) is None


def test_powershell_is_never_denied(repo: Path) -> None:
    assert _enveloped(guard.check)(_payload(repo, tool="PowerShell")) is None


def test_the_ban_matcher_set_stays_bash_only() -> None:
    assert guard.MATCHERS == ("Bash",)


def test_a_host_that_has_not_opted_in_is_unaffected(tmp_path: Path) -> None:
    _write_config(tmp_path, _NO_POLICY_FRONTMATTER)
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


def test_a_non_deny_policy_value_is_not_a_ban(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nsubagent_bash_policy: warn\n---\n")
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


def test_policy_survives_a_trailing_comment(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nsubagent_bash_policy: deny  # spawn cost\n---\n")
    assert _enveloped(guard.check)(_payload(tmp_path)) is not None


def test_config_is_found_from_a_nested_cwd(repo: Path) -> None:
    nested = repo / "coordinator" / "hooks"
    nested.mkdir(parents=True)
    assert _enveloped(guard.check)(_payload(nested)) is not None


def test_no_config_anywhere_allows(tmp_path: Path) -> None:
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


def test_missing_frontmatter_allows(tmp_path: Path) -> None:
    _write_config(tmp_path, "no frontmatter here\n")
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


def test_a_policy_key_in_the_body_of_an_unterminated_fence_allows(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nproject_type: general\n\n# Notes\nsubagent_bash_policy: deny\n")
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


def test_an_indented_policy_key_is_not_a_declaration(tmp_path: Path) -> None:
    _write_config(tmp_path, "---\nnested:\n  subagent_bash_policy: deny\n---\n")
    assert _enveloped(guard.check)(_payload(tmp_path)) is None


@pytest.mark.parametrize("raw", ["{not json", "", "[1, 2, 3]"], ids=["malformed", "empty", "non_dict"])
def test_unusable_payload_text_does_not_deny(raw: str) -> None:
    result = evaluate_payload_json(raw)
    assert not (isinstance(result, dict) and result.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")


def test_a_non_dict_payload_allows() -> None:
    assert _enveloped(guard.check)([1, 2, 3]) is None  # type: ignore[arg-type]


def test_a_missing_cwd_allows_and_never_reads_the_process_cwd(repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the deny config in the process cwd, an `os.getcwd()` fallback would
    deny; the engine's resident server must never read its own cwd instead."""
    monkeypatch.chdir(repo)
    payload = _payload(repo)
    del payload["cwd"]
    assert _enveloped(guard.check)(payload) is None


def test_registered_in_the_bash_chain(repo: Path) -> None:
    """Registration is pinned end to end: the dispatch chain advises on this fixture.

    guard-host-subagent-bash-ban is ADVISORY_BY_RULING (b17ec35251; DR-475, 307ea22403)."""
    envelope = evaluate_payload_json(json.dumps(_payload(repo)))
    assert isinstance(envelope, dict)
    hso = envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "PowerShell" in hso["additionalContext"]
    assert "python" in hso["additionalContext"]
