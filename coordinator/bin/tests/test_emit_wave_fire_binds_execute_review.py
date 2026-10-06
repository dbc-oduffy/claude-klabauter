"""`emit-wave-fire` binds `executeReview` into every fire and refuses (no fire-*.mjs) without it."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
import yaml

_BIN = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("emit_wave_fire_er", _BIN / "emit-wave-fire.py")
ewf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ewf)

SIZING_REL = "state/sizings/s1.yaml"
BATON_REL = "state/handoffs/b1.md"
BATON = "---\nhandoff_id: hnd-1\ntitle: Minted baton\nstatus: open\n---\n\n# body\n"


def _fragment(routes=("wave-fire-dispatch",)):
    def agent(t, schema, **extra):
        return {"agentType": t, "model": "opus", "effort": "low", "schema": schema, **extra}

    return {
        "schema": "review-roster-fragment",
        "schema_version": 5,
        "execute_review": {
            "required_for_emit": list(routes),
            "stages": [
                {"kind": "prep", "agents": [agent("coordinator:test-runner", "review-prep-result")]},
                {"kind": "review-wave", "agents": [agent("coordinator:code-reviewer", "slice-review-result")]},
            ],
        },
    }


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-install"))

    async def echo(msg, caller=None):
        return {"result": {"script": "export const meta = {};\nconst args = " + json.dumps(msg["params"]["args"]) + ";\n"}}

    import coordinator_core.ipc as ipc

    monkeypatch.setattr(ipc, "dispatch_message", echo)
    monkeypatch.setattr(ewf, "_load_mint", lambda: lambda rel, root: {
        "id": "hnd-1", "path": BATON_REL, "title": "Minted baton", "created": True})


def _patch_review(monkeypatch, fragment=None, load_error=None):
    from coordinator_core.ops.review_mint import op

    def load_fragment(repo_root=None):
        if load_error:
            raise load_error
        return fragment

    monkeypatch.setattr(op, "load_fragment", load_fragment)
    monkeypatch.setattr(op, "load_stage_schemas", lambda repo_root=None: {"slice-review-result": {"type": "object"}, "review-prep-result": {"type": "object"}})


def _fire(tmp_path):
    root = tmp_path / "doctrine" / "coordinator"
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    (root / "workflows" / "plan-blitz.mjs").write_text("// stub\n", encoding="utf-8")
    (root / "agents").mkdir(exist_ok=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "state" / "handoffs").mkdir(parents=True)
    (tmp_path / SIZING_REL).write_text(yaml.safe_dump({
        "schema": "sizing-object", "status": "sized", "premise": {"provenance": "unrecorded"},
        "detents": [], "fork": None, "xl_exit": None, "appetite": "medium",
        "estimate": {"tshirt": "M", "provisional": True}, "route": "plan",
        "interaction_mode": "hands-on", "intent": "do the thing",
        "exit_criterion": {"statement": "it works",
                           "accepted": {"pm_quote": "ok", "on": "2026-10-01", "mode": "hands-on"}},
    }), encoding="utf-8")
    (tmp_path / BATON_REL).write_text(BATON, encoding="utf-8")
    (tmp_path / "trail").mkdir()
    (tmp_path / ".git").mkdir()
    return ewf.main([
        "--repo-root", str(tmp_path), "--trail-dir", str(tmp_path / "trail"),
        "--plugin-root", str(root), "--from-sizing", SIZING_REL, "--live-engine-tree",
    ])


SIGNATURE = "async function executeReview({ batonId, declaredPaths }) {"


def _fire_text(tmp_path, monkeypatch):
    _patch_review(monkeypatch, _fragment())
    assert _fire(tmp_path) == ewf.EXIT_OK
    fires = list((tmp_path / "trail").glob("fire-*.mjs"))
    assert fires
    return fires[0], fires[0].read_text(encoding="utf-8")


def test_wave_fire_defines_execute_review_function(tmp_path, monkeypatch):
    _path, text = _fire_text(tmp_path, monkeypatch)
    assert text.index("export const meta") < text.index(SIGNATURE) < text.index("const args = ")
    args = json.loads(text[text.index("const args = ") + len("const args = "):].rstrip().rstrip(";"))
    assert "executeReview" not in args


def test_execute_review_scopes_to_its_own_declared_paths(tmp_path, monkeypatch):
    _path, text = _fire_text(tmp_path, monkeypatch)
    fn = text[text.index(SIGNATURE): text.index("const args = ")]
    assert "JSON.stringify(declaredPaths)" in fn
    assert "'\\nbaton_id: ' + String(batonId)" in fn
    assert "ONLY declared paths" in fn
    assert "return { prep: _reviewPrep, wave: _reviewWave, integration: null };" in fn
    assert fn.count("JSON.stringify(declaredPaths)") == 1


def test_each_baton_freezes_under_its_own_slice_id(tmp_path, monkeypatch):
    """XS reviews run concurrently, within a fire and across fires on one base SHA. A static slice
    id made every prep after the first collide and fail closed (cockpit plan-blitz, 2026-10-06)."""
    _path, text = _fire_text(tmp_path, monkeypatch)
    fn = text[text.index(SIGNATURE): text.index("const args = ")]
    assert "__SLICE_KEY__" not in fn
    assert "--slice-id ' + String(batonId).replace(/[^A-Za-z0-9_.-]/g, '-') + '-" in fn


@pytest.mark.spawns_process
def test_emitted_function_parses(tmp_path, monkeypatch):
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    path, _text = _fire_text(tmp_path, monkeypatch)
    check = path.with_suffix(".check.mjs")
    check.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run(
        [node, "--check", str(check)], capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("case", ["unloadable", "route-missing"])
def test_wave_fire_refuses_without_execute_review(tmp_path, monkeypatch, case):
    if case == "unloadable":
        _patch_review(monkeypatch, load_error=FileNotFoundError("no fragment"))
    else:
        _patch_review(monkeypatch, _fragment(routes=("plan",)))
    assert _fire(tmp_path) == ewf.EXIT_REFUSED
    assert not list((tmp_path / "trail").glob("fire-*.mjs"))
