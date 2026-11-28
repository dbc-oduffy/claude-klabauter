"""Pins the work-class route table reader, matcher, admission check and row routing."""

import json
import subprocess

import pytest

from coordinator_core.hooks import block_unenumerated_agent_type as bua
from coordinator_core.hooks.block_workflow_unmodeled_agent import _resolve_doctrine_asset
from coordinator_core.ops.dispatch_emit import agent_route as ar
from coordinator_core.ops.dispatch_emit import ask_contract
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from pathlib import Path

pytestmark = pytest.mark.complete

UE = "example-game-repo-control:ue-editor"
UE_TRIAGE = "example-game-repo-control:ue-triage"
MCP = "mcp__example_game_repo-control__"

#: Resolved at import: the `env` fixture repoints CLAUDE_HOME at an empty tree.
_DOE_SCHEMA_PATH = _resolve_doctrine_asset("schemas", "route-table.schema.json")


def _agent_md(name, tools=None, model: str = "sonnet") -> str:
    lines = ["---", f"name: {name}", f"model: {model}"]
    if tools is not None:
        lines.append(f"tools: {tools}")
    lines += ["---", "body", ""]
    return "\n".join(lines)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _row(rid: str = "C1", writes=("Content/A.uasset",), title: str = "t", body: str = "", **kw) -> WaveRow:
    return WaveRow(rid, title, "s", tuple(writes), [], [], body=body, **kw)


def test_absent_table_is_identity(env) -> None:
    rows = [_row()]
    out, routes, notes = ar.route_rows(rows, env)
    assert out is rows and routes == {} and notes == []
    assert ar.load_route_table(env) is None


def test_non_ue_repo_without_table_is_identity_with_plugin_roots_set(env, monkeypatch: pytest.MonkeyPatch) -> None:
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
        (lambda t: t["work_classes"][0].update(run_only="yes"), "work_classes[0].run_only"),
        (lambda t: t.update(agent_tool_ranges={"a": "x"}), "agent_tool_ranges.a"),
        (lambda t: t.update(brief_lint={"level": "warn"}), "brief_lint.prescribed_method_patterns"),
        (lambda t: t.update(brief_lint={"prescribed_method_patterns": ["("], "level": "warn"}), "brief_lint.prescribed_method_patterns[0]"),
        (lambda t: t.update(brief_lint={"prescribed_method_patterns": ["x"], "level": "loud"}), "brief_lint.level"),
        (lambda t: t.update(editor_python={"preferred_agent_type": UE, "level": "warn"}), "editor_python"),
        (lambda t: t.update(editor_python={"preferred_agent_type": UE, "level": "warn", "bash_patterns": ["["]}), "editor_python.bash_patterns[0]"),
        (lambda t: t.update(editor_python={"preferred_agent_type": "", "level": "warn", "tool_names": ["x"]}), "editor_python.preferred_agent_type"),
    ],
)
def test_malformed_field_names_it(env, mutate, field) -> None:
    table = _table()
    mutate(table)
    _write(env, table)
    with pytest.raises(ar.RouteTableError) as exc:
        ar.load_route_table(env)
    assert field in str(exc.value)


def test_not_json_raises(env) -> None:
    (env / ".claude" / "route-table.json").write_text("{", encoding="utf-8")
    with pytest.raises(ar.RouteTableError):
        ar.load_route_table(env)


def test_editor_python_bash_patterns_only_loads(env) -> None:
    _write(env, _table(editor_python={"preferred_agent_type": UE, "level": "warn", "bash_patterns": ["-run=pythonscript"]}))
    ep = ar.load_route_table(env).editor_python
    assert ep.tool_names == () and ep.bash_patterns[0].search("UnrealEditor -run=pythonscript")


def test_table_is_memoised_and_reread_on_change(env) -> None:
    _write(env, _table())
    first = ar.load_route_table(env)
    assert ar.load_route_table(env) is first
    _write(env, _table(default_agent_type="coordinator:executor-two"))
    assert ar.load_route_table(env).default_agent_type == "coordinator:executor-two"


