"""
coordinator_core.ops.test_migrate_state_to_claude_klabauter — behavior-parity tests for
the naked-Python port (DR-059).

Tests exercise the module against `tmp_path`-scoped throwaway CLAUDE_HOME /
CLAUDE_KLABAUTER_ROOT trees (hermetic; no shared/real repo state touched) and assert
parity with the golden-oracle behavior captured from the retired bash script
during the port (byte-diffed stdout/stderr + exit codes, see the port's own
completion record).

Spec backlink: DoE-claude:pln-bash-to-naked-python-engine-mi-c09292
Port of: migrate-state-to-claude-klabauter.sh (DoE b5a4192c, 2026-07-20)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.ops import migrate_state_to_claude_klabauter as mstm  # noqa: E402


def _mkclaude_home(root: Path, files: dict) -> Path:
    """files: {relative_path: content}."""
    home = root / "claude_home"
    for rel, content in files.items():
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return home


def _run(argv, claude_home: Path, claude_klabauter_root: Path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    # COORDINATOR_ENGINE_SOURCE_ROOT, not the retired CLAUDE_KLABAUTER_ROOT spelling
    # (DR-326's 2026-08-20 amendment eliminated CLAUDE_KLABAUTER_ROOT outright — no axis
    # reads it any more, engine_root.py's own "NEGATIVE SPEC" section). Setting
    # the dead name here left `main()` falling through to
    # `_default_claude_klabauter_root()` == this live repo's own root, so every
    # --populate/--finalize run in this suite was copying/removing files
    # against the SHARED WORKING TREE instead of tmp_path -- confirmed via
    # `state/a.txt` (this suite's own "hello\n" fixture) landing tracked in
    # the live repo at HEAD.
    monkeypatch.setenv("COORDINATOR_ENGINE_SOURCE_ROOT", str(claude_klabauter_root))
    rc = mstm.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# ---------------------------------------------------------------------------
# Usage / argument handling
# ---------------------------------------------------------------------------


def test_no_args_prints_full_usage_and_exits_1(tmp_path, monkeypatch, capsys):
    claude_home = _mkclaude_home(tmp_path, {})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    rc, out, err = _run([], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 1
    assert out == ""
    assert "Usage: migrate-state-to-claude-klabauter.sh --populate | --finalize" in err
    # Full usage block (distinguishes this branch from the unknown-subcommand
    # branch below, which is intentionally shorter — see module Negative-spec).
    assert "--populate   Copy ~/.claude working data to claude-klabauter (C6a)." in err
    assert "--finalize   Delta re-sync + remove ~/.claude originals (C6b)." in err


def test_unknown_subcommand_prints_short_usage_only_and_exits_1(tmp_path, monkeypatch, capsys):
    """Regression test for a parity bug caught during the port: the bash
    oracle's `case ... *)` branch prints ONLY the one-line usage string, NOT
    the full --populate/--finalize help block (that's reserved for the
    no-args branch). An earlier draft of this module wrongly reused the same
    `_usage()` helper for both branches — this test pins the divergence.
    """
    claude_home = _mkclaude_home(tmp_path, {})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    rc, out, err = _run(["--frobnicate"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 1
    assert out == ""
    assert err == (
        "ERROR: unknown subcommand: --frobnicate\n"
        "Usage: migrate-state-to-claude-klabauter.sh --populate | --finalize\n"
    )
    assert "--populate   Copy" not in err
    assert "--finalize   Delta" not in err


def test_missing_claude_home_exits_1(tmp_path, monkeypatch, capsys):
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()
    missing_home = tmp_path / "does-not-exist"

    rc, out, err = _run(["--populate"], missing_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 1
    assert "ERROR: CLAUDE_HOME not found at:" in err


# ---------------------------------------------------------------------------
# --populate (C6a)
# ---------------------------------------------------------------------------


def test_populate_copies_all_tree_pairs_and_leaves_originals_untouched(
    tmp_path, monkeypatch, capsys
):
    claude_home = _mkclaude_home(
        tmp_path,
        {
            "state/foo/a.txt": "hello\n",
            "archive/bar/z.txt": "arc\n",
            "docs/plans/plan1.md": "world\n",
            "docs/research/r.md": "research\n",
            "docs/problems/p.md": "problem\n",
        },
    )
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    rc, out, err = _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert err == ""
    assert "populate complete" in out

    for rel in [
        "state/foo/a.txt",
        "archive/bar/z.txt",
        "docs/plans/plan1.md",
        "docs/research/r.md",
        "docs/problems/p.md",
    ]:
        assert (claude_klabauter_root / rel).is_file()
        # Originals untouched.
        assert (claude_home / rel).is_file()


def test_populate_skips_absent_source_trees(tmp_path, monkeypatch, capsys):
    # No state/, no docs/{research,problems} at all -- only docs/plans present.
    claude_home = _mkclaude_home(tmp_path, {"docs/plans/plan1.md": "world\n"})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    rc, out, err = _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert "SKIP (source absent)" in out
    assert (claude_klabauter_root / "docs/plans/plan1.md").is_file()
    assert not (claude_klabauter_root / "state").exists()


def test_populate_is_idempotent(tmp_path, monkeypatch, capsys):
    claude_home = _mkclaude_home(tmp_path, {"state/a.txt": "v1\n"})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)
    # Overwrite source, re-populate -- destination should reflect the update.
    (claude_home / "state/a.txt").write_text("v2\n")
    rc, _out, _err = _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert (claude_klabauter_root / "state/a.txt").read_text() == "v2\n"


# ---------------------------------------------------------------------------
# --finalize (C6b)
# ---------------------------------------------------------------------------


def test_finalize_removes_source_after_successful_populate(tmp_path, monkeypatch, capsys):
    claude_home = _mkclaude_home(
        tmp_path,
        {"state/foo/a.txt": "hello\n", "docs/plans/plan1.md": "world\n"},
    )
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)
    rc, out, err = _run(["--finalize"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert "Guard PASSED" in out
    assert "finalize complete" in out
    assert err == ""

    # Source files gone, dest files retained.
    assert not (claude_home / "state/foo/a.txt").exists()
    assert not (claude_home / "docs/plans/plan1.md").exists()
    assert (claude_klabauter_root / "state/foo/a.txt").is_file()
    assert (claude_klabauter_root / "docs/plans/plan1.md").is_file()

    # Emptied source directories are cleaned up.
    assert not (claude_home / "state").exists()
    assert not (claude_home / "docs" / "plans").exists()


def test_finalize_delta_resyncs_files_written_after_populate(tmp_path, monkeypatch, capsys):
    claude_home = _mkclaude_home(tmp_path, {"state/a.txt": "v1\n"})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    # Simulate a write to CLAUDE_HOME that happened after --populate ran, with
    # an mtime strictly newer than the claude-klabauter copy.
    src = claude_home / "state/a.txt"
    dst = claude_klabauter_root / "state/a.txt"
    src.write_text("v2\n")
    newer = os.path.getmtime(dst) + 5
    os.utime(src, (newer, newer))

    rc, out, _err = _run(["--finalize"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert "Delta-synced 1 files" in out
    assert dst.read_text() == "v2\n"


def test_finalize_guard_fails_closed_when_claude_klabauter_copy_missing(tmp_path, monkeypatch, capsys):
    """Fail-closed edge: if a source file cannot be confirmed present in
    claude-klabauter (e.g. the delta-copy itself failed), --finalize MUST abort before
    removing anything, with a dedicated business exit code (1) distinct from
    a transport failure. Simulated by monkeypatching `shutil.copy2` to raise
    OSError during delta re-sync (a `chmod(0o555)`-on-directory trick does NOT
    work here: Windows ignores POSIX mode bits on directories, so the copy
    would silently succeed and the guard would never fire).
    """
    claude_home = _mkclaude_home(tmp_path, {"state/orphan.txt": "orphan\n"})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    (claude_klabauter_root / "state").mkdir(parents=True)

    def _raise_oserror(*args, **kwargs):
        raise OSError("simulated unwritable destination")

    monkeypatch.setattr(mstm.shutil, "copy2", _raise_oserror)

    rc, out, err = _run(["--finalize"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 1
    assert "GUARD FAIL: claude-klabauter copy missing for:" in err
    assert "Aborting --finalize" in err
    # Fail-closed: source file was NOT removed.
    assert (claude_home / "state/orphan.txt").is_file()


def test_finalize_skips_absent_source_trees(tmp_path, monkeypatch, capsys):
    claude_home = _mkclaude_home(tmp_path, {"state/a.txt": "hello\n"})
    claude_klabauter_root = tmp_path / "claude_klabauter_root"
    claude_klabauter_root.mkdir()

    _run(["--populate"], claude_home, claude_klabauter_root, monkeypatch, capsys)
    rc, out, _err = _run(["--finalize"], claude_home, claude_klabauter_root, monkeypatch, capsys)

    assert rc == 0
    assert "SKIP (source absent)" in out


# ---------------------------------------------------------------------------
# CLAUDE_KLABAUTER_ROOT default resolution
# ---------------------------------------------------------------------------


def test_default_claude_klabauter_root_is_this_modules_own_repo_root():
    root = mstm._default_claude_klabauter_root()
    assert (Path(root) / "coordinator_core" / "ops" / "migrate_state_to_claude_klabauter.py").is_file()
