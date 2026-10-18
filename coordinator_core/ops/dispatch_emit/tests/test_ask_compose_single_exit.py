"""The composed M+ ask script has exactly one `return` outside function bodies: the terminal one."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import PurePath

import pytest

from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import _REVIEW_SLICES_GUARD, compose_ask_script
from coordinator_core.ops.review_mint.execute_review import NO_SLICES_HALT, PREP_BUSY_HALT, PREP_MALFORMED_HALT, REVIEW_MISSING_HALT
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.session.record_homes import record_path
from coordinator_core.win_portability import no_console_creationflags

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"


def _m_plus_script(monkeypatch):
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "m_plus")
    return compose_ask_script(
        repo_root="REPO",
        prompt=None,
        sizing_rel=PurePath(record_path("", "sizings", "x.yaml")).as_posix(),
        run_id="run-1",
        session_id=None,
        wrap_stage=lambda _t: (_BLITZ_FN, ["Size", "Plan"]),
        plan_blitz_text="stub",
        **REVIEW_KW,
    )


def _top_level_returns(script: str) -> list[int]:
    """Offsets of `return` keywords outside any `function` body or arrow-function body."""
    positions: list[int] = []
    n = len(script)
    i = 0
    depth = 0
    skip_until_depth: list[int] = []  # brace depths at which a function body opened
    pending_body = False
    while i < n:
        c = script[i]
        if c in "'\"`":
            q = c
            i += 1
            while i < n and script[i] != q:
                i += 2 if script[i] == "\\" else 1
            i += 1
            continue
        if script.startswith("//", i):
            i = script.find("\n", i)
            i = n if i < 0 else i
            continue
        if script.startswith("/*", i):
            j = script.find("*/", i)
            i = n if j < 0 else j + 2
            continue
        if c == "/" and script[:i].rstrip()[-1:] in "(!,=:{[;&|?":
            i += 1
            in_class = False
            while i < n and (in_class or script[i] != "/"):
                if script[i] == "\\":
                    i += 1
                elif script[i] == "[":
                    in_class = True
                elif script[i] == "]":
                    in_class = False
                i += 1
            i += 1
            continue
        if c == "{":
            depth += 1
            if pending_body:
                skip_until_depth.append(depth)
                pending_body = False
        elif c == "}":
            if skip_until_depth and skip_until_depth[-1] == depth:
                skip_until_depth.pop()
            depth -= 1
        elif script.startswith("=>", i):
            j = i + 2
            while script[j].isspace():
                j += 1
            pending_body = script[j] == "{"
            i += 2
            continue
        elif script.startswith("get ", i) and script[i - 1] in " ,{" and script[i + 4 :].lstrip()[:1].isalpha():
            j = script.index("{", script.index(")", i))
            pending_body = True
            i = j
            continue
        elif script.startswith("function", i) and not (script[i - 1].isalnum() or script[i - 1] == "_"):
            j = script.index("{", script.index(")", i))
            pending_body = True
            i = j
            continue
        elif (
            not skip_until_depth
            and script.startswith("return", i)
            and not (script[i - 1].isalnum() or script[i - 1] == "_")
            and not (script[i + 6].isalnum() or script[i + 6] == "_")
        ):
            positions.append(i)
        i += 1
    return positions


def test_m_plus_script_has_one_return_and_it_is_the_terminal_one(monkeypatch):
    script = _m_plus_script(monkeypatch)
    found = _top_level_returns(script)
    assert len(found) == 1, [script[p : p + 40] for p in found]
    assert script[found[0] :].startswith("return { arm:")


def test_m_plus_prep_is_one_review_prep_call_and_the_no_op_exit_is_still_rewritten(monkeypatch):
    script = _m_plus_script(monkeypatch)
    assert "review.prep" in script and "review.partition_slices" not in script
    assert "freeze-review-diff" not in script
    assert "return { halted: 'no-op'," not in script
    assert "_halted = { halted: 'no-op'," in script


def test_prep_busy_and_review_missing_exits_assign_halted_and_skip_the_prep_bind(monkeypatch):
    script = _m_plus_script(monkeypatch)
    assert "return { halted:" not in script
    for halt in (PREP_BUSY_HALT, PREP_MALFORMED_HALT, REVIEW_MISSING_HALT):
        assert f"_halted = {{ halted: '{halt}'" in script
    assert "if (!_halted) _reviewPrep = (() => {" in script
    assert "if (!_halted && _waveRaw.some(" in script


def test_drifted_prep_busy_exit_refuses_composition():
    blocks = [
        "return { halted: 'no-op', wave: null, integration: null }; }\n" + _REVIEW_SLICES_GUARD
    ]
    with pytest.raises(ask_compose.AskComposeRefused):
        ask_compose._single_exit_review(blocks)


def test_m_plus_script_validates_with_zero_errors(monkeypatch):
    findings = run_checks(_m_plus_script(monkeypatch))
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def _merge_and_guard_fragment(script: str) -> str:
    start = script.index("  if (!_halted) _reviewPrep = (() => {")
    end_marker = f"halted: '{NO_SLICES_HALT}'"
    end = script.index("\n", script.index(end_marker, start))
    return script[start:end]


def test_ask_no_slices_guard_is_array_guarded_and_follows_the_slice_id_rebuild(monkeypatch):
    fragment = _merge_and_guard_fragment(_m_plus_script(monkeypatch))
    rebuild = fragment.index("id.split('+')")
    guard = fragment.index("if (!_halted && (!_reviewPrep ||")
    assert rebuild < guard
    guard_line = fragment[guard:]
    assert "Array.isArray(_reviewPrep.slices)" in guard_line
    assert "(_reviewPrep.slices ?? [])" not in guard_line


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(shutil.which("node") is None, reason="node unavailable")
@pytest.mark.parametrize(
    ("slice_id", "halts"),
    [("...b90a92-9c1affcf4e0d-prep-1+...-prep-2", False), ("", True)],
)
def test_ask_wave_review_naming_slices_prevents_the_no_slices_halt(monkeypatch, slice_id, halts):
    fragment = _merge_and_guard_fragment(_m_plus_script(monkeypatch))
    harness = (
        "let _halted = null;\n"
        "let _reviewPrep = null;\n"
        "const _prepReplies = [{ verdict: 'partition', slices: [], product_files: 3 }];\n"
        f"const _waveRaw = [{{ prep: _prepReplies[0], review: {{ slice_id: {json.dumps(slice_id)}, sidecar_path: 'x.md' }} }}];\n"
        "const _busyPrefix = 'review.prep busy:';\n"
        "const _busyReplies = [];\n"
        "const _malformed = [];\n"
        f"{fragment}\n"
        "process.stdout.write(JSON.stringify({ halted: _halted && _halted.halted, n: _reviewPrep.slices.length }));\n"
    )
    out = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, timeout=20, check=True, **no_console_creationflags()
    )
    result = json.loads(out.stdout)
    assert bool(result["halted"] and result["halted"].startswith(NO_SLICES_HALT)) is halts
    if not halts:
        assert result["n"] == 2