def test_first_match_in_list_order(env) -> None:
    classes = [
        {"id": "a", "match": {"keywords": ["mesh"]}, "agent_type": "x:a", "on_mismatch": "refuse"},
        {"id": "b", "match": {"keywords": ["mesh", "tex"]}, "agent_type": "x:b", "on_mismatch": "refuse"},
    ]
    _write(env, _table(work_classes=classes))
    t = ar.load_route_table(env)
    assert ar.match_work_class(t, title="a mesh and tex").id == "a"
    assert ar.match_work_class(t, title="only tex").id == "b"
    assert ar.match_work_class(t, title="nothing") is None


def test_path_glob_matches_backslash_write_path(env) -> None:
    t = ar.load_route_table(_write(env, _table()))
    assert ar.match_work_class(t, writes=["Content\\Maps\\A.umap"]).id == "ue-editor"
    assert ar.match_work_class(t, writes=["./Content/Maps/A.umap"]).id == "ue-editor"
    assert ar.match_work_class(t, writes=["Source/A.cpp"]) is None


def test_keyword_is_case_insensitive_and_tools_match_body(env) -> None:
    t = ar.load_route_table(_write(env, _table()))
    assert ar.match_work_class(t, title="Fix the BLUEPRINT graph").id == "ue-editor"
    assert ar.match_work_class(t, body=f"call {MCP}inspect").id == "ue-editor"


def test_covers_forms() -> None:
    assert ar.covers(["Read", "mcp__x__a"], ["mcp__x__"])
    assert ar.covers(["Read"], ["Read"])
    assert ar.covers(["mcp__x__*"], ["mcp__x__a"])
    assert not ar.covers(["Read"], ["Edit"])
    assert not ar.covers(["Read"], ["Read", "Edit"])
    assert not ar.covers(None, ["Read"])
    assert ar.covers(None, [])


def test_tool_less_definition_covers_nothing(env) -> None:
    t = ar.load_route_table(_write(env, _table()))
    assert ar.agent_tool_range("coordinator:notools", t) is None
    assert ar.agent_tool_range("general-purpose", t) is None
    cls = t.work_classes[0]
    assert ar.admit("coordinator:notools", cls, t).verdict == "refuse"


def test_agent_tool_ranges_overrides_definition(env) -> None:
    t = ar.load_route_table(_write(env, _table(agent_tool_ranges={"coordinator:executor": [MCP]})))
    assert ar.agent_tool_range("coordinator:executor", t) == (MCP,)
    assert ar.admit("coordinator:executor", t.work_classes[0], t).verdict == "admit"


def test_definition_tools_are_read_from_frontmatter(env) -> None:
    t = ar.load_route_table(_write(env, _table()))
    assert ar.agent_tool_range(UE, t) == ("Read", f"{MCP}manage_asset", f"{MCP}inspect")
    assert ar.admit(UE, t.work_classes[0], t).verdict == "admit"


def test_admit_redirect_names_target(env) -> None:
    cls = {**_table()["work_classes"][0], "on_mismatch": "redirect"}
    t = ar.load_route_table(_write(env, _table(work_classes=[cls])))
    a = ar.admit("coordinator:executor", t.work_classes[0], t)
    assert (a.verdict, a.target) == ("redirect", UE)
    assert MCP in a.reason


def test_derived_executor_row_takes_class_agent_and_frontmatter_model(env) -> None:
    _write(env, _table())
    rows = [_row()]
    out, routes, notes = ar.route_rows(rows, env)
    assert (out[0].agent_type, out[0].agent_model) == (UE, "sonnet")
    r = routes["C1"]
    assert r.work_class == "ue-editor" and r.primary == (UE, "sonnet") and r.triage is None
    assert notes and "C1" in notes[0]


def test_triage_class_marks_row_triage_first(env) -> None:
    cls = {**_table()["work_classes"][0], "triage_agent_type": UE_TRIAGE}
    _write(env, _table(work_classes=[cls]))
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].triage == (UE_TRIAGE, "haiku")
    assert routes["C1"].to_json()["triage"] == {"agent_type": UE_TRIAGE, "model": "haiku"}


