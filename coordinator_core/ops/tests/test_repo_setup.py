"""
coordinator_core/ops/tests/test_repo_setup.py -- the "repo.setup" op.

Subject: `coordinator_core.ops.repo_setup.handle`, called directly over `tmp_path` git repos with the
plugin root, machine profile, claude home and hook-bin resolver pinned to sandbox values.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import repo_setup as mod

ENGINE_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"
VERDICT_KEYS = {"created", "already_existed", "open", "needs_attention"}


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _snapshot(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git/" not in p.relative_to(root).as_posix() + "/"
        and not p.relative_to(root).as_posix().startswith(".git/")
    }


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude_home))
    monkeypatch.delenv("COORDINATOR_SETTINGS_HOME", raising=False)

    plugin = tmp_path / "plugin"
    templates = plugin / "skills" / "repo-setup" / "templates"
    templates.mkdir(parents=True)
    (templates / "CLAUDE.md.template").write_text(
        "# {{PROJECT_NAME}}\n{{GLOBAL_EXTENDS_LINE}}\n{{PROJECT_TYPE}} {{SUBTYPES}}\n{{PROJECT_TYPE_BLOCK}}\n",
        encoding="utf-8",
    )
    (templates / "README.md.template").write_text("# [PROJECT_NAME]\nUpdated [DATE]\n", encoding="utf-8")
    (templates / "gitignore.project.template").write_text(
        "# managed\n.coordinator-local/\nscratch/\n", encoding="utf-8"
    )
    tripwires = plugin / "tests" / "templates"
    tripwires.mkdir(parents=True)
    (tripwires / "test_foreign_platform_paths.py").write_text("# tripwire\n", encoding="utf-8")

    monkeypatch.setattr(mod, "_plugin_root", lambda: plugin)
    monkeypatch.setattr(mod, "_machine_profile", lambda: "consumer")
    monkeypatch.setattr(mod, "_claude_home_env", lambda: {"CLAUDE_CONFIG_DIR": str(claude_home)})
    monkeypatch.setattr("coordinator_core.machine_resolver.merged_flat_registry", lambda: {})

    ghi = mod._git_hook_install()
    assert ghi is not None
    monkeypatch.setattr(ghi, "_resolve_coord_bin", lambda bin_dir, script, **kw: str(ENGINE_BIN))

    def _fleet_forbidden(*a, **k):
        raise AssertionError("ensure_hooks_fleet must never run from repo.setup")

    monkeypatch.setattr(ghi, "ensure_hooks_fleet", _fleet_forbidden)
    monkeypatch.setattr(ghi, "_registry_repo_roots", _fleet_forbidden)
    return {"claude_home": claude_home, "ghi": ghi}


def test_fresh_root_returns_four_arrays_and_creates_files(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    verdict = mod.handle({"root": str(repo)})
    assert set(verdict) == VERDICT_KEYS
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8").startswith("# repo")
    assert (repo / "docs" / "README.md").is_file()
    assert (repo / "docs" / "plans").is_dir()
    assert ".coordinator-local/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert (repo / ".git" / "hooks" / "prepare-commit-msg").is_file()
    steps = {i["step"] for i in verdict["created"]}
    assert {"detect", "render", "gitignore", "prepare-commit-msg"} <= steps
    assert "register-repo" not in {i["step"] for i in verdict["open"]}


def test_rerun_on_onboarded_repo_creates_nothing(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    mod.handle({"root": str(repo)})
    before = _snapshot(repo)
    second = mod.handle({"root": str(repo)})
    assert second["created"] == []
    assert _snapshot(repo) == before


def test_existing_claude_md_is_not_clobbered(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    (repo / "CLAUDE.md").write_text("mine\n", encoding="utf-8")
    verdict = mod.handle({"root": str(repo)})
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8") == "mine\n"
    assert any(i["step"] == "render" and i["path"] == "CLAUDE.md" for i in verdict["already_existed"])


def test_gitignore_appends_only_missing_lines(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    (repo / ".gitignore").write_text("node_modules/\nscratch/\n", encoding="utf-8")
    mod.handle({"root": str(repo)})
    lines = (repo / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[:2] == ["node_modules/", "scratch/"]
    assert lines.count("scratch/") == 1
    assert ".coordinator-local/" in lines


@pytest.mark.parametrize(
    "answers,bad_key",
    [
        ({"bogus": 1}, "bogus"),
        ({"project_type": 5}, "project_type"),
        ('{"cross_platform": "yes"}', "cross_platform"),
        ("not json", "$"),
    ],
)
def test_bad_answers_refuse_and_write_nothing(tmp_path, sandbox, answers, bad_key):
    repo = _git_init(tmp_path / "repo")
    before = _snapshot(repo)
    hooks_before = sorted(p.name for p in (repo / ".git" / "hooks").iterdir())
    verdict = mod.handle({"root": str(repo), "answers": answers})
    assert verdict["created"] == [] and verdict["open"] == []
    assert [i["key"] for i in verdict["needs_attention"]] == [bad_key]
    assert _snapshot(repo) == before
    assert sorted(p.name for p in (repo / ".git" / "hooks").iterdir()) == hooks_before


def test_good_answers_drive_the_render(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    mod.handle({"root": str(repo), "answers": {"project_name": "Zed", "project_type": "web-dev"}})
    assert (repo / "CLAUDE.md").read_text(encoding="utf-8").startswith("# Zed")
    assert "project_type: web-dev" in (repo / "coordinator.local.md").read_text(encoding="utf-8")


def test_claude_home_root_is_refused_and_untouched(tmp_path, sandbox):
    home = sandbox["claude_home"]
    _git_init(home)
    before = _snapshot(home)
    verdict = mod.handle({"root": str(home)})
    assert verdict["created"] == [] and verdict["needs_attention"][0]["step"] == "root"
    assert _snapshot(home) == before


@pytest.mark.parametrize("root", [None, "relative/dir", "missing"])
def test_unusable_root_is_refused(tmp_path, sandbox, root):
    value = str(tmp_path / "nope") if root == "missing" else root
    verdict = mod.handle({"root": value} if value is not None else {})
    assert verdict["needs_attention"][0]["key"] == "root"
    assert verdict["created"] == []


def test_non_git_directory_is_refused(tmp_path, sandbox):
    plain = tmp_path / "plain"
    plain.mkdir()
    verdict = mod.handle({"root": str(plain)})
    assert verdict["needs_attention"][0]["step"] == "root"
    assert list(plain.iterdir()) == []


def test_no_claude_process_is_spawned(tmp_path, sandbox, monkeypatch):
    repo = _git_init(tmp_path / "repo")
    argvs: list = []
    real_popen = subprocess.Popen

    class _Recording(real_popen):
        def __init__(self, args, *a, **k):
            argvs.append(args if isinstance(args, (list, tuple)) else [args])
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", _Recording)
    mod.handle({"root": str(repo)})
    assert argvs, "the wrapper must observe the git spawns, or the assertion is vacuous"
    basenames = {os.path.basename(str(a[0])).lower() for a in argvs}
    assert not basenames & {"claude", "claude.exe"}


def test_hook_install_touches_only_the_target_repo(tmp_path, sandbox):
    repo_a = _git_init(tmp_path / "a")
    repo_b = _git_init(tmp_path / "b")
    hooks_b = repo_b / ".git" / "hooks"
    (hooks_b / "prepare-commit-msg").write_text("#!/bin/sh\n# stale coordinator-hook-gen: 1\n", encoding="utf-8")
    (hooks_b / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in hooks_b.iterdir() if p.is_file()}
    config_before = (repo_b / ".git" / "config").read_bytes()

    verdict = mod.handle({"root": str(repo_a)})

    assert (repo_a / ".git" / "hooks" / "prepare-commit-msg").is_file()
    assert {p.name: p.read_bytes() for p in hooks_b.iterdir() if p.is_file()} == before
    assert (repo_b / ".git" / "config").read_bytes() == config_before
    assert any(i["step"] == "prepare-commit-msg" for i in verdict["created"])


def test_existing_foreign_pre_commit_hook_is_left_untouched(tmp_path, sandbox):
    repo = _git_init(tmp_path / "repo")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    verdict = mod.handle({"root": str(repo)})
    assert hook.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"
    assert any(i["step"] == "pre-commit" for i in verdict["needs_attention"])


def test_failing_step_is_reported_and_the_rest_still_run(tmp_path, sandbox, monkeypatch):
    repo = _git_init(tmp_path / "repo")

    def _boom(ctx):
        raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_STEPS", (_boom, mod._step_gitignore))
    verdict = mod.handle({"root": str(repo)})
    assert any("boom" in i["detail"] for i in verdict["needs_attention"])
    assert (repo / ".gitignore").is_file()


def test_register_repo_is_an_open_item_with_the_command_on_author_box(tmp_path, sandbox, monkeypatch):
    repo = _git_init(tmp_path / "my-repo")
    monkeypatch.setattr(mod, "_machine_profile", lambda: "author")
    monkeypatch.setattr("coordinator_core.install.scaffold_structure.scaffold_canonical_structure",
                        lambda *a, **k: type("R", (), {"created_dirs": 0, "created_files": 0,
                                                        "created_readmes": 0, "created_gitkeeps": 0})())
    monkeypatch.setattr("coordinator_core.ops.setup_rag_decision.setup_rag_decision", lambda argv: 0)
    verdict = mod.handle({"root": str(repo), "answers": {"register_repo": True}})
    item = next(i for i in verdict["open"] if i["step"] == "register-repo")
    assert "register-repo --path" in item["detail"] and "repos.my_repo" in item["detail"]
