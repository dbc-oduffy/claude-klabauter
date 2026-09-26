
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parents[1]

_SHEBANG = "#!/usr/bin/env python3" + chr(10)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round_shebang_exec_bit", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(repo: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git"] + list(args),
        cwd=str(repo),
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def dest_repo(tmp_path):
    repo = tmp_path / "mirror"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "core.fileMode", "false")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    return repo


def _mode(repo: Path, rel_path: str) -> str:
    return _git(repo, "ls-files", "-s", rel_path).stdout.split(" ", 1)[0]


def test_a_shebanged_file_is_staged_executable(dest_repo):
    (dest_repo / "hook.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["hook.py"]) == 1
    assert _mode(dest_repo, "hook.py") == "100755"


def test_the_commit_leg_s_own_add_does_not_undo_it(dest_repo):
    (dest_repo / "hook.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    _mod._stage_shebang_exec_bits(dest_repo, ["hook.py"])
    _git(dest_repo, "add", "--", "hook.py")
    assert _mode(dest_repo, "hook.py") == "100755"
    _git(dest_repo, "commit", "-q", "-m", "publish")
    assert _mode(dest_repo, "hook.py") == "100755"


def test_a_file_without_a_shebang_is_left_alone(dest_repo):
    (dest_repo / "notes.md").write_text("# not a script\n", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["notes.md"]) == 0
    _git(dest_repo, "add", "--", "notes.md")
    assert _mode(dest_repo, "notes.md") == "100644"


def test_a_missing_path_is_skipped_not_raised(dest_repo):
    assert _mod._stage_shebang_exec_bits(dest_repo, ["gone.py"]) == 0


def test_a_declined_path_does_not_fail_the_round(dest_repo, capsys):
    (dest_repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (dest_repo / "ignored.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["ignored.py"]) == 0
    assert "executable bit" in capsys.readouterr().err


def test_one_git_process_for_the_whole_set(dest_repo, monkeypatch):
    """The amplification budget: a CONSTANT number of spawns for N files,
    never one per file
    (`coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`). Two,
    both batched — `ls-files -s` for the already-committed reconcile, and one
    `add --chmod=+x` naming the whole set."""
    calls: "list[list[str]]" = []
    real_run = subprocess.run

    def _spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    for index in range(5):
        (dest_repo / f"hook{index}.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )
    monkeypatch.setattr(_mod.subprocess, "run", _spy)
    assert _mod._stage_shebang_exec_bits(
        dest_repo, [f"hook{index}.py" for index in range(5)]
    ) == 5
    assert len(calls) == 2
    assert calls[0][:2] == ["git", "ls-files"]
    assert calls[1][:3] == ["git", "add", "--chmod=+x"]
    # The invariant that matters is INDEPENDENCE from N, not the number 2.
    calls.clear()
    for index in range(5, 20):
        (dest_repo / f"hook{index}.py").write_text(_SHEBANG, encoding="utf-8")
    _mod._stage_shebang_exec_bits(
        dest_repo, [f"hook{index}.py" for index in range(20)]
    )
    assert len(calls) == 2


def test_a_file_already_committed_at_100644_is_reconciled(dest_repo):
    (dest_repo / "stuck.py").write_text(_SHEBANG, encoding="utf-8")
    _git(dest_repo, "add", "--", "stuck.py")
    _git(dest_repo, "commit", "-q", "-m", "landed non-executable")
    assert _mode(dest_repo, "stuck.py") == "100644"

    (dest_repo / "unrelated.md").write_text("# doc", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["unrelated.md"]) == 1
    assert _mode(dest_repo, "stuck.py") == "100755"


def test_reconciliation_converges_and_leaves_plain_files_alone(dest_repo):
    (dest_repo / "stuck.py").write_text(_SHEBANG, encoding="utf-8")
    (dest_repo / "data.md").write_text("# not a script", encoding="utf-8")
    _git(dest_repo, "add", "--", "stuck.py", "data.md")
    _git(dest_repo, "commit", "-q", "-m", "landed")

    assert _mod._already_committed_non_executable_scripts(dest_repo, []) == ["stuck.py"]
    _mod._stage_shebang_exec_bits(dest_repo, [])
    _git(dest_repo, "commit", "-q", "-m", "modes")
    assert _mod._already_committed_non_executable_scripts(dest_repo, []) == []
    assert _mode(dest_repo, "data.md") == "100644"


def test_repo_root_may_be_a_str(dest_repo):
    (dest_repo / "hook.py").write_text(_SHEBANG, encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(str(dest_repo), ["hook.py"]) == 1
    assert _mode(dest_repo, "hook.py") == "100755"


def test_a_reconciled_path_joins_the_sink_and_reaches_head(dest_repo):
    from coordinator_core.git.commit import commit_paths

    (dest_repo / "bin/tool").parent.mkdir(parents=True, exist_ok=True)
    (dest_repo / "bin/tool").write_text(_SHEBANG, encoding="utf-8")
    _git(dest_repo, "add", "--", "bin/tool")
    _git(dest_repo, "commit", "-q", "-m", "landed non-executable")
    assert _mode(dest_repo, "bin/tool") == "100644"

    (dest_repo / "unrelated.md").write_text("# doc", encoding="utf-8")
    sink: "list[str]" = ["unrelated.md"]
    count = _mod._stage_shebang_exec_bits(
        dest_repo, ["unrelated.md"], reconciled_sink=sink
    )
    assert count == 1
    assert sink == ["unrelated.md", "bin/tool"]

    commit_paths(dest_repo, sink, "reconcile publish")
    tree_mode = _git(dest_repo, "ls-tree", "HEAD", "bin/tool").stdout.split(" ", 1)[0]
    assert tree_mode == "100755"


def test_a_reconciled_path_already_in_the_sink_is_not_duplicated(dest_repo):
    (dest_repo / "stuck.py").write_text(_SHEBANG, encoding="utf-8")
    _git(dest_repo, "add", "--", "stuck.py")
    _git(dest_repo, "commit", "-q", "-m", "landed non-executable")

    sink = ["stuck.py"]
    _mod._stage_shebang_exec_bits(dest_repo, [], reconciled_sink=sink)
    assert sink == ["stuck.py"]


def test_the_sink_is_untouched_on_the_declined_path(dest_repo, capsys):
    (dest_repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (dest_repo / "ignored.py").write_text(_SHEBANG, encoding="utf-8")
    sink: "list[str]" = []
    assert _mod._stage_shebang_exec_bits(
        dest_repo, ["ignored.py"], reconciled_sink=sink
    ) == 0
    assert sink == []
    assert "executable bit" in capsys.readouterr().err


def test_the_sink_is_untouched_on_the_raising_path(dest_repo, monkeypatch, capsys):
    (dest_repo / "hook.py").write_text(_SHEBANG, encoding="utf-8")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("git went missing")

    monkeypatch.setattr(_mod.subprocess, "run", _explode)
    sink: "list[str]" = []
    assert _mod._stage_shebang_exec_bits(
        dest_repo, ["hook.py"], reconciled_sink=sink
    ) == 0
    assert sink == []
    assert "executable-bit step failed" in capsys.readouterr().err


def test_omitting_the_sink_is_byte_identical_to_head(dest_repo):
    (dest_repo / "hook.py").write_text(_SHEBANG, encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["hook.py"]) == 1
    assert _mode(dest_repo, "hook.py") == "100755"


def test_an_in_pathspec_shebang_is_not_appended_to_the_sink(dest_repo):
    (dest_repo / "hook.py").write_text(_SHEBANG, encoding="utf-8")
    sink: "list[str]" = ["hook.py"]
    count = _mod._stage_shebang_exec_bits(
        dest_repo, ["hook.py"], reconciled_sink=sink
    )
    assert count == 1
    assert sink == ["hook.py"]


def test_an_unexpected_error_never_reaches_the_round(dest_repo, monkeypatch, capsys):
    (dest_repo / "hook.py").write_text(_SHEBANG, encoding="utf-8")

    def _explode(*_args, **_kwargs):
        raise RuntimeError("git went missing")

    monkeypatch.setattr(_mod.subprocess, "run", _explode)
    assert _mod._stage_shebang_exec_bits(dest_repo, ["hook.py"]) == 0
    assert "executable-bit step failed" in capsys.readouterr().err