def test_explicit_row_lacking_tools_refuse_raises(env) -> None:
    _write(env, _table(agent_tool_ranges={"coordinator:op-relay": ["Read"]}))
    with pytest.raises(ar.RouteAdmissionError) as exc:
        ar.route_rows([_row(agent_type="coordinator:op-relay")], env)
    assert "C1" in str(exc.value) and "ue-editor" in str(exc.value) and MCP in str(exc.value)


def test_explicit_row_lacking_tools_redirect_repoints_and_notes(env) -> None:
    cls = {**_table()["work_classes"][0], "on_mismatch": "redirect"}
    _write(env, _table(work_classes=[cls], agent_tool_ranges={"coordinator:op-relay": ["Read"]}))
    out, routes, notes = ar.route_rows([_row(agent_type="coordinator:op-relay")], env)
    assert out[0].agent_type == UE and routes["C1"].primary[0] == UE
    assert any("redirected" in n for n in notes)


@pytest.mark.parametrize("on_mismatch", ["refuse", "redirect"])
def test_explicit_executor_row_inside_footprint_is_never_redirected_or_refused(env, on_mismatch) -> None:
    cls = {**_table()["work_classes"][0], "on_mismatch": on_mismatch}
    _write(env, _table(work_classes=[cls]))
    row = _row(agent_type="coordinator:executor")
    out, routes, notes = ar.route_rows([row], env)
    assert out[0] is row and routes == {} and notes == []


def test_metahuman_surface_with_trail_and_test_writes_routes_to_executor(env) -> None:
    cls = {**_table()["work_classes"][0], "match": {"paths": ["Content/*"], "keywords": ["metahuman"]}}
    _write(env, _table(work_classes=[cls]))
    writes = ("docs/research/metahuman/a/trail.yaml", "tests/test_trail.py")
    explicit = _row(writes=writes, title="metahuman audience tag", agent_type="coordinator:executor")
    derived = _row(writes=writes, title="metahuman audience tag")
    for row in (explicit, derived):
        out, routes, _ = ar.route_rows([row], env)
        assert (out[0].agent_type or "coordinator:executor") == "coordinator:executor" and routes == {}


def _keyword_only_metahuman(env) -> None:
    cls = {**_table()["work_classes"][0], "match": {"keywords": ["metahuman"]}}
    _write(env, _table(work_classes=[cls]))


@pytest.mark.parametrize(
    "writes",
    [
        ("docs/research/metahuman/a/trail.yaml", "tests/test_trail.py"),
        ("docs\\research\\metahuman\\trail.md", "tests\\test_trail.py"),
    ],
)
def test_keyword_only_class_leaves_docs_tests_yaml_row_on_executor(env, writes) -> None:
    _keyword_only_metahuman(env)
    derived = _row(writes=writes, title="metahuman audience tag")
    out, routes, notes = ar.route_rows([derived], env)
    assert out[0].agent_type is None and routes == {} and notes == []
    explicit = _row(writes=writes, title="metahuman audience tag", agent_type=UE, agent_model="opus")
    out, routes, notes = ar.route_rows([explicit], env)
    assert out[0].agent_type == EXECUTOR and routes == {} and "outside" in notes[0]


def test_keyword_only_class_still_routes_ue_content_row(env) -> None:
    _keyword_only_metahuman(env)
    for writes in (("Content/MetaHumans/Hero.uasset",), ("Content/Tests/Hero.uasset", "tests/test_t.py"), ("docs/a.yaml", "Source/A.cpp")):
        out, routes, _ = ar.route_rows([_row(writes=writes, title="metahuman hero")], env)
        assert out[0].agent_type == UE and routes["C1"].work_class == "ue-editor"


def test_writes_are_docs_tests_yaml_classifier() -> None:
    assert ar.writes_are_docs_tests_yaml(("a.md", "x/b.yaml", "tests/c.py", "d/test_e.py", "docs/f.png"))
    assert not ar.writes_are_docs_tests_yaml(())
    assert not ar.writes_are_docs_tests_yaml(("a.md", "Content/X.uasset"))
    assert not ar.writes_are_docs_tests_yaml(("docs/X.uasset",))
    assert not ar.writes_are_docs_tests_yaml(("coordinator_core/ipc.py",))


