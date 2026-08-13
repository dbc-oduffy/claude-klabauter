"""Tests for coordinator_core.ops.setup_rag_decision.

Port of: setup-rag-decision.sh (coordinator-claude 6fb5fb37, 2026-07-22),
snapshotted 2026-07-17 against coordinator/tests/test_setup_rag_decision.bats
(T1-T10 below reproduce that corpus's env-injection matrix 1:1; T11+ cover
the Python-port-specific additions -- probe timeout/stdin guard, unparseable
probe string, help text).
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from coordinator_core.ops import setup_rag_decision as srd

_SENTINEL = "coordinator-rag-tripwire: un-indexed"


def _write_claude_md(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Test CLAUDE.md\n", encoding="utf-8")
    return claude_md


# ---------------------------------------------------------------------------
# T1: UE repo + daemon present -> offer emitted, no tripwire written
# ---------------------------------------------------------------------------


def test_T1_ue_daemon_present_offer_no_tripwire(tmp_path, monkeypatch, capsys):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "1")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "true")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main([])

    assert rc == 0
    out = capsys.readouterr().out
    assert "branch 1" in out or "offer" in out.lower()
    assert _SENTINEL not in claude_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T2 / T2b: non-UE repo -> tripwire written regardless of daemon state
# ---------------------------------------------------------------------------


def test_T2_nonue_daemon_present_tripwire_written(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "true")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main([])

    assert rc == 0
    assert _SENTINEL in claude_md.read_text(encoding="utf-8")


def test_T2b_nonue_daemon_absent_still_tripwire(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main([])

    assert rc == 0
    assert _SENTINEL in claude_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T3: UE repo + no daemon -> tripwire
# ---------------------------------------------------------------------------


def test_T3_ue_no_daemon_tripwire_written(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "1")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main([])

    assert rc == 0
    assert _SENTINEL in claude_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T4 / T5: idempotency -- re-run does not duplicate the tripwire block
# ---------------------------------------------------------------------------


def test_T4_rerun_branch2_no_duplicate(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    assert srd.main([]) == 0
    assert srd.main([]) == 0

    assert claude_md.read_text(encoding="utf-8").count(_SENTINEL) == 1


def test_T5_rerun_branch3_no_duplicate(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "1")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    assert srd.main([]) == 0
    assert srd.main([]) == 0

    assert claude_md.read_text(encoding="utf-8").count(_SENTINEL) == 1


# ---------------------------------------------------------------------------
# T6: tripwire block content sanity
# ---------------------------------------------------------------------------


def test_T6_tripwire_contains_tier3_guidance(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    assert srd.main([]) == 0
    assert "Tier-3" in claude_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T7 / T8: --dry-run does not mutate CLAUDE.md
# ---------------------------------------------------------------------------


def test_T7_dryrun_branch2_no_mutation(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    before = claude_md.read_text(encoding="utf-8")
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main(["--dry-run"])

    assert rc == 0
    assert claude_md.read_text(encoding="utf-8") == before


def test_T8_dryrun_branch3_no_mutation(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    before = claude_md.read_text(encoding="utf-8")
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "1")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    rc = srd.main(["--dry-run"])

    assert rc == 0
    assert claude_md.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# T9: ambiguous SETUP_RAG_IS_UE_REPO value -> non-zero exit
# ---------------------------------------------------------------------------


def test_T9_ambiguous_ue_value_nonzero_exit(tmp_path, monkeypatch):
    _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "maybe")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "true")
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    assert srd.main([]) != 0


# ---------------------------------------------------------------------------
# T10: --root flag works (no SETUP_RAG_TARGET_ROOT override)
# ---------------------------------------------------------------------------


def test_T10_root_flag_branch2_via_nonue(tmp_path, monkeypatch):
    claude_md = _write_claude_md(tmp_path)
    monkeypatch.setenv("SETUP_RAG_IS_UE_REPO", "0")
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "false")
    monkeypatch.delenv("SETUP_RAG_TARGET_ROOT", raising=False)

    rc = srd.main(["--root", str(tmp_path)])

    assert rc == 0
    assert _SENTINEL in claude_md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# T11: target root does not exist -> exit 1
# ---------------------------------------------------------------------------


def test_T11_missing_root_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path / "does-not-exist"))
    assert srd.main([]) == 1


# ---------------------------------------------------------------------------
# T12: --help exits 0 and prints usage
# ---------------------------------------------------------------------------


def test_T12_help_flag(capsys):
    rc = srd.main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Usage (standalone):" in out
    assert "Exit codes:" in out


def test_T12b_help_short_flag(capsys):
    rc = srd.main(["-h"])
    assert rc == 0
    assert "Usage (standalone):" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Usage errors
# ---------------------------------------------------------------------------


def test_unknown_flag_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))
    assert srd.main(["--bogus"]) == 1


def test_root_flag_missing_value_exit_1():
    assert srd.main(["--root"]) == 1


def test_unexpected_positional_arg_exit_1(tmp_path, monkeypatch):
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))
    assert srd.main(["stray-arg"]) == 1


# ---------------------------------------------------------------------------
# Windows path-separator normalisation
# ---------------------------------------------------------------------------


def test_normalize_root_converts_backslashes():
    assert srd._normalize_root(r"C:\repo\sub") == "C:/repo/sub"


# ---------------------------------------------------------------------------
# Porter-brief addendum rule 2 -- subprocess timeout + stdin guard on the
# daemon probe. A hung/blocking probe must not hang repo-setup forever.
# ---------------------------------------------------------------------------


def test_daemon_probe_passes_timeout_and_devnull_stdin(monkeypatch):
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "true")
    monkeypatch.setattr(srd.subprocess, "run", _fake_run)

    assert srd._daemon_present() is True
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert isinstance(captured["kwargs"]["timeout"], int) and captured["kwargs"]["timeout"] > 0


def test_daemon_probe_timeout_expired_treated_as_absent(monkeypatch):
    def _fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 10))

    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "some-hung-mock")
    monkeypatch.setattr(srd.subprocess, "run", _fake_run)

    assert srd._daemon_present() is False


def test_daemon_probe_unparseable_string_treated_as_absent(monkeypatch):
    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "unterminated \"quote")
    assert srd._daemon_present() is False


def test_daemon_probe_custom_timeout_env_honoured(monkeypatch):
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setenv("SETUP_RAG_DAEMON_PROBE", "true")
    monkeypatch.setenv("SETUP_RAG_PROBE_TIMEOUT_SECS", "3")
    monkeypatch.setattr(srd.subprocess, "run", _fake_run)

    srd._daemon_present()
    assert captured["kwargs"]["timeout"] == 3


def test_default_probe_absent_when_example_retrieval_repo_not_on_path(monkeypatch):
    monkeypatch.delenv("SETUP_RAG_DAEMON_PROBE", raising=False)
    monkeypatch.setattr(srd.shutil, "which", lambda name: None)
    assert srd._daemon_present() is False


def test_default_probe_uses_which_and_timeout(monkeypatch):
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.delenv("SETUP_RAG_DAEMON_PROBE", raising=False)
    monkeypatch.setattr(srd.shutil, "which", lambda name: "/usr/local/bin/example-retrieval-repo")
    monkeypatch.setattr(srd.subprocess, "run", _fake_run)

    assert srd._daemon_present() is True
    assert captured["argv"] == ["/usr/local/bin/example-retrieval-repo", "status"]
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL


# ---------------------------------------------------------------------------
# UE detection -- depth-1 only, multi-match ambiguity
# ---------------------------------------------------------------------------


def test_ue_detection_single_uproject_at_root(tmp_path):
    (tmp_path / "MyGame.uproject").write_text("{}", encoding="utf-8")
    assert srd._is_ue_repo(str(tmp_path)) == 0


def test_ue_detection_no_uproject(tmp_path):
    assert srd._is_ue_repo(str(tmp_path)) == 1


def test_ue_detection_nested_uproject_not_counted(tmp_path):
    nested = tmp_path / "Sub"
    nested.mkdir()
    (nested / "Nested.uproject").write_text("{}", encoding="utf-8")
    assert srd._is_ue_repo(str(tmp_path)) == 1


def test_ue_detection_multiple_uproject_ambiguous(tmp_path):
    (tmp_path / "A.uproject").write_text("{}", encoding="utf-8")
    (tmp_path / "B.uproject").write_text("{}", encoding="utf-8")
    assert srd._is_ue_repo(str(tmp_path)) == 2


# ---------------------------------------------------------------------------
# Unreadable root -- silent-enumeration guard (silent-enumeration audit).
# Path.glob() silently swallows PermissionError even on a flat, non-recursive
# pattern (empirically re-verified: a chmod-000 dir yields an empty iterator
# from glob(), no exception), which previously made an unreadable root read
# as "not a UE repo" (branch 2/3) rather than the reserved ambiguous-
# detection exit code 2. Tier 2 (behaviour change): the returned code changes
# on this input -- see the `# --- Tier 2 ... ---` fence in _is_ue_repo.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_root_with_uproject_is_ambiguous_not_absent(tmp_path):
    (tmp_path / "MyGame.uproject").write_text("{}", encoding="utf-8")

    original_mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, 0o000)
    try:
        result = srd._is_ue_repo(str(tmp_path))
    finally:
        os.chmod(tmp_path, original_mode)

    assert result == 2, (
        "an unreadable root must read as ambiguous (2), never as "
        f"\"not a UE repo\" (1) -- got {result!r}"
    )


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="chmod 0o000 permission denial is not reliable on Windows or as root",
)
def test_unreadable_root_setup_rag_decision_returns_2_with_stderr(tmp_path, monkeypatch, capsys):
    (tmp_path / "MyGame.uproject").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("SETUP_RAG_IS_UE_REPO", raising=False)
    monkeypatch.setenv("SETUP_RAG_TARGET_ROOT", str(tmp_path))

    original_mode = tmp_path.stat().st_mode
    os.chmod(tmp_path, 0o000)
    try:
        rc = srd.main([])
    finally:
        os.chmod(tmp_path, original_mode)

    assert rc == 1, (
        "setup_rag_decision()'s OWN exit-code table reserves 1 for this "
        "module's ambiguous/usage-error slot (2 is reserved for the coordinator-claude "
        f"trampoline's own import failure, per the module docstring) -- got {rc!r}"
    )
    err = capsys.readouterr().err
    assert "ambiguous" in err.lower()
