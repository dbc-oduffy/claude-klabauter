"""Every emitted agent prompt that names `bin/coordinator-invoke` carries the -32004 reconcile clause."""

from __future__ import annotations

import re

import yaml

from coordinator_core.ops.dispatch_emit.emit import _INDETERMINATE_RECONCILE_CLAUSE, compose_script
from coordinator_core.ops.dispatch_emit.terminal_commit_reconcile import TERMINAL_COMMIT_RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose as _compose_ask
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row
from coordinator_core.ops.dispatch_emit.tests.test_grind_compose import _compose as _compose_grind

_INVOKE_PATH = "bin/coordinator-invoke"
_INVOKE_ANY = "coordinator-invoke"
_TERMINAL_OP = "dispatch.terminal_commit"
#: Review-stage prompts are composed by review_mint.execute_review, outside this guard's scope.
_REVIEW_SIDECAR = "workflow-sidecars/review/"
_STALE = "reconcile it against `git log`"
_SESSION = "9d860631-c114-462a-a6ee-f685cc671ced"


def _prompts(script: str) -> list[str]:
    """The text of each `agent(` call up to its options object."""
    out = []
    for m in re.finditer(r"\bagent\(", script):
        tail = script[m.end():]
        end = re.search(r"\{\s*label:", tail)
        out.append((tail[: end.start()] if end else tail[:20000]).replace("\\'", "'"))
    return out


def _assert_clause_everywhere(script: str, *, marker: str = _INVOKE_PATH) -> int:
    named = [p for p in _prompts(script) if marker in p and _REVIEW_SIDECAR not in p]
    for prompt in named:
        want = TERMINAL_COMMIT_RECONCILE_CLAUSE if _TERMINAL_OP in prompt else _INDETERMINATE_RECONCILE_CLAUSE
        assert want in prompt, prompt[:200]
    assert _STALE not in script
    return len(named)


def test_clause_text_names_the_engine_contract_fields():
    for token in ("-32004", "reconcile_command", "not_received", "finished", "exactly once"):
        assert token in _INDETERMINATE_RECONCILE_CLAUSE


def test_ask_script_raw_prompt_arm():
    assert _assert_clause_everywhere(_compose_ask(in_run_commit=True), marker=_INVOKE_ANY) >= 3


def test_ask_script_sizing_arm(tmp_path):
    rel = "state/sizings/2026-10-12-existing.yaml"
    (tmp_path / "state/sizings").mkdir(parents=True)
    (tmp_path / rel).write_text(
        yaml.safe_dump({"estimate": {"tshirt": "S"}, "route": "plan", "intent": "x"}), encoding="utf-8"
    )
    script = _compose_ask(repo_root=str(tmp_path), prompt=None, sizing_rel=rel)
    assert _assert_clause_everywhere(script, marker=_INVOKE_ANY) >= 1


def test_plan_route_emit_committer_carries_the_clause():
    script = compose_script(
        [[_write_row("C1"), _write_row("C2")]],
        name="wf", description="d", session_id=_SESSION, expected_branch="work/run", **REVIEW_KW,
    )
    assert _assert_clause_everywhere(script, marker=_INVOKE_ANY) >= 1


def test_grind_script():
    assert _assert_clause_everywhere(_compose_grind()) >= 1


def test_clause_appears_once_per_prompt():
    for prompt in _prompts(_compose_ask()):
        assert prompt.count(_INDETERMINATE_RECONCILE_CLAUSE) <= 1
        assert prompt.count(TERMINAL_COMMIT_RECONCILE_CLAUSE) <= 1


def test_no_prompt_tells_the_agent_to_read_git_log():
    assert _STALE not in _compose_ask()
