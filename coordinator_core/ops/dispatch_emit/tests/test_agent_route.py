"""Pins the work-class route table reader, matcher, admission check and row routing."""

import json
import subprocess

import pytest

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.hooks.block_workflow_unmodeled_agent import _resolve_doctrine_asset
from coordinator_core.ops.dispatch_emit import agent_route as ar
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

UE = "example-game-repo-control:ue-editor"
UE_TRIAGE = "example-game-repo-control:ue-triage"
MCP = "mcp__example_game_repo-control__"

#: Resolved at import: the `env` fixture repoints CLAUDE_HOME at an empty tree.
_DOE_SCHEMA_PATH = _resolve_doctrine_asset("schemas", "route-table.schema.json")


def _agent_md(name, tools=None, model="sonnet"):
    lines = ["---", f"name: {name}", f"model: {model}"]
    if tools is not None:
        lines.append(f"tools: {tools}")
    lines += ["---", "body", ""]
    return "\n".join(lines)


@pytest.fixture
def env(tmp_path, monkeypatch):
    content = tmp_path / "content"
    agents = content / "agents"
    agents.mkdir(parents=True)
    (agents / "executor.md").write_text(_agent_md("executor", "Read, Edit, Write, Bash"), encoding="utf-8")
    (agents / "notools.md").write_text(_agent_md("notools"), encoding="utf-8")
    home = tmp_path / "home"
    plug = home / ".claude" / "plugins" / "example-game-repo-control" / "agents"
    plug.mkdir(parents=True)
    (plug / "ue-editor.md").write_text(_agent_md("ue-editor", f"Read, {MCP}manage_asset, {MCP}inspect"), encoding="utf-8")
    (plug / "ue-triage.md").write_text(_agent_md("ue-triage", "Read", model="haiku"), encoding="utf-8")
    (plug / "ue-nomodel.md").write_text(
        "---\nname: ue-nomodel\nmodel: inherit\ntools: " + MCP + "inspect\n---\n", encoding="utf-8"
    )
    monkeypatch.setattr(bua, "read_content_root", lambda: str(content))
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    repo = tmp_path / "repo"
    (repo / ".claude").mkdir(parents=True)
    return repo


def _table(**over):
    table = {
        "schema_version": 1,
        "default_agent_type": "coordinator:executor",
        "work_classes": [
            {
                "id": "ue-editor",
                "match": {"paths": ["Content/*"], "keywords": ["Blueprint"], "tools": [MCP]},
                "agent_type": UE,
                "required_tools": [MCP],
                "on_mismatch": "refuse",
            }
        ],
    }
    table.update(over)
    return table


def _write(repo, table):
    (repo / ".claude" / "route-table.json").write_text(json.dumps(table), encoding="utf-8")
    return repo


def _row(rid="C1", writes=("Content/A.uasset",), title="t", body="", **kw):
    return WaveRow(rid, title, "s", tuple(writes), [], [], body=body, **kw)


def test_absent_table_is_identity(env):
    rows = [_row()]
    out, routes, notes = ar.route_rows(rows, env)
    assert out is rows and routes == {} and notes == []
    assert ar.load_route_table(env) is None


def test_non_ue_repo_without_table_is_identity_with_plugin_roots_set(env, monkeypatch):
    from coordinator_core.ops.dispatch_emit import ue_route_source

    monkeypatch.setenv("COORDINATOR_PLUGIN_ROOTS", str(env.parent / "home" / ".claude" / "plugins" / "example-game-repo-control"))
    ue_route_source.clear_caches()
    rows = [_row(body="ExecutePythonScript")]
    out, routes, notes = ar.route_rows(rows, env)
    assert out is rows and routes == {} and notes == []


