"""coordinator/bin/tests/test_census_revalidate_input_key.py — `input_key` and the content-keyed memo.

Subject: `input_key`, `MemoEntry`, `spawning_commands` and the cost gate in
`coordinator/bin/mise-census-revalidate.py`. Every repo is built under tmp_path with `git init`.
An equal key must imply identical output, so every shape the key cannot bound gives None.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TOOL = Path(__file__).resolve().parents[1] / "mise-census-revalidate.py"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("mise_census_revalidate_ik", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@t.example"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    (root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
    (root / "f.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (root / "d").mkdir()
    (root / "d" / "tracked.txt").write_text("needle\n", encoding="utf-8")
    _git(["add", ".gitignore", "f.txt", "d/tracked.txt"], root)
    _git(["commit", "-qm", "seed"], root)
    return root


def _key(mcr, repo, command, head="H1"):
    return mcr.input_key({"command": command}, repo, head=head)


def test_edit_changes_the_key_and_an_unchanged_tree_keeps_it(mcr, repo):
    first = _key(mcr, repo, "cat f.txt")
    assert first is not None
    assert _key(mcr, repo, "cat f.txt") == first
    (repo / "f.txt").write_text("alpha\ngamma\n", encoding="utf-8")
    assert _key(mcr, repo, "cat f.txt") != first


def test_stat_stale_file_with_unchanged_content_keys_the_same_and_index_is_untouched(mcr, repo):
    before = _key(mcr, repo, "cat f.txt")
    index = repo / ".git" / "index"
    index_bytes = index.read_bytes()
    st = (repo / "f.txt").stat()
    os.utime(repo / "f.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    assert _key(mcr, repo, "cat f.txt") == before
    assert index.read_bytes() == index_bytes


def test_untracked_and_ignored_files_under_a_directory_enter_the_key(mcr, repo):
    cmd = "grep -rn needle d"
    base = _key(mcr, repo, cmd)
    assert base is not None
    for name in ("new.txt", "ignored.log"):
        path = repo / "d" / name
        path.write_text("one\n", encoding="utf-8")
        added = _key(mcr, repo, cmd)
        assert added not in (None, base)
        path.write_text("two\n", encoding="utf-8")
        edited = _key(mcr, repo, cmd)
        assert edited not in (None, added)
        path.unlink()
        assert _key(mcr, repo, cmd) == base


def test_cr_bearing_untracked_file_under_a_directory_keys_and_editing_it_changes_the_key(
    mcr, repo
):
    cmd = "grep -rn needle d"
    base = _key(mcr, repo, cmd)
    (repo / "d" / "crlf.bin").write_bytes(b"a\r\nb\r\n")
    added = _key(mcr, repo, cmd)
    assert added not in (None, base)
    (repo / "d" / "crlf.bin").write_bytes(b"a\r\nc\r\n")
    assert _key(mcr, repo, cmd) not in (None, added)


def test_cr_bearing_untracked_file_operand_still_gives_none(mcr, repo):
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    assert _key(mcr, repo, "cat crlf.txt") is None


def test_cr_bearing_tracked_stat_stale_file_gives_none(mcr, repo):
    (repo / "crlf.txt").write_bytes(b"a\r\nb\r\n")
    _git(["add", "crlf.txt"], repo)
    _git(["commit", "-qm", "crlf"], repo)
    st = (repo / "crlf.txt").stat()
    os.utime(repo / "crlf.txt", ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    assert _key(mcr, repo, "cat crlf.txt") is None
    assert _key(mcr, repo, "grep -rn a crlf.txt") is None


def test_a_crlf_rewrite_that_normalises_to_one_blob_still_changes_the_key(mcr, repo):
    _git(["config", "core.autocrlf", "true"], repo)
    (repo / "u.txt").write_bytes(b"a\nb\n")
    lf = _key(mcr, repo, "wc -c u.txt")
    (repo / "u.txt").write_bytes(b"a\r\nb\r\n")
    crlf = _key(mcr, repo, "wc -c u.txt")
    assert lf is not None and crlf is not None and lf != crlf


def test_git_history_stage_keys_on_head(mcr, repo):
    cmd = "git log --oneline -3"
    one = _key(mcr, repo, cmd, head="H1")
    assert one is not None
    assert _key(mcr, repo, cmd, head="H1") == one
    assert _key(mcr, repo, cmd, head="H2") != one
    assert _key(mcr, repo, cmd, head=None) is None
    assert _key(mcr, repo, "git -C . log --oneline") is not None


@pytest.mark.parametrize(
    "command",
    [
        "git grep needle",
        "git status --short",
        "git branch",
        "git log --all",
        "git log --branches --oneline",
        "git log main",
        "git -C d log",
        "git ls-files",
        "git log --decorate",
    ],
)
def test_git_stages_that_read_beyond_head_give_none(mcr, repo, command):
    assert _key(mcr, repo, command) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat *.txt",
        "cat $HOME/x",
        "cat /etc/hosts",
        "cat ~/x",
        "grep -rn needle .",
        "grep -rn needle",
        "rg needle",
        "ls",
        "find",
        "find d -newer f.txt",
        "ls -l d",
        "cat .git/config",
        "cat ../x",
        "cat d/../f.txt",
        "python f.py",
        "echo 5",
        "cat f.txt > out.txt",
        "cat f.txt 2>err.txt",
        "wc -l < f.txt",
        "cat f.txt; cat d/tracked.txt",
        "cat f.txt && cat d/tracked.txt",
        "cat 'unterminated",
        "cat f.txt |",
        "",
    ],
)
def test_unbounded_shapes_give_none(mcr, repo, command):
    assert _key(mcr, repo, command) is None


def test_dev_null_redirect_is_not_an_operand(mcr, repo):
    plain = _key(mcr, repo, "cat f.txt")
    assert plain is not None
    assert _key(mcr, repo, "cat f.txt 2>/dev/null") is not None
    assert _key(mcr, repo, "cat f.txt >/dev/null") is not None
    assert _key(mcr, repo, "grep -rn needle d 2>/dev/null | wc -l") is not None


def test_a_quoted_pattern_is_literal_and_an_expansion_is_not(mcr, repo):
    assert _key(mcr, repo, "grep -rn 'nee.*dle\\|x[0-9]' d") is not None
    assert _key(mcr, repo, 'grep -rn "nee*dle" d') is not None
    assert _key(mcr, repo, "grep -rn needle d/tra*") is None
    assert _key(mcr, repo, 'grep -rn "$HOME" d') is None
    assert _key(mcr, repo, "grep -rn needle d/{a,b}") is None
    assert _key(mcr, repo, "grep -rn 'needle' d") == _key(mcr, repo, "grep -rn needle d")


@pytest.mark.parametrize(
    "command",
    [
        "grep --file=pats.txt d/tracked.txt",
        "grep -rn --exclude-from=ex.txt needle d",
        "rg needle d/tracked.txt",
        "sed -n 1p f.txt",
        "awk 'NR==1' f.txt",
    ],
)
def test_readers_whose_read_set_argv_does_not_name_give_none(mcr, repo, command):
    assert _key(mcr, repo, command) is None


def test_a_dash_glob_is_literal_only_while_no_dash_named_entry_exists(mcr, repo):
    cmd = "grep -rn --include=*.txt needle d"
    assert _key(mcr, repo, cmd) is not None
    (repo / "-x").write_text("y\n", encoding="utf-8")
    assert _key(mcr, repo, cmd) is None


def test_a_typed_entry_gives_none(mcr, repo):
    assert mcr.input_key({"kind": "next-free-number", "dir": "d"}, repo, head="H1") is None


def test_a_missing_operand_keys_as_absent_and_creating_it_changes_the_key(mcr, repo):
    missing = _key(mcr, repo, "cat later.txt")
    assert missing is not None
    (repo / "later.txt").write_text("now\n", encoding="utf-8")
    assert _key(mcr, repo, "cat later.txt") not in (None, missing)


def test_the_index_is_parsed_once_per_view(mcr, repo, monkeypatch):
    mcr._census()
    from coordinator_core.git import git_index

    calls = []
    real = git_index.parse_index_identity
    monkeypatch.setattr(
        git_index, "parse_index_identity", lambda *a, **kw: calls.append(1) or real(*a, **kw)
    )
    view = mcr._TreeView(repo)
    for cmd in ("cat f.txt", "grep -rn needle d", "cat d/tracked.txt"):
        assert mcr.input_key({"command": cmd}, repo, head="H1", view=view) is not None
    assert len(calls) == 1


class _Spawns:
    def __init__(self, monkeypatch, mcr):
        self.calls = 0
        real = subprocess.run

        def counting(*a, **kw):
            self.calls += 1
            return real(*a, **kw)

        monkeypatch.setattr(mcr.subprocess, "run", counting)


def _plan(path: Path, command: str, result: str) -> Path:
    path.write_text(
        "\n".join(
            ["---", "mise_prepped_sha: abc", "census:", "  - question: q",
             f"    command: {command!r}", f"    result: {result!r}", "---", "body", ""]
        ),
        encoding="utf-8",
    )
    return path


def test_equal_key_judges_from_the_memo_with_zero_spawns(mcr, repo, monkeypatch):
    plan = _plan(repo / "p.md", "wc -l f.txt", "2")
    spawns = _Spawns(monkeypatch, mcr)
    memo: dict = {}
    first = mcr.revalidate(plan, repo, 10, memo=memo, head="H1")
    assert spawns.calls == 1 and first["memo_hits"] == 0
    entry = memo["wc -l f.txt"]
    assert isinstance(entry, mcr.MemoEntry) and entry.key is not None
    again = mcr.revalidate(plan, repo, 10, memo=memo, head="H1")
    assert spawns.calls == 1 and again["memo_hits"] == 1
    assert again["entries"][0]["state"] == first["entries"][0]["state"] == mcr.MATCH
    (repo / "f.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    moved = mcr.revalidate(plan, repo, 10, memo=memo, head="H1")
    assert spawns.calls == 2 and moved["memo_hits"] == 0
    assert moved["entries"][0]["state"] == mcr.DRIFT


def test_a_none_key_spawns_every_time_and_is_never_judged_from(mcr, repo, monkeypatch):
    plan = _plan(repo / "p.md", "echo 5", "5")
    spawns = _Spawns(monkeypatch, mcr)
    memo: dict = {}
    for _ in range(2):
        rep = mcr.revalidate(plan, repo, 10, memo=memo, head="H1")
        assert rep["memo_hits"] == 0
    assert spawns.calls == 2
    assert memo["echo 5"].key is None


def test_memo_none_runs_no_key_at_all(mcr, repo, monkeypatch):
    plan = _plan(repo / "p.md", "wc -l f.txt", "2")

    def boom(*a, **kw):
        raise AssertionError("input_key must not run without a memo")

    monkeypatch.setattr(mcr, "input_key", boom)
    rep = mcr.revalidate(plan, repo, 10)
    assert rep["entries"][0]["state"] == mcr.MATCH
    assert rep["memo_hits"] == 0 and rep["direct_runs"] == 0


def test_spawn_no_dearer_than_key_runs_directly_without_a_key(mcr, repo, monkeypatch):
    plan = _plan(repo / "p.md", "wc -l f.txt", "2")
    memo = {"wc -l f.txt": mcr.MemoEntry("stale-key", (0, "2 f.txt\n", ""), 3, 3)}

    def boom(*a, **kw):
        raise AssertionError("the cost gate must not compute a key")

    monkeypatch.setattr(mcr, "input_key", boom)
    spawns = _Spawns(monkeypatch, mcr)
    rep = mcr.revalidate(plan, repo, 10, memo=memo, head="H1")
    assert spawns.calls == 1
    assert rep["direct_runs"] == 1 and rep["memo_hits"] == 0
    row = rep["entries"][0]
    assert row["state"] == mcr.MATCH
    assert row["basis"].endswith("; direct: key cost exceeds spawn")
    stored = memo["wc -l f.txt"]
    assert stored.key is None and stored.key_ms == 3


def test_git_stage_entries_run_after_every_keyable_entry_and_rows_keep_entry_order(
    mcr, repo, monkeypatch
):
    plan = repo / "p.md"
    plan.write_text(
        "\n".join(
            [
                "---", "mise_prepped_sha: abc", "census:",
                "  - {question: g, command: 'git status --short', result: '3'}",
                "  - {question: w, command: 'wc -l f.txt', result: '2'}",
                "---", "body", "",
            ]
        ),
        encoding="utf-8",
    )
    spawns = _Spawns(monkeypatch, mcr)
    allowed = iter([5])
    monkeypatch.setattr(mcr, "_remaining_timeout", lambda t, d: next(allowed, None))
    rep = mcr.revalidate(plan, repo, 10, memo={}, head="H1", deadline=1.0)
    assert spawns.calls == 1
    git_row, wc_row = rep["entries"]
    assert git_row["command"] == "git status --short" and git_row["state"] == mcr.UNRUNNABLE
    assert wc_row["command"] == "wc -l f.txt" and wc_row["state"] == mcr.MATCH


def test_spawning_commands_are_the_screen_clear_can_drift_untyped_commands(mcr, repo):
    plan = repo / "p.md"
    plan.write_text(
        "\n".join(
            [
                "---", "mise_prepped_sha: abc", "census:",
                "  - {question: a, command: 'wc -l f.txt', result: '2'}",
                "  - {question: b, command: 'wc -l f.txt', result: '2'}",
                "  - {question: c, command: 'cat f.txt', result: 'prose'}",
                "  - {question: d, command: 'rm -rf x', result: '3'}",
                "  - {question: e, kind: next-free-number, dir: d, result: '1'}",
                "---", "body", "",
            ]
        ),
        encoding="utf-8",
    )
    assert mcr.spawning_commands(plan, repo) == ("wc -l f.txt",)
    doc = {"census": [{"command": "ls d", "result": "1"}]}
    assert mcr.spawning_commands(plan, repo, doc) == ("ls d",)
