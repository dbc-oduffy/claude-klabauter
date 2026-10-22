"""The composed ask script lists gated rows as incomplete and gives a manifest-less run no next_action."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.land_run_contract import WAKE_DIGEST_COMMIT_OP
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.wake_digest import (
    CHUNK_REASON_VAR,
    PLAN_HELD_VAR,
    RUNTIME_VARS,
)

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
    return script[script.index("  _nextAction = ") :]


def test_next_action_is_none_without_a_clean_manifest(monkeypatch):
    na = _next_action(_script(monkeypatch))
    assert na.startswith("  _nextAction = _closeable ? { kind: (")
    assert f"? null : '{WAKE_DIGEST_COMMIT_OP}'), params: " in na.split("\n", 1)[0]
    assert f"const _closeable = !!((!_halted && {_GUARD[1:-1]}" in _script(monkeypatch)
    # Semantics: no clean manifest -> kind none, unless the halt itself names a repin_register
    # next_action (6478788038); the fallback is not pinned to a literal tail.
    head = na.split("\n", 1)[0]
    assert "_halted?.next_action?.kind === 'repin_register' ? _halted.next_action : { kind: 'none', op: null, params: null }" in head
    assert na.rstrip().endswith("get terminal_commit_cli() { return _terminalCommitCli(this.next_action.params); }, };")


def test_gated_ids_are_pushed_after_stage_and_before_execute(monkeypatch):
    script = _script(monkeypatch)
    assert script.count(_PUSH) == 1
    assert script.index("if (_stagedManifest) { _manifest = _stagedManifest; }") < script.index(_PUSH) < script.index("phase('Execute')")


def test_incomplete_chunks_feed_terminal_commit_params(monkeypatch):
    params = _next_action(_script(monkeypatch))
    assert "_incompleteChunks" in params


def test_manifest_schema_carries_optional_gated():
    assert "gated" in ask_compose._MANIFEST_SCHEMA["properties"]
    assert "gated" not in ask_compose._MANIFEST_SCHEMA["required"]


def test_manifest_schema_carries_optional_owed():
    owed = ask_compose._MANIFEST_SCHEMA["properties"]["owed"]
    assert "owed" not in ask_compose._MANIFEST_SCHEMA["required"]
    assert owed["items"]["required"] == ["id"]
    assert set(owed["items"]["properties"]) == {"id", "reason", "detail"}


def test_owed_ids_are_pushed_after_stage_and_before_execute(monkeypatch):
    script = _script(monkeypatch)
    push = "  if (!_manifest.error) { for (const o of (_manifest.owed ?? [])) { _incompleteChunks.push(o.id); } }"
    assert script.count(push) == 1
    assert script.index("if (_stagedManifest) { _manifest = _stagedManifest; }") < script.index(push) < script.index("phase('Execute')")


def test_return_object_names_owed(monkeypatch):
    script = _script(monkeypatch)
    assert "owed: ((_manifest && !_manifest.error) ? (_manifest.owed ?? []) : []).map((o) => ({ id: o.id, reason: o.reason ?? '', detail: o.detail ?? '' }))" in script
    assert script.index("withheld:") < script.index("owed: ((_manifest")


def test_every_digest_var_read_is_declared(monkeypatch):
    script = _script(monkeypatch)

    for name in (*RUNTIME_VARS, PLAN_HELD_VAR, CHUNK_REASON_VAR):
        if re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", script):
            assert re.search(rf"\b(const|let) {re.escape(name)}\b", script), name


def test_unstarted_dependents_halt_before_the_review(monkeypatch):
    script = _script(monkeypatch)
    halt = (
        "  if (!_halted) { const _r = _heldDependentsReason(); if (_r) "
        f"_halted = {{ halted: '{ask_compose.HALT_DEPENDENTS_UNSTARTED}', detail: _r }}; }}"
    )
    assert script.count(halt) == 1
    assert script.index("await Promise.all(_verifications);") < script.index(halt) < script.index("phase('Review wave')")
    assert "function _heldDependentsReason()" in script


def _eval_reason(pulled_json: str) -> str:
    import shutil
    import subprocess

    from coordinator_core.ops.dispatch_emit.ask_roadmap import no_ready_reason_js

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    expr = no_ready_reason_js("_pulled")
    done = subprocess.run(
        [node, "-e", f"const _pulled = {pulled_json}; process.stdout.write({expr});"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return done.stdout


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_ready_halt_appends_pulled_reasons_after_the_literal(monkeypatch):
    script = _script(monkeypatch)
    assert "reason: ('plan-blitz reported no ready plan' + (((_blitz?.regate ?? _blitz)?.pulled ?? [])" in script
    got = _eval_reason('[{"reason":"1 blocking, 0 advisory: a"},{"x":1},{"reason":"b"}]')
    assert got == "plan-blitz reported no ready plan: 1 blocking, 0 advisory: a; b"


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_no_ready_halt_keeps_the_bare_literal_without_pulled_reasons():
    assert _eval_reason("[]") == "plan-blitz reported no ready plan"
    assert _eval_reason('[{"planPath":"p"},null,{"reason":""}]') == "plan-blitz reported no ready plan"


def test_script_stays_under_byte_cap(monkeypatch):
    assert len(_script(monkeypatch).encode("utf-8")) <= emit._WORKFLOW_SCRIPT_BYTE_CAP
