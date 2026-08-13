"""Characterization + parity tests for coordinator_core.ops.backfill_initiative_fk.

Port of: backfill-initiative-fk.sh (coordinator-claude 432e3285, 2026-07-22)
Mirrors the oracle's own six test cases (test-backfill-initiative-fk.sh, coordinator-claude 432e3285,
2026-07-22) plus unit coverage of the lock primitives and platform-portability additions.
"""
from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

import coordinator_core.ops.backfill_initiative_fk as mod
from coordinator_core.ops.backfill_initiative_fk import (
    _acquire_lock,
    _already_attached,
    _pid_is_live,
    _process_pairs,
    _release_lock,
    _strip_spaces,
    main,
)


def _make_fake_coordinator_initiative(bin_dir: Path, known_ids: set) -> Path:
    """Write a fake python3 `coordinator-initiative` that supports `attach <path> <id>`:
    succeeds (writes `initiative: <id>` into the artifact frontmatter) for ids in
    `known_ids`, fails loud for unknown ids -- mirrors the real CLI's contract
    closely enough to exercise this module's subprocess call + exit-code handling.

    Python, not bash: the real `coordinator-initiative` was ported to python3
    (coordinator-claude commit 6fb5fb37) and this module invokes it via `sys.executable`
    directly, so the fixture must be python source for that invocation to succeed."""
    script = bin_dir / "coordinator-initiative"
    known_ids_repr = repr(sorted(known_ids))
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"known_ids = set({known_ids_repr})\n"
        "if sys.argv[1] != 'attach':\n"
        "    print(f'unsupported verb: {sys.argv[1]}', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "artifact_path, target = sys.argv[2], sys.argv[3]\n"
        "if target not in known_ids:\n"
        "    print(f'unknown initiative-id: {target}', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "with open(artifact_path, 'r', encoding='utf-8') as fh:\n"
        "    contents = fh.read()\n"
        "if any(line.startswith('initiative:') for line in contents.splitlines()):\n"
        "    sys.exit(0)\n"
        "with open(artifact_path, 'a', encoding='utf-8') as fh:\n"
        "    fh.write(f'initiative: {target}\\n')\n"
        "sys.exit(0)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _make_artifact(path: Path, extra_fm: str = "") -> None:
    body = "---\ntitle: test-artifact\ncreated: 2026-07-06\n"
    if extra_fm:
        body += extra_fm + "\n"
    body += "---\n\n# Test artifact body\n"
    path.write_text(body)


@pytest.fixture(autouse=True)
def _isolated_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    yield


# ---------------------------------------------------------------------------
# _strip_spaces — space-only trim (NOT tabs/other whitespace)
# ---------------------------------------------------------------------------


def test_strip_spaces_trims_only_literal_spaces():
    assert _strip_spaces("  hello  ") == "hello"
    assert _strip_spaces("\thello\t") == "\thello\t"  # tabs untouched


# ---------------------------------------------------------------------------
# _already_attached — idempotency check, faithful unescaped-regex repro
# ---------------------------------------------------------------------------


def test_already_attached_true(tmp_path):
    p = tmp_path / "a.md"
    _make_artifact(p, "initiative: my-id")
    assert _already_attached(str(p), "my-id") is True


def test_already_attached_false_on_prefix_match(tmp_path):
    """Regression for the oracle's review-fix F2: end-anchored, so 'abc' must NOT
    match an attached value of 'abcdef'."""
    p = tmp_path / "a.md"
    _make_artifact(p, "initiative: abcdef")
    assert _already_attached(str(p), "abc") is False


def test_already_attached_false_missing_file():
    assert _already_attached("/nonexistent/path.md", "x") is False


# ---------------------------------------------------------------------------
# Lock primitives
# ---------------------------------------------------------------------------


def test_pid_is_live_true_for_self():
    assert _pid_is_live(os.getpid()) is True


def test_pid_is_live_false_for_bogus_pid():
    # A PID astronomically unlikely to exist.
    assert _pid_is_live(2**30) is False


