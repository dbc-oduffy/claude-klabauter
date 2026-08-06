"""
Tests for coordinator_core.ops.check_version_consistency.

Mirrors the AC1-AC11 acceptance criteria of the bash oracle so the Python
port is provably parity-equivalent, not just "looks about right".

Port of: test-check-version-consistency.sh (example-doctrine-repo 894d4bc6, 2026-07-22)
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.ops.check_version_consistency import main


def _make_bundle(
    root: Path,
    pver: str,
    mver: str,
    cver: str,
    layout: str = "dist",
    mode: str = "",
) -> None:
    (root / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "coordinator" / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (root / "coordinator" / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "coordinator",\n  "version": "%s"\n}\n' % pver
    )
    (root / ".claude-plugin" / "marketplace.json").write_text(
        '{\n  "name": "coordinator-claude",\n  "metadata": {\n    "version": "%s"\n  },\n'
        '  "plugins": [\n    { "name": "coordinator", "source": "./coordinator", "tags": ["x"] }\n  ]\n}\n'
        % mver
    )
    if mode != "no-changelog":
        if layout == "flat":
            cl = root / "CHANGELOG.md"
        else:
            cl = root / "coordinator" / "dist" / "publish-repo-toplevel" / "CHANGELOG.md"
            cl.parent.mkdir(parents=True, exist_ok=True)
        cl.write_text(
            "# Changelog\n\n## [Unreleased]\n\n- pending\n\n## [%s] — 2026-06-22\n\n"
            "- shipped\n\n## [2.0.0] — 2026-01-01\n\n- old\n" % cver
        )


def _run(root: Path, *extra_args: str) -> int:
    return main(["--root", str(root), *extra_args])


def test_ac1_all_surfaces_agree(tmp_path, capsys):
    d = tmp_path / "ac1"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 0


def test_ac2_plugin_json_mismatch(tmp_path):
    d = tmp_path / "ac2"
    _make_bundle(d, "2.8.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 1


def test_ac3_marketplace_mismatch(tmp_path):
    d = tmp_path / "ac3"
    _make_bundle(d, "2.9.0", "2.1.1", "2.9.0")
    assert _run(d, "--quiet") == 1


def test_ac4_changelog_mismatch(tmp_path):
    d = tmp_path / "ac4"
    _make_bundle(d, "2.9.0", "2.9.0", "2.8.1")
    assert _run(d, "--quiet") == 1


def test_ac5_unreleased_skipped(tmp_path):
    d = tmp_path / "ac5"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    assert _run(d, "--quiet") == 0


def test_ac6_missing_changelog_fails_loud(tmp_path):
    d = tmp_path / "ac6"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0", mode="no-changelog")
    assert _run(d, "--quiet") == 1


def test_ac7_flat_layout(tmp_path):
    d = tmp_path / "ac7"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0", layout="flat")
    assert _run(d, "--quiet") == 0


def _git_bundle(d: Path, tag: Optional[str] = None) -> None:
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=d,
        check=True,
    )
    if tag:
        subprocess.run(
            ["git", "-c", "tag.gpgSign=false", "-c", "tag.forcesignannotated=false", "tag", tag],
            cwd=d,
            check=True,
        )


def test_ac8_check_tag_mismatch_is_advisory(tmp_path, capsys):
    d = tmp_path / "ac8"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d, "v2.8.0")
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" in captured.err


def test_ac9_plugin_entry_version_ignored(tmp_path):
    d = tmp_path / "ac9"
    (d / ".claude-plugin").mkdir(parents=True)
    (d / "coordinator" / ".claude-plugin").mkdir(parents=True)
    (d / "coordinator" / ".claude-plugin" / "plugin.json").write_text(
        '{\n  "name": "coordinator",\n  "version": "2.9.0"\n}\n'
    )
    (d / ".claude-plugin" / "marketplace.json").write_text(
        '{\n  "name": "coordinator-claude",\n  "metadata": {\n    "version": "2.9.0"\n  },\n'
        '  "plugins": [\n    { "name": "coordinator", "source": "./coordinator", '
        '"version": "9.9.9", "tags": ["x"] }\n  ]\n}\n'
    )
    (d / "CHANGELOG.md").write_text("# Changelog\n\n## [2.9.0] — 2026-06-22\n\n- x\n")
    assert _run(d, "--quiet") == 0


def test_ac10_check_tag_matching(tmp_path, capsys):
    d = tmp_path / "ac10"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d, "v2.9.0")
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" not in captured.err


def test_ac11_check_tag_no_tags(tmp_path, capsys):
    d = tmp_path / "ac11"
    _make_bundle(d, "2.9.0", "2.9.0", "2.9.0")
    _git_bundle(d)
    rc = _run(d, "--check-tag")
    captured = capsys.readouterr()
    assert rc == 0
    assert "NOTE" not in captured.err


def test_unknown_arg_exits_2():
    assert main(["--bogus"]) == 2


def test_help_exits_0(capsys):
    assert main(["--help"]) == 0
    assert "check-version-consistency" in capsys.readouterr().out


def test_bad_root_reproduces_oracle_double_message_bug(tmp_path, capsys):
    """Byte-parity regression test for a genuine bash-oracle bug: `fail()` inside
    `discover_root()`'s command-substitution subshell only kills the subshell, so
    the parent script silently continues with BUNDLE_ROOT="" and fails a SECOND
    time at the CHANGELOG-resolution step. Verified against the live bash oracle
    on 2026-07-16 (Port of: check-version-consistency.sh, example-doctrine-repo 894d4bc6, 2026-07-22):
    `bash check-version-consistency.sh --root /nonexistent` prints both lines
    below and exits 1. This module reproduces that exactly, not just the exit code.
    """
    rc = main(["--root", str(tmp_path / "nonexistent")])
    captured = capsys.readouterr()
    assert rc == 1
    assert "no .claude-plugin/marketplace.json under --root" in captured.err
    assert "no CHANGELOG.md found" in captured.err
