"""test_record_platform_outcome.py — C2 tests for record-platform-outcome (the writer
for the C1 platform-outcome schema).

Invokes the CLI as a real subprocess (`sys.executable <path> --surface ... --command
... --exit-code ...`) against a scratch git repo standing in for the surface-providing
repo (`DOE_ROOT` env override), and asserts the emitted record:
  1. lands at the schema's RECORD LOCATION convention:
     <surface_root>/state/platform-outcomes/<platform>/<machine>/<surface>.yaml
  2. is schema-valid against `coordinator/schemas/platform-outcome.schema.json` —
     every `required` field present, every enum-constrained field (`platform`,
     `outcome`) within its declared enum, `exit_code` an integer, `observed_at`
     ISO-8601 `Z`-suffixed.

Also unit-tests the machine/hostname resolution seam (`_resolve_machine`) directly,
covering its three-rung precedence (`COORDINATOR_MACHINE` env -> machine-local
registry `coordinator.machine_slug` -> live hostname fallback).

Converted from a hand-rolled unittest runner to top-level pytest functions.

Spec backlink: DoE-claude:pln-platform-verified-is-a-distinc-a076aa § C2
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import unittest.mock
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

import pytest

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# ---------------------------------------------------------------------------
# Path setup — locate the CLI, its lib deps, and the schema relative to this
# test file.
# test file: coordinator/bin/tests/test_record_platform_outcome.py
# CLI:       coordinator/bin/record-platform-outcome
# schema:    coordinator/schemas/platform-outcome.schema.json — schemas/ is
#            CONTRACT and, per DR-047, stayed in DoE-claude when bin/ moved
#            here (see coordinator_registry.py's own layout-tolerant comment).
#            Resolved below via the already-imported coordinator_registry
#            module rather than re-implementing its rung order.
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_COORDINATOR_DIR = _BIN_DIR.parent
_CLI_PATH = _BIN_DIR / "record-platform-outcome.py"
_LIB_DIR = _BIN_DIR / "lib"

if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))


def _load_cli(path: Path, module_name: str):
    """Load the extension-less CLI as a Python module for direct unit testing
    (matches the established in-tree pattern — see
    coordinator/bin/tests/test_untested_platform_advisory.py and
    coordinator/bin/tests/test_doe_root_routing.py)."""
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli(_CLI_PATH, "record_platform_outcome")

# coordinator_registry is now import-time-resolvable (repo split, 4f74656c):
# loading the CLI above pulled it into sys.modules already having walked its
# own DOE_ROOT/REPO_DOE_CLAUDE/machine-local rungs against this process's
# ambient env, so reuse its resolved manifest path rather than re-deriving
# the schemas/ location — the real schemas dir is wherever that landed.
_REAL_MANIFEST_PATH = Path(sys.modules["coordinator_registry"]._MANIFEST_PATH)
_REAL_SCHEMAS_DIR = _REAL_MANIFEST_PATH.parent
_SCHEMA_PATH = _REAL_SCHEMAS_DIR / "platform-outcome.schema.json"


def _run_git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True, text=True, **no_console_creationflags(),
    )


def _init_scratch_repo(root: str) -> str:
    """Init a throwaway git repo at `root` with one commit; return its HEAD SHA."""
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "test@example.invalid")
    _run_git(root, "config", "user.name", "test")
    (Path(root) / "README.md").write_text("scratch\n", encoding="utf-8")
    _run_git(root, "add", "README.md")
    _run_git(root, "commit", "-q", "-m", "initial")
    sha = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    return sha


def _load_schema() -> dict:
    with open(_SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _assert_schema_valid(record: dict, schema: dict) -> "list[str]":
    """Lightweight structural check against the schema's own `required`/`enum`/
    `type` constraints (no external jsonschema dependency — pure-stdlib, matching
    this repo's Windows-clean-without-extra-installs posture). Returns a list of
    violation strings; empty means valid."""
    violations = []
    required = schema["required"]
    props = schema["properties"]
    for field in required:
        if field not in record:
            violations.append(f"missing required field: {field}")
    if "platform" in record:
        enum = props["platform"]["enum"]
        if record["platform"] not in enum:
            violations.append(f"platform {record['platform']!r} not in enum {enum}")
    if "outcome" in record:
        enum = props["outcome"]["enum"]
        if record["outcome"] not in enum:
            violations.append(f"outcome {record['outcome']!r} not in enum {enum}")
    if "exit_code" in record and not isinstance(record["exit_code"], int):
        violations.append(f"exit_code must be an integer, got {type(record['exit_code'])}")
    if "observed_at" in record and not _ISO_Z_RE.match(str(record["observed_at"])):
        violations.append(f"observed_at not ISO-8601 Z-suffixed: {record['observed_at']!r}")
    for str_field in ("surface", "command", "machine", "surface_sha", "invoking_repo"):
        if str_field in record and not (isinstance(record[str_field], str) and record[str_field]):
            violations.append(f"{str_field} must be a non-empty string")
    return violations


def _parse_flat_yaml(path: str) -> dict:
    """Parse the flat (no-nesting, no-`---`) YAML this CLI emits — bare stdlib,
    no PyYAML dependency, mirroring the CLI's own hand-emission approach."""
    record: dict = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            key, _, raw = line.partition(":")
            key = key.strip()
            val = raw.strip()
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            elif key == "exit_code":
                val = int(val)
            record[key] = val
    return record


# ---------------------------------------------------------------------------
# End-to-end CLI invocation — real subprocess, real scratch git repo.
# ---------------------------------------------------------------------------

def _setup_surface(tmp_path):
    surface_root = str(tmp_path / "surface-repo")
    os.makedirs(surface_root)
    surface_sha = _init_scratch_repo(surface_root)
    # _run_cli() points DOE_ROOT at surface_root, which coordinator_registry's
    # own import-time manifest bootstrap also reads (DOE_ROOT wins over the
    # ambient REPO_DOE_CLAUDE alias by design — same precedence as doe_root()).
    # A scratch stand-in for "the DoE/coordinator repo" must therefore carry
    # the schemas/ manifest too, or the CLI subprocess dies at import with an
    # install-integrity FileNotFoundError before ever reaching the surface
    # logic under test.
    schemas_dir = Path(surface_root) / "coordinator" / "schemas"
    schemas_dir.mkdir(parents=True)
    shutil.copy(_REAL_MANIFEST_PATH, schemas_dir / _REAL_MANIFEST_PATH.name)
    return surface_root, surface_sha


def _run_cli(surface_root, *, surface: str, command: str, exit_code: int) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DOE_ROOT"] = surface_root
    env["COORDINATOR_MACHINE"] = "test-machine"
    # Isolate the machine-local registry rung so a real developer machine's
    # coordinator.machine_slug can never leak in and override COORDINATOR_MACHINE
    # (it wouldn't — COORDINATOR_MACHINE wins rung 1 — but keep the env clean).
    env.pop("MACHINE_LOCAL_IMPL", None)
    return subprocess.run(
        [
            sys.executable, str(_CLI_PATH),
            "--surface", surface,
            "--command", command,
            "--exit-code", str(exit_code),
        ],
        cwd=surface_root,
        capture_output=True, text=True, env=env,
        **no_console_creationflags(),
    )


def test_emits_schema_valid_record_at_expected_path(tmp_path) -> None:
    surface_root, surface_sha = _setup_surface(tmp_path)
    result = _run_cli(
        surface_root,
        surface="workday-start",
        command="python coordinator/bin/workday-start.py --i-am-agent",
        exit_code=0,
    )
    assert result.returncode == 0, (
        f"CLI exited non-zero.\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    expected_platform = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[
        platform.system()
    ]
    expected_path = os.path.join(
        surface_root, "state", "platform-outcomes",
        expected_platform, "test-machine", "workday-start.yaml",
    )
    assert os.path.isfile(expected_path), (
        f"expected record at {expected_path}; stdout was: {result.stdout!r}"
    )
    # stdout is documented to print the emitted path.
    assert os.path.normcase(expected_path) in os.path.normcase(result.stdout.strip())

    record = _parse_flat_yaml(expected_path)
    schema = _load_schema()
    violations = _assert_schema_valid(record, schema)
    assert violations == [], f"schema violations: {violations}\nrecord: {record}"

    assert record["platform"] == expected_platform
    assert record["surface"] == "workday-start"
    assert record["outcome"] == "pass"
    assert record["exit_code"] == 0
    assert record["machine"] == "test-machine"
    assert record["surface_sha"] == surface_sha
    assert record["invoking_repo"] == os.path.basename(surface_root)


def test_nonzero_exit_code_maps_to_fail_outcome(tmp_path) -> None:
    surface_root, _surface_sha = _setup_surface(tmp_path)
    result = _run_cli(surface_root, surface="some-op", command="do-the-thing --flag", exit_code=1)
    assert result.returncode == 0, result.stderr

    expected_platform = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}[
        platform.system()
    ]
    record_file = os.path.join(
        surface_root, "state", "platform-outcomes",
        expected_platform, "test-machine", "some-op.yaml",
    )
    record = _parse_flat_yaml(record_file)
    assert record["outcome"] == "fail"
    assert record["exit_code"] == 1


