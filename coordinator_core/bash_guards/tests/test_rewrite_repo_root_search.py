"""rewrite-repo-root-search: repo-root recursive grep/rg -> git grep, find advised, never deny."""

import pytest

from coordinator_core.bash_guards.rewrite_repo_root_search import ADVISORY, LEG_ID, check as _check


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "src").mkdir()
    return root


def _run(cmd, cwd, tool="Bash"):
    return _check({"tool_name": tool, "tool_input": {"command": cmd, "timeout": 5}, "cwd": str(cwd)})


def _new(cmd, cwd):
    out = _run(cmd, cwd)
    assert out is not None and out.verdict_kind == "rewrite", cmd
    assert out.updated_input["timeout"] == 5
    return out.updated_input["command"]


def _advised(cmd, cwd):
    out = _run(cmd, cwd)
    assert out is not None and out.verdict_kind == "advisory", cmd
    assert out.updated_input is None
    assert ADVISORY in out.em_text
    return out


def test_leg_id():
    assert LEG_ID == "rewrite-repo-root-search"


@pytest.mark.parametrize("cmd,expected", [
    ("grep -rn foo .", "git grep -n foo"),
    ("grep -r foo", "git grep foo"),
    ("grep -rniw foo .", "git grep -niw foo"),
    ("grep -R -l -F foo .", "git grep -lF foo"),
    ("grep -rE 'a|b' .", "git grep -E 'a|b'"),
    ("grep -r -e foo -e bar .", "git grep -e foo -e bar"),
    ("rg foo", "git grep foo"),
    ("rg foo .", "git grep foo"),
    ("rg -n -i foo", "git grep -ni foo"),
    ("rg 'a|b'", "git grep -E 'a|b'"),
    ("rg -F 'a|b'", "git grep -F 'a|b'"),
])
def test_rewrites_to_git_grep_at_repo_root(repo, cmd, expected):
    assert _new(cmd, repo) == expected
    out = _run(cmd, repo)
    assert "tracked files only" in out.em_text


def test_absolute_target_uses_git_dash_c(repo, tmp_path):
    target = repo.as_posix()
    assert _new(f"grep -rn foo {target}", tmp_path) == f"git -C {target} grep -n foo"


def test_leading_cd_sets_the_root(repo, tmp_path):
    target = repo.as_posix()
    assert _new(f"cd {target} && grep -rn foo .", tmp_path) == f"cd {target} && git grep -n foo"


@pytest.mark.parametrize("cmd", [
    "rg -g '*.py' foo",
    "rg -t py foo",
    "rg --hidden foo",
    "rg '\\d+' .",
    "grep -rn --include=*.py foo .",
    "grep -rI foo .",
    "grep -rn foo . 2>/dev/null",
    "grep -rn foo . src",
])
def test_unmapped_flags_are_advised_not_rewritten(repo, cmd):
    out = _run(cmd, repo)
    assert out is not None and out.verdict_kind in ("advisory", "rewrite")
    assert ADVISORY in out.em_text
    assert not (out.updated_input or {}).get("command", "").startswith("git ")


def test_unmapped_rg_is_advisory_only(repo):
    _advised("rg -g '*.py' foo", repo)


@pytest.mark.parametrize("cmd", [
    "find . -name '*.py'",
    "find . -name '*.py' | xargs grep foo",
    "find . -exec grep foo {} +",
])
def test_find_at_repo_root_is_advised(repo, cmd):
    _advised(cmd, repo)


def test_find_with_maxdepth_passes(repo):
    assert _run("find . -maxdepth 1 -name x", repo) is None


def test_unmapped_grep_keeps_the_heavy_tree_exclusion(repo):
    (repo / "scratch").mkdir()
    out = _run("grep -rn --include=*.py foo .", repo)
    assert out.verdict_kind == "rewrite"
    assert out.updated_input["command"] == "grep --exclude-dir=scratch -rn --include=*.py foo ."
    assert ADVISORY in out.em_text


def test_covered_heavy_tree_is_not_re_excluded(repo):
    (repo / "scratch").mkdir()
    out = _run("grep -rn --include=*.py --exclude-dir=scratch foo .", repo)
    assert out.verdict_kind == "advisory"


def test_pipeline_is_handled_per_segment(repo):
    assert _new("grep -rn foo . | head", repo) == "git grep -n foo | head"
    assert _new("cat x | grep -rn foo . | grep -v y", repo) == "cat x | git grep -n foo | grep -v y"
    assert _new("grep -rn x . | grep -v y", repo) == "git grep -n x | grep -v y"


def test_pipeline_mixing_rewrite_and_advice(repo):
    out = _run("find . -name x | xargs echo; grep -rn foo .", repo)
    assert out.verdict_kind == "rewrite"
    assert out.updated_input["command"] == "find . -name x | xargs echo; git grep -n foo"
    assert ADVISORY in out.em_text


@pytest.mark.parametrize("cmd", [
    "grep -rn x coordinator/hooks",
    "grep -rn foo src",
    "rg foo src/",
    "grep foo file.txt",
    "grep -n foo .",
    "echo grep -rn foo .",
    "ls",
])
def test_scoped_or_non_recursive_passes_untouched(repo, cmd):
    assert _run(cmd, repo) is None


def test_non_repo_cwd_passes(tmp_path):
    (tmp_path / "plain").mkdir()
    assert _run("grep -rn foo .", tmp_path / "plain") is None
    assert _run("rg foo", tmp_path / "plain") is None


def test_unknown_cwd_with_relative_target_allows():
    assert _run("grep -rn foo .", "") is None
    assert _run("rg foo", "") is None


def test_powershell_tool_is_matched(repo):
    out = _run("grep -rn foo .", repo, tool="PowerShell")
    assert out is not None and out.updated_input["command"] == "git grep -n foo"


def test_other_tools_are_ignored(repo):
    assert _run("grep -rn foo .", repo, tool="Read") is None


def test_never_denies(repo):
    for cmd in ("grep -rn foo .", "rg foo", "find .", "rg -g x foo", "grep -r foo / ", "grep -r 'unterminated ."):
        out = _run(cmd, repo)
        assert out is None or out.verdict_kind != "deny", cmd


def test_registered_at_advisory_rewrite_band():
    from coordinator_core.bash_guards.dispatch import GuardBand
    from coordinator_core.bash_guards.roster import guard_roster

    entries = [e for e in guard_roster() if e.id == LEG_ID]
    assert len(entries) == 1
    assert entries[0].band == GuardBand.ADVISORY_REWRITE.value
    assert entries[0].fail_closed is False