@pytest.mark.parametrize(
    "mutate, field",
    [
        (lambda t: t.update(schema_version=2), "schema_version"),
        (lambda t: t.update(default_agent_type=""), "default_agent_type"),
        (lambda t: t.update(work_classes="x"), "work_classes"),
        (lambda t: t["work_classes"][0].update(match={}), "work_classes[0].match"),
        (lambda t: t["work_classes"][0].update(match={"paths": [""]}), "work_classes[0].match.paths"),
        (lambda t: t["work_classes"][0].update(on_mismatch="ignore"), "work_classes[0].on_mismatch"),
        (lambda t: t["work_classes"][0].update(agent_type=""), "work_classes[0].agent_type"),
        (lambda t: t["work_classes"][0].update(id=""), "work_classes[0].id"),
        (lambda t: t["work_classes"][0].update(required_tools="x"), "work_classes[0].required_tools"),
        (lambda t: t.update(agent_tool_ranges={"a": "x"}), "agent_tool_ranges.a"),
        (lambda t: t.update(brief_lint={"level": "warn"}), "brief_lint.prescribed_method_patterns"),
        (lambda t: t.update(brief_lint={"prescribed_method_patterns": ["("], "level": "warn"}), "brief_lint.prescribed_method_patterns[0]"),
        (lambda t: t.update(brief_lint={"prescribed_method_patterns": ["x"], "level": "loud"}), "brief_lint.level"),
        (lambda t: t.update(editor_python={"preferred_agent_type": UE, "level": "warn"}), "editor_python"),
        (lambda t: t.update(editor_python={"preferred_agent_type": UE, "level": "warn", "bash_patterns": ["["]}), "editor_python.bash_patterns[0]"),
        (lambda t: t.update(editor_python={"preferred_agent_type": "", "level": "warn", "tool_names": ["x"]}), "editor_python.preferred_agent_type"),
    ],
)
def test_malformed_field_names_it(env, mutate, field):
    table = _table()
    mutate(table)
    _write(env, table)
    with pytest.raises(ar.RouteTableError) as exc:
        ar.load_route_table(env)
    assert field in str(exc.value)


def test_not_json_raises(env):
    (env / ".claude" / "route-table.json").write_text("{", encoding="utf-8")
    with pytest.raises(ar.RouteTableError):
        ar.load_route_table(env)


def test_editor_python_bash_patterns_only_loads(env):
    _write(env, _table(editor_python={"preferred_agent_type": UE, "level": "warn", "bash_patterns": ["-run=pythonscript"]}))
    ep = ar.load_route_table(env).editor_python
    assert ep.tool_names == () and ep.bash_patterns[0].search("UnrealEditor -run=pythonscript")


def test_table_is_memoised_and_reread_on_change(env):
    _write(env, _table())
    first = ar.load_route_table(env)
    assert ar.load_route_table(env) is first
    _write(env, _table(default_agent_type="coordinator:executor-two"))
    assert ar.load_route_table(env).default_agent_type == "coordinator:executor-two"


def test_first_match_in_list_order(env):
    classes = [
        {"id": "a", "match": {"keywords": ["mesh"]}, "agent_type": "x:a", "on_mismatch": "refuse"},
        {"id": "b", "match": {"keywords": ["mesh", "tex"]}, "agent_type": "x:b", "on_mismatch": "refuse"},
    ]
    _write(env, _table(work_classes=classes))
    t = ar.load_route_table(env)
    assert ar.match_work_class(t, title="a mesh and tex").id == "a"
    assert ar.match_work_class(t, title="only tex").id == "b"
    assert ar.match_work_class(t, title="nothing") is None


def test_path_glob_matches_backslash_write_path(env):
    t = ar.load_route_table(_write(env, _table()))
    assert ar.match_work_class(t, writes=["Content\\Maps\\A.umap"]).id == "ue-editor"
    assert ar.match_work_class(t, writes=["./Content/Maps/A.umap"]).id == "ue-editor"
    assert ar.match_work_class(t, writes=["Source/A.cpp"]) is None


