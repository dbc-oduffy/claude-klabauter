"""`_share_body_blocks` names each repeated body block once in the unhoisted shape and leaves every run-time string as it was."""

from __future__ import annotations

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_compose import ASK_MANIFEST_MARKER, _share_body_blocks
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import _decode, _literal_runs
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_FLOOR = ask_compose._BODY_BLOCK_MIN_BYTES
_BLOCK = "This shared sentence is spelled out in full at every use in the script.\n" * (_FLOOR // 60 + 1)
_OTHER = "A second shared sentence that is also spelled out in full at each of its uses. " * (_FLOOR // 60 + 1)
_DECL = re.compile(r"^const (_BB\d+) = `((?:[^`\\]|\\.)*)`;\n", re.M | re.S)


def _lit(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _script(*lines: str) -> str:
    return "const a = 1;\n" + ASK_MANIFEST_MARKER + "m.json\n" + "\n".join(lines) + "\n"


def _resolved(script: str) -> list[str]:
    """The text each literal run of ``script`` denotes, with every `_BBn` name spelled out and its declaration dropped."""
    unescape = {"n": "\n", "t": "\t", "r": "\r"}
    decls = {
        m.group(1): re.sub(r"\\(.)", lambda g: unescape.get(g.group(1), g.group(1)), m.group(2), flags=re.S)
        for m in _DECL.finditer(script)
    }
    body = _DECL.sub("", script)
    for name, text in sorted(decls.items(), key=lambda kv: -len(kv[0])):
        body = re.sub(rf"\b{name}\b", lambda _m, t=text: _lit(t), body)
    runs = []
    for run in _literal_runs(body):
        runs.append("".join(_decode(body[a + 1 : b - 1]) or "" for a, b in run))
    return runs


@pytest.fixture(autouse=True)
def _unhoisted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", False)


def test_a_block_repeated_three_times_is_declared_once() -> None:
    src = _script(*(f"const s{i} = {_lit(f'Lead {i}. ' + _BLOCK + f'Tail {i}.')};" for i in range(3)))
    out = _share_body_blocks(src)
    assert len(_DECL.findall(out)) == 1
    assert out.count(_BLOCK.splitlines()[0]) == _BLOCK.count("\n")
    assert out.count("_BB0") == 4
    assert out.index("const _BB0 = ") < out.index(ASK_MANIFEST_MARKER)
    assert len(out) < len(src)
    assert _resolved(out) == _resolved(src)


def test_a_block_used_once_and_a_block_under_the_floor_stay_inline() -> None:
    short = "Short repeated line.\n"
    src = _script(
        f"const a = {_lit('Only once. ' + _BLOCK)};",
        f"const b = {_lit('x ' + short + 'x')};",
        f"const c = {_lit('y ' + short + 'y')};",
    )
    assert _share_body_blocks(src) == src


def test_the_floor_is_the_smallest_block_that_is_named() -> None:
    def block(size: int) -> str:
        return "b" * (size - 1) + "\n"

    for size, named in ((_FLOOR - 1, False), (_FLOOR, True)):
        src = _script(*(f"const s{i} = {_lit(f'Lead {i}. ' + block(size))};" for i in range(6)))
        assert (_share_body_blocks(src) != src) is named


def test_a_run_with_an_escape_the_stager_does_not_decode_stays_inline() -> None:
    odd = "'\\u00e9 " + _BLOCK.replace("\n", "\\n") + "'"
    src = _script(f"const a = {odd};", f"const b = {odd};", f"const c = {_lit(_BLOCK)};")
    assert _share_body_blocks(src) == src


def test_the_hoisted_shape_is_left_to_the_fragment_hoist(monkeypatch: pytest.MonkeyPatch) -> None:
    src = _script(*(f"const s{i} = {_lit(f'Lead {i}. ' + _BLOCK)};" for i in range(3)))
    assert _share_body_blocks(src) != src
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    assert _share_body_blocks(src) == src


def test_a_chain_beside_a_method_call_is_parenthesised_and_an_object_key_is_kept() -> None:
    src = _script(
        f"const a = {_lit('Lead a. ' + _BLOCK)}.trim();",
        f"const b = {_lit('Lead b. ' + _BLOCK)}.trim();",
        f"const k = {{ {_lit(_BLOCK)}: 1, {_lit(_OTHER)}: 2 }};",
        f"const m = [{_lit(_OTHER)}, {_lit(_OTHER)}];",
        f"const t = typeof {_lit('Lead c. ' + _BLOCK)};",
    )
    out = _share_body_blocks(src)
    assert "(" + _lit("Lead a. ") + " + _BB" in out
    assert f"{{ {_lit(_BLOCK)}: 1" in out
    assert re.search(r"typeof \('Lead c\. ' \+ _BB\d+\);", out)
    assert _resolved(out) == _resolved(src)


def test_adjacent_blocks_repeated_together_cost_one_name() -> None:
    para = _BLOCK + _OTHER
    src = _script(*(f"const s{i} = {_lit(f'Lead {i}. ' + para + f'Tail {i}.')};" for i in range(4)))
    out = _share_body_blocks(src)
    assert len(_DECL.findall(out)) == 1
    assert _resolved(out) == _resolved(src)


def _compose(monkeypatch: pytest.MonkeyPatch, *, share: bool) -> str:
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    if not share:
        monkeypatch.setattr(ask_compose, "_share_body_blocks", lambda script: script)
    return ask_compose.compose_ask_script(
        repo_root="REPO",
        prompt="ship the exporter and, separately, the dashboard",
        sizing_rel=None,
        run_id="run-split-1",
        session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub",
        in_run_commit=True,
        **REVIEW_KW,
    )


def test_every_run_time_string_of_a_composed_ask_script_is_unchanged_and_the_script_shrinks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = _compose(monkeypatch, share=True)
    plain = _compose(monkeypatch, share=False)
    assert len(_DECL.findall(shared)) > 10
    assert len(shared.encode("utf-8")) < len(plain.encode("utf-8")) - 50_000
    assert [r for r in _resolved(shared) if r] == [r for r in _resolved(plain) if r]
