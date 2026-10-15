"""block_push_pr_non_owned_remote: push and PR toward a declared non-owned remote are denied."""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import block_push_pr_non_owned_remote as guard

_CONFIG = """\
[remote "origin"]
\turl = git@github.com:mine/engine.git
[remote "upstream"]
\turl = https://github.com/Theirs/Repo.git
[remote "fork"]
\turl = https://github.com/mine/fork.git
\tpushurl = ssh://git@github.com/theirs/repo
[branch "feat"]
\tremote = upstream
"""


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "config").write_text(_CONFIG, encoding="utf-8")
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


@pytest.fixture
def registry(tmp_path, monkeypatch):
    reg = tmp_path / "registry"
    reg.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))

    def arm(entries):
        listed = ", ".join(f'"{e}"' for e in entries)
        (reg / "registry.local.toml").write_text(
            f"[policy]\nnon_owned_remotes = [{listed}]\n", encoding="utf-8"
        )

    return arm


def _run(cmd: str, repo, tool: str = "Bash"):
    return guard.check({"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(repo)})


def _denied(out) -> bool:
    return (
        out is not None
        and out["hookSpecificOutput"]["permissionDecision"] == "deny"
        and guard.ANCHOR in out["hookSpecificOutput"]["permissionDecisionReason"]
        and guard.WIKI_PATH in out["hookSpecificOutput"]["permissionDecisionReason"]
    )


def _set_branch(repo, name):
    (repo / ".git" / "HEAD").write_text(f"ref: refs/heads/{name}\n", encoding="utf-8")


@pytest.mark.parametrize(
    "cmd",
    [
        "git push upstream main",
        "git push -u upstream feat",
        "git push --force upstream main",
        "git push https://github.com/theirs/repo.git main",
        "git commit -m x && git push upstream main",
        "git push fork main",
        "git push $R main",
        "gh pr create -R theirs/repo",
        "gh pr create --repo theirs/repo --fill",
    ],
)
def test_denied_when_armed(repo, registry, cmd):
    registry(["github.com/theirs/"])
    assert _denied(_run(cmd, repo))


@pytest.mark.parametrize("tool,cmd", [("PowerShell", "& git push upstream main"), ("PowerShell", "git.exe push upstream main")])
def test_powershell_forms_denied(repo, registry, tool, cmd):
    registry(["github.com/theirs/"])
    assert _denied(_run(cmd, repo, tool))


def test_push_with_no_remote_follows_branch_remote(repo, registry):
    registry(["github.com/theirs/repo"])
    _set_branch(repo, "feat")
    assert _denied(_run("git push", repo))


def test_push_with_no_remote_defaults_to_origin(repo, registry):
    registry(["github.com/mine/engine"])
    assert _denied(_run("git push", repo))


def test_dash_c_dir_names_the_repo(tmp_path, repo, registry):
    registry(["github.com/theirs/"])
    assert _denied(_run(f"git -C {repo} push upstream main", tmp_path))


def test_pr_create_defaults_to_first_present_remote(repo, registry):
    registry(["github.com/theirs/repo"])
    assert _denied(_run("gh pr create --fill", repo))


def test_pr_create_default_remote_owned_is_allowed(repo, registry):
    registry(["github.com/other/"])
    assert _run("gh pr create --fill", repo) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "git push --dry-run upstream main",
        "git fetch upstream",
        "git pull upstream main",
        "gh pr view 3",
        "gh pr list",
        "gh pr checkout 3",
        'echo "git push upstream main"',
        'grep "gh pr create" notes.txt',
        "git commit -m x",
    ],
)
def test_allowed_when_armed(repo, registry, cmd):
    registry(["github.com/theirs/"])
    assert _run(cmd, repo) is None


def test_owned_remote_pr_is_allowed(repo, registry):
    registry(["github.com/theirs/"])
    assert _run("gh pr create -R mine/engine", repo) is None


def test_entry_matches_whole_segment_only(repo, registry):
    registry(["github.com/theirs/rep"])
    assert _run("git push upstream main", repo) is None


@pytest.mark.parametrize("cmd", ["git push upstream main", "gh pr create -R theirs/repo"])
def test_off_with_no_registry_entry(repo, registry, cmd):
    assert _run(cmd, repo) is None


def test_off_with_empty_list(repo, registry):
    registry([])
    assert _run("git push upstream main", repo) is None


def test_host_port_entry(repo, registry):
    registry(["p4.example.com:1666"])
    assert _denied(_run("git push ssh://git@P4.example.com:1666/depot main", repo))


def test_unreadable_registry_denies(repo, monkeypatch):
    from coordinator_core import machine_resolver

    def boom():
        raise OSError("unreadable")

    monkeypatch.setattr(machine_resolver, "registry_dir", boom)
    assert _denied(_run("git push upstream main", repo))
    assert _run("git status", repo) is None


def test_unreadable_registry_leaves_non_push_commands_alone(repo, monkeypatch):
    from coordinator_core import machine_resolver

    monkeypatch.setattr(machine_resolver, "registry_dir", lambda: (_ for _ in ()).throw(OSError("x")))
    assert _run("gh pr view 3", repo) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("git@GitHub.com:Org/Repo.git", "github.com/org/repo"),
        ("https://user@github.com/org/repo.git", "github.com/org/repo"),
        ("ssh://git@github.com/org/repo", "github.com/org/repo"),
        ("p4.example.com:1666", "p4.example.com:1666"),
    ],
)
def test_normalize_remote(raw, expected):
    assert guard.normalize_remote(raw) == expected
