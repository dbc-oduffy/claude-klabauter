"""Agy headless read grants and the tamper guard over them.

Agy has no read-only workspace grant: an `--add-dir` is writable under `--mode accept-edits`. The one
subtree its stage reads outside the repo is the generated plugin skills, so that is the only dir granted;
this module hashes it before and after the stage so a write into it is detected, not prevented.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

from coordinator_core._settings_home import settings_home

REFUSAL_KIND = "agy_grant_tamper"
MARKER_NAME = "agy-grant-tamper.json"
# Measured read set of a headless Agy EM stage (transcript of the 1a run): <plugin tree>/skills/*/SKILL.md.
_SKILLS_RELPATH = Path("unimatrix") / "agy-plugin" / "skills"


def grant_dirs(environ: Mapping[str, str] | None = None) -> list[Path]:
    """Existing read-grant dirs for an Agy stage; never the settings home or engine root themselves."""
    environ = os.environ if environ is None else environ
    override = environ.get("COORDINATOR_SETTINGS_HOME")
    home = Path(override) if override else settings_home()
    skills = home / _SKILLS_RELPATH
    return [skills] if skills.is_dir() else []


def snapshot(dirs) -> dict[str, str]:
    """{path: sha256} for every file under `dirs`; content-hashed, so a restored mtime hides nothing."""
    out: dict[str, str] = {}
    for root in dirs:
        for dirpath, _names, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, "rb") as fh:
                        out[path] = hashlib.sha256(fh.read()).hexdigest()
                except OSError:
                    out[path] = "unreadable"
    return out


def changed_paths(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def marker_path() -> Path:
    return settings_home() / MARKER_NAME


def write_marker(paths: list[str]) -> None:
    """Record the tamper where `engine.drift` reads it; best effort, the refusal never depends on it."""
    try:
        marker_path().write_text(json.dumps({"paths": paths, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def read_marker() -> dict | None:
    try:
        value = json.loads(marker_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
