"""test_generate_tested_platforms.py — tests for coordinator/bin/generate-tested-platforms.

Drives the generator end-to-end against a scratch repo fixture (temp dir with its
own `git init`, its own agent-install-manifest.json, and its own
state/platform-outcomes/ tree) so the real repo's manifest is never touched by a
test run. Invoked with an explicit `sys.executable` (never relies on the target's
shebang/exec bit — Windows cannot exec an extensionless polyglot directly, and this
test must be Windows-clean per the chunk's own execution criterion).

Covers:
  (a) a passing, fresh entry-point record promotes its platform.
  (b) dry-run (no --write) writes nothing to the manifest on disk.
  (c) grandfather clause preserves a pre-existing tested_platforms entry that has
      zero backing entry-point-surface records.
  (d) a stale record (surface_sha mismatch) does NOT promote its platform.
  (e) bonus: a ceremony-hot-path surface (not an entry point) does not promote,
      even if passing and fresh — proves the entry-point-only scope.

Spec backlink: DoE-claude:pln-platform-verified-is-a-distinc-a076aa § C3a1

Run with: python test_generate_tested_platforms.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.platform_outcome_records import (
    PLATFORM_OUTCOME_STALENESS_DAYS,
)

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_GENERATOR = os.path.join(_THIS_DIR, "generate-tested-platforms.py")

TESTS_PASSED = 0
TESTS_FAILED = 0
FAILURES: list[str] = []


def _check(condition: bool, label: str) -> None:
    """Fail the enclosing test.

    Negative-spec: this MUST raise on a false condition. It previously only
    printed and bumped a module-global counter that nothing ever asserted on,
    which made every check routed through it decorative. Do not "restore"
    the counting-only shape.
    """
    global TESTS_PASSED, TESTS_FAILED
    if condition:
        TESTS_PASSED += 1
    else:
        TESTS_FAILED += 1
        FAILURES.append(label)
        print(f"FAIL: {label}")
        pytest.fail(label, pytrace=False)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_MANIFEST_REL = os.path.join("coordinator", "docs", "install", "agent-install-manifest.json")


def _run_git(repo_root: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


def _make_scratch_repo(tested_platforms: list[str]) -> tuple[str, str]:
    """Create a temp dir, git-init it, commit once, and seed a minimal
    agent-install-manifest.json with the given tested_platforms. Returns
    (repo_root, head_sha)."""
    repo_root = tempfile.mkdtemp(prefix="gen-tested-platforms-test-")
    _run_git(repo_root, "init", "-q")
    _run_git(repo_root, "config", "user.email", "test@example.com")
    _run_git(repo_root, "config", "user.name", "Test")

    manifest_path = os.path.join(repo_root, _MANIFEST_REL)
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    manifest = {
        "standalone_setup_script": {"posix": "coordinator/scripts/setup.py"},
        "programmatic_entry_point": {"posix": "coordinator/scripts/install-maximalist.py"},
        "tested_platforms": tested_platforms,
        "present_platforms": ["macos", "linux", "windows"],
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    _run_git(repo_root, "add", "-A")
    _run_git(repo_root, "commit", "-q", "-m", "seed")
    sha = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()
    return repo_root, sha


def _write_record(repo_root: str, platform: str, machine: str, surface: str, **overrides) -> None:
    """Write one platform-outcome record.

    Negative-spec: `observed_at` MUST be computed relative to `datetime.now()`,
    never a hardcoded literal. A fixed calendar date turns any freshness-
    dependent test vacuous the moment real time passes it out of
    PLATFORM_OUTCOME_STALENESS_DAYS's window — silently, since nothing fails
    loudly, the promotion this fixture is meant to exercise simply stops
    happening. Callers that need a stale record must override `observed_at`
    with an offset likewise derived from PLATFORM_OUTCOME_STALENESS_DAYS, not
    a second hardcoded date.
    """
    fresh_observed_at = (
        (datetime.now(timezone.utc) - timedelta(days=3))
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    record = {
        "platform": platform,
        "surface": surface,
        "command": "coordinator/scripts/setup.py --i-am-agent",
        "outcome": "pass",
        "exit_code": 0,
        "observed_at": fresh_observed_at,
        "machine": machine,
        "surface_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "invoking_repo": "coordinator-claude",
    }
    record.update(overrides)
    records_dir = os.path.join(repo_root, "state", "platform-outcomes", platform, machine)
    os.makedirs(records_dir, exist_ok=True)
    lines = [f'{k}: "{v}"' if isinstance(v, str) else f"{k}: {v}" for k, v in record.items()]
    with open(os.path.join(records_dir, f"{surface}.yaml"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def _run_generator(repo_root: str, write: bool = False) -> subprocess.CompletedProcess:
    args = [sys.executable, _GENERATOR, "--repo-root", repo_root]
    if write:
        args.append("--write")
    return subprocess.run(args, capture_output=True, text=True, **no_console_creationflags())


def _read_manifest(repo_root: str) -> dict:
    with open(os.path.join(repo_root, _MANIFEST_REL), "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_passing_entry_point_record_promotes() -> None:
    repo_root, sha = _make_scratch_repo(tested_platforms=[])
    try:
        _write_record(
            repo_root,
            platform="windows",
            machine="machine-b",
            surface="standalone_setup_script",
            surface_sha=sha,
        )
        proc = _run_generator(repo_root, write=True)
        _check(proc.returncode == 0, f"(a) generator exits 0: {proc.stderr}")
        manifest = _read_manifest(repo_root)
        _check(
            manifest.get("tested_platforms") == ["windows"],
            f"(a) passing entry-point record promotes windows: got {manifest.get('tested_platforms')}",
        )
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


def test_dry_run_writes_nothing() -> None:
    repo_root, sha = _make_scratch_repo(tested_platforms=[])
    try:
        _write_record(
            repo_root,
            platform="windows",
            machine="machine-b",
            surface="standalone_setup_script",
            surface_sha=sha,
        )
        before = _read_manifest(repo_root)
        proc = _run_generator(repo_root, write=False)
        _check(proc.returncode == 0, f"(b) dry-run exits 0: {proc.stderr}")
        _check("dry-run" in proc.stdout, "(b) dry-run stdout says dry-run")
        after = _read_manifest(repo_root)
        _check(before == after, "(b) dry-run leaves manifest byte-identical")
        _check(after.get("tested_platforms") == [], "(b) dry-run: manifest tested_platforms unchanged ([])")
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


def test_grandfather_preserves_recordless_pre_existing_entry() -> None:
    repo_root, _sha = _make_scratch_repo(tested_platforms=["macos", "linux"])
    try:
        # Zero records at all on disk (no state/platform-outcomes/ dir).
        proc = _run_generator(repo_root, write=True)
        _check(proc.returncode == 0, f"(c) generator exits 0: {proc.stderr}")
        _check(
            "grandfathered: macos" in proc.stdout and "grandfathered: linux" in proc.stdout,
            f"(c) advisory lines emitted for both platforms: {proc.stdout}",
        )
        manifest = _read_manifest(repo_root)
        _check(
            sorted(manifest.get("tested_platforms", [])) == ["linux", "macos"],
            f"(c) grandfather preserves macos+linux with zero records: got {manifest.get('tested_platforms')}",
        )
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


def test_stale_record_does_not_promote() -> None:
    repo_root, _sha = _make_scratch_repo(tested_platforms=[])
    try:
        stale_observed_at = (
            (datetime.now(timezone.utc) - timedelta(days=PLATFORM_OUTCOME_STALENESS_DAYS + 10))
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        _write_record(
            repo_root,
            platform="windows",
            machine="machine-b",
            surface="standalone_setup_script",
            surface_sha="0000000000000000000000000000000000000000",  # deliberately mismatched
            observed_at=stale_observed_at,  # also stale by the SECONDARY rule, by construction
        )
        proc = _run_generator(repo_root, write=True)
        _check(proc.returncode == 0, f"(d) generator exits 0: {proc.stderr}")
        manifest = _read_manifest(repo_root)
        _check(
            manifest.get("tested_platforms") == [],
            f"(d) stale (surface_sha mismatch) record does not promote: got {manifest.get('tested_platforms')}",
        )
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


def test_write_does_not_reflow_unrelated_manifest_content() -> None:
    """--write must splice only the tested_platforms array's own text region.

    Negative-spec: a whole-file `json.dump` re-serialization would escape the
    em-dash to `\\u2014`, blow the hand-inlined `present_platforms` array out
    to one element per line, and (pre-fix) drop the trailing newline. None of
    that may happen to a field --write was not asked to change.

    cross-repo/archive/2026-08-26-doe-claude-em-generate-tested-platforms-write-reflows-the-manifest.md
    """
    repo_root, sha = _make_scratch_repo(tested_platforms=[])
    try:
        manifest_path = os.path.join(repo_root, _MANIFEST_REL)
        raw = (
            "{\n"
            '  "standalone_setup_script": {"posix": "coordinator/scripts/setup.py"},\n'
            '  "programmatic_entry_point": {"posix": "coordinator/scripts/install-maximalist.py"},\n'
            '  "why": "an em—dash in hand-authored prose",\n'
            '  "tested_platforms": [],\n'
            '  "present_platforms": ["macos", "linux", "windows"]\n'
            "}\n"
        )
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(raw)
        _run_git(repo_root, "add", "-A")
        _run_git(repo_root, "commit", "-q", "-m", "reseed with hand-formatted manifest")
        sha = _run_git(repo_root, "rev-parse", "HEAD").stdout.strip()

        _write_record(
            repo_root,
            platform="windows",
            machine="machine-b",
            surface="standalone_setup_script",
            surface_sha=sha,
        )
        proc = _run_generator(repo_root, write=True)
        _check(proc.returncode == 0, f"(f) generator exits 0: {proc.stderr}")

        with open(manifest_path, "r", encoding="utf-8", newline="") as fh:
            after_raw = fh.read()

        _check(
            "—" in after_raw and "\\u2014" not in after_raw,
            "(f) em-dash prose is not escaped by --write (ensure_ascii=False)",
        )
        _check(
            '"present_platforms": ["macos", "linux", "windows"]' in after_raw,
            f"(f) unrelated hand-inlined array is byte-preserved: {after_raw!r}",
        )
        _check(after_raw.endswith("\n"), "(f) file retains a trailing newline")
        manifest = json.loads(after_raw)
        _check(
            manifest.get("tested_platforms") == ["windows"],
            f"(f) tested_platforms itself is still correctly updated: got {manifest.get('tested_platforms')}",
        )
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)


def test_hot_path_surface_does_not_promote() -> None:
    """Bonus (e): a ceremony-hot-path surface (e.g. 'workday-start', C5's KR-2
    shape) is NOT a manifest-declared entry point and must not promote, even
    when passing and fresh — proves the entry-point-only scope decision."""
    repo_root, sha = _make_scratch_repo(tested_platforms=[])
    try:
        _write_record(
            repo_root,
            platform="windows",
            machine="machine-b",
            surface="workday-start",
            surface_sha=sha,
        )
        proc = _run_generator(repo_root, write=True)
        _check(proc.returncode == 0, f"(e) generator exits 0: {proc.stderr}")
        manifest = _read_manifest(repo_root)
        _check(
            manifest.get("tested_platforms") == [],
            f"(e) ceremony-hot-path surface does not promote: got {manifest.get('tested_platforms')}",
        )
    finally:
        shutil.rmtree(repo_root, ignore_errors=True)