def test_acquire_and_release_lock_roundtrip(tmp_path):
    lock_path = str(tmp_path / "test.lock")
    stream = io.StringIO()
    acquired, rc = _acquire_lock(lock_path, stream=stream)
    assert acquired is True
    assert rc == 0
    assert os.path.isfile(lock_path)
    with open(lock_path, encoding="utf-8") as fh:
        assert fh.read().strip() == str(os.getpid())

    _release_lock(lock_path)
    assert not os.path.isfile(lock_path)


def test_acquire_lock_blocked_by_live_holder(tmp_path):
    """Simulates a second invocation while a live process holds the lock --
    the current test process's own PID stands in for 'a live holder'."""
    lock_path = str(tmp_path / "test.lock")
    with open(lock_path, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()}\n")  # our own PID is definitely live

    stream = io.StringIO()
    acquired, rc = _acquire_lock(lock_path, stream=stream)
    assert acquired is False
    assert rc == 1
    assert "another instance is running" in stream.getvalue()


def test_acquire_lock_overwrites_stale_lock(tmp_path):
    lock_path = str(tmp_path / "test.lock")
    with open(lock_path, "w", encoding="utf-8") as fh:
        fh.write(f"{2**30}\n")  # bogus, definitely-dead PID

    stream = io.StringIO()
    acquired, rc = _acquire_lock(lock_path, stream=stream)
    assert acquired is True
    assert rc == 0
    with open(lock_path, encoding="utf-8") as fh:
        assert fh.read().strip() == str(os.getpid())


def test_release_lock_only_removes_own_lock(tmp_path):
    """Regression for the oracle's review-fix F1: a second invocation must NOT
    delete the legitimate first holder's lockfile on release."""
    lock_path = str(tmp_path / "test.lock")
    other_pid = os.getpid() + 1  # not us
    with open(lock_path, "w", encoding="utf-8") as fh:
        fh.write(f"{other_pid}\n")

    _release_lock(lock_path)
    assert os.path.isfile(lock_path)  # untouched -- we don't own it


# ---------------------------------------------------------------------------
# _process_pairs + main() — end-to-end via a fake coordinator-initiative
# ---------------------------------------------------------------------------


def test_happy_path(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"my-initiative"})

    artifact = tmp_path / "plan.md"
    _make_artifact(artifact)

    stdin = io.StringIO(f"{artifact}\tmy-initiative\n")
    old_stdin = sys.stdin
    sys.stdin = stdin
    try:
        rc = main([], script_dir=str(bin_dir))
    finally:
        sys.stdin = old_stdin

    assert rc == 0
    assert "initiative: my-initiative" in artifact.read_text()


def test_idempotent_rerun(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"stable-initiative"})

    artifact = tmp_path / "doc.md"
    _make_artifact(artifact, "initiative: stable-initiative")

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(f"{artifact}\tstable-initiative\n")

    rc1 = main([str(mapping)], script_dir=str(bin_dir))
    fk_after_first = artifact.read_text()
    rc2 = main([str(mapping)], script_dir=str(bin_dir))
    fk_after_second = artifact.read_text()

    assert rc1 == 0
    assert rc2 == 0
    assert fk_after_first == fk_after_second


def test_unknown_initiative_fail_loud(tmp_path, capfd):
    # capfd (fd-level), not capsys: the failure message this asserts on is
    # emitted by the fake coordinator-initiative CHILD PROCESS's stderr, which
    # bypasses Python's sys.stdout/stderr objects entirely.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, set())  # no known ids

    artifact = tmp_path / "other.md"
    _make_artifact(artifact)

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(f"{artifact}\tnonexistent-id\n")

    rc = main([str(mapping)], script_dir=str(bin_dir))
    assert rc == 1
    captured = capfd.readouterr()
    assert "nonexistent-id" in (captured.out + captured.err)


def test_write_then_redetect(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"write-detect-initiative"})

    artifact = tmp_path / "fresh.md"
    _make_artifact(artifact)

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(f"{artifact}\twrite-detect-initiative\n")

    rc1 = main([str(mapping)], script_dir=str(bin_dir))
    assert rc1 == 0
    assert "initiative: write-detect-initiative" in artifact.read_text()

    rc2 = main([str(mapping)], script_dir=str(bin_dir))
    assert rc2 == 0
    fk_count = artifact.read_text().count("initiative:")
    assert fk_count == 1  # no duplicate written by second run


