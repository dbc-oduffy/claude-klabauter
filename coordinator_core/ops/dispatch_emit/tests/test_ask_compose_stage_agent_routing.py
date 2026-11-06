"""compose_ask_script takes plan, plan-review and code-review agent types from the repo's routing."""

from __future__ import annotations

import copy
import json

import pytest

from coordinator_core.ops.dispatch_emit import agent_route, ask_compose
from coordinator_core.ops.dispatch_emit.ask_compose import AskComposeRefused
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose, _presized

GAME = "game-dev:staff-game-dev"
DOMAIN_PLAN = "game-dev:plan-author"


def _named_fragment():
    fragment = copy.deepcopy(REVIEW_KW["review_roster_fragment"])
    wave = next(s for s in fragment["execute_review"]["stages"] if s["kind"] == "review-wave")
    wave["agents"].append(
        {"accepts_signals": "named", "per": "whole-diff", "applies": "none", "schema": "wave"}
    )
    return {"review_roster_fragment": fragment, "review_stage_schemas": REVIEW_KW["review_stage_schemas"]}


@pytest.fixture(autouse=True)
def _model(monkeypatch):
    calls: list[str] = []

    def fake(agent_type, defs, override=None):
        calls.append(agent_type)
        return "opus"

    monkeypatch.setattr(agent_route, "_model_of", fake)
    return calls


