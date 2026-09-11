"""Tests for coordinator_core.ops.workflow_bind — `workflow.bind_args`.

The op's whole value is that a bound copy is the SOURCE plus exactly one
statement. Every test below pins one half of that: the body survives verbatim,
or a shape that would fail at fire time is refused here instead.
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.workflow_bind import _workflow_bind_args, bind_args

SOURCE = """\
/* leading comment */
export const meta = {
  name: 'demo',
  description: 'a demo with a } brace in prose and an apostrophe-free line',
  phases: [
    { title: 'Run', detail: 'the only phase, mentioning { and } in text' },
  ],
}

const parsedArgs = (typeof args === 'string') ? JSON.parse(args) : args
await phase('Run')
return { ok: parsedArgs.waveIndex }
"""


def test_binds_args_after_the_meta_block_and_keeps_the_body_verbatim():
    out = bind_args(SOURCE, {"waveIndex": 0, "batons": [{"id": "b1"}]})

    meta_end = out.index("\n}\n")
    binding_at = out.index("const args = ")
    body_at = out.index("const parsedArgs")
    assert meta_end < binding_at < body_at, "binding must sit between meta and body"

    # The body is untouched — not reordered, not rewritten, not re-indented.
    assert SOURCE[SOURCE.index("const parsedArgs") :].rstrip("\n") in out

    literal = out[out.index("const args = ") + len("const args = ") :]
    literal = literal[: literal.index("\n}\n") + 2]
    assert json.loads(literal) == {"waveIndex": 0, "batons": [{"id": "b1"}]}


def test_meta_block_closes_on_column_zero_brace_not_on_a_brace_in_prose():
    # The meta literal above carries `}` inside two description strings. A
    # brace-counting parser closes the block early and binds args INSIDE meta,
    # which the contract checker then reads as a computed meta.
    out = bind_args(SOURCE, {"k": 1})
    head = out[: out.index("const args = ")]
    assert head.count("phases:") == 1
    assert "title: 'Run'" in head


def test_refuses_a_source_that_is_not_a_fleet_workflow_script():
    with pytest.raises(ValueError, match="does not open a"):
        bind_args("const x = 1\n", {"k": 1})


def test_refuses_a_meta_block_that_never_closes():
    with pytest.raises(ValueError, match="never closed"):
        bind_args("export const meta = {\n  name: 'x',\n", {"k": 1})


def test_refuses_a_source_that_already_declares_args_at_top_level():
    src = SOURCE.replace("const parsedArgs", "const args = {}\nconst parsedArgs", 1)
    with pytest.raises(ValueError, match="already declares `args`"):
        bind_args(src, {"k": 1})


def test_an_args_parameter_inside_a_function_is_not_a_top_level_declaration():
    src = SOURCE.replace(
        "await phase('Run')",
        "function f() {\n  const args = 1\n  return args\n}\nawait phase('Run')",
        1,
    )
    # Indented — shadows nothing this op writes, so it must NOT be refused.
    assert "const args = {" in bind_args(src, {"k": 1})


def test_refuses_args_that_are_not_an_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        bind_args(SOURCE, ["not", "an", "object"])


def test_refuses_args_that_do_not_round_trip_through_json():
    with pytest.raises(ValueError, match="not JSON-serializable"):
        bind_args(SOURCE, {"k": object()})


def test_op_handler_refuses_a_missing_source_and_names_the_path(tmp_path):
    with pytest.raises(ValueError, match="no script at"):
        _workflow_bind_args({"script_path": str(tmp_path / "nope.mjs"), "args": {}})


def test_op_handler_returns_the_text_the_source_path_and_the_bound_keys(tmp_path):
    src = tmp_path / "demo.mjs"
    src.write_text(SOURCE, encoding="utf-8")

    reply = _workflow_bind_args(
        {"script_path": str(src), "args": {"b": 2, "a": 1}}
    )

    assert reply["source_path"] == str(src)
    assert reply["bound_keys"] == ["a", "b"]
    assert reply["script"].startswith("/* leading comment */")
    assert "const args = " in reply["script"]


def test_op_handler_requires_both_params(tmp_path):
    with pytest.raises(ValueError, match="requires param: script_path"):
        _workflow_bind_args({"args": {}})
    with pytest.raises(ValueError, match="requires param: args"):
        _workflow_bind_args({"script_path": str(tmp_path / "x.mjs")})
