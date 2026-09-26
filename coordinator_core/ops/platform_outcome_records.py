"""
coordinator_core.ops.platform_outcome_records — shared platform-outcome record
reader + staleness/derivation core.

ONE staleness implementation, TWO consumers:
  1. `coordinator/bin/generate-tested-platforms.py` (C3a) — derives (and
     optionally writes) a manifest's `tested_platforms` field from committed
     records.
  2. `coordinator_core.ops.validate_install_contract._check_point4` (check-
     point4 ask#2) — cross-checks a manifest's DECLARED `tested_platforms`
     against the same derivation, so the validator can never fail a manifest
     the generator itself would have written (grandfather clause included).

This module is a MOVE, not a rewrite: it was extracted verbatim (behavior
byte-for-byte equivalent) from generate-tested-platforms.py, which now
imports these names and keeps its old module-level names as thin aliases so
its own test suite (coordinator/bin/test_generate_tested_platforms.py,
coordinator/bin/tests/test_generate_tested_platforms_manifest_path.py) stays
green untouched.

Record schema: coordinator/schemas/platform-outcome.schema.json (DoE-owned).
Fields: platform (macos|linux|windows), surface, command, outcome (pass|fail),
exit_code, observed_at, machine, surface_sha, invoking_repo. Path:
state/platform-outcomes/<platform>/<machine>/<surface>.yaml.

STALENESS (C1's two rules, both checked — mirrors platform-outcome.schema.
json's schema-level description verbatim):
  PRIMARY   — surface_sha no longer matches this repo's current HEAD SHA.
  SECONDARY — observed_at is more than PLATFORM_OUTCOME_STALENESS_DAYS (30)
              days old.
A record failing either rule is stale and cannot promote/back a platform.

Spec backlink: DoE-claude:pln-platform-verified-is-a-distinc-a076aa § C3a1
Spec backlink: state/handoffs/2026-07-21_190724_checkpoint4-platform-outcome-records-crosscheck.md
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on a broken environment
    yaml = None

# Mirrors platform-outcome.schema.json's SECONDARY staleness constant
# (PLATFORM_OUTCOME_STALENESS_DAYS = 30), named here rather than encoded as a
PLATFORM_OUTCOME_STALENESS_DAYS = 30

PLATFORM_ENUM_ORDER = ["macos", "linux", "windows"]

REQUIRED_RECORD_FIELDS = [
    "platform",
    "surface",
    "command",
    "outcome",
    "exit_code",
    "observed_at",
    "machine",
    "surface_sha",
    "invoking_repo",
]

_RECORDS_RELATIVE = os.path.join("state", "platform-outcomes")


def records_root(repo_root: str) -> str:
    return os.path.join(repo_root, _RECORDS_RELATIVE)


def current_repo_sha(repo_root: str) -> str | None:
    try:
        from coordinator_core.win_portability import leaf_spawn_creationflags

        proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.SubprocessError, ImportError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def entry_point_surfaces(manifest: dict) -> set[str]:
    names: set[str] = set()
    if manifest.get("standalone_setup_script"):
        names.add("standalone_setup_script")
    if manifest.get("programmatic_entry_point"):
        names.add("programmatic_entry_point")
    return names


def load_record(path: str) -> dict | None:
    if yaml is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    if any(field not in data for field in REQUIRED_RECORD_FIELDS):
        return None
    if data.get("platform") not in {"macos", "linux", "windows"}:
        return None
    if data.get("outcome") not in {"pass", "fail"}:
        return None
    return data


def is_stale(record: dict, current_sha: str | None, now: datetime) -> bool:
    """PRIMARY: surface_sha mismatch against `current_sha`. SECONDARY: observed_at
    more than PLATFORM_OUTCOME_STALENESS_DAYS calendar days before `now`. Either
    condition alone makes the record stale (platform-outcome.schema.json's
    schema-level rule — both are independently checked, neither alone suffices
    as a freshness proof, but either alone suffices to invalidate)."""
    if current_sha is not None and record.get("surface_sha") != current_sha:
        return True
    observed_raw = record.get("observed_at")
    try:
        observed = datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return True
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    if now - observed > timedelta(days=PLATFORM_OUTCOME_STALENESS_DAYS):
        return True
    return False


def iter_record_paths(records_root_: str):
    if not os.path.isdir(records_root_):
        return
    for platform_name in sorted(os.listdir(records_root_)):
        platform_dir = os.path.join(records_root_, platform_name)
        if not os.path.isdir(platform_dir):
            continue
        for machine_name in sorted(os.listdir(platform_dir)):
            machine_dir = os.path.join(platform_dir, machine_name)
            if not os.path.isdir(machine_dir):
                continue
            for fname in sorted(os.listdir(machine_dir)):
                if fname.endswith((".yaml", ".yml")):
                    yield os.path.join(machine_dir, fname)


def _sort_platforms(platforms) -> list[str]:
    """Canonical PLATFORM_ENUM_ORDER first, then any unrecognized value
    alphabetically appended (defensive — schema-valid input never hits this)."""
    known = [p for p in PLATFORM_ENUM_ORDER if p in platforms]
    unknown = sorted(p for p in platforms if p not in PLATFORM_ENUM_ORDER)
    return known + unknown


def derive_tested_platforms(
    records_root_: str,
    manifest: dict,
    current_sha: str | None,
    now: datetime | None = None,
) -> tuple[list[str], list[str]]:
    surfaces = entry_point_surfaces(manifest)
    existing = list(manifest.get("tested_platforms") or [])
    now = now or datetime.now(timezone.utc)

    seen_entry_platforms: set[str] = set()
    passing_platforms: set[str] = set()

    for path in iter_record_paths(records_root_):
        record = load_record(path)
        if record is None:
            continue
        if record.get("surface") not in surfaces:
            continue
        platform = record["platform"]
        seen_entry_platforms.add(platform)
        if record.get("outcome") == "pass" and not is_stale(record, current_sha, now):
            passing_platforms.add(platform)

    derived = set(passing_platforms)
    advisories: list[str] = []
    for platform in existing:
        if platform in passing_platforms:
            continue
        if platform not in seen_entry_platforms:
            derived.add(platform)
            advisories.append(f"grandfathered: {platform} has no backing records")

    return _sort_platforms(derived), advisories
