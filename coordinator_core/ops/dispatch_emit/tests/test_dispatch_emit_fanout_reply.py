"""dispatch.emit threads `fanout_group` to the composers and returns the script's meta `fanout`.

The pipeline composer is stubbed through sys.modules to return a fixed script carrying a fanout
meta, so these tests hold the op and CLI wiring only, not any composer's grouping.
"""

from __future__ import annotations

import sys
import types

import pytest

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.fanout_cost import FanoutCost, read_fanout, with_fanout
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.pipeline_contract import Manifest, Schedule

_PKG = "coordinator_core.ops.dispatch_emit"
_BARE = "export const meta = { name: 'p', phases: [{ title: 'a' }] };\nreturn {};\n"
_COST = FanoutCost(units=6, agents=3, basis="exact", groups=(("a", "b"), ("c", "d"), ("e", "f")))
_COSTED = with_fanout(_BARE, _COST)


@pytest.fixture
def composer(monkeypatch, tmp_path):
    seen: dict = {"script": _COSTED, "kwargs": None}
    manifest = Manifest("structured", tmp_path, {}, (), {}, {}, "m" * 64)

    def compose(m, inputs, s, *, run_id, agent_type_host, **kwargs):
        seen["kwargs"] = kwargs
        return seen["script"]

    monkeypatch.setitem(
        sys.modules, f"{_PKG}.pipeline_manifest",
        types.SimpleNamespace(load_manifest=lambda root, name: manifest, validate=lambda m, i: Schedule({}, {})),
    )
    monkeypatch.setitem(sys.modules, f"{_PKG}.pipeline_compose", types.SimpleNamespace(compose_pipeline_script=compose))
    monkeypatch.setattr(op_module, "read_content_root", lambda: "doe")
    monkeypatch.setattr(op_module, "content_root_for", lambda _root: tmp_path / "doe-content")
    return seen


def _params(tmp_path, **extra):
    (tmp_path / "brief.md").write_text("research X", encoding="utf-8")
    return {"pipeline": "structured", "brief": "brief.md", "target_root": str(tmp_path),
            "session_id": "sess-1", **extra}


def test_reply_carries_the_scripts_fanout_with_groups(composer, tmp_path):
    reply = _dispatch_emit(_params(tmp_path))

    assert reply["fanout"] == read_fanout(_COSTED)
    assert reply["fanout"]["expectedAgents"] == 3
    assert reply["fanout"]["groups"] == [["a", "b"], ["c", "d"], ["e", "f"]]


def test_reply_fanout_is_none_when_the_script_carries_none(composer, tmp_path):
    composer["script"] = _BARE

    assert _dispatch_emit(_params(tmp_path))["fanout"] is None


def test_default_passes_no_fanout_group_and_split_passes_it(composer, tmp_path):
    _dispatch_emit(_params(tmp_path))
    assert composer["kwargs"] == {}

    _dispatch_emit(_params(tmp_path, fanout_group="split"))
    assert composer["kwargs"] == {"fanout_group": "split"}


@pytest.mark.parametrize("bad", ["merge", "", "SPLIT", 3])
def test_a_value_outside_the_modes_is_refused_with_nothing_composed(composer, tmp_path, bad):
    reply = _dispatch_emit(_params(tmp_path, fanout_group=bad))

    assert "fanout_group" in reply["error"]
    assert composer["kwargs"] is None
    assert not any(p.suffix == ".mjs" for p in tmp_path.rglob("*"))


@pytest.mark.parametrize("route", [{"plan_path": "p.md"}, {"queue": ["q"], "profile": "g"}, {"ask": "do x"}])
def test_split_is_refused_on_routes_whose_composer_takes_no_override(route, tmp_path):
    reply = _dispatch_emit({**route, "fanout_group": "split", "output_path": str(tmp_path / "o.mjs")})

    assert "pipeline and research" in reply["error"]
    assert not (tmp_path / "o.mjs").exists()


def test_cli_maps_fanout_flag_to_the_param(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_emit(params, repo_root=None, **_):
        captured.update(params)
        return {"path": str(tmp_path / "x.mjs"), "sha256": "0" * 64, "ok": True, "findings": [],
                "error_count": 0, "warn_count": 0, "receipt": str(tmp_path / "x.emitted.json")}

    monkeypatch.setattr(cli_module, "_dispatch_emit", fake_emit)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    (tmp_path / "brief.md").write_text("b", encoding="utf-8")

    cli_module.main(["--pipeline", "structured", "--brief", "brief.md", "--fanout", "split", "--repo-root", str(tmp_path)])
    assert captured["fanout_group"] == "split"

    captured.clear()
    cli_module.main(["--pipeline", "structured", "--brief", "brief.md", "--repo-root", str(tmp_path)])
    assert "fanout_group" not in captured


def test_cli_refuses_a_fanout_value_outside_the_choices():
    with pytest.raises(SystemExit):
        cli_module._build_parser().parse_args(["--fanout", "merge"])
