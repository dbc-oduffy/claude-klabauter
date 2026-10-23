"""Whole-filesystem scan deny: root-shaped search roots deny, scoped searches allow."""

import pytest

from coordinator_core.bash_guards.block_whole_filesystem_scan import check as _check
from coordinator_core.guard_messages.testing import envelope_of


def check(*args, **kwargs):
    return envelope_of(_check(*args, **kwargs))


BS = chr(92)

DENY = [
    "find / -path *MetaHumanCharacterEditor*",
    "find / -iname *template_uv*",
    "find / -path /proc -prune -o -name plan-tasks.schema.json -print",
    "find -L / -name x",
    "find C:/ -name x",
    "find 'C:" + BS + "' -name x",
    "find C:" + BS + " -name x",
    "find /c -name x",
    "find /c/ -name x",
    "find /x -name x",
    "find X:" + BS + " -name x",
    "find ~ -name x",
    "find ~/ -name x",
    "cd a && find / -name x | head",
    "rg foo /",
    "rg -g '*.py' foo C:/",
    "rg -e foo /",
    "rg foo ~",
    "grep -r foo /",
    "grep -rn foo ~",
    "grep -R foo /c/",
    "grep --recursive foo /",
    "fd foo /",
    "fd foo C:/",
]

ALLOW = [
    "find C:/claude-klabauter -name x",
    "find . -name x",
    "find /x/foo -name x",
    "find . -path /x/y",
    "find . -name /",
    "find . -path / -prune",
    "grep -r foo .",
    "grep foo /",
    "grep -E '^[A-Z_0-9]*=' file.txt",
    "rg / src",
    "rg foo",
    "fd / src",
    "fd /",
    "grep -r / src",
    "echo find /",
]


def _run(cmd, tool="Bash"):
    return check({"tool_name": tool, "tool_input": {"command": cmd}})


@pytest.mark.parametrize("cmd", DENY)
def test_denies_root_scan(cmd):
    out = _run(cmd)
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("cmd", ALLOW)
def test_allows_scoped(cmd):
    assert _run(cmd) is None


def test_powershell_tool_is_matched():
    assert _run("find / -name x", tool="PowerShell")

def _repo(tmp_path, heavy="scratch", git_file=False):
    root = tmp_path / "repo"
    root.mkdir()
    if git_file:
        (root / ".git").write_text("gitdir: elsewhere")
    else:
        (root / ".git").mkdir()
    if heavy:
        (root / heavy).mkdir()
    (root / "src").mkdir()
    return root


def _run_cwd(cmd, cwd):
    return check({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": str(cwd)})


def _reason(out):
    return out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("heavy", ["scratch", "data", "toolchains", "node_modules"])
def test_heavy_tree_denies_recursive_grep_at_repo_root(tmp_path, heavy):
    root = _repo(tmp_path, heavy)
    for cmd in ("grep -rn foo .", "grep -r foo", f"cd {root.as_posix()} && grep -R foo .", f"grep -r foo {root.as_posix()}"):
        out = _run_cwd(cmd, root)
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
        assert heavy in _reason(out) and "--exclude-dir=" in _reason(out)


def test_heavy_tree_allows_covered_scoped_or_non_repo(tmp_path):
    root = _repo(tmp_path)
    (root / "data").mkdir()
    for cmd in (
        "grep -rn --exclude-dir=scratch --exclude-dir=data foo .",
        "grep -r --exclude-dir={scratch,data} foo .",
        "grep -r --exclude-dir scratch --exclude-dir 'da*' foo .",
        "grep -r foo src",
        "grep foo .",
        "rg foo .",
    ):
        assert _run_cwd(cmd, root) is None, cmd
    bare = tmp_path / "nogit"
    bare.mkdir()
    (bare / "scratch").mkdir()
    assert _run_cwd("grep -rn foo .", bare) is None


def test_heavy_tree_partial_cover_still_denies_naming_only_the_uncovered(tmp_path):
    root = _repo(tmp_path)
    (root / "data").mkdir()
    out = _run_cwd("grep -rn --exclude-dir=scratch foo .", root)
    assert out and "data" in _reason(out) and "walks scratch" not in _reason(out)


def test_heavy_tree_git_file_counts_as_repo_root(tmp_path):
    root = _repo(tmp_path, git_file=True)
    assert _run_cwd("grep -rn foo .", root)


def test_heavy_tree_msys_and_drive_spellings_of_the_root(tmp_path):
    root = _repo(tmp_path)
    posix = root.as_posix()
    spellings = [posix]
    if len(posix) > 2 and posix[1] == ":":
        spellings.append("/" + posix[0].lower() + posix[2:])
    for sp in spellings:
        assert _run_cwd(f"grep -r foo {sp}", tmp_path), sp


def test_heavy_tree_unknown_cwd_allows_relative_target():
    assert _run("grep -rn foo .") is None
    assert _run("grep -r foo") is None


def test_heavy_tree_unreadable_root_allows(tmp_path, monkeypatch):
    root = _repo(tmp_path)

    def boom(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr("coordinator_core.bash_guards.block_whole_filesystem_scan.os.scandir", boom)
    assert _run_cwd("grep -rn foo .", root) is None


def test_heavy_tree_deny_names_at_most_three_children(tmp_path):
    root = _repo(tmp_path)
    for n in ("data", "toolchains", "Saved", "Intermediate"):
        (root / n).mkdir()
    reason = _reason(_run_cwd("grep -r foo .", root))
    assert "walks Intermediate, Saved, data" in reason and "toolchains," not in reason.split("and outlives")[0]
    assert "--exclude-dir={" in reason


def test_deny_is_a_floor_and_survives_a_warn_level():
    from coordinator_core import machine_profile

    assert "block-whole-filesystem-scan" in machine_profile.FLOOR_GUARDS
