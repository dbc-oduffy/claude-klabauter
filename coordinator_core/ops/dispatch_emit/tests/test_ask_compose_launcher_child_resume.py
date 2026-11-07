"""A launcher child's render resumes past stage when its staged twin's inline slot carries a resume block.

Render-level only: the emitted bytes are inspected, no node run. The node-level falsifier is the incident test."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import compose_ask_script
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_stage_handle import _BLITZ_FN, _SIZING

pytestmark = pytest.mark.complete

_GUARD_OPEN = "  if (!_STAGED_RESUME) {\n  phase('size');"


def _script(monkeypatch, *, launcher_child: bool, raw: bool, arm: str = "s") -> str:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: arm)
    return compose_ask_script(
        repo_root="REPO", prompt="do the thing" if raw else None, sizing_rel=None if raw else _SIZING,
        run_id="run-1", session_id=None, wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub", script_path="scratch/warp/x.mjs", in_run_commit=True,
        launcher_child=launcher_child, **REVIEW_KW,
    )


def _label_index(script: str, label: str) -> list[int]:
    return [m.start() for m in re.finditer(r"label: '" + re.escape(label) + r"'", script)]


@pytest.mark.parametrize("raw", [True, False])
def test_a_standalone_emit_holds_no_resume_guard(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=False, raw=raw)
    assert "_STAGED_RESUME" not in script


@pytest.mark.parametrize("raw", [True, False])
def test_a_launcher_child_emit_declares_the_resume_once_from_the_inline_slot(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=True, raw=raw)
    assert script.count("const _STAGED_RESUME = ") == 1
    decl = script.index("const _STAGED_RESUME = ")
    assert script.index("const _STAGED_INLINE = ") < decl
    assert "s.resume" in script[decl:decl + 400]
    assert "typeof r.arm === 'string'" in script[decl:decl + 400]
    assert "_STAGED_RESUME" not in script[: script.index("const _STAGED_INLINE = ")]


def test_the_raw_ask_size_block_and_every_size_leg_sit_inside_the_resume_guard(monkeypatch):
    script = _script(monkeypatch, launcher_child=True, raw=True)
    assert script.count(_GUARD_OPEN) == 1
    start = script.index(_GUARD_OPEN)
    end = script.index("  phase('gate');")
    assert start < end
    for label in ("size", "size-retry", "size-reword"):
        spots = _label_index(script, label)
        assert spots, label
        assert all(start < i < end for i in spots), label
    block = script[start:end]
    assert block.rstrip().endswith("}")
    assert not re.search(r"const (_scouted|_scoutClean|_pmExit)\b", script)
    assert "let _scouted = null;" in script[:start]


def test_the_gate_is_seeded_from_the_resume_block_and_its_leg_never_runs_on_resume(monkeypatch):
    raw = _script(monkeypatch, launcher_child=True, raw=True)
    assert "let _gate = _STAGED_RESUME ? { arm: _STAGED_RESUME.arm, halt: null," in raw
    sizing = _script(monkeypatch, launcher_child=True, raw=False)
    assert "let _gate = _STAGED_RESUME ? { arm: _STAGED_RESUME.arm, halt: null," in sizing
    (gate_leg,) = _label_index(sizing, "gate")
    seed = sizing.index("let _gate = _STAGED_RESUME ?")
    assert seed < gate_leg < sizing.index("\n", seed)


@pytest.mark.parametrize("raw", [True, False])
def test_every_later_pre_stage_leg_is_gated_on_the_resume(monkeypatch, raw):
    script = _script(monkeypatch, launcher_child=True, raw=raw, arm="m_plus")
    assert "if (!_halted && !_STAGED_RESUME && _gate.arm === 's') {" in script
    assert "if (!_halted && !_STAGED_RESUME && _gate.arm === 'm_plus') {" in script
    if raw:
        assert "_round < 3 && !_STAGED_RESUME && _gate.halt" in script
    for label in ("plan", "plan-review", "plan-revise", "xl-exit"):
        for i in _label_index(script, label):
            before = script[:i]
            guard = max(before.rfind("!_STAGED_RESUME &&"), before.rfind("  if (!_STAGED_RESUME) {"))
            assert guard != -1, label


def test_the_resume_seeds_the_manifest_through_the_existing_adopt_so_a_failed_verify_halts_by_name(monkeypatch):
    script = _script(monkeypatch, launcher_child=True, raw=True)
    seed_call = "await _adoptStageReply({ manifest_handle: _STAGED_INLINE.full_handle ?? _STAGED_INLINE.handle });"
    assert seed_call in script
    seed = script.index(seed_call)
    assert seed < script.index(_GUARD_OPEN)
    assert "stage reply sidecar does not match its handle: " in script
    for field in ("_sizingRel = _STAGED_RESUME.sizing_rel", "_planRel = _STAGED_RESUME.plan_rel"):
        assert field in script


def test_the_guard_keys_on_the_resume_block_never_on_the_arm(monkeypatch):
    for arm in ("xs", "s", "m_plus"):
        script = _script(monkeypatch, launcher_child=True, raw=True, arm=arm)
        assert script.count(_GUARD_OPEN) == 1
        assert not re.search(r"_STAGED_RESUME\.arm\s*===\s*'(?:xs|s|m_plus)'\s*\)\s*\{\s*phase\('size'", script)


def test_only_a_launcher_child_holds_the_slim_manifest_halt(monkeypatch):
    assert "staged twin is slim" in _script(monkeypatch, launcher_child=True, raw=True)
    assert "staged twin is slim" not in _script(monkeypatch, launcher_child=False, raw=True)


def _adopt_with_node(inline: dict, h: dict) -> dict:
    import json
    import subprocess
    import shutil

    from coordinator_core.ops.dispatch_emit import reply_sidecar

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    prog = (
        reply_sidecar.VERIFY_REPLY_JS
        + f"const _STAGED_INLINE = {json.dumps(inline)};\n"
        + ask_compose._ADOPT_STAGED_INLINE_JS
        + f"console.log(JSON.stringify(_adoptStagedInline({json.dumps(h)})));\n"
    )
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, timeout=60, check=True,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return json.loads(out.stdout)


def _slim_inline():
    import hashlib
    import json

    text = json.dumps({"rows": [{"id": "C1", "writes": [], "writes_count": 400}]}, sort_keys=True, separators=(",", ":"))
    full = {"path": "state/warp/r/manifest.json", "sha256": "f" * 64, "bytes": 99999}
    own = {"path": full["path"], "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text)}
    return {"handle": own, "full_handle": full, "spill": "scratch/warp/r.staged-" + "f" * 64 + ".spill.json", "text": text}, full


def test_adopt_accepts_the_slim_text_against_the_full_handle_and_marks_the_spill():
    inline, full = _slim_inline()
    got = _adopt_with_node(inline, full)
    assert got["manifest"]["spill"] == inline["spill"]
    assert got["manifest"]["full_handle"] == full
    assert got["manifest"]["rows"][0]["writes_count"] == 400


def test_adopt_refuses_a_slim_text_whose_bytes_do_not_match_its_own_handle():
    inline, full = _slim_inline()
    inline["text"] = inline["text"].replace("C1", "C2")
    assert "fail" in _adopt_with_node(inline, full)


def test_adopt_returns_null_when_the_handle_is_not_the_full_handle():
    inline, full = _slim_inline()
    assert _adopt_with_node(inline, inline["handle"]) is None


def test_adopt_of_a_whole_inline_carries_no_spill_mark():
    import hashlib
    import json

    text = json.dumps({"rows": [{"id": "C1", "writes": ["a.py"]}]}, sort_keys=True, separators=(",", ":"))
    h = {"path": "state/warp/r/manifest.json", "sha256": hashlib.sha256(text.encode()).hexdigest(), "bytes": len(text)}
    got = _adopt_with_node({"handle": h, "text": text}, h)
    assert "spill" not in got["manifest"]


def _run_chain_loop(pending: dict) -> dict:
    """Run the chain resume loop in node against a stubbed workflow(); returns {chained, why, calls}."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    code = (
        "const REPO_ROOT = '/r';\n"
        "const calls = [];\n"
        f"const PENDING = {json.dumps({'stage_pending': pending})};\n"
        "const workflow = async (o) => { calls.push(o.scriptPath); return { done: true }; };\n"
        "(async () => {\n  let _why = null; let _chained = PENDING; const _chainPath = '/r/scratch/warp/ask-r1.workflow.mjs';\n"
        + ask_compose._chain_stage_loop_js()
        + "\n  console.log(JSON.stringify({ chained: _chained, why: _why, calls }));\n})();\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_restaged_twin_named_by_the_reply_is_started_not_the_stale_handle_twin():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    got = _run_chain_loop({"handle": {"sha256": "3c" * 32}, "twin_path": staged_twin_rel("ask-r1", "2d" * 32)})
    assert got["why"] is None and got["calls"] == ["/r/" + staged_twin_rel("ask-r1", "2d" * 32)]