def test_comment_blank_skipped(tmp_path, capsys):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"comment-test-initiative"})

    artifact = tmp_path / "comment-test.md"
    _make_artifact(artifact)

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(f"\n# this is a comment\n{artifact}\tcomment-test-initiative\n")

    rc = main([str(mapping)], script_dir=str(bin_dir))
    assert rc == 0
    captured = capsys.readouterr()
    assert "attached=1" in captured.out
    assert "errors=0" in captured.out


def test_multi_failure_collection(tmp_path, capfd):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, set())

    artifact1 = tmp_path / "fail1.md"
    artifact2 = tmp_path / "fail2.md"
    _make_artifact(artifact1)
    _make_artifact(artifact2)

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(
        f"{artifact1}\tunknown-id-alpha\n{artifact2}\tunknown-id-beta\n"
    )

    rc = main([str(mapping)], script_dir=str(bin_dir))
    assert rc == 1
    captured = capfd.readouterr()
    combined = captured.out + captured.err
    assert "unknown-id-alpha" in combined
    assert "unknown-id-beta" in combined
    assert "errors=2" in combined


# ---------------------------------------------------------------------------
# Platform-edge case (addendum §2, subprocess timeout rule): a hung
# `coordinator-initiative attach` child must not block the whole batch --
# degrade to a counted error and keep processing remaining pairs. Exercises
# the fixed-timeout path via a monkeypatched subprocess.run, not a real
# _ATTACH_TIMEOUT_SECS-second wait. Mirrors
# test_verify_arch_audit_atlas_refresh.py::test_git_timeout_degrades_gracefully
# and test_multi_failure_collection's "collect all failures, don't stop at
# first" assertion shape, but for the timeout path specifically.
# ---------------------------------------------------------------------------
def test_attach_timeout_counted_as_error_and_batch_continues(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"good-id"})

    timeout_artifact = tmp_path / "timeout.md"
    ok_artifact = tmp_path / "ok.md"
    _make_artifact(timeout_artifact)
    _make_artifact(ok_artifact)

    real_run = subprocess.run

    def _raise_timeout_for_timeout_artifact(cmd, *a, **kw):
        if str(timeout_artifact) in cmd:
            raise subprocess.TimeoutExpired(cmd, mod._ATTACH_TIMEOUT_SECS)
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(mod.subprocess, "run", _raise_timeout_for_timeout_artifact)

    coordinator_initiative_path = os.path.join(str(bin_dir), "coordinator-initiative")
    lines = [
        f"{timeout_artifact}\ttimeout-id\n",
        f"{ok_artifact}\tgood-id\n",
    ]
    out = io.StringIO()
    err = io.StringIO()

    attached, skipped, errors = _process_pairs(
        lines, coordinator_initiative_path, out=out, err=err
    )

    # Timed-out pair is counted as an error, not silently dropped...
    assert errors == 1
    assert "FAILED pair (timeout" in err.getvalue()
    assert str(timeout_artifact) in err.getvalue()
    # ...and the batch continues: the second (non-timing-out) pair still attaches.
    assert attached == 1
    assert skipped == 0


def test_mapping_file_not_found(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, set())

    rc = main([str(tmp_path / "does-not-exist.tsv")], script_dir=str(bin_dir))
    assert rc == 1


def test_coordinator_initiative_missing(tmp_path):
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    rc = main([], script_dir=str(bin_dir))
    assert rc == 1


def test_three_column_tsv_row_trims_extra_columns(tmp_path):
    """F9 regression: a 3rd tab-separated column must not leak into initiative_id."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_coordinator_initiative(bin_dir, {"my-id"})

    artifact = tmp_path / "plan.md"
    _make_artifact(artifact)

    mapping = tmp_path / "mapping.tsv"
    mapping.write_text(f"{artifact}\tmy-id\textra-column\n")

    rc = main([str(mapping)], script_dir=str(bin_dir))
    assert rc == 0
    assert "initiative: my-id" in artifact.read_text()
