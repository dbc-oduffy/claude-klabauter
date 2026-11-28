import pytest

from coordinator_core.ops import repo_setup
from coordinator_core.ops.repo_induct._doctrine_fill import fill_doctrine
from coordinator_core.ue_project_detection import declared_project

KEY = "repos.fifa_main"
_TEMPLATE = (
    "# {{PROJECT_NAME}}\n{{GLOBAL_EXTENDS_LINE}}\ntype={{PROJECT_TYPE}} subtypes=[{{SUBTYPES}}]\n{{PROJECT_TYPE_BLOCK}}\n"
)


@pytest.fixture(autouse=True)
def plugin_templates(tmp_path_factory, monkeypatch):
    plugin = tmp_path_factory.mktemp("plugin")
    tdir = plugin / "skills" / "repo-setup" / "templates"
    tdir.mkdir(parents=True)
    (tdir / "CLAUDE.md.template").write_text(_TEMPLATE, encoding="utf-8")
    (tdir / "project-type-block.unreal.template").write_text("UNREAL-BLOCK", encoding="utf-8")
    monkeypatch.setattr(repo_setup, "_plugin_root", lambda: plugin)
    return plugin


def _by_step(records):
    return {r["step"]: r for r in records}


def test_absent_files_created_and_declared(tmp_path):
    recs = _by_step(fill_doctrine(tmp_path, KEY, "unreal"))
    assert recs["claude_md"]["status"] == "done"
    assert recs["coordinator_local_md"]["status"] == "done"
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "# fifa_main" in claude and "type=unreal" in claude and "UNREAL-BLOCK" in claude
    assert declared_project(tmp_path).project_type == "unreal"


def test_claude_md_without_resolvable_templates_fails_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(repo_setup, "_plugin_root", lambda: None)
    recs = _by_step(fill_doctrine(tmp_path, KEY, "python"))
    assert recs["claude_md"]["status"] == "failed"
    assert not (tmp_path / "CLAUDE.md").exists()
    assert recs["coordinator_local_md"]["status"] == "done"


def test_claude_md_with_unsubstituted_token_fails_and_writes_nothing(tmp_path, plugin_templates):
    (plugin_templates / "skills" / "repo-setup" / "templates" / "CLAUDE.md.template").write_text(
        "{{UNKNOWN_TOKEN}}", encoding="utf-8"
    )
    recs = _by_step(fill_doctrine(tmp_path, KEY, "python"))
    assert recs["claude_md"]["status"] == "failed"
    assert not (tmp_path / "CLAUDE.md").exists()


def test_second_run_changes_nothing(tmp_path):
    fill_doctrine(tmp_path, KEY, "python")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    recs = fill_doctrine(tmp_path, KEY, "python")
    assert [r["status"] for r in recs] == ["skipped", "skipped"]
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before


def test_existing_claude_md_byte_identical(tmp_path):
    original = b"# mine\r\n\xc3\xa9 keep\r\n"
    (tmp_path / "CLAUDE.md").write_bytes(original)
    recs = _by_step(fill_doctrine(tmp_path, KEY, "python"))
    assert recs["claude_md"]["status"] == "skipped"
    assert recs["claude_md"]["changed"] == []
    assert (tmp_path / "CLAUDE.md").read_bytes() == original


def test_conflicting_type_refused_without_write(tmp_path):
    local = tmp_path / "coordinator.local.md"
    local.write_bytes(b"---\nproject_type: python\n---\nbody\n")
    before = local.read_bytes()
    recs = _by_step(fill_doctrine(tmp_path, KEY, "unreal"))
    assert recs["coordinator_local_md"]["status"] == "failed"
    assert "project_type_conflict" in recs["coordinator_local_md"]["detail"]
    assert local.read_bytes() == before
    assert declared_project(tmp_path).project_type == "python"


def test_existing_local_md_without_key_gains_it_and_keeps_rest(tmp_path):
    local = tmp_path / "coordinator.local.md"
    local.write_bytes(b"---\r\ntest_cmd: pytest\r\n---\r\nbody\r\n")
    recs = _by_step(fill_doctrine(tmp_path, KEY, "python"))
    assert recs["coordinator_local_md"]["status"] == "done"
    assert local.read_bytes() == b"---\r\nproject_type: python\r\ntest_cmd: pytest\r\n---\r\nbody\r\n"
    assert declared_project(tmp_path).project_type == "python"


def test_local_md_without_frontmatter_gets_one(tmp_path):
    local = tmp_path / "coordinator.local.md"
    local.write_bytes(b"# notes\n")
    fill_doctrine(tmp_path, KEY, "python")
    assert local.read_bytes() == b"---\nproject_type: python\n---\n\n# notes\n"
    assert declared_project(tmp_path).project_type == "python"


def test_dot_claude_local_md_is_the_file_extended(tmp_path):
    (tmp_path / ".claude").mkdir()
    local = tmp_path / ".claude" / "coordinator.local.md"
    local.write_bytes(b"---\nfoo: 1\n---\n")
    fill_doctrine(tmp_path, KEY, "python")
    assert not (tmp_path / "coordinator.local.md").exists()
    assert declared_project(tmp_path).source_file == local


def test_unsafe_project_type_refused_and_nothing_declared(tmp_path):
    recs = _by_step(fill_doctrine(tmp_path, KEY, "x\n---\nevil: 1"))
    assert recs["coordinator_local_md"]["status"] == "failed"
    assert not (tmp_path / "coordinator.local.md").exists()


def test_missing_root_fails_both_steps(tmp_path):
    recs = fill_doctrine(tmp_path / "nope", KEY, "python")
    assert [r["status"] for r in recs] == ["failed", "failed"]