def test_keyword_is_case_insensitive_and_tools_match_body(env):
    t = ar.load_route_table(_write(env, _table()))
    assert ar.match_work_class(t, title="Fix the BLUEPRINT graph").id == "ue-editor"
    assert ar.match_work_class(t, body=f"call {MCP}inspect").id == "ue-editor"


def test_covers_forms():
    assert ar.covers(["Read", "mcp__x__a"], ["mcp__x__"])
    assert ar.covers(["Read"], ["Read"])
    assert ar.covers(["mcp__x__*"], ["mcp__x__a"])
    assert not ar.covers(["Read"], ["Edit"])
    assert not ar.covers(["Read"], ["Read", "Edit"])
    assert not ar.covers(None, ["Read"])
    assert ar.covers(None, [])


def test_tool_less_definition_covers_nothing(env):
    t = ar.load_route_table(_write(env, _table()))
    assert ar.agent_tool_range("coordinator:notools", t) is None
    assert ar.agent_tool_range("general-purpose", t) is None
    cls = t.work_classes[0]
    assert ar.admit("coordinator:notools", cls, t).verdict == "refuse"


def test_agent_tool_ranges_overrides_definition(env):
    t = ar.load_route_table(_write(env, _table(agent_tool_ranges={"coordinator:executor": [MCP]})))
    assert ar.agent_tool_range("coordinator:executor", t) == (MCP,)
    assert ar.admit("coordinator:executor", t.work_classes[0], t).verdict == "admit"


def test_definition_tools_are_read_from_frontmatter(env):
    t = ar.load_route_table(_write(env, _table()))
    assert ar.agent_tool_range(UE, t) == ("Read", f"{MCP}manage_asset", f"{MCP}inspect")
    assert ar.admit(UE, t.work_classes[0], t).verdict == "admit"


def test_admit_redirect_names_target(env):
    cls = {**_table()["work_classes"][0], "on_mismatch": "redirect"}
    t = ar.load_route_table(_write(env, _table(work_classes=[cls])))
    a = ar.admit("coordinator:executor", t.work_classes[0], t)
    assert (a.verdict, a.target) == ("redirect", UE)
    assert MCP in a.reason


def test_derived_executor_row_takes_class_agent_and_frontmatter_model(env):
    _write(env, _table())
    rows = [_row()]
    out, routes, notes = ar.route_rows(rows, env)
    assert (out[0].agent_type, out[0].agent_model) == (UE, "sonnet")
    r = routes["C1"]
    assert r.work_class == "ue-editor" and r.primary == (UE, "sonnet") and r.triage is None
    assert notes and "C1" in notes[0]


def test_triage_class_marks_row_triage_first(env):
    cls = {**_table()["work_classes"][0], "triage_agent_type": UE_TRIAGE}
    _write(env, _table(work_classes=[cls]))
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].triage == (UE_TRIAGE, "haiku")
    assert routes["C1"].to_json()["triage"] == {"agent_type": UE_TRIAGE, "model": "haiku"}


def test_explicit_row_lacking_tools_refuse_raises(env):
    _write(env, _table())
    with pytest.raises(ar.RouteAdmissionError) as exc:
        ar.route_rows([_row(agent_type="coordinator:executor")], env)
    assert "C1" in str(exc.value) and "ue-editor" in str(exc.value) and MCP in str(exc.value)


def test_explicit_row_lacking_tools_redirect_repoints_and_notes(env):
    cls = {**_table()["work_classes"][0], "on_mismatch": "redirect"}
    _write(env, _table(work_classes=[cls]))
    out, routes, notes = ar.route_rows([_row(agent_type="coordinator:executor")], env)
    assert out[0].agent_type == UE and routes["C1"].primary[0] == UE
    assert any("redirected" in n for n in notes)


def test_explicit_covering_row_is_admitted_unchanged(env):
    _write(env, _table())
    row = _row(agent_type=UE, agent_model="opus")
    out, routes, _ = ar.route_rows([row], env)
    assert out[0] == row and routes["C1"].primary == (UE, "opus")


