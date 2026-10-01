"""Tests for coordinator_core._fleet_names."""

from coordinator_core import _fleet_names as fn


def _env(tmp_path, monkeypatch, registry="", local=""):
    home = tmp_path / "settings"
    (home / "machine-local").mkdir(parents=True)
    if registry:
        (home / "machine-local" / "registry.toml").write_text(registry)
    if local:
        (home / "machine-local" / "registry.local.toml").write_text(local)
    claude = tmp_path / "claude"
    claude.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    return claude


def test_posix_windows_working_repos_and_empty(tmp_path, monkeypatch):
    _env(
        tmp_path,
        monkeypatch,
        registry='"repos.a" = "/x/Alpha"\n"repos.b" = "C:\\\\w\\\\Beta\\\\"\n"repos.c" = ""\n',
        local='[engine.working_repos]\nz = "/y/Gamma"\n',
    )
    assert fn.registry_repo_names() == ("Alpha", "Beta", "Gamma")


def test_dedup_by_case_first_spelling_wins(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, registry='"repos.a" = "/x/alpha"\n"repos.b" = "/x/Beta"\n')
    assert fn.sibling_repo_names(("ALPHA", "Delta")) == ("ALPHA", "Delta", "Beta")


def test_missing_registry(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch)
    assert fn.registry_repo_names() == ()
    assert fn.sibling_repo_names(("Q",)) == ("Q",)
    assert fn.doctrine_repo_name() is None


def test_unparseable_registry(tmp_path, monkeypatch):
    _env(tmp_path, monkeypatch, registry="= not toml [")
    assert fn.registry_repo_names() == ()


def test_doctrine_via_pointer(tmp_path, monkeypatch):
    claude = _env(tmp_path, monkeypatch)
    repo = tmp_path / "Doc"
    repo.mkdir()
    (repo / ".coordinator-dev-repo").write_text("")
    (claude / ".coordinator-content-root").write_text(str(repo) + "\n")
    assert fn.doctrine_repo_name() == "Doc"


def test_doctrine_via_sentinel_scan(tmp_path, monkeypatch):
    plain = tmp_path / "Plain"
    plain.mkdir()
    doc = tmp_path / "Doc2"
    doc.mkdir()
    (doc / ".coordinator-dev-repo").write_text("")
    _env(tmp_path, monkeypatch, registry=f'"repos.p" = "{plain.as_posix()}"\n"repos.d" = "{doc.as_posix()}"\n')
    assert fn.doctrine_repo_name() == "Doc2"


def test_pointer_without_sentinel_falls_through(tmp_path, monkeypatch):
    doc = tmp_path / "Scanned"
    doc.mkdir()
    (doc / ".coordinator-dev-repo").write_text("")
    claude = _env(tmp_path, monkeypatch, registry=f'"repos.d" = "{doc.as_posix()}"\n')
    repo = tmp_path / "NoSentinel"
    repo.mkdir()
    (claude / ".coordinator-content-root").write_text(str(repo))
    assert fn.doctrine_repo_name() == "Scanned"


def test_no_subprocess_import():
    import ast

    with open(fn.__file__, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "subprocess" not in imported
