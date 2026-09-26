
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.subagent_sandbox import engine
from coordinator_core.hooks.cater_subagent_start import (
    NAMED_DISPATCH_ROW_RESOLVED_MARKER,
    compose_catering,
)
from coordinator_core.win_portability import no_console_passthrough_kwargs

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

ELIGIBLE_TYPE = "coordinator:code-reviewer"
ABSENT_ROW_TYPE = "coordinator:git-commit-agent"

SNIPPET_A = "quota-self-detect-preamble"
SNIPPET_A_BODY = "INJECTION-ONLY-CANARY-A: this sentence exists nowhere except snippet A."

NAMED_AGENT_ID = "a-catering-tester-0123456789abcdef"
EM_SESSION_ID = "em-session-cater-1"


def _canonical_key(raw_agent_id: str, payload_session_id: str) -> str:
    return engine._canonical_agent_id(raw_agent_id, payload_session_id)


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, **no_console_passthrough_kwargs())
    (tmp_path / "coordinator" / "snippets").mkdir(parents=True)
    # fixture's own git root -- point its `CLAUDE_PLUGIN_ROOT` rung at
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path / "coordinator"))
    registry = tmp_path / "coordinator" / "snippets" / "registry.toml"
    registry.write_text(
        "schema_version = 1\n\n"
        f'[snippet.{SNIPPET_A}]\n'
        f'sentinel_begin = "<!-- BEGIN {SNIPPET_A} -->"\n'
        f'sentinel_end = "<!-- END {SNIPPET_A} -->"\n'
        'consumers = []\n',
        encoding="utf-8",
    )
    (tmp_path / "coordinator" / "snippets" / f"{SNIPPET_A}.md").write_text(
        f"<!-- BEGIN {SNIPPET_A} -->\n{SNIPPET_A_BODY}\n<!-- END {SNIPPET_A} -->\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    policy = tmp_path / "subagent-sandbox-policy.yaml"
    policy.write_text(
        "report_sidecar:\n"
        f"  - {ELIGIBLE_TYPE}\n",
        encoding="utf-8",
    )
    return policy


@pytest.fixture(autouse=True)
def _policy_env(monkeypatch: pytest.MonkeyPatch, policy_path: Path) -> None:
    monkeypatch.setenv("SUBAGENT_SANDBOX_POLICY", str(policy_path))


@pytest.fixture(autouse=True)
def _no_role_append(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-config"))


def _payload(agent_type: str, session_id: str, cwd: str, contract_blocks=None, agent_id=None) -> dict:
    payload = {"agent_type": agent_type, "session_id": session_id, "cwd": cwd}
    if contract_blocks is not None:
        payload["contract_blocks"] = contract_blocks
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return payload


def _write_backpointer(git_root: Path, agent_id: str, em_sid: str, resolved_subagent_type: str) -> None:
    agents_dir = git_root / ".git" / "coordinator-sessions" / ".agents" / agent_id
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "em-session-id.txt").write_text(em_sid + "\n", encoding="utf-8")

    session_dir = git_root / ".git" / "coordinator-sessions" / em_sid
    session_dir.mkdir(parents=True, exist_ok=True)
    dispatch_file = session_dir / "dispatched-agents.txt"
    with open(dispatch_file, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{agent_id}\tclaude-sonnet-5\t{resolved_subagent_type}\t1700000000\n")


def test_list_shape_still_composes_blocks_inline_unchanged(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(
        ELIGIBLE_TYPE, "session-list-1", str(git_repo), contract_blocks=[SNIPPET_A]
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY in result
    stderr = capsys.readouterr().err
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in stderr


def test_mapping_shape_unnamed_dispatch_selects_row_on_agent_type(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(
        ELIGIBLE_TYPE,
        "session-map-unnamed-1",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY in result
    stderr = capsys.readouterr().err
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in stderr


def test_mapping_shape_named_dispatch_resolves_via_backpointer(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    _write_backpointer(
        git_repo,
        _canonical_key(NAMED_AGENT_ID, "session-map-named-1"),
        EM_SESSION_ID,
        ELIGIBLE_TYPE,
    )
    payload = _payload(
        "patrik",
        "session-map-named-1",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},
        agent_id=NAMED_AGENT_ID,
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY in result

    stderr = capsys.readouterr().err
    assert stderr.count(NAMED_DISPATCH_ROW_RESOLVED_MARKER) == 1
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in result


def test_mapping_shape_legitimately_absent_row_no_diagnostic(
    git_repo: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    role_canary = "INJECTION-ONLY-CANARY-ROLE: role framing text exists nowhere else."
    claude_dir = tmp_path / "role-claude-config"
    snippet_dir = claude_dir / "plugins" / "coordinator-claude" / "coordinator" / "snippets"
    snippet_dir.mkdir(parents=True)
    (snippet_dir / "agent-role-dispatched.md").write_text(role_canary, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_dir))

    payload = _payload(
        ABSENT_ROW_TYPE,
        "session-map-absent-1",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},  # ABSENT_ROW_TYPE is not a key
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY not in result
    assert role_canary in result

    stderr = capsys.readouterr().err
    assert "present but empty" not in stderr
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in stderr


def test_mapping_shape_present_but_empty_row_emits_diagnostic(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    payload = _payload(
        ELIGIBLE_TYPE,
        "session-map-empty-1",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: []},
    )
    result = compose_catering(payload, cwd=str(git_repo))

    assert SNIPPET_A_BODY not in result

    stderr = capsys.readouterr().err
    assert "present but empty" in stderr
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in stderr


def test_counter_fires_only_for_named_dispatch(
    git_repo: Path, capsys: pytest.CaptureFixture
) -> None:
    unnamed_payload = _payload(
        ELIGIBLE_TYPE,
        "session-counter-unnamed",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},
    )
    compose_catering(unnamed_payload, cwd=str(git_repo))
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in capsys.readouterr().err

    _write_backpointer(
        git_repo,
        _canonical_key(NAMED_AGENT_ID, "session-counter-named"),
        EM_SESSION_ID,
        ELIGIBLE_TYPE,
    )
    named_payload = _payload(
        "patrik",
        "session-counter-named",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},
        agent_id=NAMED_AGENT_ID,
    )
    compose_catering(named_payload, cwd=str(git_repo))
    assert capsys.readouterr().err.count(NAMED_DISPATCH_ROW_RESOLVED_MARKER) == 1


def test_counter_output_is_stderr_only(git_repo: Path, capsys: pytest.CaptureFixture) -> None:
    _write_backpointer(
        git_repo,
        _canonical_key(NAMED_AGENT_ID, "session-stderr-only-1"),
        EM_SESSION_ID,
        ELIGIBLE_TYPE,
    )
    payload = _payload(
        "patrik",
        "session-stderr-only-1",
        str(git_repo),
        contract_blocks={ELIGIBLE_TYPE: [SNIPPET_A]},
        agent_id=NAMED_AGENT_ID,
    )
    result = compose_catering(payload, cwd=str(git_repo))

    captured = capsys.readouterr()
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER in captured.err
    assert NAMED_DISPATCH_ROW_RESOLVED_MARKER not in result


@pytest.mark.parametrize("malformed", [None, "not-a-list-or-map", 42, 3.14, True])
def test_malformed_contract_blocks_fail_open(git_repo: Path, malformed) -> None:
    payload = _payload(
        ELIGIBLE_TYPE, "session-malformed-1", str(git_repo), contract_blocks=malformed
    )
    result = compose_catering(payload, cwd=str(git_repo))
    assert isinstance(result, str)