def test_enricher_derived_row_is_untouched(env):
    _write(env, _table())
    row = _row(writes=("docs/plans/p.md",), title="Blueprint")
    out, routes, _ = ar.route_rows([row], env)
    assert out[0] is row and routes == {}


def test_unmatched_executor_row_takes_default_agent_type(env):
    _write(env, _table(default_agent_type="coordinator:test-runner"))
    out, routes, _ = ar.route_rows([_row(writes=("src/a.py",))], env)
    assert (out[0].agent_type, out[0].agent_model) == ("coordinator:test-runner", "haiku")
    assert routes == {}


def test_unmatched_executor_row_under_executor_default_is_unchanged(env):
    _write(env, _table())
    row = _row(writes=("src/a.py",))
    out, routes, notes = ar.route_rows([row], env)
    assert out[0] is row and routes == {} and notes == []


def test_routed_type_with_no_model_raises(env):
    cls = {**_table()["work_classes"][0], "agent_type": "example-game-repo-control:ue-nomodel"}
    _write(env, _table(work_classes=[cls]))
    with pytest.raises(ar.RouteAdmissionError) as exc:
        ar.route_rows([_row()], env)
    assert "agent_model" in str(exc.value)


def test_routed_class_agent_lacking_its_own_required_tools_raises(env):
    cls = {**_table()["work_classes"][0], "agent_type": "coordinator:executor"}
    _write(env, _table(work_classes=[cls]))
    with pytest.raises(ar.RouteAdmissionError):
        ar.route_rows([_row()], env)


def test_targets_hold_only_covering_types_with_models(env):
    t = _table(
        default_agent_type="coordinator:executor",
        agent_tool_ranges={"example-game-repo-control:ue-nomodel": [MCP], "x:other": ["Read"]},
    )
    t["work_classes"][0]["triage_agent_type"] = UE_TRIAGE
    _write(env, t)
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].targets == {UE: "sonnet"}


def test_targets_include_other_covering_type(env):
    t = _table(agent_tool_ranges={"coordinator:executor": [MCP]})
    _write(env, t)
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].targets == {UE: "sonnet", "coordinator:executor": "sonnet"}


def test_manifest_row_round_trips_with_and_without_route():
    plain = ask_contract.ManifestRow("C1", "executor", "sonnet", "b.md", ("a",), 0)
    assert "route" not in plain.to_json()
    assert ask_contract.ManifestRow.from_json(plain.to_json()) == plain
    route = ar.Route("c", (UE, "sonnet"), {UE: "sonnet"}).to_json()
    routed = ask_contract.ManifestRow("C1", UE, "sonnet", "b.md", ("a",), 0, route=route)
    out = json.loads(json.dumps(routed.to_json()))
    assert out["route"]["primary"] == {"agent_type": UE, "model": "sonnet"}
    assert ask_contract.ManifestRow.from_json(out) == routed


def test_repo_root_of_prefers_table_then_git(env, tmp_path):
    sub = env / "a" / "b"
    sub.mkdir(parents=True)
    _write(env, _table())
    assert ar.repo_root_of(str(sub)) == env
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    assert ar.repo_root_of(str(other)) == other
    assert ar.repo_root_of("") is None


