"""The composed ask script lists gated rows as incomplete and gives a manifest-less run no next_action."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_GUARD = "(_manifest && !_manifest.error)"
_PUSH = "  if (!_manifest.error) { for (const g of (_manifest.gated ?? [])) { _incompleteChunks.push(g.id); } }"


def _script(monkeypatch) -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel="state/sizings/x.yaml",
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        script_path="x.mjs",
        **REVIEW_KW,
    )


def _next_action(script: str) -> str:
    return script[script.index("next_action: { kind: ") :]


def test_next_action_is_none_without_a_clean_manifest(monkeypatch):
    na = _next_action(_script(monkeypatch))
    assert na.startswith("next_action: { kind: 'terminal_commit', op: 'dispatch.terminal_commit'")
    override = f"...({_GUARD} ? {{}} : {{ next_action: {{ kind: 'none', op: null, params: null }} }}) }};"
    assert na.rstrip().endswith(override)


def test_gated_ids_are_pushed_after_stage_and_before_execute(monkeypatch):
    script = _script(monkeypatch)
    assert script.count(_PUSH) == 1
    assert script.index("_manifest = await agent(") < script.index(_PUSH) < script.index("phase('execute')")


def test_incomplete_chunks_feed_terminal_commit_params(monkeypatch):
    params = _next_action(_script(monkeypatch))
    assert "_incompleteChunks" in params


def test_manifest_schema_carries_optional_gated():
    assert "gated" in ask_compose._MANIFEST_SCHEMA["properties"]
    assert "gated" not in ask_compose._MANIFEST_SCHEMA["required"]


def test_script_stays_under_byte_cap(monkeypatch):
    assert len(_script(monkeypatch).encode("utf-8")) <= emit._WORKFLOW_SCRIPT_BYTE_CAP
