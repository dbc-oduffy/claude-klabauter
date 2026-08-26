"""test_percolate_round_shebang_exec_bit — a published shebanged file must
land executable IN THE DEST INDEX, from any host.

The defect this pins, reported by doe-claude-em 2026-08-26 after three
self-blocked rounds: `publish_sync._restore_shebang_executable_bit` chmods the
copied file on disk and that is the module's only exec-bit mechanism. Both the
source and the mirror run `core.fileMode=false`, so git ignores the filesystem
bit and the file lands `100644` whatever the source records. The bit reached
mirrors only from hosts where `core.fileMode` happens to be true — correct on
macOS, silently wrong on Windows, and visible only when a NEW executable is
published from the wrong host. The mirror's own CI `check-exec-bit` then fails
the round it just committed, and its remediation is unreachable: the durable
fix is already in source, and the publish-mirror guard correctly refuses a
direct write at the mirror.

These tests use real `git` against a real temporary repo — the point is what
GIT records, and a `subprocess.run` spy would only prove which arguments we
chose, not that they produce `100755`. That is the same reasoning
`test_percolate_round.py` uses in reverse for its push assertions.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_shebang_exec_bit.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2): these drive
# real `git` on purpose. What is under test is the MODE GIT RECORDS, and a
# stubbed spawn would only prove which arguments were chosen -- the defect
# being closed is precisely that plausible-looking arguments (an on-disk
# chmod) record nothing under `core.fileMode=false`.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parents[1]

#: Named once so the literal that MAKES a file a script is unmissable.
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
    """A stand-in for the publish mirror, carrying the property that makes the
    on-disk chmod inert: `core.fileMode=false`, which both real repos set."""
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
    """The load-bearing sequencing claim. This step runs BEFORE
    `run_commit_pipeline` stages, so the pipeline's own `git add` must not
    reset the mode — it does not, because `core.fileMode=false` makes git
    reuse the recorded index mode rather than re-read it off disk. That is the
    same property that makes the on-disk chmod inert, used in our favour."""
    (dest_repo / "hook.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    _mod._stage_shebang_exec_bits(dest_repo, ["hook.py"])
    _git(dest_repo, "add", "--", "hook.py")
    assert _mode(dest_repo, "hook.py") == "100755"
    _git(dest_repo, "commit", "-q", "-m", "publish")
    assert _mode(dest_repo, "hook.py") == "100755"


def test_a_file_without_a_shebang_is_left_alone(dest_repo):
    """`#!` is the whole test, matching `_restore_shebang_executable_bit`'s own
    rule. Marking published data or documentation executable would be a new
    defect wearing this fix's clothes."""
    (dest_repo / "notes.md").write_text("# not a script\n", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["notes.md"]) == 0
    _git(dest_repo, "add", "--", "notes.md")
    assert _mode(dest_repo, "notes.md") == "100644"


def test_a_missing_path_is_skipped_not_raised(dest_repo):
    """Pathspec entries can name a path that is not on disk (a removal leg's
    entry, most plainly). Reading it must not raise into the round."""
    assert _mod._stage_shebang_exec_bits(dest_repo, ["gone.py"]) == 0


def test_a_declined_path_does_not_fail_the_round(dest_repo, capsys):
    """A path git refuses here (gitignored at dest is the likely one) leaves
    the round exactly where it stood before this step existed. It warns and
    returns; it must never raise, because a publish that succeeded must not be
    failed by a mode it could not record."""
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
    """The stuck case, and the reason the fix cannot stop at the pathspec. A
    script committed non-executable never changes again, so it never re-enters
    a pathspec, while the mirror's CI keeps failing on it every round and the
    durable fix at source has nothing left to cross."""
    (dest_repo / "stuck.py").write_text(_SHEBANG, encoding="utf-8")
    _git(dest_repo, "add", "--", "stuck.py")
    _git(dest_repo, "commit", "-q", "-m", "landed non-executable")
    assert _mode(dest_repo, "stuck.py") == "100644"

    # A later round copying something else entirely.
    (dest_repo / "unrelated.md").write_text("# doc", encoding="utf-8")
    assert _mod._stage_shebang_exec_bits(dest_repo, ["unrelated.md"]) == 1
    assert _mode(dest_repo, "stuck.py") == "100755"


def test_reconciliation_converges_and_leaves_plain_files_alone(dest_repo):
    """Once recorded, a path leaves the candidate set permanently -- the scan
    reads `100644` entries only. And a committed file without a shebang is
    never touched, however often the scan runs."""
    (dest_repo / "stuck.py").write_text(_SHEBANG, encoding="utf-8")
    (dest_repo / "data.md").write_text("# not a script", encoding="utf-8")
    _git(dest_repo, "add", "--", "stuck.py", "data.md")
    _git(dest_repo, "commit", "-q", "-m", "landed")

    assert _mod._already_committed_non_executable_scripts(dest_repo, []) == ["stuck.py"]
    _mod._stage_shebang_exec_bits(dest_repo, [])
    _git(dest_repo, "commit", "-q", "-m", "modes")
    assert _mod._already_committed_non_executable_scripts(dest_repo, []) == []
    assert _mode(dest_repo, "data.md") == "100644"
