"""Apply the settings manifest's all-machines env values to a `settings.json`.

Shared by `scripts/setup.py` (workstation) and `scripts/cloud_setup.py`. The
values are never named here: `<plugin-root>/bin/check-settings-env.py --apply`
owns them, and writes only `all_machines` rows. Stdlib only — both installers
run before the engine's third-party deps are guaranteed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SETTINGS_ENV_CHECKER_REL = ("bin", "check-settings-env.py")


class SettingsEnvError(RuntimeError):
    """Apply failed or left findings; `record` is the verdict for the caller's report."""

    def __init__(self, message: str, record: dict):
        super().__init__(message)
        self.record = record


def apply_settings_env(plugin_root: Path, settings_path: Path) -> dict:
    """Run the checker's `--apply` pass; return `{exit_code, applied, findings}`.

    Raises `SettingsEnvError` on a non-zero exit, a missing checker is
    `FileNotFoundError`. An idempotent re-run repairs drift.
    """
    checker = Path(plugin_root).joinpath(*SETTINGS_ENV_CHECKER_REL)
    if not checker.is_file():
        raise FileNotFoundError(f"settings-env checker not found at {checker}")
    result = subprocess.run(
        [sys.executable, str(checker), "--settings", str(settings_path), "--apply", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        verdict = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        verdict = None
    if not isinstance(verdict, dict):
        raise SettingsEnvError(
            f"check-settings-env exited {result.returncode} with no JSON verdict: "
            + ((result.stderr or result.stdout or "").strip()[-400:] or "<no output>"),
            {"exit_code": result.returncode, "applied": None, "findings": None},
        )
    findings = verdict.get("findings") or []
    record = {
        "exit_code": result.returncode,
        "applied": verdict.get("applied") or [],
        "findings": findings,
    }
    if result.returncode != 0:
        named = ", ".join(f"{f.get('var')} ({f.get('kind')})" for f in findings) or "<none named>"
        raise SettingsEnvError(f"check-settings-env exited {result.returncode}; unapplied: {named}", record)
    return record
