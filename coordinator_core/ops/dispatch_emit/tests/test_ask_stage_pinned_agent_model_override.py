"""A refire through dispatch.ask_gate stages a row whose agent_model conflicts with its type's pin at the pin."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_gate, emit, halted_run, kept_work
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import _handler
from coordinator_core.ops.dispatch_emit.reply_sidecar import read_reply
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_resume import PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate_s_resume_stages import _git

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _refire_repo(root, agent_extra):
    _put(root, tshirt="S", route="spec-dispatch")
    plan = root / PLAN_REL
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text(
        "---\nrun_id: fixture\n---\n\n## Tasks\n\n```yaml plan-tasks\n"
        f"- id: A1\n  title: TA1\n  disposition: open\n{agent_extra}  writes:\n    - some/A1.py\n```\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")


def _staged_row(root):
    out = _handler({"sizing_path": REL, "run_id": "r1", "writes": ["a.py"]}, repo_root=root)
    assert out.get("halt") is None and "manifest_handle" in out, out
    (row,) = read_reply(root, out["manifest_handle"])["rows"]
    return row


def test_haiku_request_for_pinned_executor_stages_at_the_pin(repo):  # noqa: F811
    _refire_repo(repo, "  agent_model: haiku\n")
    row = _staged_row(repo)
    assert (row["agent_type"], row["model"]) == ("coordinator:executor", "sonnet")


def test_unpinned_type_keeps_its_override(repo):  # noqa: F811
    _refire_repo(repo, "  agent_type: acme:custom-agent\n  agent_model: haiku\n")
    row = _staged_row(repo)
    assert (row["agent_type"], row["model"]) == ("acme:custom-agent", "haiku")


def test_haiku_origin_is_not_the_ask_compose_agent_helper_or_emit_row_opts():
    assert ask_compose._MECHANICAL_AGENT_TYPE not in emit._AGENT_MODELS
    assert ask_compose._RULINGS_AGENT_TYPE not in emit._AGENT_MODELS
    with pytest.raises(emit.MalformedAgentOverrideError):
        ask_compose._agent("p", label="l", phase="p", agent_type="coordinator:executor", schema={}, agent_model="haiku")
    with pytest.raises(emit.MalformedAgentOverrideError):
        emit._model_opt("coordinator:executor", "haiku")


def test_pinned_executor_agent_opts_carry_sonnet_never_haiku():
    composed = ask_compose._agent("p", label="l", phase="p", agent_type="coordinator:executor", schema={})
    row = WaveRow(
        id="A1", title="t", surface="s", writes=["some/A1.py"], reads=[], depends_on=[],
        agent_type="coordinator:executor", agent_model=None,
    )
    over = emit._agent_call_over("'p'", row, None, label_id="A1")
    for call in (composed, over):
        assert "model: 'sonnet'" in call and "haiku" not in call


def test_review_blocked_refire_from_foreign_cwd_stages_fix_leg_at_the_pin(repo, tmp_path_factory, monkeypatch):  # noqa: F811
    monkeypatch.setattr(ask_gate, "_bind_run", lambda *a, **k: None)
    _refire_repo(repo, "  agent_model: haiku\n")
    record = repo / RECORD_DIR_ROOT / "ask-20261001T000000-aaaaaa"
    halted_run.write_halt_marker(record, halted="review_blocked", sizing_rel=REL, plan_rel=PLAN_REL)
    kept_work.write(record, kept_work.kept_chunks(repo, {}, {}), None)
    monkeypatch.chdir(tmp_path_factory.mktemp("foreign_cwd"))
    out = _handler({"sizing_path": REL, "run_id": "ask-20261002T000000-bbbbbb", "writes": ["a.py"]}, repo_root=repo)
    assert "MalformedAgentOverrideError" not in str(out), out
    assert out.get("halt") is None, out
    (row,) = read_reply(repo, out["manifest_handle"])["rows"]
    assert (row["agent_type"], row["model"]) == ("coordinator:executor", "sonnet")