def test_explicit_covering_row_is_admitted_unchanged(env) -> None:
    _write(env, _table())
    row = _row(agent_type=UE, agent_model="opus")
    out, routes, _ = ar.route_rows([row], env)
    assert out[0] == row and routes["C1"].primary == (UE, "opus")


def test_enricher_derived_row_is_untouched(env) -> None:
    _write(env, _table())
    row = _row(writes=("docs/plans/p.md",), title="Blueprint")
    out, routes, _ = ar.route_rows([row], env)
    assert out[0] is row and routes == {}


def test_unmatched_executor_row_takes_default_agent_type(env) -> None:
    _write(env, _table(default_agent_type="coordinator:test-runner"))
    out, routes, _ = ar.route_rows([_row(writes=("src/a.py",))], env)
    assert (out[0].agent_type, out[0].agent_model) == ("coordinator:test-runner", "haiku")
    assert routes == {}


def test_unmatched_executor_row_under_executor_default_is_unchanged(env) -> None:
    _write(env, _table())
    row = _row(writes=("src/a.py",))
    out, routes, notes = ar.route_rows([row], env)
    assert out[0] is row and routes == {} and notes == []


def test_routed_type_with_no_model_raises(env) -> None:
    cls = {**_table()["work_classes"][0], "agent_type": "example-game-repo-control:ue-nomodel"}
    _write(env, _table(work_classes=[cls]))
    with pytest.raises(ar.RouteAdmissionError) as exc:
        ar.route_rows([_row()], env)
    assert "agent_model" in str(exc.value)
    assert "not found in installed plugins" not in str(exc.value)


def test_routed_type_absent_from_installed_plugins_names_the_plugin(env) -> None:
    ghost = "ghost-plugin:ue-ghost"
    cls = {**_table()["work_classes"][0], "agent_type": ghost}
    _write(env, _table(work_classes=[cls], agent_tool_ranges={ghost: [MCP]}))
    with pytest.raises(ar.RouteAdmissionError) as exc:
        ar.route_rows([_row()], env)
    msg = str(exc.value)
    assert "not found in installed plugins (is ghost-plugin installed?)" in msg
    assert "no model pin" not in msg


def test_routed_class_agent_lacking_its_own_required_tools_raises(env) -> None:
    cls = {**_table()["work_classes"][0], "agent_type": "coordinator:executor"}
    _write(env, _table(work_classes=[cls]))
    with pytest.raises(ar.RouteAdmissionError):
        ar.route_rows([_row()], env)


def test_targets_hold_only_covering_types_with_models(env) -> None:
    t = _table(
        default_agent_type="coordinator:executor",
        agent_tool_ranges={"example-game-repo-control:ue-nomodel": [MCP], "x:other": ["Read"]},
    )
    t["work_classes"][0]["triage_agent_type"] = UE_TRIAGE
    _write(env, t)
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].targets == {UE: "sonnet"}


def test_targets_include_other_covering_type(env) -> None:
    t = _table(agent_tool_ranges={"coordinator:executor": [MCP]})
    _write(env, t)
    _, routes, _ = ar.route_rows([_row()], env)
    assert routes["C1"].targets == {UE: "sonnet", "coordinator:executor": "sonnet"}


def test_manifest_row_round_trips_with_and_without_route() -> None:
    plain = ask_contract.ManifestRow("C1", "executor", "sonnet", "b.md", ("a",), 0)
    assert "route" not in plain.to_json()
    assert ask_contract.ManifestRow.from_json(plain.to_json()) == plain
    route = ar.Route("c", (UE, "sonnet"), {UE: "sonnet"}).to_json()
    routed = ask_contract.ManifestRow("C1", UE, "sonnet", "b.md", ("a",), 0, route=route)
    out = json.loads(json.dumps(routed.to_json()))
    assert out["route"]["primary"] == {"agent_type": UE, "model": "sonnet"}
    assert ask_contract.ManifestRow.from_json(out) == routed


def test_repo_root_of_prefers_table_then_git(env, tmp_path: Path) -> None:
    sub = env / "a" / "b"
    sub.mkdir(parents=True)
    _write(env, _table())
    assert ar.repo_root_of(str(sub)) == env
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    assert ar.repo_root_of(str(other)) == other
    assert ar.repo_root_of("") is None