def test_invalid_surface_rejected(tmp_path) -> None:
    surface_root, _surface_sha = _setup_surface(tmp_path)
    result = _run_cli(surface_root, surface="../escape", command="x", exit_code=0)
    assert result.returncode != 0
    assert "invalid --surface" in result.stderr


def test_doe_root_unresolvable_errors_cleanly(tmp_path) -> None:
    surface_root, _surface_sha = _setup_surface(tmp_path)
    env = dict(os.environ)
    env.pop("DOE_ROOT", None)
    # REPO_DOE_CLAUDE is the ambient alias doe_root() also checks (d5e22cb2) —
    # left set, it resolves the real DoE-claude clone regardless of the
    # MACHINE_LOCAL_IMPL stub below, defeating the "fully unresolvable" premise
    # this test exists to cover.
    env.pop("REPO_DOE_CLAUDE", None)
    stub = str(tmp_path / "_machine_local_stub.py")
    with open(stub, "w", encoding="utf-8") as fh:
        fh.write("import sys\nsys.exit(1)\n")
    env["MACHINE_LOCAL_IMPL"] = stub
    # CLAUDE_HOME must also be isolated: doe_root()'s codename-free rungs
    # (`.doe-root` pointer file, marketplace-cache, flat plugin layout) all
    # derive their candidate paths from claude_home() independent of the
    # MACHINE_LOCAL_IMPL stub above. Left ambient, a real dev box's
    # ~/.claude/.doe-root (or <settings-home>/machine-local/.doe-root)
    # resolves the real DoE-claude clone and defeats the "fully unresolvable"
    # premise this test exists to cover, exactly like the REPO_DOE_CLAUDE
    # leak the comment above already guards against.
    env["CLAUDE_HOME"] = str(tmp_path / "no-such-claude-home")
    result = subprocess.run(
        [
            sys.executable, str(_CLI_PATH),
            "--surface", "x", "--command", "y", "--exit-code", "0",
        ],
        cwd=surface_root,
        capture_output=True, text=True, env=env,
        **no_console_creationflags(),
    )
    assert result.returncode != 0
    assert "DOE_ROOT" in result.stderr


