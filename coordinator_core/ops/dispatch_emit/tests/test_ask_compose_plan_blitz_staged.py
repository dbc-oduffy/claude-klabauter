"""The embedded plan-blitz stage's static prose is staged, so growth in the asset stays out of the composed script.

Measured on the production m_plus shape (known sizing, `staged_out` sink, hoisting on, installed asset). The pad is a
template literal, the asset's dominant prose form: a single-quoted pad would stage by construction and measure nothing.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import load_plan_blitz_text
from coordinator_core.ops.dispatch_emit.compose_size import ASK_SCRIPT_CEILING_BYTES
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_script_size_budget import _SIZING_REL, _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_m_plus_production_shapes_under_cap import (  # noqa: F401
    _production_inputs,
    staged_m_plus,
)

_PAD_BYTES = 65536
_PAD_BODY = "x" * (_PAD_BYTES - 24)
_PADDED_DELTA_LIMIT = 8192


def _compose(root: Path, text: str, staged_out: dict | None) -> str:
    _write_sizing(root)
    return ask_compose.compose_ask_script(
        repo_root=str(root), prompt=None, sizing_rel=_SIZING_REL, run_id="run-1", session_id=None,
        script_path="scratch/warp/x.mjs", plan_blitz_text=text, staged_out=staged_out,
    )


def _with_template_pad(text: str) -> str:
    return text + "\nconst __pad_more = `" + _PAD_BODY + "`;\n"


def test_the_production_m_plus_parent_is_under_the_shared_ceiling(tmp_path, _production_inputs):
    script, sink = staged_m_plus(tmp_path)
    assert any(name.startswith("plan-blitz") for name in sink), "the plan-blitz body is staged into the sink"
    assert len(script.encode("utf-8")) <= ASK_SCRIPT_CEILING_BYTES


def test_a_template_literal_pad_of_the_asset_barely_grows_the_script(tmp_path, _production_inputs):
    base_root, pad_root = tmp_path / "base", tmp_path / "pad"
    base_root.mkdir()
    pad_root.mkdir()
    installed = load_plan_blitz_text()
    base = len(_compose(base_root, installed, {}).encode("utf-8"))
    pad_sink: dict = {}
    grown = len(_compose(pad_root, _with_template_pad(installed), pad_sink).encode("utf-8"))
    assert grown - base < _PADDED_DELTA_LIMIT, f"a {_PAD_BYTES}-byte pad grew the script by {grown - base} bytes"
    assert grown <= ASK_SCRIPT_CEILING_BYTES
    assert _PAD_BODY in pad_sink.values(), "the pad's prose reached the sink, not the script"


def test_the_staged_pad_is_the_exact_text_the_unstaged_script_inlines(tmp_path, _production_inputs):
    padded = _with_template_pad(load_plan_blitz_text())
    sink: dict = {}
    (tmp_path / "s").mkdir()
    (tmp_path / "u").mkdir()
    staged = _compose(tmp_path / "s", padded, sink)
    unstaged = _compose(tmp_path / "u", padded, None)
    assert _PAD_BODY in unstaged and _PAD_BODY not in staged
    assert [n for n, t in sink.items() if t == _PAD_BODY and n.startswith("plan-blitz")]


def test_unstaged_composition_keeps_the_plan_blitz_prose_inline(tmp_path, _production_inputs):
    script = _compose(tmp_path, _with_template_pad(load_plan_blitz_text()), None)
    assert _PAD_BODY in script