def test_a_reply_that_names_no_twin_still_resumes_from_the_handle_sha():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    got = _run_chain_loop({"handle": {"sha256": "ab" * 32}})
    assert got["why"] is None and got["calls"] == ["/r/" + staged_twin_rel("ask-r1", "ab" * 32)]


def _adopt_launcher_reply(reply: dict, recovered: dict | None) -> dict:
    """Run the launcher-child stage-reply adopter in node on `reply`; `recovered` is the stage-recover return (None: none wired)."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node unavailable")
    code = (
        "(async () => {\n"
        "let _stagedManifest = null, _stageRefusal = null, _relayGap = null, _parked = null, _diskHalt = null;\n"
        "let _stageRecoverCalls = 0;\n"
        "const _adoptStagedInline = () => null; const _wholeManifestGap = () => null;\n"
        + ask_compose._ADOPT_STAGE_REPLY_HEAD_JS
        + (
            f"  _stageRecover = async () => {{ _stageRecoverCalls++; return {{ stage_reply: {json.dumps(recovered)} }}; }};\n"
            if recovered is not None else ""
        )
        + ask_compose._ADOPT_STAGE_REPLY_LAUNCHER_JS
        + ask_compose._ADOPT_STAGE_REPLY_TAIL_JS
        + f"\n  await _adoptStageReply({json.dumps(reply)});\n"
        "  console.log(JSON.stringify({ pending: _stagePending, calls: _stageRecoverCalls }));\n"
        "})();\n"
    )
    out = subprocess.run([node, "-e", code], capture_output=True, text=True, timeout=30,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


_H_OLD = {"path": "state/warp/r/manifest.json", "sha256": "6f" * 32, "bytes": 10}
_H_NEW = {"path": "state/warp/r/manifest.json", "sha256": "89" * 32, "bytes": 11}


def test_a_replayed_reply_without_a_twin_path_takes_the_twin_from_a_fresh_stage_op_reply():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    twin = staged_twin_rel("ask-r1", "89" * 32)
    got = _adopt_launcher_reply({"manifest_handle": _H_OLD}, {"manifest_handle": _H_NEW, "twin_path": twin})
    assert got["pending"] == {"handle": _H_NEW, "twin_path": twin} and got["calls"] == 1


def test_a_reply_that_names_its_twin_path_is_still_replaced_by_the_fresh_stage_op_twin():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    old = staged_twin_rel("ask-r1", "6f" * 32)
    new = staged_twin_rel("ask-r1", "89" * 32)
    got = _adopt_launcher_reply({"manifest_handle": _H_OLD, "twin_path": old}, {"manifest_handle": _H_NEW, "twin_path": new})
    assert got["pending"] == {"handle": _H_NEW, "twin_path": new} and got["calls"] == 1


def test_a_recover_reply_that_names_no_twin_leaves_the_replayed_handle():
    got = _adopt_launcher_reply({"manifest_handle": _H_OLD}, {"manifest_handle": _H_NEW})
    assert got["pending"] == {"handle": _H_OLD} and got["calls"] == 1


def test_a_named_twin_of_another_run_is_refused_and_nothing_starts():
    from coordinator_core.ops.dispatch_emit.nested_run_contract import staged_twin_rel

    got = _run_chain_loop({"handle": {"sha256": "2d" * 32}, "twin_path": staged_twin_rel("ask-other", "2d" * 32)})
    assert "twin_path is not a staged twin of this run" in got["why"] and got["calls"] == []
