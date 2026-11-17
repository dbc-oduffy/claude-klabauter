"""publish.py child output is decoded utf-8/replace and python children are spawned UTF-8."""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "publish.py"
_TREE = ast.parse(_SRC.read_text(encoding="utf-8"))


def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)


def _subprocess_run_calls():
    for node in ast.walk(_TREE):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
        ):
            yield node


def test_every_text_mode_spawn_decodes_utf8_with_replace():
    offenders = []
    for call in _subprocess_run_calls():
        text = _kw(call, "text")
        if not (isinstance(text, ast.Constant) and text.value is True):
            continue
        enc, err = _kw(call, "encoding"), _kw(call, "errors")
        ok = (
            isinstance(enc, ast.Constant) and enc.value == "utf-8"
            and isinstance(err, ast.Constant) and err.value == "replace"
        )
        if not ok:
            offenders.append(call.lineno)
    assert not offenders, f"text=True spawns without utf-8/replace at lines {offenders}"


def _load_fn(name: str):
    fn = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
    ns: dict = {"os": os, "subprocess": subprocess}
    exec(compile(ast.Module([fn], []), str(_SRC), "exec"), ns)  # noqa: S102
    return ns[name]


def test_utf8_child_env_forces_utf8_and_keeps_environment():
    env = _load_fn("_utf8_child_env")()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env.get("PATH") == os.environ.get("PATH")


def test_version_gate_spawn_passes_utf8_env():
    gate = [
        c for c in _subprocess_run_calls()
        if any(isinstance(a, ast.Attribute) and a.attr == "executable" for a in ast.walk(c.args[0]))
    ]
    assert gate
    for call in gate:
        env = _kw(call, "env")
        assert isinstance(env, ast.Call) and getattr(env.func, "id", "") == "_utf8_child_env"
