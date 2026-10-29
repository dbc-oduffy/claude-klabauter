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


def _raw(cmd, cwd):
    return _check({"tool_name": "Bash", "tool_input": {"command": cmd, "timeout": 5}, "cwd": str(cwd)})


def _rewritten(cmd, cwd):
    out = _raw(cmd, cwd)
    assert out is not None and out.verdict_kind == "rewrite", cmd
    return out.updated_input["command"]


@pytest.mark.parametrize("heavy", ["scratch", "data", "toolchains", "node_modules"])
def test_heavy_tree_recursive_grep_at_repo_root_is_rewritten_never_denied(tmp_path, heavy):
    root = _repo(tmp_path, heavy)
    for cmd in ("grep -rn foo .", "grep -r foo", f"cd {root.as_posix()} && grep -R foo .", f"grep -r foo {root.as_posix()}"):
        out = _raw(cmd, root)
        assert out.verdict_kind == "rewrite", cmd
        assert f"--exclude-dir={heavy} " in out.updated_input["command"], cmd
        assert out.updated_input["timeout"] == 5
        assert _run_cwd(cmd, root)["hookSpecificOutput"].get("permissionDecision") != "deny"


def test_heavy_tree_rewrite_is_idempotent_and_whole_drive_scan_still_denies(tmp_path):
    root = _repo(tmp_path)
    again = _rewritten("grep -rn foo .", root)
    assert _raw(again, root) is None
    assert _run_cwd("grep -rn foo ~", root)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_heavy_tree_unrewritable_command_is_admitted_without_context(tmp_path):
    root = _repo(tmp_path)
    assert _raw("grep -rn foo . ; grep -rn bar .", root) is None


def test_include_filter_does_not_narrow_the_walked_tree(tmp_path):
    root = _repo(tmp_path)
    for cmd in ("grep -rln foo --include=*.py .", "grep -rln --include '*.py' foo ."):
        out = _run_cwd(cmd, root)
        assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
        assert "project-rag" in _reason(out) and "--exclude-dir=scratch" in _reason(out)
    for cmd in (
        "grep -rln foo --include=*.py --exclude-dir=scratch .",
        "grep -rln foo --include=*.py coordinator_core",
    ):
        assert _run_cwd(cmd, root) is None, cmd
    assert _run_cwd("grep -rln foo --include=*.py .", root / "scratch")


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


def test_heavy_tree_partial_cover_rewrites_adding_only_the_uncovered(tmp_path):
    root = _repo(tmp_path)
    (root / "data").mkdir()
    new = _rewritten("grep -rn --exclude-dir=scratch foo .", root)
    assert new.startswith("grep --exclude-dir=data ") and new.count("--exclude-dir=scratch") == 1


def test_heavy_tree_git_file_counts_as_repo_root(tmp_path):
    root = _repo(tmp_path, git_file=True)
    assert _rewritten("grep -rn foo .", root)


def test_heavy_tree_msys_and_drive_spellings_of_the_root(tmp_path):
    root = _repo(tmp_path)
    posix = root.as_posix()
    spellings = [posix]
    if len(posix) > 2 and posix[1] == ":":
        spellings.append("/" + posix[0].lower() + posix[2:])
    for sp in spellings:
        assert _rewritten(f"grep -r foo {sp}", tmp_path), sp


def test_heavy_tree_unknown_cwd_allows_relative_target():
    assert _run("grep -rn foo .") is None
    assert _run("grep -r foo") is None


def test_heavy_tree_unreadable_root_allows(tmp_path, monkeypatch):
    root = _repo(tmp_path)

    def boom(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr("coordinator_core.bash_guards.block_whole_filesystem_scan.os.scandir", boom)
    assert _run_cwd("grep -rn foo .", root) is None


def test_heavy_tree_rewrite_excludes_every_uncovered_child(tmp_path):
    root = _repo(tmp_path)
    for n in ("data", "toolchains", "Saved", "Intermediate"):
        (root / n).mkdir()
    new = _rewritten("grep -r foo .", root)
    assert "--exclude-dir={Intermediate,Saved,data,scratch,toolchains}" in new


UE = "/opt/UE_5.8"

HUGE_DENY = [
    f'grep -rn "class X" "{UE}/Engine/Plugins"',
    f"grep -rl foo {UE}/Engine/Source",
    f"rg foo {UE}",
    f"rg foo {UE}/Engine",
    f"grep -rn foo {UE}/Engine/Plugins/Runtime/GroomPlugin",
    f"find {UE}/Engine/Plugins -name '*.h' -exec grep -l foo {{}} +",
    f"du -sh {UE}/Engine/Source",
    "grep -rln relb example-retrieval-repo-ue-addon/scratch",
    "grep -rl 45ac1e scratch --include=*.log",
    "rg foo scratch",
    "du -sh scratch",
    "find scratch -name x -exec cat {} +",
]

HUGE_ALLOW = [
    "grep -rn foo scratch/run-1",
    "rg foo scratch/warp/ask-1/briefs",
    "du -sh scratch/run-1",
    "find scratch -name x",
    "grep -n foo scratch",
    f"grep -rn foo {UE}/Engine/Content",
    f"grep -n foo {UE}/Engine/Source/a.h",
    "grep -rn foo src",
]


@pytest.mark.parametrize("cmd", HUGE_DENY)
def test_denies_recursive_scan_of_engine_or_scratch_root(cmd):
    out = _run(cmd)
    assert out and out["hookSpecificOutput"]["permissionDecision"] == "deny", cmd
    reason = _reason(out)
    assert "project_engine_type_hierarchy" in reason or "scratch/<run>" in reason


@pytest.mark.parametrize("cmd", HUGE_ALLOW)
def test_allows_scoped_run_dir_and_non_recursive(cmd):
    assert _run(cmd) is None, cmd


def test_scratch_cwd_with_bare_recursive_target_denies(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    assert _run_cwd("grep -rn foo .", d)
    assert _run_cwd("du -sh", d)


@pytest.mark.parametrize(
    "cmd",
    [
        "find / -maxdepth 2 -name x",
        "find /c -maxdepth 4 -iname '*x*'",
        "sh -c 'find / -maxdepth 1 -name x'",
    ],
)
def test_runaway_find_bounded_root_warns(cmd):
    from coordinator_core.bash_guards import _dc_rewrites as dc

    rec = dc.check_runaway_find(cmd, "s")
    assert rec is not None and rec.verdict_kind == "advisory"
    assert rec.updated_input is None


@pytest.mark.parametrize(
    "cmd",
    [
        "find / -name x",
        "find / -maxdepth -name x",
        "find / -maxdepth 2 -name x; find / -name y",
        "sh -c 'find / -name x'",
    ],
)
def test_runaway_find_unbounded_root_still_denies(cmd):
    from coordinator_core.bash_guards import _dc_rewrites as dc

    rec = dc.check_runaway_find(cmd, "s")
    assert rec is not None and rec.verdict_kind == "deny"


def test_deny_is_a_floor_and_survives_a_warn_level():
    from coordinator_core import machine_profile

    assert "block-whole-filesystem-scan" in machine_profile.FLOOR_GUARDS
