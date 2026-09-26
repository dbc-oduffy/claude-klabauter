# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
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
    assert "no .claude/settings.json — skipped" not in out
    assert "enabledPlugins drift advisory" in out


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
    _git(str(tmp_path), "commit", "-q", "-m", "initial")

    rc = _mod.main(["cve-recheck", "--repo-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "dispatching dep-cve-auditor" in out
    assert "package.json" in out


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