def _repo(tmp_path, *, stage_agents=None, review_routing=None):
    if stage_agents is not None:
        (tmp_path / ".claude").mkdir()
        table = {
            "schema_version": 1,
            "default_agent_type": "coordinator:executor",
            "work_classes": [],
            "stage_agents": stage_agents,
        }
        (tmp_path / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    if review_routing is not None:
        (tmp_path / "coordinator.local.md").write_text(
            f"---\nreview_routing: {review_routing}\n---\n", encoding="utf-8"
        )
    return str(tmp_path)


def _m_plus(monkeypatch, root):
    return _presized(monkeypatch, arm="m_plus", repo_root=root, **_named_fragment())


def _plan_review_opts(script):
    start = script.index("label: 'plan-review'")
    return script[start : script.index("schema:", start)]


def test_defaults_when_nothing_routed(monkeypatch, tmp_path, _model):
    script = _m_plus(monkeypatch, _repo(tmp_path))
    assert "agentType: 'coordinator:staff-eng'" in _plan_review_opts(script)
    assert GAME not in script
    assert _model == []


def test_table_stage_agents_route_plan_and_plan_review(monkeypatch, tmp_path, _model):
    root = _repo(tmp_path, stage_agents={"plan": DOMAIN_PLAN, "plan_review": GAME})
    script = _m_plus(monkeypatch, root)
    assert f"agentType: '{GAME}', model: 'opus'" in _plan_review_opts(script)
    assert f"_signalSelects[s] !== '{GAME}'" in script
    assert "agentType: 'coordinator:staff-eng'" not in script
    assert f"agentType: '{DOMAIN_PLAN}', model: 'opus'" in _compose(repo_root=root)
    assert {GAME, DOMAIN_PLAN} <= set(_model)


def test_review_routing_routes_plan_review(monkeypatch, tmp_path):
    root = _repo(tmp_path, review_routing=GAME)
    assert f"agentType: '{GAME}'" in _plan_review_opts(_m_plus(monkeypatch, root))


def test_routed_code_review_joins_the_wave_and_is_not_duplicated(tmp_path):
    root = _repo(tmp_path, stage_agents={"code_review": GAME})
    routed = _compose(repo_root=root, **_named_fragment())
    assert GAME in routed
    assert _compose(repo_root=root, required_reviewers=(GAME,), **_named_fragment()) == routed


def test_routed_code_review_is_additive_to_the_default(tmp_path):
    root = _repo(tmp_path, stage_agents={"code_review": GAME})
    routed = _compose(repo_root=root, **_named_fragment())
    assert "coordinator:code-reviewer" in routed
    assert GAME in routed


def test_table_value_equal_to_the_default_is_not_routed(tmp_path):
    root = _repo(tmp_path, stage_agents={"code_review": "coordinator:code-reviewer"})
    _types, _models, routed = ask_compose._resolve_stage_agents(root, "x", ())
    assert not routed
    assert GAME not in _compose(repo_root=root, **_named_fragment())


def test_review_routing_inline_comment_is_yaml_and_routes(tmp_path):
    root = _repo(tmp_path, review_routing=f"{GAME} # note")
    assert GAME in _compose(repo_root=root, **_named_fragment())


def test_unrouted_code_review_leaves_the_wave_unchanged(tmp_path):
    assert GAME not in _compose(repo_root=_repo(tmp_path), **_named_fragment())


def test_malformed_review_routing_refuses_compose(tmp_path):
    with pytest.raises(AskComposeRefused):
        _compose(repo_root=_repo(tmp_path, review_routing="not an agent"))


def test_malformed_route_table_refuses_compose(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "route-table.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(AskComposeRefused):
        _compose(repo_root=str(tmp_path))


_CLAUDE_DIR_LIST = (
    "---\n"
    "review_routing:\n"
    "  - paths: ['control/plugin/**', '**/*.Build.cs']\n"
    f"    reviewer: {GAME}\n"
    "---\n"
)


def _claude_dir_repo(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "coordinator.local.md").write_text(_CLAUDE_DIR_LIST, encoding="utf-8")
    return str(tmp_path)


def test_claude_dir_path_glob_routes_plan_review_and_code_review_for_matching_writes(
    monkeypatch, tmp_path
):
    root = _claude_dir_repo(tmp_path)
    writes = ("control/plugin/X/Foo.cpp",)
    script = _presized(monkeypatch, arm="m_plus", repo_root=root, writes=writes, **_named_fragment())
    assert f"agentType: '{GAME}'" in _plan_review_opts(script)
    assert "coordinator:code-reviewer" in script
    types, _models, routed = ask_compose._resolve_stage_agents(root, "x", writes)
    assert types["plan_review"] == types["code_review"] == GAME
    assert routed


def test_claude_dir_path_glob_non_matching_writes_keep_the_defaults(monkeypatch, tmp_path):
    root = _claude_dir_repo(tmp_path)
    writes = ("docs/readme.md",)
    script = _presized(monkeypatch, arm="m_plus", repo_root=root, writes=writes, **_named_fragment())
    assert GAME not in script
    assert "agentType: 'coordinator:staff-eng'" in _plan_review_opts(script)
    _types, _models, routed = ask_compose._resolve_stage_agents(root, "x", writes)
    assert not routed


def test_claude_dir_path_glob_raw_prompt_naming_a_path_routes_without_writes(tmp_path):
    root = _claude_dir_repo(tmp_path)
    routed = _compose(
        repo_root=root, prompt="fix the crash in control/plugin/X/Foo.cpp", **_named_fragment()
    )
    assert GAME in routed
    assert "coordinator:code-reviewer" in routed
    assert GAME not in _compose(repo_root=root, prompt="fix the crash", **_named_fragment())


def test_claude_dir_path_glob_uninstalled_reviewer_refuses(monkeypatch, tmp_path):
    def missing(agent_type, defs, override=None):
        raise agent_route.RouteAdmissionError(f"{agent_type} is not installed")

    monkeypatch.setattr(agent_route, "_model_of", missing)
    root = _claude_dir_repo(tmp_path)
    with pytest.raises(AskComposeRefused):
        _compose(repo_root=root, writes=("control/plugin/X/Foo.cpp",))


DOCS = "coordinator:docs-checker"


@pytest.fixture
def _ue_cache():
    from coordinator_core.ops.dispatch_emit import ue_route_source

    ue_route_source.clear_caches()
    yield
    ue_route_source.clear_caches()


def _ue_declared_repo(tmp_path, extra=""):
    (tmp_path / "coordinator.local.md").write_text(
        f"---\nproject_type: unreal-engine\n{extra}---\n", encoding="utf-8"
    )
    return str(tmp_path)


def test_declared_ue_repo_routes_sid_and_docs_checker(monkeypatch, tmp_path, _ue_cache):
    root = _ue_declared_repo(tmp_path)
    script = _m_plus(monkeypatch, root)
    assert f"agentType: '{GAME}'" in _plan_review_opts(script)
    assert "label: 'plan-docs-check'" in script and f"agentType: '{DOCS}'" in script
    types, _models, routed = ask_compose._resolve_stage_agents(root, "x", ())
    assert types["plan_review"] == types["code_review"] == GAME and routed
    wave = _compose(repo_root=root, **_named_fragment())
    assert GAME in wave and "coordinator:code-reviewer" in wave
    assert "label: 'diff-docs-check'" in wave and f"agentType: '{DOCS}'" in wave
    assert f"label: 'review:{DOCS}'" not in wave, "docs-checker no longer joins the named review wave"


def test_ue_marker_repo_routes_sid(tmp_path, _ue_cache):
    (tmp_path / "Game.uproject").write_text("{}", encoding="utf-8")
    types, _m, routed = ask_compose._resolve_stage_agents(str(tmp_path), "x", ())
    assert types["code_review"] == GAME and routed


def test_ue_default_yields_to_review_routing(tmp_path, _ue_cache):
    root = _ue_declared_repo(tmp_path, "review_routing: coordinator:apm\n")
    types, _m, _r = ask_compose._resolve_stage_agents(root, "x", ())
    assert types["plan_review"] == types["code_review"] == "coordinator:apm"


def test_ue_default_yields_to_stage_agents_table(tmp_path, _ue_cache):
    root = _repo(tmp_path, stage_agents={"plan_review": "coordinator:apm"})
    (tmp_path / "coordinator.local.md").write_text(
        "---\nproject_type: unreal-engine\n---\n", encoding="utf-8"
    )
    types, _m, _r = ask_compose._resolve_stage_agents(root, "x", ())
    assert types["plan_review"] == "coordinator:apm"
    assert types["code_review"] == GAME


def test_non_ue_repo_gets_no_docs_checker(monkeypatch, tmp_path, _ue_cache):
    root = _repo(tmp_path)
    script = _m_plus(monkeypatch, root)
    assert DOCS not in script and "plan-docs-check" not in script
    assert DOCS not in _compose(repo_root=root, **_named_fragment())
    (tmp_path / "coordinator.local.md").write_text("---\nproject_type: web\n---\n", encoding="utf-8")
    ask_compose._ue_route_source.clear_caches()
    assert ask_compose._resolve_stage_agents(root, "x", ())[0]["code_review"] == "coordinator:code-reviewer"


def test_pinned_type_passes_no_model_and_scans_nothing(tmp_path, _model):
    root = _repo(tmp_path, stage_agents={"plan_review": "coordinator:apm"})
    types, models, code_routed = ask_compose._resolve_stage_agents(root, "x", ())
    assert types["plan_review"] == "coordinator:apm"
    assert models["plan_review"] is None
    assert not code_routed
    assert _model == []
