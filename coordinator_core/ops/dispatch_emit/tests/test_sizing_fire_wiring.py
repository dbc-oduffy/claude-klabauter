"""Wiring of `dispatch.emit --sizing`: arm dispatch, exclusivity, one refusal, zero spawns.

The three arm callables are stubbed in sys.modules; their own tests live beside them.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops import dispatch_emit as pkg
from coordinator_core.ops.dispatch_emit import op
from coordinator_core.ops.dispatch_emit.sizing_fire import SizingFireRefused

_PKG = "coordinator_core.ops.dispatch_emit"


def _sizing(tshirt: str, route: str, **over) -> dict:
    doc = {
        "status": "sized",
        "estimate": {"tshirt": tshirt},
        "route": route,
        "interaction_mode": "fire-and-forget",
        "exit_criterion": {"statement": "it works", "accepted": {"pm_quote": "yes"}},
    }
    doc.update(over)
    return doc


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    calls: dict = {}

    def mint_xs_spine(sizing, *, sizing_rel, writes, out_dir):
        calls["xs"] = {"sizing_rel": sizing_rel, "writes": list(writes), "out_dir": out_dir}
        return "SPINE\n", Path(out_dir) / "x.spine.md"

    def compose_s_stage(sizing, *, sizing_rel, repo_root, session_id=None):
        calls["s"] = {"sizing_rel": sizing_rel, "repo_root": repo_root}
        return "// s stage\n"

    def delegate_m_plus(*, sizing_rel, repo_root, trail_dir):
        calls["m"] = {"sizing_rel": sizing_rel, "trail_dir": trail_dir}
        script = Path(repo_root) / "m.workflow.mjs"
        script.write_text("// m\n", encoding="utf-8")
        return {
            "scriptPath": str(script),
            "exit_code": calls.get("m_exit", 0),
            "batons": ["b-1"],
            "refusal": "emit-wave-fire: REFUSED - nope",
        }

    for name, attr, fn in (
        ("sizing_xs_mint", "mint_xs_spine", mint_xs_spine),
        ("sizing_s_compose", "compose_s_stage", compose_s_stage),
        ("sizing_m_delegate", "delegate_m_plus", delegate_m_plus),
    ):
        mod = types.ModuleType(f"{_PKG}.{name}")
        setattr(mod, attr, fn)
        monkeypatch.setitem(sys.modules, f"{_PKG}.{name}", mod)
        # `from pkg import sub` reads the package attribute once the real submodule was imported.
        monkeypatch.setattr(pkg, name, mod, raising=False)

    def fake_emit_script(plan_path, **kw):
        calls["plan"] = plan_path
        return "// plan route\n"

    monkeypatch.setattr(op, "emit_script", fake_emit_script)
    monkeypatch.setattr(op, "_load_review_inputs", lambda route: (None, None))
    monkeypatch.setattr(op, "check_agent_types_resolve", lambda *a, **k: None)
    monkeypatch.setattr(op, "run_checks", lambda script: [])
    return types.SimpleNamespace(root=tmp_path, calls=calls)


def _write(env, name: str, doc: dict) -> str:
    (env.root / "state" / "sizings" / name).write_text(yaml.safe_dump(doc), encoding="utf-8")
    return f"state/sizings/{name}"


def _emit(env, rel: str, **extra) -> dict:
    return op._dispatch_emit({"sizing_path": rel, **extra}, repo_root=env.root)


def test_xs_mints_spine_and_reenters_plan_route(env):
    rel = _write(env, "a.yaml", _sizing("XS", "dispatch"))
    out = env.root / "docs" / "a.workflow.mjs"
    out.parent.mkdir()
    reply = _emit(env, rel, output_path=str(out), writes=["src/x.py"])
    assert reply["arm"] == "xs"
    assert env.calls["xs"]["writes"] == ["src/x.py"]
    spine = out.parent / "x.spine.md"
    assert spine.read_text(encoding="utf-8") == "SPINE\n"
    assert Path(env.calls["plan"]) == spine
    assert out.read_text(encoding="utf-8") == "// plan route\n"


def test_s_composes_stage_and_writes_receipt(env):
    rel = _write(env, "b.yaml", _sizing("S", "spec-dispatch"))
    out = env.root / "b.workflow.mjs"
    reply = _emit(env, rel, output_path=str(out))
    assert reply["arm"] == "s"
    assert env.calls["s"]["sizing_rel"] == rel
    assert env.calls["s"]["repo_root"] == env.root.as_posix()
    assert out.read_text(encoding="utf-8") == "// s stage\n"
    assert reply["receipt"] and Path(reply["receipt"]).is_file()
    assert "plan" not in env.calls


def test_m_plus_delegates_and_passes_script_path_through(env):
    rel = _write(env, "c.yaml", _sizing("L", "plan", baton="state/handoffs/baton-c.md"))
    reply = _emit(env, rel, trail_dir="trail")
    assert reply["arm"] == "m_plus"
    assert reply["path"] == str(env.root / "m.workflow.mjs")
    assert env.calls["m"]["trail_dir"] == "trail"
    assert reply["batons"] == ["b-1"]
    assert reply["uncommitted"] == ["state/handoffs/baton-c.md", rel]


def test_m_plus_nonzero_exit_is_a_refusal(env):
    rel = _write(env, "c.yaml", _sizing("M", "plan"))
    env.calls["m_exit"] = 3
    with pytest.raises(SizingFireRefused) as ei:
        _emit(env, rel)
    assert "exited 3" in ei.value.fields[0] and "REFUSED - nope" in ei.value.fields[0]


def test_sizing_path_is_exclusive(env):
    rel = _write(env, "a.yaml", _sizing("XS", "dispatch"))
    for other in ({"plan_path": "p.md"}, {"inventory_path": "i.md"}, {"queue": ["q"]}):
        with pytest.raises(op.SizingPathConflictError):
            _emit(env, rel, output_path=str(env.root / "o.workflow.mjs"), **other)


def test_one_refusal_names_every_field(env):
    rel = _write(
        env,
        "bad.yaml",
        _sizing("XS", "plan", interaction_mode=None, exit_criterion={"accepted": None}),
    )
    with pytest.raises(SizingFireRefused) as ei:
        _emit(env, rel, output_path=str(env.root / "o.workflow.mjs"))
    joined = " ".join(ei.value.fields)
    for needle in ("statement", "accepted", "interaction_mode", "route", "--writes"):
        assert needle in joined
    assert len(ei.value.fields) >= 5


def test_xs_and_s_emit_spawn_nothing(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned on --sizing without --fire")

    monkeypatch.setattr(subprocess, "Popen", boom)
    xs = _write(env, "a.yaml", _sizing("XS", "dispatch"))
    s = _write(env, "b.yaml", _sizing("S", "spec-dispatch"))
    _emit(env, xs, output_path=str(env.root / "a.workflow.mjs"), writes=["w.py"])
    _emit(env, s, output_path=str(env.root / "b.workflow.mjs"))
