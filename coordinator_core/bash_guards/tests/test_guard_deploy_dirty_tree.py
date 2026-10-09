import subprocess

import pytest

from coordinator_core.bash_guards import guard_deploy_dirty_tree as g


def _payload(cmd, cwd, tool="Bash"):
    return {"tool_name": tool, "tool_input": {"command": cmd}, "cwd": str(cwd)}


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


@pytest.fixture
def no_git(monkeypatch):
    def boom(_cwd):
        raise AssertionError("git work on a command that is not a deploy")

    monkeypatch.setattr(g, "_dirty_paths", boom)


DEPLOYS = [
    "firebase deploy",
    "firebase deploy --only hosting",
    "npx firebase deploy",
    "npx --yes firebase deploy",
    "vercel deploy",
    "vercel --prod",
    "netlify deploy --prod",
    "npm run deploy",
    "npm run deploy:prod",
    "yarn deploy",
    "wrangler deploy",
    "wrangler publish",
    "cd x && firebase deploy",
]


@pytest.mark.parametrize("cmd", DEPLOYS)
def test_deploy_with_dirty_tree_denies(repo, cmd):
    (repo / "peer.txt").write_text("x")
    out = g.check(_payload(cmd, repo))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "peer.txt" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_powershell_tool_name_matches(repo):
    (repo / "peer.txt").write_text("x")
    assert g.check(_payload("firebase deploy", repo, tool="PowerShell"))


def test_tracked_modification_denies(repo):
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / "a.txt").write_text("1")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "c"], check=True)
    assert g.check(_payload("vercel deploy", repo)) is None
    (repo / "a.txt").write_text("2")
    assert g.check(_payload("vercel deploy", repo))


def test_clean_tree_passes(repo):
    assert g.check(_payload("firebase deploy", repo)) is None


def test_ignored_files_pass(repo):
    (repo / ".gitignore").write_text("build/\n")
    (repo / "build").mkdir()
    (repo / "build" / "o.js").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        check=True,
    )
    assert g.check(_payload("firebase deploy", repo)) is None


def test_path_list_is_capped(repo):
    for i in range(14):
        (repo / f"f{i:02}.txt").write_text("x")
    reason = g.check(_payload("firebase deploy", repo))["hookSpecificOutput"]["permissionDecisionReason"]
    assert "(+4 more)" in reason and "f09.txt" in reason and "f10.txt" not in reason


def test_not_a_git_repo_passes(tmp_path):
    assert g.check(_payload("firebase deploy", tmp_path)) is None


def test_git_check_exception_passes(monkeypatch, tmp_path):
    def boom(_cwd):
        raise RuntimeError("x")

    monkeypatch.setattr(g, "_dirty_paths", boom)
    assert g.check(_payload("firebase deploy", tmp_path)) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "ls -la",
        "git status",
        "npm run build",
        "npm install",
        "firebase login",
        "vercel logs",
        "echo firebase deploy",
        'echo "firebase deploy"',
        "grep -rn 'npm run deploy' docs",
        "git commit -m 'wrangler deploy fix'",
        "cat <<'EOF'\nfirebase deploy\nEOF",
    ],
)
def test_unrelated_or_mentioned_command_passes_without_git(no_git, cmd):
    assert g.check(_payload(cmd, "/nonexistent")) is None


def test_other_tool_name_passes(no_git):
    assert g.check(_payload("firebase deploy", "/x", tool="Read")) is None