def test_no_subprocess_spawn(env, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    _write(env, _table())
    ar.route_rows([_row(), _row("C2", writes=("src/a.py",))], env)
    ar.repo_root_of(str(env))


def test_fixture_tables_conform_to_doe_schema(env) -> None:
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


def _local_md(repo, body) -> None:
    (repo / "coordinator.local.md").write_text(body, encoding="utf-8")


def _stages(repo, ask: str = "refactor the thing", writes=()) -> dict[str, tuple[str, str]]:
    return ar.resolve_stage_agents(repo, ask_text=ask, writes=writes, defaults=_DEFAULTS)


def test_stage_agents_no_table_no_review_routing_is_defaults(env) -> None:
    assert _stages(env) == {s: (d, "default") for s, d in _DEFAULTS.items()}


def test_stage_agents_table_level_routes_plan_review_only(env) -> None:
    _write(env, _table(stage_agents={"plan_review": GAME}))
    out = _stages(env)
    assert out["plan_review"] == (GAME, "table")
    assert out["plan"] == (_DEFAULTS["plan"], "default")
    assert out["code_review"] == (_DEFAULTS["code_review"], "default")


def test_stage_agents_work_class_wins_over_table(env) -> None:
    table = _table(stage_agents={"code_review": "x:table-reviewer"})
    table["work_classes"][0]["stage_agents"] = {"code_review": GAME}
    _write(env, table)
    assert _stages(env, writes=["Content/A.uasset"])["code_review"] == (GAME, "work-class:ue-editor")
    assert _stages(env, ask="Blueprint rework")["code_review"] == (GAME, "work-class:ue-editor")
    assert _stages(env, ask="docs only")["code_review"] == ("x:table-reviewer", "table")


def test_stage_agents_review_routing_alone_routes_reviews_not_plan(env) -> None:
    _local_md(env, f"---\nreview_routing: {GAME}\n---\nbody\n")
    out = _stages(env)
    assert out["plan_review"] == (GAME, "review_routing")
    assert out["code_review"] == (GAME, "review_routing")
    assert out["plan"] == (_DEFAULTS["plan"], "default")


def test_stage_agents_table_beats_review_routing(env) -> None:
    _local_md(env, f"---\nreview_routing: {GAME}\n---\n")
    _write(env, _table(stage_agents={"code_review": "x:table-reviewer"}))
    out = _stages(env)
    assert out["code_review"] == ("x:table-reviewer", "table")
    assert out["plan_review"] == (GAME, "review_routing")


@pytest.mark.parametrize("value", ["staff-game-dev", "a:b:c", "has space:x"])
def test_malformed_review_routing_raises(env, value) -> None:
    _local_md(env, f"---\nreview_routing: {value}\n---\n")
    with pytest.raises(ar.RouteTableError, match="review_routing"):
        _stages(env)


def test_review_routing_absent_cases_return_none(env) -> None:
    assert ar.read_review_routing(env) is None
    _local_md(env, "---\nother: 1\n---\n")
    assert ar.read_review_routing(env) is None
    _local_md(env, "no frontmatter\nreview_routing: a:b\n")
    assert ar.read_review_routing(env) is None
    _local_md(env, "---\nreview_routing: 'a:b'\n---\n")
    assert ar.read_review_routing(env) == "a:b"


_LIST_ROUTING = (
    "---\nreview_routing:\n"
    "  - paths: ['control/plugin/**', '**/*.Build.cs']\n"
    f"    reviewer: {GAME}\n"
    "  - paths: ['docs/**']\n"
    "    reviewer: x:docs-reviewer\n"
    "---\n"
)


def _claude_local_md(repo, body) -> None:
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / "coordinator.local.md").write_text(body, encoding="utf-8")


def test_review_routing_claude_dir_preferred_over_root(env) -> None:
    _local_md(env, "---\nreview_routing: root:reviewer\n---\n")
    assert ar.read_review_routing(env) == "root:reviewer"
    _claude_local_md(env, "---\nreview_routing: claude:reviewer\n---\n")
    assert ar.read_review_routing(env) == "claude:reviewer"
    _claude_local_md(env, "---\nother: 1\n---\n")
    assert ar.read_review_routing(env) == "root:reviewer"


