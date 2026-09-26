
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_PUBLISH = Path(__file__).parent.parent / "publish.py"


def _load_publish():
    spec = importlib.util.spec_from_file_location("_publish_under_test", _PUBLISH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_publish_under_test"] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        **no_console_creationflags(),
    )
    return cp.stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(path)],
        check=True,
        **no_console_creationflags(),
    )
    _git(path, "config", "core.fileMode", "false")


def _mode_of(repo: Path, rel: str) -> str:
    for line in _git(repo, "ls-files", "--stage", "--", rel).splitlines():
        return line.split()[0]
    raise AssertionError(f"{rel} is not tracked")


def _write_tracked(repo: Path, rel: str, body: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "--", rel)


@pytest.fixture
def publish():
    return _load_publish()


def test_shebanged_file_is_remoded_and_reported(tmp_path, publish):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    _write_tracked(repo, "bin/entry", "#!/usr/bin/env python3\nprint('hi')\n")

    assert _mode_of(repo, "bin/entry") == "100644"

    fixed = publish._normalize_dest_exec_bits(repo, [Path("bin")])

    assert fixed == ["bin/entry"]
    assert _mode_of(repo, "bin/entry") == "100755"


def test_non_shebanged_file_is_left_alone(tmp_path, publish):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    _write_tracked(repo, "bin/data.json", '{"not": "a script"}\n')

    assert publish._normalize_dest_exec_bits(repo, [Path("bin")]) == []
    assert _mode_of(repo, "bin/data.json") == "100644"


def test_scope_bounds_the_repair(tmp_path, publish):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    _write_tracked(repo, "bin/entry", "#!/usr/bin/env python3\n")
    _write_tracked(repo, "other/entry", "#!/usr/bin/env python3\n")

    fixed = publish._normalize_dest_exec_bits(repo, [Path("bin")])

    assert fixed == ["bin/entry"]
    assert _mode_of(repo, "other/entry") == "100644"


def test_already_correct_mode_is_a_no_op(tmp_path, publish):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    _write_tracked(repo, "bin/entry", "#!/usr/bin/env python3\n")

    assert publish._normalize_dest_exec_bits(repo, [Path("bin")]) == ["bin/entry"]
    assert publish._normalize_dest_exec_bits(repo, [Path("bin")]) == []


def test_a_hand_staged_then_removed_entry_refuses_the_whole_commit(
    tmp_path, publish, capsys
):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    _write_tracked(repo, "bin/kept.py", "kept\n")
    _git(repo, "commit", "-qm", "seed")
    head_before = _git(repo, "rev-parse", "HEAD").strip()

    (repo / "bin" / "kept.py").write_text("kept v2\n", encoding="utf-8")

    ghost = repo / "bin" / "ghost.py"
    ghost.write_text("ghost\n", encoding="utf-8")
    _git(repo, "add", "--", "bin/ghost.py")
    ghost.unlink()

    ok = publish._commit_published_dests(
        {repo: {Path("bin")}},
        succeeded_row_names=["mirror"],
        round_pinned_shas={},
    )

    assert ok is False
    head_after = _git(repo, "rev-parse", "HEAD").strip()
    assert head_after == head_before
    err = capsys.readouterr().err
    assert "bin/ghost.py" in err


def test_binary_blob_does_not_derail_the_batch(tmp_path, publish):
    repo = tmp_path / "mirror"
    _init_repo(repo)
    binary = repo / "bin" / "blob.bin"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"\x00\x01\x02\xff\xfe\n" * 32)
    _git(repo, "add", "--", "bin/blob.bin")
    _write_tracked(repo, "bin/zz-entry", "#!/bin/sh\necho hi\n")

    fixed = publish._normalize_dest_exec_bits(repo, [Path("bin")])

    assert fixed == ["bin/zz-entry"]
    assert _mode_of(repo, "bin/blob.bin") == "100644"