# ---------------------------------------------------------------------------
# _resolve_machine() — three-rung precedence, direct unit test (no subprocess).
# ---------------------------------------------------------------------------
# Mirrors cross-repo-memo:_resolve_machine_slug's precedence contract.

def test_resolve_machine_env_override_wins() -> None:
    with unittest.mock.patch.dict(os.environ, {"COORDINATOR_MACHINE": "override-box"}):
        assert _cli._resolve_machine() == "override-box"


def test_resolve_machine_registry_wins_over_hostname() -> None:
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_MACHINE"}
    with (
        unittest.mock.patch.dict(os.environ, env, clear=True),
        unittest.mock.patch.object(_cli, "_registry_machine_local_get", return_value="registry-box"),
    ):
        assert _cli._resolve_machine() == "registry-box"


def test_resolve_machine_falls_back_to_hostname() -> None:
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_MACHINE"}
    with (
        unittest.mock.patch.dict(os.environ, env, clear=True),
        unittest.mock.patch.object(_cli, "_registry_machine_local_get", return_value=None),
        unittest.mock.patch.object(_cli.socket, "gethostname", return_value="my-host.local"),
    ):
        assert _cli._resolve_machine() == "my-host"


def test_resolve_machine_never_raises_on_hostname_oserror() -> None:
    env = {k: v for k, v in os.environ.items() if k != "COORDINATOR_MACHINE"}
    with (
        unittest.mock.patch.dict(os.environ, env, clear=True),
        unittest.mock.patch.object(_cli, "_registry_machine_local_get", return_value=None),
        unittest.mock.patch.object(_cli.socket, "gethostname", side_effect=OSError("boom")),
    ):
        assert _cli._resolve_machine() == "unknown-machine"


# ---------------------------------------------------------------------------
# _yaml_str() / record_path() / build_record() — pure-function unit checks.
# ---------------------------------------------------------------------------

def test_yaml_str_bare_when_safe() -> None:
    assert _cli._yaml_str("workday-start") == "workday-start"
    assert _cli._yaml_str("machine-b") == "machine-b"


def test_yaml_str_quotes_when_needed() -> None:
    assert _cli._yaml_str("has: colon-space") == '"has: colon-space"'
    assert _cli._yaml_str("- leading dash") == '"- leading dash"'


def test_yaml_quote_always_quotes_command_and_observed_at() -> None:
    # command/observed_at are ALWAYS quoted regardless of _yaml_str's
    # conditional heuristic — matches the C1 fixture convention exactly.
    assert _cli._yaml_quote_always(
        "python coordinator/bin/workday-start.py --i-am-agent"
    ) == '"python coordinator/bin/workday-start.py --i-am-agent"'
    assert _cli._yaml_quote_always("2026-07-20T14:32:00Z") == '"2026-07-20T14:32:00Z"'


def test_record_path_matches_sharding_convention() -> None:
    path = _cli.record_path("/repo", "windows", "machine-b", "workday-start")
    assert path == os.path.join(
        "/repo", "state", "platform-outcomes", "windows", "machine-b", "workday-start.yaml",
    )


def test_build_record_outcome_derivation() -> None:
    rec = _cli.build_record(
        platform_id="windows", surface="s", command="c", exit_code=0,
        observed_at="2026-01-01T00:00:00Z", machine="m", surface_sha="abc", invoking_repo="r",
    )
    assert rec["outcome"] == "pass"
    rec2 = _cli.build_record(
        platform_id="windows", surface="s", command="c", exit_code=2,
        observed_at="2026-01-01T00:00:00Z", machine="m", surface_sha="abc", invoking_repo="r",
    )
    assert rec2["outcome"] == "fail"


def test_invalid_surface_raises() -> None:
    for bad in ("../escape", "a/b", ""):
        try:
            _cli._validate_surface(bad)
        except _cli.RecordPlatformOutcomeError:
            pass
        else:
            raise AssertionError(f"expected RecordPlatformOutcomeError for {bad!r}")
