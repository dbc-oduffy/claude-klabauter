"""
Tests for coordinator_core.ops.refresh_roadmap_callout.

Port of: test-refresh-roadmap-callout.sh (example-doctrine-repo a1a568d2, 2026-07-22), plus
additional negative-corpus coverage (missing arg, invalid roadmap_id,
traversal, no-callout-marker) captured from the bash oracle during the
DOE-PORT parity gate.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coordinator_core.ops import refresh_roadmap_callout
from coordinator_core.ops.refresh_roadmap_callout import (
    _strip_one_quote_layer,
    _validate_roadmap_id,
    main,
)


def _make_stub_index(root: Path, roadmap_id: str, body: str) -> Path:
    roadmap_dir = root / "state" / "roadmap" / roadmap_id
    roadmap_dir.mkdir(parents=True, exist_ok=True)
    stub_index = roadmap_dir / "STUB-INDEX.md"
    stub_index.write_text(body, encoding="utf-8")
    return stub_index


CALLOUT_BODY = """# STUB-INDEX: test-roadmap-a

<!-- BEGIN query: handoff where=roadmap_id=test-roadmap-a sort=sprint -->
STALE PLACEHOLDER TEXT
<!-- END query -->
"""


def test_known_roadmap_with_callout_invokes_refresh(tmp_path, capsys, monkeypatch):
    # Review: code-reviewer -- the prior version of this test asserted
    # `rc is not None` (always true for an int-returning function) and
    # `X or True` (always true regardless of X), providing zero real
    # coverage of the "known roadmap with a callout delegates to the
    # refresh-queries rendering engine" path.
    #
    # 2026-07-17 (refresh-queries BIG_PORT item): the delegate target
    # switched from `subprocess.run(["node", ".../refresh-queries.js", ...])`
    # to a direct in-process import of
    # `coordinator_core.text.refresh_queries.main` (refresh-queries.js is
    # retired). Mock that import target (patched at its use site inside
    # refresh_roadmap_callout, matching the local-import-inside-main shape)
    # and assert it was invoked with the expected argv instead.
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)
    with mock.patch("coordinator_core.text.refresh_queries.main") as mock_main:
        mock_main.return_value = 0
        rc = main(["test-roadmap-a", "--root", str(tmp_path)])
    assert rc == 0
    mock_main.assert_called_once_with(
        ["--files", str(stub_index), "--root", str(tmp_path)],
    )


def test_unknown_roadmap_id_is_clean_noop(tmp_path, capsys):
    rc = main(["no-such-roadmap-xyz", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no-op" in out.lower()


def test_no_callout_marker_is_clean_noop(tmp_path, capsys):
    _make_stub_index(tmp_path, "no-callout", "# no callout here\njust text\n")
    rc = main(["no-callout", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no-op" in out.lower()
    assert "no query callout" in out


def test_missing_roadmap_id_arg_fails_loud(capsys):
    rc = main(["--root", "/tmp/whatever"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "requires <roadmap_id>" in err


def test_invalid_roadmap_id_rejected(capsys):
    rc = main(["bad;id", "--root", "/tmp/whatever"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid roadmap_id" in err


def test_traversal_roadmap_id_rejected(capsys):
    rc = main(["..evil", "--root", "/tmp/whatever"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "invalid roadmap_id" in err


def test_help_flag_exits_zero(capsys):
    rc = main(["-h"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Usage:" in out


def test_quote_strip_single_layer():
    assert _strip_one_quote_layer('"v3split"') == "v3split"
    assert _strip_one_quote_layer("'v3split'") == "v3split"
    assert _strip_one_quote_layer("v3split") == "v3split"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("v3split", True),
        ("test-roadmap-a", True),
        ("a.b_c-1", True),
        ("", False),
        ("bad;id", False),
        ("..evil", False),
        ("/etc/passwd", False),
        (".hidden", True),  # allowlist first-char class includes '.'? -> mirrors regex, see note below
    ],
)
def test_validate_roadmap_id(value, expected):
    # Note: the allowlist regex's first-char class is [A-Za-z0-9] only, so a
    # leading '.' is actually rejected — this parametrize case documents the
    # bash oracle's own behavior (not this module inventing stricter rules).
    if value == ".hidden":
        expected = False
    assert _validate_roadmap_id(value) == expected


def test_no_stub_index_at_all_is_clean_noop(tmp_path, capsys):
    rc = main(["nope", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no STUB-INDEX.md" in out


# ---------------------------------------------------------------------------
# self_commit (C5 residue, 2026-07-23 wsc-tail-slim-down): the detached-render
# self-commit disposition -- opt-in only, exercised by the
# coordinator/bin/refresh-roadmap-callout.py CLI trampoline.
# ---------------------------------------------------------------------------


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True)
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], cwd=str(root), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed"],
        cwd=str(root), check=True,
    )


def _log_subjects(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(root), capture_output=True, text=True, check=True,
    )
    return result.stdout.splitlines()


def test_self_commit_true_commits_changed_stub_index(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True,
    )
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)

    def _fake_refresh(argv):
        stub_index.write_text(CALLOUT_BODY.replace("STALE PLACEHOLDER TEXT", "REFRESHED"), encoding="utf-8")
        return 0

    with mock.patch("coordinator_core.text.refresh_queries.main", side_effect=_fake_refresh):
        rc = main(["test-roadmap-a", "--root", str(tmp_path)], self_commit=True)

    assert rc == 0
    subjects = _log_subjects(tmp_path)
    assert subjects[0] == "roadmap: refresh test-roadmap-a STUB-INDEX callout (detached render)"
    show = subprocess.run(
        ["git", "show", "--name-only", "--format="], cwd=str(tmp_path),
        capture_output=True, text=True, check=True,
    )
    assert show.stdout.strip() == "state/roadmap/test-roadmap-a/STUB-INDEX.md"


def test_self_commit_true_no_change_produces_no_commit(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True,
    )
    before = _log_subjects(tmp_path)
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)

    with mock.patch("coordinator_core.text.refresh_queries.main", return_value=0):
        rc = main(["test-roadmap-a", "--root", str(tmp_path)], self_commit=True)

    assert rc == 0
    assert _log_subjects(tmp_path) == before
    assert not (tmp_path / "state" / "housekeeping-failures.log").exists()


def test_self_commit_true_skips_commit_on_failed_render(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True,
    )
    before = _log_subjects(tmp_path)
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)

    def _fake_refresh_fails(argv):
        # Even though it mutates the file, a non-zero rc must NOT be committed
        # ("do not fire into a failed render").
        stub_index.write_text(CALLOUT_BODY.replace("STALE PLACEHOLDER TEXT", "PARTIAL"), encoding="utf-8")
        return 1

    with mock.patch("coordinator_core.text.refresh_queries.main", side_effect=_fake_refresh_fails):
        rc = main(["test-roadmap-a", "--root", str(tmp_path)], self_commit=True)

    assert rc == 1
    assert _log_subjects(tmp_path) == before  # no follow-up commit landed
    assert not (tmp_path / "state" / "housekeeping-failures.log").exists()


def test_self_commit_false_default_never_commits(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True,
    )
    before = _log_subjects(tmp_path)
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)

    def _fake_refresh(argv):
        stub_index.write_text(CALLOUT_BODY.replace("STALE PLACEHOLDER TEXT", "REFRESHED"), encoding="utf-8")
        return 0

    with mock.patch("coordinator_core.text.refresh_queries.main", side_effect=_fake_refresh):
        rc = main(["test-roadmap-a", "--root", str(tmp_path)])  # self_commit defaults False

    assert rc == 0
    assert _log_subjects(tmp_path) == before  # unchanged -- default preserves the
    # pre-existing extra_stage_paths-coupled behavior for the still-live
    # synchronous ceremony-tail caller.
