"""
coordinator/tests/test_schema_cli.py — pytest re-home of the retired
coordinator/bin/tests/test-schema-cli.bats bats suite.

Purpose: verifies --describe introspection, --validate record validation,
error handling, and field-order pin for the schema CLI's argv contract.

Subject was JS (coordinator/bin/schema-cli.js) by design when this module was
first re-homed from bats. schema-cli.js was DELETED in 480ad8f8 (two live
callers — coordinator-queue-append, coordinator-lesson-promote — were still
shelling into it, MODULE_NOT_FOUND) with NO replacement Node script; its
byte-identical-parity Python successor is
coordinator_core.frontmatter.schema_cli (see that module's docstring for the
full argv-contract parity note). This test module now spawns the Python
successor's argv contract (`python3 -m coordinator_core.frontmatter.schema_cli
--describe|--validate ...`) instead of `node schema-cli.js ...` — same
subprocess-CLI shape, same assertions, different interpreter.

COORDINATOR_SCHEMAS_DIR override (former AC-5/AC-6) is a documented
negative-spec on the Python successor — schema_cli.py: "Does NOT support
COORDINATOR_SCHEMAS_DIR override ... a deliberate, narrower scope than
schema-cli.js's env-override — claude-klabauter has no consumer-test schema-dir
isolation need today." Those three tests are marked skip (not deleted) below,
citing that negative-spec, rather than silently dropping the coverage record.

Spec backlink: (dual-yaml-parser option-d, C2)
Retired suite: coordinator/bin/tests/test-schema-cli.bats
Retired subject: coordinator/bin/schema-cli.js (deleted 480ad8f8)
Successor subject: coordinator_core/frontmatter/schema_cli.py
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_LIB_DIR = str(REPO_ROOT / "coordinator" / "bin" / "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)
from coordinator_data_root import data_root  # noqa: E402

# Spawns a real external process; runs at cadence gates, not per-commit.
# Spawn ratchet: coordinator_core/tests/test_no_new_spawning_tests.py
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

# schemas/ is DoE-resident post-2026-07-22 executable-surface migration —
# resolve via the shared two-rung helper rather than a bare
# REPO_ROOT-relative path. data_root() raises RuntimeError when neither rung
# resolves (no DoE-claude sibling checkout); the sentinel keeps that a skip
# for the COORDINATOR_SCHEMAS_DIR-override tests below — its only consumers —
# instead of a collection-time crash for the module.
try:
    SCHEMAS_DIR = data_root("schemas")
except RuntimeError:
    SCHEMAS_DIR = Path("/doe-root-unresolved/schemas")


def _run_describe(schema_name: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "coordinator_core.frontmatter.schema_cli", "--describe", schema_name],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _run_validate(schema_name: str, record: dict, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "coordinator_core.frontmatter.schema_cli", "--validate", schema_name],
        input=json.dumps(record),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _assert_describe_output_valid(stdout: str) -> dict:
    d = json.loads(stdout)
    assert isinstance(d.get("required"), list), f"required not array: {type(d.get('required'))}"
    assert isinstance(d.get("optional"), list), f"optional not array: {type(d.get('optional'))}"
    enums = d.get("enums")
    assert isinstance(enums, dict), f"enums not object: {type(enums)}"
    return d


# ---------------------------------------------------------------------------
# --describe: all 6 YAML schemas exit 0 and emit valid JSON with required as array
# (AC-1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "schema_name",
    ["bug-backlog", "debt-backlog", "lessons-outbox", "review-trail"],
)
def test_describe_exits_zero_with_valid_json(schema_name: str) -> None:
    cp = _run_describe(schema_name)
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    _assert_describe_output_valid(cp.stdout)


def test_describe_improvement_queue_has_change_kind_enum() -> None:
    cp = _run_describe("improvement-queue")
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = _assert_describe_output_valid(cp.stdout)
    assert d["enums"].get("change_kind"), "missing change_kind enum"
    assert isinstance(d["enums"]["change_kind"], list), "change_kind not array"
    assert len(d["enums"]["change_kind"]) > 0, "change_kind enum is empty"


def test_describe_lesson_entry_has_base_fields_and_status_enum() -> None:
    cp = _run_describe("lesson-entry")
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = _assert_describe_output_valid(cp.stdout)
    expected_required = ["created", "title", "body", "status", "scope", "from_repo"]
    for f in expected_required:
        assert f in d["required"], f"missing required field: {f}"
    assert d["enums"].get("status"), "missing status enum"
    assert isinstance(d["enums"]["status"], list), "status enum not array"
    assert len(d["enums"]["status"]) > 0, "status enum is empty"


# ---------------------------------------------------------------------------
# --describe: applies_to field present in the describe payload
# (AC-1b)
# ---------------------------------------------------------------------------

def test_describe_bug_backlog_applies_to() -> None:
    cp = _run_describe("bug-backlog")
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = json.loads(cp.stdout)
    assert d.get("applies_to") == "state/bug-backlog/*.yaml", (
        f"expected applies_to state/bug-backlog/*.yaml, got: {d.get('applies_to')!r}"
    )


def test_describe_cross_repo_commitment_applies_to() -> None:
    cp = _run_describe("cross-repo-commitment")
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = json.loads(cp.stdout)
    assert d.get("applies_to") == "state/cross-repo-commitments/*.yaml", (
        f"expected applies_to state/cross-repo-commitments/*.yaml, got: {d.get('applies_to')!r}"
    )


# ---------------------------------------------------------------------------
# --validate: known-good record for review-trail schema -> ok:true, exit 0
# (AC-2)
# ---------------------------------------------------------------------------

def test_validate_review_trail_good_record() -> None:
    record = {
        "sha_range": "abc1234..def5678",
        "reviewer": "code-reviewer",
        "scope": "bin/schema-cli.js",
        "scope_kind": "file",
        "verdict": "pass",
        "diff_loc": 100,
        "session_id": "test-session-1",
        "workstream": None,
    }
    cp = _run_validate("review-trail", record)
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = json.loads(cp.stdout)
    assert d.get("ok") is True, f"expected ok:true, got: {d!r}"
    assert isinstance(d.get("errors"), list), "errors not array"
    assert len(d["errors"]) == 0, f"expected empty errors, got: {d['errors']!r}"


# ---------------------------------------------------------------------------
# --validate: record missing a required field -> ok:false, field in errors,
# non-zero exit (AC-3)
# ---------------------------------------------------------------------------

def test_validate_bug_backlog_missing_required_title() -> None:
    record = {
        "created": "2026-06-27",
        "body": "test body content",
        "status": "open",
        "surface": "bin/schema-cli.js",
        "severity": "P2",
    }
    cp = _run_validate("bug-backlog", record)
    assert cp.returncode != 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = json.loads(cp.stdout)
    assert d.get("ok") is False, f"expected ok:false, got: {d!r}"
    assert isinstance(d.get("errors"), list), "errors not array"
    assert any("title" in e for e in d["errors"]), (
        f"expected title in errors, got: {d['errors']!r}"
    )


# ---------------------------------------------------------------------------
# Unknown schema name -> fail loud, non-zero exit
# (AC-4)
# ---------------------------------------------------------------------------

def test_describe_unknown_schema_name_nonzero_exit() -> None:
    cp = _run_describe("totally-nonexistent-schema-xyz-abc")
    assert cp.returncode != 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"


def test_validate_unknown_schema_name_nonzero_exit() -> None:
    cp = _run_validate("totally-nonexistent-schema-xyz-abc", {})
    assert cp.returncode != 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"


# ---------------------------------------------------------------------------
# COORDINATOR_SCHEMAS_DIR override — SKIPPED, not deleted (former AC-5/AC-6).
#
# The Python successor (coordinator_core/frontmatter/schema_cli.py) documents
# this as a deliberate negative-spec: "Does NOT support COORDINATOR_SCHEMAS_DIR
# override ... schema_validate.describe()/validate() always read claude-klabauter's own
# vendored schema set ... a deliberate, narrower scope than schema-cli.js's
# env-override — claude-klabauter has no consumer-test schema-dir isolation need today."
# The three tests below asserted schema-cli.js's env-override behavior, which
# has no successor to test against; skipped (with reason) rather than deleted
# so the former AC-5/AC-6 coverage record stays visible.
# ---------------------------------------------------------------------------

_SCHEMAS_DIR_OVERRIDE_SKIP_REASON = (
    "COORDINATOR_SCHEMAS_DIR override was NOT ported to the Python successor "
    "(coordinator_core/frontmatter/schema_cli.py negative-spec) — schema-cli.js "
    "itself was deleted in 480ad8f8"
)


@pytest.mark.skip(reason=_SCHEMAS_DIR_OVERRIDE_SKIP_REASON)
def test_schemas_dir_override_describe_reads_from_override(tmp_path: Path) -> None:
    shutil.copy(SCHEMAS_DIR / "bug-backlog.schema.json", tmp_path / "bug-backlog.schema.json")

    env = dict(os.environ)
    env["COORDINATOR_SCHEMAS_DIR"] = str(tmp_path)
    cp = _run_describe("bug-backlog", env=env)
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    _assert_describe_output_valid(cp.stdout)


@pytest.mark.skip(reason=_SCHEMAS_DIR_OVERRIDE_SKIP_REASON)
def test_schemas_dir_override_schema_not_present_is_unknown(tmp_path: Path) -> None:
    shutil.copy(SCHEMAS_DIR / "bug-backlog.schema.json", tmp_path / "bug-backlog.schema.json")

    env = dict(os.environ)
    env["COORDINATOR_SCHEMAS_DIR"] = str(tmp_path)
    cp = _run_describe("debt-backlog", env=env)
    assert cp.returncode != 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"


@pytest.mark.skip(reason=_SCHEMAS_DIR_OVERRIDE_SKIP_REASON)
def test_schemas_dir_override_empty_dir_fails_loud(tmp_path: Path) -> None:
    env = dict(os.environ)
    env["COORDINATOR_SCHEMAS_DIR"] = str(tmp_path)
    cp = _run_describe("bug-backlog", env=env)
    assert cp.returncode != 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"


# ---------------------------------------------------------------------------
# Field-order pin: --describe emits required in the schema YAML's field order
# (AC-7)
#
# bug-backlog.yaml required fields (YAML key order):
#   created, title, body, status, surface, severity
# ---------------------------------------------------------------------------

def test_field_order_pin_bug_backlog_required_fields() -> None:
    cp = _run_describe("bug-backlog")
    assert cp.returncode == 0, f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
    d = json.loads(cp.stdout)
    expected = ["created", "title", "body", "status", "surface", "severity"]
    assert d["required"] == expected, (
        f"field order mismatch:\n  expected: {expected!r}\n  got:      {d['required']!r}"
    )
