"""ask_staged_prompts: static prompt bodies leave the composed ask script as pointers to record-dir files."""

from __future__ import annotations

from pathlib import Path

import re

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose, ask_staged_prompts, cli, prompt_fragments
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import MIN_STAGE_BYTES, stage, stage_literal_runs
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, unshare_agent_head
from coordinator_core.ops.workflow_scaffold import _js_string_literal

_RUN = "run-1"
_BIG = "A large static sentence the agent must follow. " * 20
_STAGED_NAMES = (
    "verify", "scout", "reword", "accept", "xl-exit", "review-reverify", "halt-fix", "halt-reverify",
    "ledger-fix", "ledger-reverify", "ledger-remaining",
)


def _stager(sink):
    return lambda name, text: stage(name, text, sink, record_dir="rec")


def _compose(**over) -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id=_RUN, session_id=None,
        wrap_stage=lambda text: ("  async function planBlitz(args) {\n    return { ready: [] };\n  }", ["Size", "Plan"]),
        plan_blitz_text="stub", **REVIEW_KW, **over,
    )


def test_stage_records_the_body_and_returns_the_pointer_literal() -> None:
    sink: dict[str, str] = {}
    pointer = stage("verify", "BODY", sink, record_dir="state/warp/run-1")
    assert sink == {"verify": "BODY"}
    assert pointer == _js_string_literal(
        "Read state/warp/run-1/prompts/verify.md in full and follow it as the rest of this brief.\n"
    )


def test_stage_refuses_a_name_recorded_with_other_text_and_accepts_the_same_text() -> None:
    sink: dict[str, str] = {}
    stage("x", "one", sink, record_dir="r")
    stage("x", "one", sink, record_dir="r")
    with pytest.raises(ValueError):
        stage("x", "two", sink, record_dir="r")


def test_a_large_run_is_staged_and_a_small_one_stays() -> None:
    sink: dict[str, str] = {}
    js = f"({_js_string_literal(_BIG)} + dyn + 'small') + {_js_string_literal(_BIG + 'tail')}"
    out = stage_literal_runs(js, "p", _stager(sink))
    assert sink == {"p": _BIG, "p-2": _BIG + "tail"}
    assert "dyn + 'small'" in out and _BIG[:30] not in out


def test_adjacent_literals_join_into_one_staged_run_with_escapes_decoded() -> None:
    half = "it's a line\n" * 30
    sink: dict[str, str] = {}
    js = _js_string_literal(half) + " + " + _js_string_literal(half)
    out = stage_literal_runs(js, "p", _stager(sink))
    assert sink == {"p": half + half}
    assert out.count("prompts/p.md") == 1


def test_double_quoted_literals_and_runs_below_the_floor_are_never_staged() -> None:
    sink: dict[str, str] = {}
    schema_like = '"' + "d" * (MIN_STAGE_BYTES + 10) + '"'
    short = _js_string_literal("s" * (MIN_STAGE_BYTES - 1))
    js = f"{schema_like} + {short}"
    assert stage_literal_runs(js, "p", _stager(sink)) == js
    assert sink == {}


def test_a_run_with_an_undecoded_escape_stays_inline() -> None:
    sink: dict[str, str] = {}
    js = "'" + "x" * MIN_STAGE_BYTES + "\\u00e9'"
    assert stage_literal_runs(js, "p", _stager(sink)) == js
    assert sink == {}


def test_a_composed_bare_ask_names_every_staged_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    sink: dict[str, str] = {}
    script = _compose(staged_out=sink)
    for name in _STAGED_NAMES:
        assert name in sink, name
        assert f"{RECORD_DIR_ROOT}/{_RUN}/prompts/{name}.md in full" in script, name
    assert len(_compose().encode()) > len(script.encode())


def test_the_pointer_and_the_ask_text_path_are_anchored_to_the_repo_root() -> None:
    script = _compose(staged_out={})
    assert f"REPO/{RECORD_DIR_ROOT}/{_RUN}/pm-intent.md" in script
    assert f"Read REPO/{RECORD_DIR_ROOT}/{_RUN}/prompts/verify.md" in script
    assert f"Read {RECORD_DIR_ROOT}/{_RUN}/prompts/" not in script


def test_without_staged_out_every_body_stays_inline() -> None:
    script = _compose()
    assert "/prompts/" not in script
    assert "You are the run's one closing agent" in script.replace("\\'", "'")


def _unshare_precedence(script: str) -> str:
    """Inline the hoisted precedence-tail const back into each use; staging runs before the share, so only the inline script carries it."""
    fn = ask_compose._PRECEDENCE_TAIL_FN
    script = unshare_agent_head(script)
    m = re.search(rf"const {fn} = `([^`]*)`;\n", script)
    if not m:
        return script
    tail = m.group(1)
    body = script[: m.start()] + script[m.end() :]
    body = body.replace(f"' + {fn} + '", tail)
    return re.sub(rf"' \+ {fn}(?![A-Za-z0-9_])", lambda _m: tail + "'", body)


