"""Tests for coordinator_core.ops.verify_dist_publish_repo_sync.

Port of: verify-dist-publish-repo-sync.sh (example-doctrine-repo b5a4192c, 2026-07-20),
snapshotted 2026-07-17. Positive corpus = a synthetic
plugin_root/publish_repo_root pair built under tmp_path (identical files,
one MISMATCH, one MISSING, one IGNORED). Negative corpus = missing source
dirs, missing publish-repo dirs, and unresolved publish-repo-root env.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import verify_dist_publish_repo_sync as vdprs


def _build_tree(tmp_path: Path):
    plugin_root = tmp_path / "plugin"
    dist_toplevel = plugin_root / "dist" / "publish-repo-toplevel"
    dist_docs = plugin_root / "dist" / "publish-repo-docs"
    dist_toplevel.mkdir(parents=True)
    dist_docs.mkdir(parents=True)

    pub_root = tmp_path / "publish-repo"
    pub_docs = pub_root / "docs"
    pub_root.mkdir(parents=True)
    pub_docs.mkdir(parents=True)

    (dist_toplevel / "README.md").write_text("hello\n", encoding="utf-8")
    (pub_root / "README.md").write_text("hello\n", encoding="utf-8")

    (dist_toplevel / "CHANGED.md").write_text("new content\n", encoding="utf-8")
    (pub_root / "CHANGED.md").write_text("old content\n", encoding="utf-8")

    (dist_toplevel / "NEWFILE.md").write_text("brand new\n", encoding="utf-8")
    # NEWFILE.md deliberately absent from pub_root -> MISSING

    (dist_toplevel / "IGNOREME.md").write_text("infra-owned\n", encoding="utf-8")
    (dist_toplevel / ".percolate-ignore").write_text(
        "# comment\n\nIGNOREME.md\n", encoding="utf-8"
    )

    (dist_docs / "guide.md").write_text("docs content\n", encoding="utf-8")
    (pub_docs / "guide.md").write_text("docs content\n", encoding="utf-8")

    return plugin_root, pub_root


def test_all_identical_exits_0(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "dist" / "publish-repo-toplevel").mkdir(parents=True)
    (plugin_root / "dist" / "publish-repo-docs").mkdir(parents=True)
    pub_root = tmp_path / "publish-repo"
    (pub_root / "docs").mkdir(parents=True)
    (plugin_root / "dist" / "publish-repo-toplevel" / "a.md").write_text("x\n", encoding="utf-8")
    (pub_root / "a.md").write_text("x\n", encoding="utf-8")

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("COORDINATOR_CLAUDE_PUBLISH_REPO", str(pub_root))

    rc = vdprs.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK         toplevel/a.md" in out
    assert "DRIFT DETECTED" not in out


def test_mismatch_missing_ignored_exits_1(tmp_path, monkeypatch, capsys):
    plugin_root, pub_root = _build_tree(tmp_path)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("COORDINATOR_CLAUDE_PUBLISH_REPO", str(pub_root))

    rc = vdprs.main([])
    out = capsys.readouterr().out

    assert rc == 1
    assert "OK         toplevel/README.md" in out
    assert "MISMATCH   toplevel/CHANGED.md" in out
    assert "MISSING    toplevel/NEWFILE.md" in out
    assert "IGNORED    toplevel/IGNOREME.md" in out
    assert "OK         docs/guide.md" in out
    # the .percolate-ignore file itself is not exempted from the scan (it is
    # not self-listed in its own ignore file) -- faithfully reproduces the
    # bash oracle's unfiltered `find -maxdepth 1 -type f` listing, which also
    # walks .percolate-ignore and reports it MISSING (publish-repo-owned
    # infra files are not expected to exist source-side under dist/).
    assert "MISSING    toplevel/.percolate-ignore" in out
    assert "checked: 3, mismatched: 1, missing: 2" in out
    assert "DRIFT DETECTED" in out
    # unicode arrow/em-dash in the drift-detected remediation block must
    # survive the port unchanged (byte-parity with the bash oracle)
    assert "→ plugins/coordinator/" in out
    assert "report MISMATCH by design — the publish-tree version" in out


def test_missing_source_dir_exits_2(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "plugin-empty"
    plugin_root.mkdir()
    pub_root = tmp_path / "publish-repo"
    (pub_root / "docs").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("COORDINATOR_CLAUDE_PUBLISH_REPO", str(pub_root))

    rc = vdprs.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "source dir not found" in err


def test_missing_publish_repo_dir_exits_2(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "dist" / "publish-repo-toplevel").mkdir(parents=True)
    (plugin_root / "dist" / "publish-repo-docs").mkdir(parents=True)
    pub_root = tmp_path / "nonexistent-publish-repo"

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("COORDINATOR_CLAUDE_PUBLISH_REPO", str(pub_root))

    rc = vdprs.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "publish repo root not found" in err


def test_missing_plugin_root_env_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("COORDINATOR_CLAUDE_PUBLISH_REPO", raising=False)

    rc = vdprs.main([])
    err = capsys.readouterr().err
    assert rc == 2
    assert "CLAUDE_PLUGIN_ROOT is unset" in err


def test_publish_repo_root_unresolvable_exits_1(tmp_path, monkeypatch, capsys):
    plugin_root = tmp_path / "plugin"
    (plugin_root / "dist" / "publish-repo-toplevel").mkdir(parents=True)
    (plugin_root / "dist" / "publish-repo-docs").mkdir(parents=True)

    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
    monkeypatch.delenv("COORDINATOR_CLAUDE_PUBLISH_REPO", raising=False)
    monkeypatch.setattr(vdprs.shutil, "which", lambda name: None)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)

    rc = vdprs.main([])
    err = capsys.readouterr().err
    assert rc == 1
    assert "cannot resolve publish repo root" in err


def _write_fake_ml_stub(path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 1\n")
    path.chmod(0o755)


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_resolve_publish_repo_root_prefers_settings_home_over_legacy(monkeypatch, tmp_path):
    """Settings-home (DR-072, canonical) must be tried before the legacy
    ~/.claude/bin rung, retired 2026-07-28 and kept only for machines that
    predate the move — see [[check_registry_codename_leak]]/
    [[verify_ue_overrides]] sibling resolvers for the same ordering."""
    settings_ml = tmp_path / "settings" / "bin" / "machine-local"
    _write_fake_ml_stub(settings_ml)
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml_stub(legacy_ml)

    monkeypatch.delenv("COORDINATOR_CLAUDE_PUBLISH_REPO", raising=False)
    monkeypatch.setattr(vdprs.shutil, "which", lambda name: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []

    def _fake_run_machine_local(ml_bin, key):
        calls.append(ml_bin)
        return "/resolved/from/settings-home"

    monkeypatch.setattr(vdprs, "_run_machine_local", _fake_run_machine_local)

    result = vdprs._resolve_publish_repo_root()
    assert result == "/resolved/from/settings-home"
    assert calls == [str(settings_ml)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_resolve_publish_repo_root_falls_back_to_legacy_when_settings_home_absent(
    monkeypatch, tmp_path
):
    """Back-compat: a machine that predates the settings-home move must keep
    resolving via the legacy ~/.claude/bin rung."""
    legacy_ml = tmp_path / "home" / ".claude" / "bin" / "machine-local"
    _write_fake_ml_stub(legacy_ml)

    monkeypatch.delenv("COORDINATOR_CLAUDE_PUBLISH_REPO", raising=False)
    monkeypatch.setattr(vdprs.shutil, "which", lambda name: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-settings-here"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    calls = []

    def _fake_run_machine_local(ml_bin, key):
        calls.append(ml_bin)
        return "/resolved/from/legacy-home"

    monkeypatch.setattr(vdprs, "_run_machine_local", _fake_run_machine_local)

    result = vdprs._resolve_publish_repo_root()
    assert result == "/resolved/from/legacy-home"
    assert calls == [str(legacy_ml)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec-bit fixture")
def test_resolve_publish_repo_root_remediation_names_resolved_rung_not_hardcoded_path(
    monkeypatch, tmp_path, capsys
):
    """The (b) remediation line must name the machine-local binary actually
    resolved (here: the settings-home rung), never a hardcoded
    ~/.claude/bin/machine-local path — that path is stale once a machine has
    migrated off the legacy rung entirely."""
    settings_ml = tmp_path / "settings" / "bin" / "machine-local"
    _write_fake_ml_stub(settings_ml)

    monkeypatch.delenv("COORDINATOR_CLAUDE_PUBLISH_REPO", raising=False)
    monkeypatch.setattr(vdprs.shutil, "which", lambda name: None)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "settings"))
    monkeypatch.setenv("HOME", str(tmp_path / "nonexistent-home-for-test"))
    monkeypatch.setattr(vdprs, "_run_machine_local", lambda ml_bin, key: None)

    result = vdprs._resolve_publish_repo_root()
    assert result is None
    err = capsys.readouterr().err
    assert str(settings_ml) in err
    assert "~/.claude/bin/machine-local" not in err


def test_load_percolate_ignore_skips_blank_and_comment_lines(tmp_path):
    ignore_file = tmp_path / ".percolate-ignore"
    ignore_file.write_text("# a comment\n\nfoo.md\nbar/baz.md\n", encoding="utf-8")
    result = vdprs._load_percolate_ignore(ignore_file)
    assert result == {"foo.md", "bar/baz.md"}


def test_load_percolate_ignore_missing_file_returns_empty_set(tmp_path):
    result = vdprs._load_percolate_ignore(tmp_path / "does-not-exist")
    assert result == set()


def test_run_machine_local_passes_timeout_and_devnull_stdin(monkeypatch):
    """P1 unbounded-hang guard (porter-brief addendum § 2): every subprocess
    call to the external `machine-local` binary must cap wall-clock time and
    must not block waiting on stdin — a hung/interactive machine-local binary
    must not wedge this gate script.
    """
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="/some/path\n", stderr="")

    monkeypatch.setattr(vdprs.subprocess, "run", _fake_run)
    value = vdprs._run_machine_local("machine-local", "publish.mirrors.coordinator_claude.path")

    assert value == "/some/path"
    assert captured.get("timeout") == vdprs._SUBPROCESS_TIMEOUT_SECS
    assert captured.get("stdin") == subprocess.DEVNULL


def test_run_machine_local_timeout_returns_none(monkeypatch):
    def _fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(vdprs.subprocess, "run", _fake_run)
    value = vdprs._run_machine_local("machine-local", "some.key")
    assert value is None


def test_check_pair_byte_diff_not_shallow(tmp_path):
    """filecmp.cmp(shallow=False) must be used -- a shallow compare trusts
    os.stat metadata and would report a false OK after a publish.sh rsync
    run touches mtime without changing content.
    """
    src = tmp_path / "src.md"
    dst = tmp_path / "dst.md"
    src.write_text("same content\n", encoding="utf-8")
    dst.write_text("same content\n", encoding="utf-8")
    # Force differing mtimes -- a shallow comparison could be fooled by this
    os.utime(dst, (1000000000, 1000000000))

    counters = vdprs._Counters()
    out = []
    vdprs._check_pair(src, dst, "rel/src.md", counters, out)
    assert out == ["OK         rel/src.md"]
    assert counters.checked == 1
    assert counters.mismatch == 0


def test_module_docstring_and_drift_message_have_no_dead_publish_sh_command():
    # Regression pin: setup/publish.sh was retired repo-wide by example-doctrine-repo's
    # percolate-python-port work (2026-07-21/22). Neither the module
    # docstring's "no --fix mode" rationale nor the runtime drift-detected
    # remediation may still name the dead bash command.
    assert "bash setup/publish.sh" not in vdprs.__doc__
    assert "python coordinator/bin/publish.py <target>" in vdprs.__doc__

    import inspect

    source = inspect.getsource(vdprs.main)
    assert "bash setup/publish.sh" not in source
    assert "python coordinator/bin/publish.py <target>" in source