def test_no_subprocess_spawn(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _write(env, _table())
    ar.route_rows([_row(), _row("C2", writes=("src/a.py",))], env)
    ar.repo_root_of(str(env))


def test_fixture_tables_conform_to_doe_schema(env):
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = _DOE_SCHEMA_PATH
    if schema_path is None:
        pytest.skip("DoE route-table.schema.json not installed")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    full = _table(
        agent_tool_ranges={"coordinator:executor": ["Read"]},
        brief_lint={"prescribed_method_patterns": [r"\bue_\w+_job\.py\b"], "level": "warn"},
        editor_python={"preferred_agent_type": UE, "level": "strict", "bash_patterns": ["-run=pythonscript"]},
    )
    full["work_classes"][0]["triage_agent_type"] = UE_TRIAGE
    for table in (_table(), full):
        jsonschema.validate(table, schema)
        _write(env, table)
        assert ar.load_route_table(env) is not None


_DEFAULTS = {
    "plan": "coordinator:plan-author",
    "plan_review": "coordinator:staff-eng",
    "code_review": "coordinator:code-reviewer",
}
GAME = "game-dev:staff-game-dev"


def _local_md(repo, body):
    (repo / "coordinator.local.md").write_text(body, encoding="utf-8")


def _stages(repo, ask="refactor the thing", writes=()):
    return ar.resolve_stage_agents(repo, ask_text=ask, writes=writes, defaults=_DEFAULTS)


def test_stage_agents_no_table_no_review_routing_is_defaults(env):
    assert _stages(env) == {s: (d, "default") for s, d in _DEFAULTS.items()}


def test_stage_agents_table_level_routes_plan_review_only(env):
    _write(env, _table(stage_agents={"plan_review": GAME}))
    out = _stages(env)
    assert out["plan_review"] == (GAME, "table")
    assert out["plan"] == (_DEFAULTS["plan"], "default")
    assert out["code_review"] == (_DEFAULTS["code_review"], "default")


def test_stage_agents_work_class_wins_over_table(env):
    table = _table(stage_agents={"code_review": "x:table-reviewer"})
    table["work_classes"][0]["stage_agents"] = {"code_review": GAME}
    _write(env, table)
    assert _stages(env, writes=["Content/A.uasset"])["code_review"] == (GAME, "work-class:ue-editor")
    assert _stages(env, ask="Blueprint rework")["code_review"] == (GAME, "work-class:ue-editor")
    assert _stages(env, ask="docs only")["code_review"] == ("x:table-reviewer", "table")


def test_stage_agents_review_routing_alone_routes_reviews_not_plan(env):
    _local_md(env, f"---\nreview_routing: {GAME}\n---\nbody\n")
    out = _stages(env)
    assert out["plan_review"] == (GAME, "review_routing")
    assert out["code_review"] == (GAME, "review_routing")
    assert out["plan"] == (_DEFAULTS["plan"], "default")


def test_stage_agents_table_beats_review_routing(env):
    _local_md(env, f"---\nreview_routing: {GAME}\n---\n")
    _write(env, _table(stage_agents={"code_review": "x:table-reviewer"}))
    out = _stages(env)
    assert out["code_review"] == ("x:table-reviewer", "table")
    assert out["plan_review"] == (GAME, "review_routing")


@pytest.mark.parametrize("value", ["staff-game-dev", "a:b:c", "has space:x"])
def test_malformed_review_routing_raises(env, value):
    _local_md(env, f"---\nreview_routing: {value}\n---\n")
    with pytest.raises(ar.RouteTableError, match="review_routing"):
        _stages(env)


def test_review_routing_absent_cases_return_none(env):
    assert ar.read_review_routing(env) is None
    _local_md(env, "---\nother: 1\n---\n")
    assert ar.read_review_routing(env) is None
    _local_md(env, "no frontmatter\nreview_routing: a:b\n")
    assert ar.read_review_routing(env) is None
    _local_md(env, "---\nreview_routing: 'a:b'\n---\n")
    assert ar.read_review_routing(env) == "a:b"


def test_unknown_stage_agents_key_raises(env):
    _write(env, _table(stage_agents={"planning": GAME}))
    with pytest.raises(ar.RouteTableError, match="stage_agents"):
        ar.load_route_table(env)
    table = _table()
    table["work_classes"][0]["stage_agents"] = {"bogus": GAME}
    _write(env, table)
    with pytest.raises(ar.RouteTableError, match=r"work_classes\[0\]\.stage_agents"):
        ar.load_route_table(env)
