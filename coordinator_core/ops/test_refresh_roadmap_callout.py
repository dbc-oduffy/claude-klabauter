"""
Tests for coordinator_core.ops.refresh_roadmap_callout.

Port of: test-refresh-roadmap-callout.sh (DoE a1a568d2, 2026-07-22), plus
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
from coordinator_core.win_portability import (
    no_console_creationflags,
    no_console_passthrough_kwargs,
)

# The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue and is
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


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
    # 2026-07-17 (refresh-queries BIG_PORT item): the delegate target
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
        (".hidden", True),
    ],
)
def test_validate_roadmap_id(value, expected):
    if value == ".hidden":
        expected = False
    assert _validate_roadmap_id(value) == expected


def test_no_stub_index_at_all_is_clean_noop(tmp_path, capsys):
    rc = main(["nope", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no STUB-INDEX.md" in out


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(root), check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(root), check=True, **no_console_passthrough_kwargs())
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(root), check=True, **no_console_passthrough_kwargs())
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "README.md"], cwd=str(root), check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed"],
        cwd=str(root), check=True, **no_console_passthrough_kwargs(),
    )


def _log_subjects(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "log", "--format=%s"], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )
    return result.stdout.splitlines()


def test_self_commit_true_commits_changed_stub_index(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs(),
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
        **no_console_creationflags(),
    )
    assert show.stdout.strip() == "state/roadmap/test-roadmap-a/STUB-INDEX.md"


def test_self_commit_true_no_change_produces_no_commit(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs(),
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
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs(),
    )
    before = _log_subjects(tmp_path)
    fake_cc_root = tmp_path / "fake-cc-root"
    fake_cc_root.mkdir()
    monkeypatch.setattr(refresh_roadmap_callout, "_resolve_cc_root", lambda: str(fake_cc_root))
    monkeypatch.setattr(refresh_roadmap_callout, "_is_trusted_root", lambda cc_root: True)

    def _fake_refresh_fails(argv):
        stub_index.write_text(CALLOUT_BODY.replace("STALE PLACEHOLDER TEXT", "PARTIAL"), encoding="utf-8")
        return 1

    with mock.patch("coordinator_core.text.refresh_queries.main", side_effect=_fake_refresh_fails):
        rc = main(["test-roadmap-a", "--root", str(tmp_path)], self_commit=True)

    assert rc == 1
    assert _log_subjects(tmp_path) == before
    assert not (tmp_path / "state" / "housekeeping-failures.log").exists()


def test_self_commit_false_default_never_commits(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    stub_index = _make_stub_index(tmp_path, "test-roadmap-a", CALLOUT_BODY)
    subprocess.run(["git", "add", "--", "state"], cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs())
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-m", "seed stub-index"],
        cwd=str(tmp_path), check=True, **no_console_passthrough_kwargs(),
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
        rc = main(["test-roadmap-a", "--root", str(tmp_path)])

    assert rc == 0
    assert _log_subjects(tmp_path) == before


def _seed_doe_pointer(tmp_path, monkeypatch, doe_root: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".doe-root").write_text(str(doe_root) + "\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_HOME", str(home))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


def test_resolve_cc_root_private_layout(tmp_path, monkeypatch):
    doe_root = tmp_path / "doe-clone"
    (doe_root / "coordinator").mkdir(parents=True)
    _seed_doe_pointer(tmp_path, monkeypatch, doe_root)
    assert refresh_roadmap_callout._resolve_cc_root() == str(doe_root / "coordinator")


def test_resolve_cc_root_flat_mirror_layout(tmp_path, monkeypatch):
    doe_root = tmp_path / "flat-mirror"
    (doe_root / ".claude-plugin").mkdir(parents=True)
    (doe_root / ".claude-plugin" / "plugin.json").write_text("{}\n", encoding="utf-8")
    _seed_doe_pointer(tmp_path, monkeypatch, doe_root)
    assert refresh_roadmap_callout._resolve_cc_root() == str(doe_root)


def test_resolve_cc_root_bare_directory_is_unresolved(tmp_path, monkeypatch):
    doe_root = tmp_path / "bare"
    doe_root.mkdir()
    _seed_doe_pointer(tmp_path, monkeypatch, doe_root)
    assert refresh_roadmap_callout._resolve_cc_root() == ""
