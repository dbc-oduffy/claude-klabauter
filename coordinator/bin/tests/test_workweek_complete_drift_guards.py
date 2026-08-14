# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""bin/tests/test_workweek_complete_drift_guards.py

Purpose: unit tests for coordinator/bin/workweek-complete-drift-guards.py —
the M3 chunk WWC-3 port of five genuine bash-logic fences out of
DoE-claude's `coordinator/commands/workweek-complete.md`:
description-length/enabledPlugins-drift advisory dispatch, the change-aware
dep-manifest CVE-recheck (manifest-presence gate + 14-day window), the
schema-drift-gate three-way rc branch, the repo-wide ShellCheck sweep loop,
and the console-flash / multi-event-hook guard dispatch pairs.

Coverage:
  test_enabled_plugins_skips_when_settings_json_absent
  test_enabled_plugins_dispatches_when_settings_json_present
  test_cve_recheck_no_tracked_manifests_skips
  test_cve_recheck_manifest_untouched_in_window_skips
  test_cve_recheck_manifest_changed_in_window_dispatches
  test_schema_drift_gate_pass
  test_schema_drift_gate_block
  test_schema_drift_gate_error
  test_schema_drift_gate_unexpected_rc_treated_as_error
  test_schema_drift_gate_missing_sibling_is_error
  test_shellcheck_sweep_clean_repo
  test_shellcheck_sweep_reports_findings
  test_shellcheck_sweep_not_installed_skips_cleanly
  test_console_flash_guard_missing_sibling_skips_cleanly
  test_multi_event_hook_guard_missing_sibling_skips_cleanly
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from shutil import which

import pytest

from coordinator_core.win_portability import no_console_creationflags

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    """Load workweek-complete-drift-guards.py by file path (hyphenated name bypass)."""
    spec = importlib.util.spec_from_file_location(
        "workweek_complete_drift_guards",
        _BIN_DIR / "workweek-complete-drift-guards.py",
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(cwd: str, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        **no_console_creationflags(),
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(str(path), "init", "-q", ".")
    _git(str(path), "config", "user.email", "test@test.com")
    _git(str(path), "config", "user.name", "Test")


def test_exists() -> None:
    assert (_BIN_DIR / "workweek-complete-drift-guards.py").is_file()


# ---------------------------------------------------------------------------
# enabled-plugins — Step 4f advisory (settings.json presence gate)
# ---------------------------------------------------------------------------


def test_enabled_plugins_skips_when_settings_json_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _mod.main(["enabled-plugins", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no .claude/settings.json — skipped" in out


def test_enabled_plugins_dispatches_when_settings_json_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text("{}\n", encoding="utf-8")

    rc = _mod.main(["enabled-plugins", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # Dispatch happened — banner reports SOME rc from the sibling audit
    # script, not the "skipped" short-circuit message.
    assert "no .claude/settings.json — skipped" not in out
    assert "enabledPlugins drift advisory" in out


# ---------------------------------------------------------------------------
# cve-recheck — Step 4h change-aware gate
# ---------------------------------------------------------------------------


def test_cve_recheck_no_tracked_manifests_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(str(tmp_path), "add", "README.md")
    _git(str(tmp_path), "commit", "-q", "-m", "initial")

    rc = _mod.main(["cve-recheck", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no tracked dependency manifests" in out


def test_cve_recheck_manifest_untouched_in_window_skips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    _git(str(tmp_path), "add", "package.json")
    # Backdate the commit well outside the 14-day window.
    env_args = ["commit", "-q", "-m", "initial", "--date=2020-01-01T00:00:00"]
    subprocess.run(
        ["git", *env_args],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2020-01-01T00:00:00"},
        **no_console_creationflags(),
    )

    rc = _mod.main(["cve-recheck", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "unchanged in the last 14 days" in out


def test_cve_recheck_manifest_changed_in_window_dispatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "package.json").write_text("{}\n", encoding="utf-8")
    _git(str(tmp_path), "add", "package.json")
    _git(str(tmp_path), "commit", "-q", "-m", "initial")  # commits now, inside window

    rc = _mod.main(["cve-recheck", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dispatching dep-cve-auditor" in out
    assert "package.json" in out


# ---------------------------------------------------------------------------
# schema-drift-gate — three-way rc branch
# ---------------------------------------------------------------------------


def _install_fake_sibling(bin_dir: Path, name: str, rc: int, message: str = "") -> None:
    script = bin_dir / name
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({message!r})\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)


def test_schema_drift_gate_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sibling(tmp_path, "schema-drift-gate.py", 0, "PASS\n")
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["schema-drift-gate"])
    assert rc == 0


def test_schema_drift_gate_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sibling(tmp_path, "schema-drift-gate.py", 1, "drift found\n")
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["schema-drift-gate"])
    assert rc == 1


def test_schema_drift_gate_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_sibling(tmp_path, "schema-drift-gate.py", 2, "transport failure\n")
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["schema-drift-gate"])
    assert rc == 2


def test_schema_drift_gate_unexpected_rc_treated_as_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_sibling(tmp_path, "schema-drift-gate.py", 7, "")
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["schema-drift-gate"])
    assert rc == 2


def test_schema_drift_gate_missing_sibling_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["schema-drift-gate"])
    assert rc == 2


# ---------------------------------------------------------------------------
# shellcheck-sweep — Step 6 repo-wide loop
# ---------------------------------------------------------------------------


_SHELLCHECK_MISSING = which("shellcheck") is None


@pytest.mark.skipif(_SHELLCHECK_MISSING, reason="shellcheck not installed on this machine")
def test_shellcheck_sweep_clean_repo(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    clean_script = tmp_path / "clean.sh"
    clean_script.write_text('#!/usr/bin/env bash\necho "hello"\n', encoding="utf-8")
    _git(str(tmp_path), "add", "clean.sh")
    _git(str(tmp_path), "commit", "-q", "-m", "add clean script")

    rc = _mod.main(["shellcheck-sweep", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "all .sh files clean" in out


@pytest.mark.skipif(_SHELLCHECK_MISSING, reason="shellcheck not installed on this machine")
def test_shellcheck_sweep_reports_findings(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _init_repo(tmp_path)
    dirty_script = tmp_path / "dirty.sh"
    # Unquoted variable expansion — a reliable ShellCheck SC2086 trigger.
    dirty_script.write_text(
        '#!/usr/bin/env bash\nfoo=$1\necho $foo\n', encoding="utf-8"
    )
    _git(str(tmp_path), "add", "dirty.sh")
    _git(str(tmp_path), "commit", "-q", "-m", "add dirty script")

    rc = _mod.main(["shellcheck-sweep", "--repo-root", str(tmp_path)])
    assert rc == 1
    out = capsys.readouterr().out
    # The bash "-:" stdin marker must have been rewritten to the real path.
    assert "dirty.sh:" in out
    assert "-:" not in out.split("dirty.sh:")[0][-3:]


def test_shellcheck_sweep_not_installed_skips_cleanly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_mod, "_which", lambda _name: None)
    rc = _mod.main(["shellcheck-sweep", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not installed" in out


# ---------------------------------------------------------------------------
# console-flash-guard / multi-event-hook-guard — dispatch pairs, missing-sibling case
# ---------------------------------------------------------------------------


def test_console_flash_guard_missing_sibling_skips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["console-flash-guard"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "guard not found" in out


def test_multi_event_hook_guard_missing_sibling_skips_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_mod, "_BIN_DIR", str(tmp_path))
    rc = _mod.main(["multi-event-hook-guard"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "guard not found" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
