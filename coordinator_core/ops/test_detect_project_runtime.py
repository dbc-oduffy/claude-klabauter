"""
Characterization tests for coordinator_core.ops.detect_project_runtime.

Golden-oracle corpus captured via manual filesystem fixtures — see
docs/plans/2026-07-16-bash-clean-slate-residual-migration.md port notes.

Port of: detect-project-runtime.sh (example-doctrine-repo b5a4192c, 2026-07-20)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from coordinator_core.ops.detect_project_runtime import main, render, scan


def _touch(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Empty / negative corpus
# ---------------------------------------------------------------------------


def test_empty_dir_no_markers(tmp_path):
    assert scan(str(tmp_path)) == []
    rendered = render(str(tmp_path), [])
    assert rendered == f"Detected: no known stack markers (script saw {tmp_path})."


def test_main_always_exits_0_on_empty_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no known stack markers" in out


# ---------------------------------------------------------------------------
# Node — lockfile precedence + multiple-lockfile join order
# ---------------------------------------------------------------------------


def test_node_no_lockfile(tmp_path):
    _touch(tmp_path / "package.json")
    assert scan(str(tmp_path)) == ["Node (no lockfile detected)"]


def test_node_single_lockfile_yarn(tmp_path):
    _touch(tmp_path / "package.json")
    _touch(tmp_path / "yarn.lock")
    assert scan(str(tmp_path)) == ["Node (yarn)"]


def test_node_multiple_lockfiles_fixed_precedence_order(tmp_path):
    _touch(tmp_path / "package.json")
    _touch(tmp_path / "yarn.lock")
    _touch(tmp_path / "package-lock.json")
    assert scan(str(tmp_path)) == ["Node (multiple lockfiles: yarn,npm)"]


def test_node_all_four_lockfiles_bun_pnpm_yarn_npm_order(tmp_path):
    _touch(tmp_path / "package.json")
    _touch(tmp_path / "package-lock.json")
    _touch(tmp_path / "yarn.lock")
    _touch(tmp_path / "pnpm-lock.yaml")
    _touch(tmp_path / "bun.lockb")
    assert scan(str(tmp_path)) == ["Node (multiple lockfiles: bun,pnpm,yarn,npm)"]


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def test_python_pyproject_takes_precedence_over_requirements(tmp_path):
    _touch(tmp_path / "pyproject.toml")
    _touch(tmp_path / "requirements.txt")
    assert scan(str(tmp_path)) == ["Python (pyproject.toml — poetry/pdm/uv/pip)"]


def test_python_requirements_glob(tmp_path):
    _touch(tmp_path / "requirements-dev.txt")
    assert scan(str(tmp_path)) == ["Python (requirements.txt — pip)"]


# ---------------------------------------------------------------------------
# Rust / Go / Ruby / Make / Docker / CI — simple presence markers
# ---------------------------------------------------------------------------


def test_rust_go_ruby_make_all_fire_in_declared_order(tmp_path):
    _touch(tmp_path / "Cargo.toml")
    _touch(tmp_path / "go.mod")
    _touch(tmp_path / "Gemfile")
    _touch(tmp_path / "Makefile")
    assert scan(str(tmp_path)) == [
        "Rust (cargo)",
        "Go (modules)",
        "Ruby (bundler)",
        "Make (build entry)",
    ]


def test_docker_and_compose_both_fire(tmp_path):
    _touch(tmp_path / "Dockerfile")
    _touch(tmp_path / "docker-compose.yml")
    assert scan(str(tmp_path)) == ["Docker (image build)", "Docker Compose (multi-service)"]


def test_github_actions_requires_yaml_file_present_not_just_dir(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    assert scan(str(tmp_path)) == []


def test_github_actions_and_gitlab_ci_both_fire(tmp_path):
    _touch(tmp_path / ".github" / "workflows" / "ci.yml")
    _touch(tmp_path / ".gitlab-ci.yml")
    assert scan(str(tmp_path)) == ["GitHub Actions CI", "GitLab CI"]


# ---------------------------------------------------------------------------
# Secrets — non-git context (env_tracked defaults to True/"unknown")
# ---------------------------------------------------------------------------


def test_secrets_infisical_alone_outside_git_detects_nothing(tmp_path):
    # Negative-spec: bash oracle's env_tracked=1 default for non-git contexts
    # means bare Infisical-with-no-.env.example is silent outside a repo.
    _touch(tmp_path / "infisical.json")
    assert scan(str(tmp_path)) == []


def test_secrets_env_example_alone_outside_git_fires(tmp_path):
    _touch(tmp_path / ".env.example")
    assert scan(str(tmp_path)) == ["Secrets: .env (template only — .env.example present)"]


def test_secrets_both_outside_git_only_env_example_line_fires(tmp_path):
    _touch(tmp_path / "infisical.json")
    _touch(tmp_path / ".env.example")
    assert scan(str(tmp_path)) == ["Secrets: .env (template only — .env.example present)"]


# ---------------------------------------------------------------------------
# Secrets — inside a git worktree (env_tracked derived from `git ls-files`)
# ---------------------------------------------------------------------------


def _git_init_commit(path: Path, *files: str) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "-c", "user.email=a@a.com", "-c", "user.name=a", "add", *files], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@a.com", "-c", "user.name=a", "commit", "-q", "-m", "x"],
        cwd=path,
        check=True,
    )


def test_secrets_infisical_git_repo_no_env_tracked_fires(tmp_path):
    _touch(tmp_path / "infisical.json")
    _git_init_commit(tmp_path, "infisical.json")
    assert scan(str(tmp_path)) == ["Secrets: Infisical (no .env files tracked)"]


def test_secrets_infisical_git_repo_env_tracked_suppresses_infisical_line(tmp_path):
    _touch(tmp_path / "infisical.json")
    (tmp_path / ".env").write_text("secret=1\n", encoding="utf-8")
    _git_init_commit(tmp_path, "infisical.json", ".env")
    lines = scan(str(tmp_path))
    assert lines == []


def test_secrets_both_infisical_and_env_example_git_untracked_env_lists_both(tmp_path):
    # .env.example is present on disk (filesystem check) but NOT git-committed
    # here — only infisical.json is tracked, so env_tracked stays False.
    # Condition 2 ("Secrets: .env (template only...)") requires
    # `!infisical_present || env_tracked` — both false here — so it does NOT
    # fire; only the Infisical bullet and the "both signals" bullet do. This
    # is the bash oracle's own branch shape, not an approximation.
    _touch(tmp_path / "infisical.json")
    _touch(tmp_path / ".env.example")
    _git_init_commit(tmp_path, "infisical.json")
    assert scan(str(tmp_path)) == [
        "Secrets: Infisical (no .env files tracked)",
        "Secrets: .env.example also present (legacy template)",
    ]


def test_secrets_env_example_git_tracked_counts_as_a_tracked_env_file(tmp_path):
    # Negative-spec quirk (preserved verbatim from the bash oracle): the
    # tracked-file regex `^\.env$|^\.env\.` also matches ".env.example"
    # itself, so git-committing ONLY .env.example (no actual .env) already
    # sets env_tracked=True — suppressing the Infisical bullet even though
    # no real .env secret file is tracked.
    _touch(tmp_path / "infisical.json")
    _touch(tmp_path / ".env.example")
    _git_init_commit(tmp_path, "infisical.json", ".env.example")
    assert scan(str(tmp_path)) == ["Secrets: .env (template only — .env.example present)"]


# ---------------------------------------------------------------------------
# Render shape
# ---------------------------------------------------------------------------


def test_render_shape_with_hits(tmp_path):
    rendered = render(str(tmp_path), ["Rust (cargo)", "Go (modules)"])
    base = tmp_path.name
    assert rendered == (
        f"Detected runtime profile for {base}:\n"
        "  - Rust (cargo)\n"
        "  - Go (modules)\n"
        "\n"
        "Notes:\n"
        "  - This is advisory context for repo-setup. Confirm with the PM before relying on it."
    )
