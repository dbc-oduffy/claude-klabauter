"""`coordinator-safe-commit --pathspec-from-file` carries a path list too long for argv."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import pathlib
import sys
import types

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent
_N = 2000


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _names(n: int = _N) -> list[str]:
    return [f"gen/sub{i % 40}/file_{i:05d}.txt" for i in range(n)]


def _write_list(tmp_path, lines: list[str]) -> str:
    f = tmp_path / "paths.txt"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(f)


def _make_files(root: pathlib.Path, names: list[str]) -> None:
    for name in names:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")


def _stub_commit_path(monkeypatch, mod, root: pathlib.Path):
    """Run `do_pathspec` to the `ceremony.commit_v2` hop and capture its params."""
    calls: list[tuple[str, dict]] = []

    def _fake_cc_invoke(op, params, worktree_root):
        calls.append((op, params))
        return {"committed": True, "sha": "deadbeef", "warnings": []}

    fake = types.ModuleType("cc_invoke")
    fake.cc_invoke = _fake_cc_invoke
    fake.require_engine_on_path = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "cc_invoke", fake)
    monkeypatch.setattr(mod, "_bootstrap_engine", lambda: None)
    mod.require_engine_on_path = lambda *_a, **_k: None
    monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_warn_undeclared_untracked_siblings", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_resolve_pre_sha_for_reconcile", lambda root: None)
    monkeypatch.setattr(
        mod,
        "_classify_paths_for_commit_v2",
        lambda r, paths: (*mod._split_paths_for_commit_v2(r, paths), []),
    )
    (root / ".git").mkdir(exist_ok=True)
    monkeypatch.chdir(root)
    return calls


class TestParse:
    def test_two_thousand_paths_arrive_intact_and_ordered(self, tmp_path):
        mod = _load_cli_module()
        names = _names()
        args = mod.parse_args(
            ["--pathspec-from-file", _write_list(tmp_path, names), "bulk regen"]
        )
        assert args.paths == names

    def test_blank_and_comment_lines_are_ignored(self, tmp_path):
        mod = _load_cli_module()
        listing = _write_list(
            tmp_path, ["# header", "", "a/one.py", "   ", "  # indented note", "b/two.py"]
        )
        args = mod.parse_args(["--pathspec-from-file", listing, "bulk regen"])
        assert args.paths == ["a/one.py", "b/two.py"]

    def test_crlf_and_bom_are_tolerated(self, tmp_path):
        mod = _load_cli_module()
        f = tmp_path / "p.txt"
        f.write_bytes(b"\xef\xbb\xbfa/one.py\r\nb/two.py\r\n")
        args = mod.parse_args(["--pathspec-from-file", str(f), "bulk regen"])
        assert args.paths == ["a/one.py", "b/two.py"]

    def test_merges_with_positional_paths_and_deduplicates(self, tmp_path):
        mod = _load_cli_module()
        listing = _write_list(tmp_path, ["a/one.py", "b/two.py", "c/three.py"])
        args = mod.parse_args(
            ["--pathspec-from-file", listing, "bulk regen", "--", "b/two.py", "z/last.py"]
        )
        assert args.paths == ["b/two.py", "z/last.py", "a/one.py", "c/three.py"]

    def test_dash_reads_stdin(self, monkeypatch):
        mod = _load_cli_module()
        monkeypatch.setattr(
            sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(b"a/one.py\n# c\nb/two.py\n"))
        )
        args = mod.parse_args(["--pathspec-from-file", "-", "bulk regen"])
        assert args.paths == ["a/one.py", "b/two.py"]

    def test_empty_list_is_a_usage_error(self, tmp_path):
        mod = _load_cli_module()
        listing = _write_list(tmp_path, ["# nothing", ""])
        with pytest.raises(mod.UsageError):
            mod.parse_args(["--pathspec-from-file", listing, "bulk regen"])

    def test_unreadable_file_is_a_usage_error(self, tmp_path):
        mod = _load_cli_module()
        with pytest.raises(mod.UsageError):
            mod.parse_args(["--pathspec-from-file", str(tmp_path / "nope.txt"), "bulk regen"])

    def test_combined_with_blanket_is_refused(self, tmp_path):
        mod = _load_cli_module()
        listing = _write_list(tmp_path, ["a/one.py"])
        with pytest.raises(mod.UsageError):
            mod.parse_args(["--blanket", "--pathspec-from-file", listing, "bulk regen"])

    def test_help_documents_the_flag(self):
        mod = _load_cli_module()
        stream = io.StringIO()
        mod.usage(stream)
        text = " ".join(stream.getvalue().split())
        assert "--pathspec-from-file <file>" in text
        assert "Argument list too long" in text


class TestReachesCommitOp:
    def test_two_thousand_paths_reach_commit_v2_params_intact(self, monkeypatch, tmp_path):
        mod = _load_cli_module()
        names = _names()
        _make_files(tmp_path, names)
        calls = _stub_commit_path(monkeypatch, mod, tmp_path)
        listing = _write_list(tmp_path, ["# generated", ""] + names)

        mod.main(["--pathspec-from-file", listing, "bulk regen"])

        assert len(calls) == 1
        op, params = calls[0]
        assert op == "ceremony.commit_v2"
        assert params["paths"] == names
        assert params["deleted_paths"] == []

    def test_file_and_positional_paths_merge_into_one_commit(self, monkeypatch, tmp_path):
        mod = _load_cli_module()
        names = _names(50)
        _make_files(tmp_path, names)
        calls = _stub_commit_path(monkeypatch, mod, tmp_path)
        listing = _write_list(tmp_path, names[:30])

        mod.main(
            ["--pathspec-from-file", listing, "bulk regen", "--", names[29], *names[30:]]
        )

        assert calls[0][1]["paths"] == names[29:] + names[:29]

    def test_a_directory_line_is_still_refused(self, monkeypatch, tmp_path, capsys):
        mod = _load_cli_module()
        _make_files(tmp_path, ["gen/a.txt"])
        calls = _stub_commit_path(monkeypatch, mod, tmp_path)
        listing = _write_list(tmp_path, ["gen/a.txt", "gen"])

        with pytest.raises(SystemExit) as exc:
            mod.main(["--pathspec-from-file", listing, "bulk regen"])

        assert exc.value.code == 1
        assert "gen is a directory" in capsys.readouterr().err
        assert calls == []

    def test_an_unknown_path_line_is_still_refused(self, monkeypatch, tmp_path, capsys):
        mod = _load_cli_module()
        _make_files(tmp_path, ["gen/a.txt"])
        calls = _stub_commit_path(monkeypatch, mod, tmp_path)
        monkeypatch.setattr(mod, "_paths_tracked_at_head", lambda root, paths: set())
        listing = _write_list(tmp_path, ["gen/a.txt", "gen/ghost.txt"])

        with pytest.raises(SystemExit):
            mod.main(["--pathspec-from-file", listing, "bulk regen"])

        assert "gen/ghost.txt is neither in the worktree nor in HEAD" in capsys.readouterr().err
        assert calls == []

    def test_contest_gate_receives_the_full_merged_list(self, monkeypatch, tmp_path):
        mod = _load_cli_module()
        names = _names(300)
        _make_files(tmp_path, names)
        calls = _stub_commit_path(monkeypatch, mod, tmp_path)
        seen: list[list[str]] = []
        monkeypatch.setattr(mod, "_refuse_contested_pathspec", lambda paths, root: seen.append(list(paths)))
        listing = _write_list(tmp_path, names)

        mod.main(["--pathspec-from-file", listing, "bulk regen"])

        assert seen == [names]
        assert len(calls) == 1


class TestGitHopsNeverCarryTheFullListOnOneArgv:
    def test_argv_batches_bound_every_run_and_lose_nothing(self):
        mod = _load_cli_module()
        names = [f"deep/{'d' * 60}/{i:05d}.txt" for i in range(_N)]
        batches = mod._argv_batches(names)
        assert len(batches) > 1
        assert [p for b in batches for p in b] == names
        for batch in batches:
            assert sum(len(p) + 1 for p in batch) < mod._ARGV_PATHSPEC_BUDGET

    def test_short_list_is_a_single_spawn(self):
        mod = _load_cli_module()
        assert mod._argv_batches(["a", "b", "c"]) == [["a", "b", "c"]]

    def test_head_probe_over_two_thousand_paths_stays_under_the_command_line_cap(
        self, monkeypatch
    ):
        mod = _load_cli_module()
        names = [f"deep/{'d' * 60}/{i:05d}.txt" for i in range(_N)]
        argv_lengths: list[int] = []

        def _fake_run(cmd, **_kwargs):
            argv_lengths.append(sum(len(c) + 1 for c in cmd))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        assert mod._paths_tracked_at_head("/repo", names) == set()
        assert len(argv_lengths) > 1
        assert max(argv_lengths) < 32000

    def test_staged_deletion_probe_carries_no_pathspec(self, monkeypatch, tmp_path):
        mod = _load_cli_module()
        names = _names(50)
        _make_files(tmp_path, names)
        seen: list[list[str]] = []

        def _fake_run(cmd, **_kwargs):
            seen.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout=names[3] + "\0", stderr="")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        present, deleted, untracked = mod._classify_paths_for_commit_v2(str(tmp_path), names)

        assert len(seen) == 1 and "--" not in seen[0]
        assert untracked == [names[3]]
        assert names[3] not in present