def test_review_routing_list_form_matches_writes(env) -> None:
    _claude_local_md(env, _LIST_ROUTING)
    assert ar.read_review_routing(env, ["control/plugin/Source/X.cpp"]) == GAME
    assert ar.read_review_routing(env, ["docs/a.md"]) == "x:docs-reviewer"
    assert ar.read_review_routing(env, ["src/other.py"]) is None
    assert ar.read_review_routing(env) is None


def test_review_routing_double_star_matches_root_level_file(env) -> None:
    _claude_local_md(env, _LIST_ROUTING)
    assert ar.read_review_routing(env, ["Foo.Build.cs"]) == GAME
    assert ar.read_review_routing(env, ["Source/Foo.Build.cs"]) == GAME


def test_review_routing_scalar_ignores_writes(env) -> None:
    _claude_local_md(env, f"---\nreview_routing: {GAME}\n---\n")
    assert ar.read_review_routing(env, ["anything.txt"]) == GAME


def test_stage_agents_list_form_routes_by_writes_and_ask_tokens(env) -> None:
    _claude_local_md(env, _LIST_ROUTING)
    out = _stages(env, writes=["control/plugin/A.cpp"])
    assert out["code_review"] == (GAME, "review_routing")
    out = _stages(env, ask="please fix `control/plugin/A.cpp`, then ship")
    assert out["plan_review"] == (GAME, "review_routing")
    assert _stages(env, ask="refactor the thing")["code_review"] == (_DEFAULTS["code_review"], "default")


@pytest.mark.parametrize(
    "body",
    [
        "review_routing:\n  - just-a-string\n",
        "review_routing:\n  - paths: []\n    reviewer: a:b\n",
        "review_routing:\n  - paths: ['x/**']\n    reviewer: nocolon\n",
        "review_routing:\n  - paths: ['x/**']\n",
        "review_routing: 5\n",
    ],
)
def test_malformed_review_routing_list_raises(env, body) -> None:
    _claude_local_md(env, f"---\n{body}---\n")
    with pytest.raises(ar.RouteTableError, match="review_routing"):
        ar.read_review_routing(env, ["x/a.cpp"])


def test_unknown_stage_agents_key_raises(env) -> None:
    _write(env, _table(stage_agents={"planning": GAME}))
    with pytest.raises(ar.RouteTableError, match="stage_agents"):
        ar.load_route_table(env)
    table = _table()
    table["work_classes"][0]["stage_agents"] = {"bogus": GAME}
    _write(env, table)
    with pytest.raises(ar.RouteTableError, match=r"work_classes\[0\]\.stage_agents"):
        ar.load_route_table(env)


EXECUTOR = "coordinator:executor"


def _run_only_table():
    cls = {**_table()["work_classes"][0], "run_only": True}
    return _table(work_classes=[cls])


def _code_row(**kw) -> WaveRow:
    return _row(
        writes=("Content/likeness/extract.py", "Content/tests/test_extract.py"),
        body="Blueprint likeness refactor",
        change_kind="code-edit",
        **kw,
    )


def test_run_only_class_keeps_derived_code_row_on_executor(env) -> None:
    _write(env, _run_only_table())
    rows = [_code_row()]
    out, routes, notes = ar.route_rows(rows, env)
    assert out[0].agent_type is None and routes == {} and notes == []


def test_run_only_class_redirects_explicit_specialist_code_row_with_note(env) -> None:
    _write(env, _run_only_table())
    out, routes, notes = ar.route_rows([_code_row(agent_type=UE, agent_model="opus")], env)
    assert out[0].agent_type == EXECUTOR
    assert routes == {}
    assert len(notes) == 1 and "C1" in notes[0] and "run-only" in notes[0]


def test_run_only_class_leaves_explicit_executor_code_row_untouched(env) -> None:
    _write(env, _run_only_table())
    row = _code_row(agent_type=EXECUTOR)
    out, routes, notes = ar.route_rows([row], env)
    assert out[0] is row and routes == {} and notes == []