def test_pointer_replaced_text_equals_the_inline_script(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", False)
    # Binding rewrites staged text by design; this test pins only that staging moves text verbatim.
    monkeypatch.setattr(ask_staged_prompts, "bind_engine_commands", lambda text: text)
    sink: dict[str, str] = {}
    staged = _compose(staged_out=sink)
    inline = _compose()
    for name, text in sink.items():
        pointer = _js_string_literal(
            f"Read REPO/{RECORD_DIR_ROOT}/{_RUN}/prompts/{name}.md in full and follow it as the rest of this brief.\n"
        )
        assert pointer in staged
        staged = staged.replace(pointer, _js_string_literal(text))
    assert prompt_fragments.fold_literals(_unshare_precedence(staged)) == prompt_fragments.fold_literals(_unshare_precedence(inline))


def test_emit_writes_each_staged_body_beside_the_ask_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: "export const meta = { phases: [] };\n")
    assert cli.main(["--ask", "add a thing", "--ask-source", "pm", "--repo-root", str(tmp_path)]) == 0
    (ask_md,) = tmp_path.rglob("ask.md")
    prompts = ask_md.parent / "prompts"
    written = {p.name for p in prompts.glob("*.md")}
    assert {f"{n}.md" for n in _STAGED_NAMES} <= written
    verify = (prompts / "verify.md").read_text(encoding="utf-8")
    assert "\r" not in verify and "You are the run's one closing agent" in verify


def test_decode_is_memoised_and_still_refuses_an_unknown_escape() -> None:
    ask_staged_prompts._decode.cache_clear()
    assert ask_staged_prompts._decode("a\\nb") == "a\nb"
    assert ask_staged_prompts._decode("a\\nb") == "a\nb"
    assert ask_staged_prompts._decode.cache_info().hits == 1
    assert ask_staged_prompts._decode("a\\xb") is None
    assert ask_staged_prompts._decode("a\\xb") is None


def _pointer(name: str) -> str:
    return stage(name, "", {}, record_dir="rec")


def test_a_static_template_literal_is_staged_and_spliced_back_as_a_pointer() -> None:
    sink: dict[str, str] = {}
    out = stage_literal_runs("`" + _BIG + "`", "p", _stager(sink))
    assert sink == {"p": _BIG}
    assert out == "`${" + _pointer("p") + "}`"


def test_a_template_splits_at_its_substitutions_and_each_large_static_run_is_staged() -> None:
    small = "tiny "
    sink: dict[str, str] = {}
    js = "x = `" + _BIG + "${dyn}" + small + "${other}" + _BIG + "B`"
    out = stage_literal_runs(js, "p", _stager(sink))
    assert sink == {"p": _BIG, "p-2": _BIG + "B"}
    assert "${dyn}" + small + "${other}" in out and _BIG[:30] not in out


def test_template_escapes_are_decoded_and_the_staged_text_is_the_runtime_text() -> None:
    raw = ("a \\`tick\\` and \\${not} and back\\\\slash\\nnext line. " * 20)
    expected = "a `tick` and ${not} and back\\slash\nnext line. " * 20
    sink: dict[str, str] = {}
    stage_literal_runs("`" + raw + "`", "p", _stager(sink))
    assert sink == {"p": expected}


def test_a_substitution_holding_quotes_or_a_nested_template_does_not_end_the_run() -> None:
    sink: dict[str, str] = {}
    expr = "${f('}`' + `n${g({a: 1})}` + \"{\")}"
    js = "`" + _BIG + expr + _BIG + "`"
    out = stage_literal_runs(js, "p", _stager(sink))
    assert sink == {"p": _BIG, "p-2": _BIG}
    assert expr in out


def test_a_template_run_with_an_undecoded_escape_stays_inline() -> None:
    sink: dict[str, str] = {}
    for bad in ("\\u00e9", "\\x41", "\\\n", "\\r"):
        js = "`" + "x" * MIN_STAGE_BYTES + bad + "`"
        assert stage_literal_runs(js, "p", _stager(sink)) == js
    assert sink == {}


def test_a_tagged_template_stays_inline_and_an_untagged_one_after_a_keyword_is_staged() -> None:
    for tag in ("String.raw", "tag", "f(1)", "a[0]"):
        sink: dict[str, str] = {}
        js = tag + "`" + _BIG + "`"
        assert stage_literal_runs(js, "p", _stager(sink)) == js
        assert sink == {}
    for pre in ("return ", "x = ", "f(", "typeof "):
        sink = {}
        stage_literal_runs(pre + "`" + _BIG + "`", "p", _stager(sink))
        assert sink == {"p": _BIG}


def test_a_template_run_below_the_floor_stays_inline() -> None:
    sink: dict[str, str] = {}
    js = "`" + "s" * (MIN_STAGE_BYTES - 1) + "${a}`"
    assert stage_literal_runs(js, "p", _stager(sink)) == js
    assert sink == {}


def test_an_unbalanced_code_fence_line_beside_a_substitution_stays_inline() -> None:
    fence = "\\`\\`\\`"
    sink: dict[str, str] = {}
    out = stage_literal_runs("`${a}" + fence + " close\n" + _BIG + "`", "p", _stager(sink))
    assert sink == {"p": _BIG}
    assert out == "`${a}" + fence + " close\n${" + _pointer("p") + "}`"
    sink = {}
    out = stage_literal_runs("`" + _BIG + "\nopen " + fence + "py${a}`", "p", _stager(sink))
    assert sink == {"p": _BIG + "\n"}
    assert out == "`${" + _pointer("p") + "}open " + fence + "py${a}`"