def test_run_only_class_still_routes_a_non_authoring_row(env) -> None:
    _write(env, _run_only_table())
    row = _row(writes=("Content/A.uasset",), body="Blueprint", change_kind="doc-edit", agent_type=UE)
    out, routes, _ = ar.route_rows([row], env)
    assert out[0].agent_type == UE and routes["C1"].primary[0] == UE


def test_unmarked_class_still_stamps_specialist_on_code_row(env) -> None:
    _write(env, _table())
    out, routes, _ = ar.route_rows([_code_row()], env)
    assert out[0].agent_type == UE and routes["C1"].work_class == "ue-editor"


# --- routing follows the declared edit footprint -------------------------------------------------

_C7_WRITES = ("docs/trail/a.trail.yaml", "tests/test_x.py")
_UE_TEXT = dict(title="Blueprint likeness gate", body="Touches the Blueprint pipeline")


def test_footprint_outside_class_paths_keeps_derived_row_on_executor(env) -> None:
    _write(env, _table())
    out, routes, notes = ar.route_rows([_row(writes=_C7_WRITES, **_UE_TEXT)], env)
    assert out[0].agent_type is None and routes == {} and notes == []


def test_footprint_outside_class_paths_redirects_explicit_class_agent_with_note(env) -> None:
    _write(env, _table())
    out, routes, notes = ar.route_rows([_row(writes=_C7_WRITES, agent_type=UE, agent_model="opus", **_UE_TEXT)], env)
    assert out[0].agent_type == EXECUTOR and routes == {}
    assert len(notes) == 1 and "C1" in notes[0] and "outside" in notes[0]


def test_footprint_outside_leaves_other_explicit_type_alone(env) -> None:
    _write(env, _table())
    row = _row(writes=_C7_WRITES, agent_type="game-dev:other", **_UE_TEXT)
    out, routes, notes = ar.route_rows([row], env)
    assert out[0] is row and routes == {} and notes == []


def test_footprint_inside_class_paths_still_routes_to_ue_agent(env) -> None:
    _write(env, _table())
    out, routes, _ = ar.route_rows([_row(writes=("Content/A.uasset", "tests/test_x.py"), **_UE_TEXT)], env)
    assert out[0].agent_type == UE and routes["C1"].work_class == "ue-editor"


def test_undeclared_writes_with_keyword_still_routes_to_ue_agent(env) -> None:
    from coordinator_core.ops.dispatch_emit.wave_map import UNDECLARED

    _write(env, _table())
    for writes in ((), UNDECLARED):
        row = WaveRow("C1", "t", "s", writes, [], [], body="Blueprint work")
        out, routes, _ = ar.route_rows([row], env)
        assert out[0].agent_type == UE and routes["C1"].work_class == "ue-editor"


def test_class_without_path_globs_keeps_keyword_routing_for_any_writes(env) -> None:
    cls = {**_table()["work_classes"][0], "match": {"keywords": ["Blueprint"]}}
    _write(env, _table(work_classes=[cls]))
    out, routes, _ = ar.route_rows([_row(writes=("Source/Game/A.cpp", "tests/test_x.py"), **_UE_TEXT)], env)
    assert out[0].agent_type == UE and routes["C1"].work_class == "ue-editor"


def test_footprint_backslash_writes_classify_like_posix(env) -> None:
    _write(env, _table())
    inside = _row(writes=("Content\\A.uasset",), **_UE_TEXT)
    outside = _row(writes=("docs\\trail\\a.trail.yaml", "tests\\test_x.py"), **_UE_TEXT)
    assert ar.route_rows([inside], env)[0][0].agent_type == UE
    assert ar.route_rows([outside], env)[0][0].agent_type is None


def test_run_only_fence_follows_the_declared_footprint(env) -> None:
    _write(env, _run_only_table())
    inside = _code_row()
    outside = _row(writes=_C7_WRITES, change_kind="code-edit", **_UE_TEXT)
    assert ar.run_only_fence(inside, env) is not None
    assert ar.run_only_fence(outside, env) is None
