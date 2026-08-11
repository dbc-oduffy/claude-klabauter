#!/usr/bin/env python3
"""
test_coordinator_queue_append.py — integration tests for coordinator-queue-append CLI.

Spec backlink: docs/plans/2026-06-15-structured-queue-medium-rollout.md § C5
Spec backlink: docs/plans/2026-06-25-example-initiative-tc-2-queues-lessons-consolidation.md § C9

Tests:
  1. --help exits 0 with non-empty stdout. Smoke test.
  2. Unknown --schema exits non-zero; stderr names known schemas.
  3. Missing required field (debt-backlog: source, risk, proposed_action absent)
     exits non-zero; stderr names a missing field.
  4. Invalid enum value for --status exits non-zero; stderr names valid values.
  5. Valid debt-backlog write: exits 0, file exists at expected path, non-empty,
     YAML structure includes required fields (proposed_action, no id:).
  6. Roundtrip: emitted YAML is well-formed and all passed field values survive
     (proposed_action roundtrips; no id: field emitted).
  6b. Mid-string ' #' survives the emit-parse roundtrip.
  7. Schema-doc-not-runtime-parsed (the Staff Engineer F0 guard): deleting the wiki file from
     a tmpdir does not break a valid write invocation.
  8. --id flag rejected (id field dropped in D2 — filename is the handle).
  9a. --queue-scope central writes to CLAUDE_KLABAUTER_ROOT, not cwd.
  9b. Project scope (default) still writes cwd-relative.
  10. --queue-scope central rejected for non-improvement-queue schemas.
  11. Unified shape: --status closed accepted; --status resolved rejected for debt.
  12. Unified shape: entry has proposed_action: and no id: field.

Run with: python3 -m pytest test_coordinator_queue_append.py
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from coordinator_core.win_portability import no_console_creationflags


# Repo root — this checkout's own coordinator_core is the byte-identical parity
# successor to the deleted node schema-cli.js (480ad8f8); `-m coordinator_core.
# frontmatter.schema_cli` must be spawned with cwd here so the import resolves.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------

def _script_path() -> str:
    """Return the absolute path to coordinator-queue-append."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-queue-append")


def _python() -> str:
    """Return the Python interpreter to use for subprocess invocations.

    Uses sys.executable — the interpreter running this test script is always a
    valid Python interpreter. This is the Windows-compatible zero-probe pattern
    (avoids FileNotFoundError from subprocess probing python3/python on Windows).
    """
    return sys.executable


def _run_cli(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Invoke the CLI as a subprocess.

    Always drives via `python <script>` — the script has no .py extension and is
    not directly executable on Windows. Same pattern as test_coordinator_lesson_promote.py.
    """
    effective_env = {**os.environ}
    if env:
        effective_env.update(env)
    return subprocess.run(
        [_python(), _script_path()] + args,
        env=effective_env,
        capture_output=True,
        text=True,
        cwd=cwd,
        **no_console_creationflags(),
    )


def _minimal_yaml_parse(content: str) -> dict:
    """Minimal YAML parser for simple key: value lines and block scalars.

    Handles:
    - Scalar values (quoted and unquoted)
    - Block scalars (| clip chomping) — collects lines until next top-level key
    - Block scalars (|- strip chomping) — same collection logic as |
      (Review: code-reviewer Slice-B — F2: coordinator-queue-append emits |- at :428;
      without this branch the fallback parser sets body="|-" literal and multi-line
      body tests misparse when PyYAML is absent.)

    Limitations (Review: code-reviewer Slice-B — F4):
    - Does NOT parse nested dicts (e.g. the system: block). Those fields are
      returned as empty string "" when PyYAML is absent. Tests that assert on
      system.created_by_session etc. require PyYAML.
    - Does NOT handle flow sequences, anchors, aliases, or multi-document streams.

    Matches the shape used in test_coordinator_lesson_promote.py.
    """
    result: dict = {}
    lines = content.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i]
        if line.strip() == "---":
            i += 1
            continue
        if ":" in line and not line.startswith(" "):
            key, _, rest = line.partition(":")
            key = key.strip()
            value = rest.strip()
            if value in ("|", "|-"):
                # Review: code-reviewer Slice-B — (F2) recognize |- (strip chomping)
                # alongside | (clip chomping); same collection logic for both.
                # Block scalar — collect subsequent indented lines.
                block_lines = []
                i += 1
                while i < len(lines) and (lines[i].startswith("  ") or lines[i].strip() == ""):
                    block_lines.append(lines[i][2:] if lines[i].startswith("  ") else "")
                    i += 1
                result[key] = "\n".join(block_lines).rstrip("\n")
                continue
            elif value.startswith('"'):
                inner = value[1:]
                if inner.endswith('"'):
                    inner = inner[:-1]
                # Protect literal backslashes before resolving \" so adjacent
                # escapes (e.g. \\") don't mis-combine under sequential replaces.
                result[key] = (
                    inner.replace("\\\\", "\x00")
                    .replace('\\"', '"')
                    .replace("\x00", "\\")
                )
            else:
                result[key] = value
        i += 1
    return result


def _parse_yaml_file(path: str) -> dict:
    """Parse a YAML file, falling back to the minimal parser if PyYAML absent."""
    try:
        import yaml as _yaml  # PyYAML — available on most coordinator installs
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        try:
            parsed = _yaml.safe_load(content)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return _minimal_yaml_parse(content)
    except ImportError:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        return _minimal_yaml_parse(content)
    except OSError as exc:
        raise RuntimeError(f"could not read YAML file {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Shared helpers for building valid debt-backlog invocation args
# ---------------------------------------------------------------------------

def _debt_backlog_required_args() -> list[str]:
    """Return the minimal valid CLI args for a debt-backlog entry.

    All required fields for debt-backlog (unified shape, tc-2 D1):
      created (auto), title, body, status, source, risk, proposed_action.
    Note: id field is DROPPED in D2 — filename is the canonical handle.
    Explicit --from-repo to bypass machine-local resolution in test environments.
    """
    return [
        "--schema", "debt-backlog",
        "--title", "Test debt entry",
        "--body", "Body text for the test entry.",
        "--status", "open",
        "--source", "daily-review/test/2026-06-15",
        "--risk", "Test risk description.",
        "--proposed-action", "Take the proposed action.",
        "--from-repo", "test-repo-em",
    ]


def _today_iso() -> str:
    return datetime.date.today().isoformat()


def _cross_repo_commitment_required_args() -> list[str]:
    """Return the minimal valid CLI args for a cross-repo-commitment entry.

    All required fields (base + domain): created (auto), title, body, status,
    committed_by, memo, commitment, observed. This schema does NOT use
    from_repo — --from-repo is passed anyway (harmless; _build_yaml drops any
    field the schema's required/optional lists don't name) to bypass
    machine-local resolution in test environments, matching every other
    schema's test helper.

    Spec backlink: docs/plans/2026-07-11-cross-repo-commitment-lifecycle.md § C3b
    """
    return [
        "--schema", "cross-repo-commitment",
        "--title", "machine-b: land addressee-guard registry check",
        "--body", "machine-b accepted our ask memo and committed to landing this.",
        "--status", "open",
        "--committed-by", "machine-b",
        "--memo", "archive/cross-repo-memos/2026-07/machine-b-registry-check-ask.md",
        "--commitment", "Will land the addressee-guard registry check by end of week.",
        "--observed", "2026-07-09",
        "--from-repo", "test-repo-em",
    ]


# ---------------------------------------------------------------------------
# Test 1 — --help exits 0 with non-empty stdout
# ---------------------------------------------------------------------------

def test_help_exits_zero() -> None:
    name = "Test 1 — --help exits 0 with non-empty stdout"
    result = _run_cli(["--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--help exited {result.returncode}, expected 0"))
        return
    if not result.stdout.strip():
        raise AssertionError(f"{name}: " + ("--help produced empty stdout"))
        return


# ---------------------------------------------------------------------------
# Test 2 — Unknown schema exits non-zero; stderr names known schemas
# ---------------------------------------------------------------------------

def test_unknown_schema_exits_nonzero() -> None:
    name = "Test 2 — unknown --schema exits non-zero, stderr names known schemas"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "notaschema",
                "--title", "X",
                "--body", "Y",
                "--status", "open",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for unknown schema; got 0"))
        return
    combined = result.stdout + result.stderr
    # stderr should name at least one of the known schemas.
    if "debt-backlog" not in combined and "bug-backlog" not in combined and "improvement-queue" not in combined:
        raise AssertionError(f"{name}: " + (f"stderr does not name any known schema. stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test 3 — Missing required field exits non-zero; stderr names a missing field
# ---------------------------------------------------------------------------

def test_missing_required_field_exits_nonzero() -> None:
    name = "Test 3 — missing required field exits non-zero, stderr names the field"
    # debt-backlog requires (unified shape, tc-2 D1): source, risk, proposed_action
    # beyond universal fields. Omit all three; the CLI should reject with one named.
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "T",
                "--body", "B",
                "--status", "open",
                "--from-repo", "test-repo-em",
                # Missing: --source, --risk, --proposed-action
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for missing required fields; got 0"))
        return
    combined = result.stdout + result.stderr
    # At least one of the missing field names should appear in the output.
    missing_field_names = ("source", "risk", "proposed-action", "proposed_action")
    if not any(f in combined.lower() for f in missing_field_names):
        raise AssertionError(f"{name}: " + (f"error output does not name any missing field. stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test 4 — Invalid enum value exits non-zero; stderr lists valid status values
# ---------------------------------------------------------------------------

def test_invalid_enum_value_exits_nonzero() -> None:
    name = "Test 4 — invalid --status value exits non-zero, stderr names valid values"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Note: enum validation runs before file I/O, so invalid enum exits before writing.
        # QUEUE_APPEND_OUTPUT_ROOT is set but the CLI should reject before touching the FS.
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "T",
                "--body", "B",
                "--status", "not-a-valid-status",
                "--source", "daily-review/test",
                "--risk", "Some risk.",
                "--proposed-action", "Some action.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for invalid --status; got 0"))
        return
    combined = result.stdout + result.stderr
    # stderr should name at least one valid status value.
    if "open" not in combined:
        raise AssertionError(f"{name}: " + (f"error output should name valid status values (e.g. 'open'). stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test 4b — No args: a single run names the FULL required set (not one field
# per run) — cross-repo memo
# 2026-08-11-example-retrieval-repo-em-queue-append-required-fields-undiscoverable.md item 1.
# ---------------------------------------------------------------------------

def test_no_args_names_full_required_set_in_one_run() -> None:
    name = "Test 4b — --schema debt-backlog with no other args names ALL required flags in one run"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            ["--schema", "debt-backlog"],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for missing required fields; got 0"))
    combined = result.stdout + result.stderr
    # All of debt-backlog's required-beyond-auto-filled flags must be named in
    # this single invocation's output — not disclosed one at a time.
    expected_flags = ("--title", "--body", "--status", "--source", "--risk", "--proposed-action")
    missing_from_output = [flag for flag in expected_flags if flag not in combined]
    if missing_from_output:
        raise AssertionError(
            f"{name}: " + (
                f"one run did not name all required flags — missing from output: "
                f"{missing_from_output}. stderr: {result.stderr!r}"
            )
        )
    # item 2: --status is not merely optional-looking — it must also be named
    # as required here (previously argparse never declared it required=True).
    if "--status" not in combined:
        raise AssertionError(f"{name}: " + ("--status not named as required. stderr: " + repr(result.stderr)))


# ---------------------------------------------------------------------------
# Test 4c — `--schema debt-backlog --help` prints the --severity enum
# (P0-P3), not a bare LEVEL placeholder — memo item 3.
# ---------------------------------------------------------------------------

def test_schema_help_prints_severity_enum() -> None:
    name = "Test 4c — --schema debt-backlog --help names the --severity enum (P0-P3)"
    result = _run_cli(["--schema", "debt-backlog", "--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--schema debt-backlog --help exited {result.returncode}, expected 0"))
    combined = result.stdout + result.stderr
    for value in ("P0", "P1", "P2", "P3"):
        if value not in combined:
            raise AssertionError(f"{name}: " + (f"severity enum value {value!r} missing from --help output: {combined!r}"))


def test_invalid_severity_value_names_valid_set() -> None:
    name = "Test 4d — --severity medium is rejected and the valid P0-P3 set is named"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _debt_backlog_required_args() + ["--severity", "medium"],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for --severity medium; got 0"))
    combined = result.stdout + result.stderr
    for value in ("P0", "P1", "P2", "P3"):
        if value not in combined:
            raise AssertionError(f"{name}: " + (f"valid severity value {value!r} missing from rejection output: {combined!r}"))


# ---------------------------------------------------------------------------
# Test 5 — Valid write: exits 0, file exists at expected path, YAML has required fields
# ---------------------------------------------------------------------------

_DEBT_BACKLOG_REQUIRED_YAML_FIELDS = (
    # Note: 'id' is NOT in this list — id field was dropped in D2 (tc-2).
    # The filename is the canonical handle; no id: is generated or emitted.
    "created",
    "source",
    "status",
    "title",
    "body",
    "risk",
    "proposed_action",
)


def test_valid_write_creates_yaml_file() -> None:
    name = "Test 5 — valid debt-backlog write exits 0, file exists with required fields"
    with tempfile.TemporaryDirectory() as tmpdir:
        # QUEUE_APPEND_OUTPUT_ROOT is the root; schema output_dir is "state/debt-backlog"
        # so the CLI constructs: <QUEUE_APPEND_OUTPUT_ROOT>/state/debt-backlog/<file>.yaml
        result = _run_cli(
            _debt_backlog_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        # Expected output dir: <tmpdir>/state/debt-backlog/
        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"output directory not created: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        if os.path.getsize(yaml_path) == 0:
            raise AssertionError(f"{name}: " + (f"YAML file is empty: {yaml_path}"))
            return

        # File name must start with today's ISO date.
        today = _today_iso()
        if not yaml_files[0].startswith(today):
            raise AssertionError(f"{name}: " + (f"YAML filename does not start with today's date ({today}): {yaml_files[0]}"))
            return

        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        missing = [f for f in _DEBT_BACKLOG_REQUIRED_YAML_FIELDS if f not in parsed]
        if missing:
            raise AssertionError(f"{name}: " + (f"YAML missing required fields: {missing}. Parsed: {parsed}"))
            return

        # D2 (tc-2): id field must NOT be present — filename is the canonical handle.
        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field (D2 drop); got id={parsed['id']!r}"))
            return



# ---------------------------------------------------------------------------
# Test 6 — Roundtrip: emitted YAML is well-formed; all passed values preserved
# ---------------------------------------------------------------------------

def test_roundtrip_yaml_parseable() -> None:
    name = "Test 6 — roundtrip: emitted YAML is well-formed and field values match"
    title = "Roundtrip test entry for queue-append"
    body = "This is the roundtrip body. It verifies field preservation."
    source = "daily-review/roundtrip/2026-06-15"
    risk = "Missing roundtrip would break YAML fidelity guarantee."
    proposed_action = "Confirm all fields survive the emit-parse cycle."
    status = "open"

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", title,
                "--body", body,
                "--status", status,
                "--source", source,
                "--risk", risk,
                "--proposed-action", proposed_action,
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; got {len(yaml_files)}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        # Verify scalar field values round-trip exactly.
        # proposed_action replaces the old suggested_action (D1 canonical rename).
        checks = [
            ("title", title),
            ("body", body),
            ("status", status),
            ("source", source),
            ("risk", risk),
            ("proposed_action", proposed_action),
        ]
        for field, expected in checks:
            got = parsed.get(field)
            if got != expected:
                raise AssertionError(f"{name}: " + (f"field {field!r}: expected {expected!r}, got {got!r}"))
                return

        # D2 (tc-2): id field must NOT be present — filename is the canonical handle.
        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field (D2 drop); got id={parsed['id']!r}"))
            return

        # created must be a non-empty date string.
        created = parsed.get("created", "")
        if not created:
            raise AssertionError(f"{name}: " + (f"created field is empty or absent: {created!r}"))
            return



# ---------------------------------------------------------------------------
# Test 6b — mid-string ` #` survives the emit-parse roundtrip (data-loss guard)
# ---------------------------------------------------------------------------

def test_mid_string_hash_roundtrips() -> None:
    name = "Test 6b — mid-string ' #' title/body survives roundtrip (no silent truncation)"
    # Each value embeds a whitespace-preceded '#' that YAML reads as an inline
    # comment introducer unless the emitter quotes the scalar. Regression for the
    # _yaml_quote_string start-position-only check (example-cockpit-repo memo 2026-06-24).
    title = "fix the bug #urgent in parser"
    body = "see PR #123 and issue #4 for context"
    risk = "has # hash mid and a\t#tab-preceded hash"  # both ' #' and '\t#'

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", title,
                "--body", body,
                "--status", "open",
                "--source", "daily-review/hash/2026-06-24",
                "--risk", risk,
                "--proposed-action", "Quote scalars containing ' #'.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; got {len(yaml_files)}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        for field, expected in [("title", title), ("body", body), ("risk", risk)]:
            got = parsed.get(field)
            if got != expected:
                raise AssertionError(f"{name}: " + (f"field {field!r} truncated at ' #': expected {expected!r}, got {got!r}"))
                return



# ---------------------------------------------------------------------------
# Test 7 — Schema-doc-not-runtime-parsed (the Staff Engineer F0 guard)
# ---------------------------------------------------------------------------

def test_schema_doc_not_runtime_parsed() -> None:
    """Confirm the CLI does NOT read docs/wiki/<schema>-schema.md at runtime.

    Mechanism: set up a tmpdir as cwd with no docs/wiki/ subtree at all, then
    invoke a valid debt-backlog write. If the CLI tries to open that file, it
    would fail. If the SCHEMAS dict is hardcoded (per the negative-spec), the
    write succeeds regardless.
    """
    name = "Test 7 — schema docs not read at runtime (the Staff Engineer F0 guard)"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Confirm there is no docs/wiki/ under tmpdir (it's a fresh tmpdir).
        wiki_path = os.path.join(tmpdir, "docs", "wiki", "debt-backlog-schema.md")
        if os.path.exists(wiki_path):
            # Shouldn't happen in a fresh tmpdir, but be defensive.
            os.remove(wiki_path)

        result = _run_cli(
            _debt_backlog_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI failed with no schema-doc present (exit {result.returncode}). "
                f"CLI may be reading the wiki doc at runtime. stderr: {result.stderr!r}"))
            return

        # Confirm a YAML file was actually produced (not a silent success with no write).
        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")] if os.path.isdir(expected_dir) else []
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("CLI exited 0 but no YAML file was produced."))
            return



# ---------------------------------------------------------------------------
# Test 7b — schema-load fails loud (rewired: bad CLAUDE_KLABAUTER_ROOT, not COORDINATOR_SCHEMAS_DIR)
# ---------------------------------------------------------------------------

def test_schema_load_fails_loud_via_env_override() -> None:
    """C2 testability seam: when the native schema seam cannot be reached, the
    CLI must exit non-zero with a remediation message in stderr (not silently
    accept or write a file).

    Rewired (de-node cutover, 480ad8f8): schema-cli.js — which honored
    COORDINATOR_SCHEMAS_DIR — was deleted with no legacy fallback; its parity
    successor coordinator_core/frontmatter/schema_cli.py does NOT support that
    override (documented negative-spec, see coordinator/tests/test_schema_cli.py
    and coordinator_core/frontmatter/schema_cli.py:55). An unresolvable
    CLAUDE_KLABAUTER_ROOT is schema.describe/schema.validate's own fail-loud trigger
    instead (_schema_cli_no_legacy: "no legacy fallback exists") — mirrors the
    sibling rewire in coordinator/tests/test_lesson_promote_node_enum.py::
    test_schema_load_failure_fails_loud.
    """
    name = "Test 7b — bad CLAUDE_KLABAUTER_ROOT: native schema seam unreachable → fail loud"
    with tempfile.TemporaryDirectory() as bad_claude_klabauter_root, \
         tempfile.TemporaryDirectory() as tmpdir:
        # bad_claude_klabauter_root has no coordinator_core package — schema.describe/validate
        # have no legacy fallback, so the CLI must fail loud before writing anything.
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "CLAUDE_KLABAUTER_ROOT": bad_claude_klabauter_root,
            },
            cwd=tmpdir,
        )

    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit when CLAUDE_KLABAUTER_ROOT has no coordinator_core; got 0"))
        return

    combined = result.stdout + result.stderr
    # The error must include some remediation signal — either "schema" reference,
    # the schema name, or the directory path.
    if not any(
        token in combined
        for token in ("schema", "debt-backlog", bad_claude_klabauter_root, "remediation", "Remediation")
    ):
        raise AssertionError(f"{name}: " + (f"stderr does not contain a remediation message. stderr: {result.stderr!r}"))
        return

    # Confirm no YAML file was written (CLI must exit before writing).
    written = []
    debt_dir = os.path.join(tmpdir, "state", "debt-backlog")
    if os.path.isdir(debt_dir):
        written = [f for f in os.listdir(debt_dir) if f.endswith(".yaml")]
    if written:
        raise AssertionError(f"{name}: " + (f"YAML file was written despite schema load failure: {written}"))
        return



# ---------------------------------------------------------------------------
# Test 8 — --id flag rejected as unknown (D2 drop: id field removed from schema)
# ---------------------------------------------------------------------------

def test_id_flag_rejected_as_unknown() -> None:
    """D2 (tc-2): the --id flag was dropped from coordinator-queue-append.

    Passing --id must fail non-zero since argparse no longer knows it.
    Regression guard: if someone accidentally restores --id, this test will catch it.
    """
    name = "Test 8 — --id flag rejected as unknown argument (D2: id field dropped)"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--id", "DSR-2026-06-25-1",   # --id no longer exists (D2 drop)
                "--title", "T",
                "--body", "B",
                "--status", "open",
                "--source", "daily-review/test",
                "--risk", "Some risk.",
                "--proposed-action", "Some action.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for --id (dropped flag); got 0"))
        return
    combined = result.stdout + result.stderr
    # argparse should mention "unrecognized arguments" or "--id" in the error.
    if "--id" not in combined and "unrecognized" not in combined.lower():
        raise AssertionError(f"{name}: " + (f"stderr does not mention --id or 'unrecognized'. stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test 9 — central queue_scope writes to CLAUDE_KLABAUTER_ROOT, not cwd
# ---------------------------------------------------------------------------

def _improvement_queue_required_args(extra: list[str] | None = None) -> list[str]:
    """Return the minimal valid CLI args for an improvement-queue entry.

    Unified shape (tc-2 D1): --proposed-target renamed to --proposed-action.
    """
    args = [
        "--schema", "improvement-queue",
        "--title", "Test central improvement entry",
        "--body", "Body describing the improvement.",
        "--surface", "state/lessons.md:42",
        "--proposed-action", "plugins/coordinator-claude/coordinator/skills/workstream-complete/SKILL.md",
        "--change-kind", "skill-edit",
        "--status", "open",
        "--from-repo", "test-repo-em",
    ]
    if extra:
        args.extend(extra)
    return args


def _seed_symlinked_claude_klabauter_root(fake_root: str) -> None:
    """Symlink THIS checkout's real coordinator_core package into fake_root.

    Post de-node-cutover (480ad8f8 / W0.5 Option B+C), schema.describe/schema.validate
    have NO legacy fallback — cc_invoke.route() checks seam presence against the exact
    same CLAUDE_KLABAUTER_ROOT the CLI uses for central-scope output routing, so a CLAUDE_KLABAUTER_ROOT
    that is merely "present but empty" (the pre-cutover test-isolation trick) now makes
    ANY write fail loud at the schema step, before the central-routing behavior this
    test targets is ever reached. Symlinking the real coordinator_core in gives the
    schema seam a working native implementation while state/ output under fake_root
    stays a disposable tmpdir, never the real checkout's own state/ tree.
    """
    os.symlink(
        os.path.join(_REPO_ROOT, "coordinator_core"),
        os.path.join(fake_root, "coordinator_core"),
    )


def test_central_scope_writes_to_claude_klabauter_root() -> None:
    """With CLAUDE_KLABAUTER_ROOT set (no QUEUE_APPEND_OUTPUT_ROOT), --queue-scope central must write
    to <CLAUDE_KLABAUTER_ROOT>/state/improvement-queue/, even when cwd is a different tmpdir.

    This is the load-bearing invariant: a session in a sibling repo must not write central
    entries into the sibling's state/improvement-queue/ — they must always land in
    claude-klabauter's own state/ tree.

    Central state routes to _claude_klabauter_root() unconditionally, per
    docs/wiki/state-placement-law.md § Taxonomy "Central/global state" (governing law:
    example-doctrine-repo coordinator/docs/wiki/state-placement-law.md:36). The
    [example-doctrine-repo] docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md plan's
    proposal to instead route this branch to example-doctrine-repo was never ratified: that plan is
    `status: draft`, its AC1/AC2 are `pending`, and its own C3 is HELD with recorded
    disk proof the flip never took effect on the production path — see
    _output_path()'s own negative-spec docstring.

    Spec backlink: docs/plans/2026-07-03-stop-the-rot-claude-klabauter-state-home-placement.md § AC1 / AC13
    Negative-spec: this branch does NOT route to DOE_ROOT or CLAUDE_HOME — both were
    superseded by the unconditional CLAUDE_KLABAUTER_ROOT route above.

    Rewired (de-node cutover, 480ad8f8 / W0.5 Option B+C, 2026-07-19): schema.describe/
    schema.validate now hard-require the native coordinator_core.invoke seam with no
    legacy fallback, checked against the SAME CLAUDE_KLABAUTER_ROOT this test uses to redirect
    central-scope output — a bare empty CLAUDE_KLABAUTER_ROOT (the pre-cutover isolation trick)
    now makes the CLI fail loud at the schema step before any write is attempted. Fixed
    by symlinking the real coordinator_core into CLAUDE_KLABAUTER_ROOT (_seed_symlinked_claude_klabauter_root)
    and running sibling_cwd as a real git repo (the native queue.append op's routing-key
    resolution requires one). This also closes the coverage caveat a prior review raised
    here: with a working seam, `_cc_route("queue.append", ...)` now takes the LIVE
    State-2 native path (coordinator_core/ops/queue_append.py), not just the CLI's own
    local `_output_path`/`legacy_fn()` — so this test now exercises the exact transport
    selection point a native-seam routing regression would need to break.
    """
    name = "Test 9a — --queue-scope central writes to CLAUDE_KLABAUTER_ROOT, not cwd"
    with tempfile.TemporaryDirectory() as claude_klabauter_root_dir, \
         tempfile.TemporaryDirectory() as sibling_cwd:
        # claude_klabauter_root_dir simulates claude-klabauter's own repo root; sibling_cwd simulates a
        # sibling repo whose cwd must NOT receive the central-scope write.
        _seed_symlinked_claude_klabauter_root(claude_klabauter_root_dir)
        init = subprocess.run(["git", "init", sibling_cwd], capture_output=True, text=True)
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return
        env = {
            "CLAUDE_KLABAUTER_ROOT": claude_klabauter_root_dir,
        }
        # Do NOT set QUEUE_APPEND_OUTPUT_ROOT — that would override the central redirect.

        result = _run_cli(
            _improvement_queue_required_args(["--queue-scope", "central"]),
            env=env,
            cwd=sibling_cwd,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        # Entry must be under CLAUDE_KLABAUTER_ROOT, not under sibling_cwd.
        expected_dir = os.path.join(claude_klabauter_root_dir, "state", "improvement-queue")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"expected output dir not created under CLAUDE_KLABAUTER_ROOT: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML in CLAUDE_KLABAUTER_ROOT; found {len(yaml_files)}: {yaml_files}"))
            return

        # Confirm nothing was written to the sibling cwd.
        sibling_queue_dir = os.path.join(sibling_cwd, "state", "improvement-queue")
        if os.path.isdir(sibling_queue_dir):
            sibling_files = [f for f in os.listdir(sibling_queue_dir) if f.endswith(".yaml")]
            if sibling_files:
                raise AssertionError(f"{name}: " + (f"YAML written to sibling cwd (wrong): {sibling_files}"))
                return

        # Review: code-reviewer — F4: verify YAML content, not just file location.
        # queue_scope must be "central" and from_repo must match the passed value.
        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        got_scope = parsed.get("queue_scope")
        if got_scope != "central":
            raise AssertionError(f"{name}: " + (f"expected queue_scope='central' in YAML; got {got_scope!r}"))
            return

        got_from_repo = parsed.get("from_repo")
        if got_from_repo != "test-repo-em":
            raise AssertionError(f"{name}: " + (f"expected from_repo='test-repo-em' in YAML; got {got_from_repo!r}"))
            return



def test_project_scope_still_writes_cwd_relative() -> None:
    """Regression guard: project-scoped improvement-queue writes must remain cwd-relative.

    Ensure the central-scope redirect does NOT affect the default project-scope path.

    Rewired (de-node cutover): schema.describe/schema.validate's native seam resolves
    CLAUDE_KLABAUTER_ROOT via the SAME CLAUDE_HOME-derived settings-home ladder this test
    deliberately points at an empty claude_home_dir (to prove CLAUDE_HOME is unused for
    OUTPUT routing) — with no legacy fallback, that empty-settings-home also starves the
    schema seam itself, unrelated to the output-path invariant under test. CLAUDE_KLABAUTER_ROOT is
    now set explicitly (rung 1, highest precedence) via a symlinked real coordinator_core
    so the schema seam resolves independently of CLAUDE_HOME, leaving the CLAUDE_HOME/
    output-path assertion below unchanged.
    """
    name = "Test 9b — project scope (default) still writes cwd-relative (regression guard)"
    with tempfile.TemporaryDirectory() as claude_home_dir, \
         tempfile.TemporaryDirectory() as fake_claude_klabauter_root, \
         tempfile.TemporaryDirectory() as project_cwd:
        # Set CLAUDE_HOME to a different dir to confirm it is NOT used for project scope.
        _seed_symlinked_claude_klabauter_root(fake_claude_klabauter_root)
        init = subprocess.run(["git", "init", project_cwd], capture_output=True, text=True)
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return
        env = {"CLAUDE_HOME": claude_home_dir, "CLAUDE_KLABAUTER_ROOT": fake_claude_klabauter_root}

        result = _run_cli(
            _improvement_queue_required_args(),  # no --queue-scope → defaults to project
            env=env,
            cwd=project_cwd,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        # Entry must be under project_cwd, not under claude_home_dir.
        expected_dir = os.path.join(project_cwd, "state", "improvement-queue")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"expected output dir not created under cwd: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML in cwd; found {len(yaml_files)}: {yaml_files}"))
            return

        # Confirm nothing was written to claude_home_dir.
        home_queue_dir = os.path.join(claude_home_dir, "state", "improvement-queue")
        if os.path.isdir(home_queue_dir):
            home_files = [f for f in os.listdir(home_queue_dir) if f.endswith(".yaml")]
            if home_files:
                raise AssertionError(f"{name}: " + (f"YAML written to CLAUDE_HOME (wrong for project scope): {home_files}"))
                return

        # Review: code-reviewer — F5: verify queue_scope is absent or None in project-scope output.
        # _build_yaml skips None-valued optional fields, so queue_scope must not appear in the YAML.
        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        got_scope = parsed.get("queue_scope")
        if got_scope is not None and got_scope != "" and got_scope != "null":
            raise AssertionError(f"{name}: " + (f"expected queue_scope absent/None in project-scope YAML; got {got_scope!r}"))
            return



# ---------------------------------------------------------------------------
# Test 10 — --queue-scope central rejected for non-improvement-queue schemas (F1 guard)
# ---------------------------------------------------------------------------

def test_queue_scope_central_rejected_for_non_improvement_schemas() -> None:
    """Review: code-reviewer — F1: --schema debt-backlog --queue-scope central must exit non-zero.

    Without the schema guard, a caller could silently redirect debt-backlog entries
    into ~/.claude/state/debt-backlog/ — wrong semantics, undocumented behaviour.
    """
    name = "Test 10 — --queue-scope central rejected for non-improvement-queue schemas"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--queue-scope", "central",
                "--title", "T",
                "--body", "B",
                "--status", "open",
                "--source", "daily-review/test",
                "--risk", "Some risk.",
                "--proposed-action", "Some action.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("--schema debt-backlog --queue-scope central should exit non-zero; got 0"))
        return
    combined = result.stdout + result.stderr
    if "improvement-queue" not in combined:
        raise AssertionError(f"{name}: " + (f"error output should mention 'improvement-queue' (one of the schemas accepting --queue-scope central). stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test 11 — Base status enum: closed accepted; resolved rejected for debt
# ---------------------------------------------------------------------------

def test_base_status_enum_closed_accepted_resolved_rejected() -> None:
    """Unified base status enum: closed is valid; resolved was dropped in D-status (tc-2).

    debt-backlog status enum = {open, closed, deferred} — NOT {open, resolved, ...}.
    Passing --status closed must succeed; passing --status resolved must fail.
    """
    name = "Test 11 — --status closed accepted; --status resolved rejected for debt"

    # Part A: --status closed must succeed (base enum includes 'closed').
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Closed entry",
                "--body", "Closed body.",
                "--status", "closed",
                "--source", "daily-review/test",
                "--risk", "Test risk.",
                "--proposed-action", "Already done.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--status closed must be accepted but got exit {result.returncode}: {result.stderr!r}"))
            return

    # Part B: --status resolved must fail (resolved was the old debt-specific status,
    # dropped in D-status reconciliation — debt now uses 'closed' for DONE).
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Resolved entry",
                "--body", "Resolved body.",
                "--status", "resolved",
                "--source", "daily-review/test",
                "--risk", "Test risk.",
                "--proposed-action", "Some action.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("--status resolved must be REJECTED for debt (dropped in D-status); got 0"))
            return
        combined = result.stdout + result.stderr
        if "open" not in combined and "closed" not in combined:
            raise AssertionError(f"{name}: " + (f"error output should name valid status values. stderr: {result.stderr!r}"))
            return



# ---------------------------------------------------------------------------
# Test 12 — Unified shape: written entry has proposed_action:, no id: field
# ---------------------------------------------------------------------------

def test_unified_shape_proposed_action_no_id() -> None:
    """Verify the unified field shape on the written YAML:
    - proposed_action: present (canonical name replacing suggested_action/proposed_target)
    - id: absent (D2 drop — filename is the canonical handle)
    - surface: present for bug-backlog (canonical name replacing 'system')
    """
    name = "Test 12 — unified shape: proposed_action: present, id: absent, surface: present"

    # debt-backlog: check proposed_action present, id absent
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Shape validation entry",
                "--body", "Body for shape test.",
                "--status", "open",
                "--source", "daily-review/shape/2026-06-25",
                "--risk", "Shape risk.",
                "--proposed-action", "Shape action target.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("no YAML file produced for debt-backlog"))
            return
        yaml_path = os.path.join(expected_dir, yaml_files[0])
        parsed = _parse_yaml_file(yaml_path)

        if "proposed_action" not in parsed:
            raise AssertionError(f"{name}: " + (f"expected proposed_action: in debt YAML; got keys: {list(parsed.keys())}"))
            return
        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"expected NO id: in debt YAML (D2 drop); got id={parsed['id']!r}"))
            return

    # bug-backlog: check surface present (was 'system'), proposed_action absent (not required for bug)
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Bug shape entry",
                "--body", "Bug body.",
                "--status", "open",
                "--surface", "coordinator/auto-push",
                "--severity", "P2",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"bug-backlog write failed: exit {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "bug-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("no YAML file produced for bug-backlog"))
            return
        yaml_path = os.path.join(expected_dir, yaml_files[0])
        parsed = _parse_yaml_file(yaml_path)

        if "surface" not in parsed:
            raise AssertionError(f"{name}: " + (f"expected surface: in bug YAML (was 'system'); got keys: {list(parsed.keys())}"))
            return
        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"expected NO id: in bug YAML (D2 drop); got id={parsed['id']!r}"))
            return



# ---------------------------------------------------------------------------
# Test F6 — wontfix status: accepted for bug-backlog, rejected for debt-backlog
# ---------------------------------------------------------------------------

def test_wontfix_status_acceptance() -> None:
    """Review: code-reviewer — F6: wontfix is a valid bug-backlog status but NOT
    valid for debt-backlog. Verify both sides of the enum gate.
    """
    name = "Test F6 — --status wontfix accepted for bug-backlog, rejected for debt-backlog"

    # Part A: bug-backlog --status wontfix must succeed (wontfix is in the bug enum).
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Wontfix bug entry",
                "--body", "This is a known issue we will not fix.",
                "--status", "wontfix",
                "--surface", "coordinator/auto-push",
                "--severity", "P3",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"--schema bug-backlog --status wontfix must be accepted; got exit {result.returncode}: {result.stderr!r}"))
            return

    # Part B: debt-backlog --status wontfix must fail (wontfix not in debt enum).
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Wontfix debt entry",
                "--body", "Debt we will not address.",
                "--status", "wontfix",
                "--source", "daily-review/test",
                "--risk", "Some risk.",
                "--proposed-action", "Some action.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("--schema debt-backlog --status wontfix must be REJECTED; got exit 0"))
            return
        combined = result.stdout + result.stderr
        if "open" not in combined and "wontfix" not in combined:
            raise AssertionError(f"{name}: " + (f"error output should mention valid status values. stderr: {result.stderr!r}"))
            return



# ---------------------------------------------------------------------------
# Test F7 — multi-line body roundtrip: |- header, no trailing newline
# ---------------------------------------------------------------------------

def test_multiline_body_roundtrip() -> None:
    """Review: code-reviewer — F7: multi-line body must roundtrip without trailing
    newline. Uses |- (strip chomping) so 'First line.\\nSecond line.' parses back
    as exactly that string with NO trailing newline.

    This test FAILS before F2's fix (| clip chomping adds a trailing newline)
    and PASSES after (|- strip chomping preserves exact bytes).
    """
    name = "Test F7 — multi-line body roundtrip: |- header, no trailing newline"
    body_input = "First line.\nSecond line."

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Multiline body test entry",
                "--body", body_input,
                "--status", "open",
                "--source", "daily-review/multiline/2026-06-26",
                "--risk", "Multi-line bodies with trailing newline break YAML fidelity.",
                "--proposed-action", "Use strip chomping (|-) in block scalar emitter.",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])

        # Check the raw YAML contains the |- header (strip chomping, not clip).
        with open(yaml_path, encoding="utf-8") as fh:
            raw = fh.read()
        if "body: |-" not in raw:
            raise AssertionError(f"{name}: " + (f"expected 'body: |-' (strip chomping) in raw YAML; got: {raw!r}"))
            return

        # Parse and verify no trailing newline (byte-fidelity guarantee).
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        got_body = parsed.get("body", None)
        if got_body is None:
            raise AssertionError(f"{name}: " + ("body field missing from parsed YAML"))
            return
        if got_body != body_input:
            raise AssertionError(f"{name}: " + (f"body roundtrip mismatch: expected {body_input!r}, got {got_body!r}. "
                f"(trailing newline indicates | clip chomping instead of |- strip chomping)"))
            return



# ---------------------------------------------------------------------------
# Test C2a — system block with resolvable session ID (CCOS-3 C2)
# ---------------------------------------------------------------------------

def test_system_block_with_session_id() -> None:
    """With CLAUDE_CODE_SESSION_ID set, the written record must contain a system block
    with created_by_session == the known id, linked_sessions containing it, and
    provenance_completeness == 'complete'. Record must also pass schema validation.

    Spec backlink: docs/plans/2026-06-26-queue-schema-unify.md § C2 AC4 (a)
    """
    name = "Test C2a — system block populated when CLAUDE_CODE_SESSION_ID is set"
    session_id = "test-session-c2a-abc123"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "CLAUDE_CODE_SESSION_ID": session_id,
            },
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        system = parsed.get("system")
        if not isinstance(system, dict):
            raise AssertionError(f"{name}: " + (f"expected 'system' key as dict; got {type(system).__name__!r}: {system!r}"))
            return

        got_session = system.get("created_by_session")
        if got_session != session_id:
            raise AssertionError(f"{name}: " + (f"system.created_by_session: expected {session_id!r}, got {got_session!r}"))
            return

        linked = system.get("linked_sessions")
        if not isinstance(linked, list):
            raise AssertionError(f"{name}: " + (f"system.linked_sessions: expected list, got {type(linked).__name__!r}: {linked!r}"))
            return
        if session_id not in linked:
            raise AssertionError(f"{name}: " + (f"system.linked_sessions does not contain {session_id!r}; got {linked!r}"))
            return

        completeness = system.get("provenance_completeness")
        if completeness != "complete":
            raise AssertionError(f"{name}: " + (f"system.provenance_completeness: expected 'complete', got {completeness!r}"))
            return

        # Schema validation: record (including system block) must pass schema validation.
        # Rewired from schema_loader.validate() to node schema-cli.js (C5b, schema_loader
        # retirement), then from node schema-cli.js (DELETED 480ad8f8) to its byte-identical
        # parity Python successor coordinator_core/frontmatter/schema_cli.py, invoked with the
        # SAME `--validate <schema-name>` / stdin-JSON argv contract the deleted node script
        # used (see coordinator_core/frontmatter/schema_cli.py module docstring and
        # coordinator/tests/test_lesson_promote_node_enum.py's analogous
        # `--describe` subprocess call for the sibling successor-CLI invocation pattern).
        effective_fields = {k: v for k, v in parsed.items() if v is not None and v != ""}
        try:
            # PyYAML may parse date fields as Python date objects; convert to ISO strings
            # before serialising to JSON for the validator.
            def _json_serialise(obj):
                if isinstance(obj, (datetime.date, datetime.datetime)):
                    return obj.isoformat()
                raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

            validate_proc = subprocess.run(
                [sys.executable, "-m", "coordinator_core.frontmatter.schema_cli", "--validate", "debt-backlog"],
                input=json.dumps(effective_fields, default=_json_serialise),
                capture_output=True,
                text=True,
                cwd=_REPO_ROOT,
                **no_console_creationflags(),
            )
            validate_json = json.loads(validate_proc.stdout)
        except Exception as exc:
            raise AssertionError(f"{name}: " + (f"schema_cli.py validate raised: {exc}"))
            return
        if not validate_json.get("ok"):
            raise AssertionError(f"{name}: " + (f"schema validation failed: {validate_json.get('errors')!r}"))
            return



# ---------------------------------------------------------------------------
# Test C2b — system block with unresolvable session (CCOS-3 C2)
# ---------------------------------------------------------------------------

def test_system_block_without_session_id() -> None:
    """With CLAUDE_CODE_SESSION_ID unset and no reachable sentinel (cwd is a
    fresh tmpdir with no .git), the written record must have:
    - provenance_completeness == 'unknown'
    - NO created_by_session key
    - linked_sessions == []

    Spec backlink: docs/plans/2026-06-26-queue-schema-unify.md § C2 AC4 (b)
    """
    name = "Test C2b — system block with unresolvable session (provenance_completeness=unknown)"
    with tempfile.TemporaryDirectory() as tmpdir:
        # Explicitly clear CLAUDE_CODE_SESSION_ID so it's not inherited from
        # the test runner's environment. An empty string strips to empty → unresolved.
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=tmpdir,  # fresh tmpdir — not a git repo, so sentinel is unreachable
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        system = parsed.get("system")
        if not isinstance(system, dict):
            raise AssertionError(f"{name}: " + (f"expected 'system' key as dict; got {type(system).__name__!r}: {system!r}"))
            return

        # created_by_session MUST be absent (no fabrication — honest, no fake id).
        if "created_by_session" in system:
            raise AssertionError(f"{name}: " + (f"system.created_by_session must be ABSENT when session unresolvable; "
                f"got {system['created_by_session']!r}"))
            return

        completeness = system.get("provenance_completeness")
        if completeness != "unknown":
            raise AssertionError(f"{name}: " + (f"system.provenance_completeness: expected 'unknown', got {completeness!r}"))
            return

        linked = system.get("linked_sessions")
        if linked != [] and linked is not None and linked != "[]":
            # Tolerate PyYAML parsing '[]' as [] OR as the string '[]'
            raise AssertionError(f"{name}: " + (f"system.linked_sessions: expected [] for unresolved session, got {linked!r}"))
            return



# ---------------------------------------------------------------------------
# Test C2-sentinel — sentinel tier REMOVED from _resolve_session_id (KS-3)
# ---------------------------------------------------------------------------

def test_sentinel_file_ignored_KS3() -> None:
    """KS-3 (2026-08-07): the `.current-session-id` sentinel tier was removed
    from _resolve_session_id — unsound under concurrency (last-writer-wins
    across concurrent sessions sharing one worktree) and its sole writer
    (session-init.py) was deleted 2026-07-15 (PM directive). A well-formed
    sentinel file must now be IGNORED: with CLAUDE_CODE_SESSION_ID unset, the
    session id resolves to "" and provenance_completeness degrades to
    "unknown" — the record must NOT vouch for its own attribution when it
    can't be resolved.

    Spec backlink: docs/plans/2026-06-26-queue-schema-unify.md § C2 STEP 1
    """
    name = "Test C2-sentinel — sentinel file is ignored (KS-3)"
    sentinel_session_id = "test-sentinel-session-c2-xyz789"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Initialize a real git repo so git rev-parse --show-toplevel returns tmpdir.
        # A manually-created .git/HEAD is insufficient — git requires a properly
        # initialized object store for rev-parse to succeed.
        init_result = subprocess.run(
            ["git", "init", tmpdir],
            capture_output=True, text=True,
        )
        if init_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init_result.stderr!r}"))
            return

        # Write the sentinel file — must be ignored post-KS-3.
        git_dir = os.path.join(tmpdir, ".git")
        sentinel_dir = os.path.join(git_dir, "coordinator-sessions")
        os.makedirs(sentinel_dir, exist_ok=True)
        sentinel_path = os.path.join(sentinel_dir, ".current-session-id")
        with open(sentinel_path, "w", encoding="utf-8") as fh:
            fh.write(sentinel_session_id + "\n")

        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "CLAUDE_CODE_SESSION_ID": "",  # unresolved — sentinel must not rescue this
            },
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        system = parsed.get("system")
        if not isinstance(system, dict):
            # Without PyYAML the minimal parser returns "" for the nested system block.
            # In that case, inspect the raw YAML directly.
            with open(yaml_path, encoding="utf-8") as fh:
                raw = fh.read()
            if sentinel_session_id in raw:
                raise AssertionError(f"{name}: " + (f"sentinel session id {sentinel_session_id!r} FOUND in written YAML "
                    f"— sentinel tier must be ignored post-KS-3. Raw YAML excerpt: {raw[:500]!r}"))
                return
            if "provenance_completeness: unknown" not in raw:
                raise AssertionError(f"{name}: " + (f"expected 'provenance_completeness: unknown' in raw YAML (unresolved session_id); "
                    f"raw: {raw[:500]!r}"))
                return
            return

        got_session = system.get("created_by_session")
        if got_session == sentinel_session_id:
            raise AssertionError(f"{name}: " + (f"system.created_by_session: sentinel id {sentinel_session_id!r} was used — sentinel tier must be ignored post-KS-3"))
            return

        completeness = system.get("provenance_completeness")
        if completeness != "unknown":
            raise AssertionError(f"{name}: " + (f"system.provenance_completeness: expected 'unknown' (unresolved session_id), got {completeness!r}"))
            return



# ---------------------------------------------------------------------------
# Test C2c — provenance_completeness enum guard (CCOS-3 C2 STEP 3)
# ---------------------------------------------------------------------------

def test_provenance_completeness_is_valid_by_construction() -> None:
    """Guard check: provenance_completeness is set by construction to 'complete' or 'unknown'.
    There is no user-facing path (no --provenance-completeness flag, no generic --field
    key=value path) that could supply a different value. This test verifies the
    derived value is always in {complete, unknown} for the two possible session states.

    Spec backlink: docs/plans/2026-06-26-queue-schema-unify.md § C2 STEP 3 / AC4 (c)
    """
    name = "Test C2c — provenance_completeness valid-by-construction for both session states"

    valid_values = {"complete", "unknown"}

    # Case 1: session resolved → expect 'complete'
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _debt_backlog_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir, "CLAUDE_CODE_SESSION_ID": "sess-guard-test"},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"[resolved] CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("[resolved] no YAML file produced"))
            return
        parsed = _parse_yaml_file(os.path.join(expected_dir, yaml_files[0]))
        system = parsed.get("system", {})
        val = system.get("provenance_completeness") if isinstance(system, dict) else None
        if val not in valid_values:
            raise AssertionError(f"{name}: " + (f"[resolved] provenance_completeness {val!r} not in {valid_values}"))
            return

    # Case 2: session unresolved → expect 'unknown'
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _debt_backlog_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir, "CLAUDE_CODE_SESSION_ID": ""},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"[unresolved] CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("[unresolved] no YAML file produced"))
            return
        parsed = _parse_yaml_file(os.path.join(expected_dir, yaml_files[0]))
        system = parsed.get("system", {})
        val = system.get("provenance_completeness") if isinstance(system, dict) else None
        if val not in valid_values:
            raise AssertionError(f"{name}: " + (f"[unresolved] provenance_completeness {val!r} not in {valid_values}"))
            return

    # No --provenance-completeness flag exists — verify argparse rejects it.
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _debt_backlog_required_args() + ["--provenance-completeness", "invalid-value"],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + ("--provenance-completeness flag must not exist (no user path to set it); "
                "CLI accepted it unexpectedly"))
            return
        # argparse will reject an unknown flag — that's the guard: no user path exists.



# ---------------------------------------------------------------------------
# Helpers for coordinator-lesson-add (wrapper) tests
# ---------------------------------------------------------------------------

def _lesson_add_script_path() -> str:
    """Return the absolute path to coordinator-lesson-add."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-lesson-add")


def _run_lesson_add_cli(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Invoke coordinator-lesson-add as a subprocess (drives via python <script>).

    Inherits the full environment so QUEUE_APPEND_OUTPUT_ROOT propagates transitively
    to the coordinator-queue-append subprocess spawned by the wrapper.
    """
    effective_env = {**os.environ}
    if env:
        effective_env.update(env)
    return subprocess.run(
        [_python(), _lesson_add_script_path()] + args,
        env=effective_env,
        capture_output=True,
        text=True,
        cwd=cwd,
        **no_console_creationflags(),
    )


# ---------------------------------------------------------------------------
# Test C3a — lessons facet flags roundtrip: --trigger/--why/--how-to-apply
# ---------------------------------------------------------------------------

def _lessons_required_args() -> list[str]:
    """Return the minimal valid CLI args for a lessons entry."""
    return [
        "--schema", "lessons",
        "--title", "Test lesson facets roundtrip",
        "--body", "Body prose for the facets roundtrip test.",
        "--scope", "project",
        "--from-repo", "test-repo-em",
    ]


def test_lessons_facets_roundtrip() -> None:
    """C3 AC4(a): write a lesson with all three facet flags set; read back; assert
    all three facet values are present and correct AND prose body is unchanged.

    Spec backlink: docs/plans/2026-06-30-lesson-structured-facets-and-emit-metadata-fix.md § C3
    """
    name = "Test C3a — lessons --trigger/--why/--how-to-apply roundtrip with body intact"
    trigger_val = "The agent emitted lessons prose without structured fields."
    why_val = "Without structured facets, triage automation cannot parse intent or routing."
    how_to_apply_val = "Pass --trigger/--why/--how-to-apply to coordinator-lesson-add on every new capture."
    body_val = "Body prose for the facets roundtrip test."

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _lessons_required_args() + [
                "--trigger", trigger_val,
                "--why", why_val,
                "--how-to-apply", how_to_apply_val,
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "lessons")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"output directory not created: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        for field, expected in [
            ("trigger", trigger_val),
            ("why", why_val),
            ("how_to_apply", how_to_apply_val),
            ("body", body_val),
        ]:
            got = parsed.get(field)
            if got != expected:
                raise AssertionError(f"{name}: " + (f"field {field!r}: expected {expected!r}, got {got!r}"))
                return



# ---------------------------------------------------------------------------
# Test C3b — lessons without facets: facet keys absent from YAML
# ---------------------------------------------------------------------------

def test_lessons_facets_absent_when_not_passed() -> None:
    """C3 AC4(b): write a lesson WITHOUT the facet flags; assert the three facet
    keys are ABSENT from the YAML (not empty-string, not null-key) — honest non-coverage.

    Spec backlink: docs/plans/2026-06-30-lesson-structured-facets-and-emit-metadata-fix.md § C3
    """
    name = "Test C3b — facet keys absent from YAML when --trigger/--why/--how-to-apply not passed"

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _lessons_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "lessons")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        with open(yaml_path, encoding="utf-8") as fh:
            raw = fh.read()

        # Check parsed dict does not contain facet keys.
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        for facet_key in ("trigger", "why", "how_to_apply"):
            # Key must be ABSENT entirely from the parsed dict — a null-valued key
            # (e.g. `trigger: null`) is a writer regression and must also be caught.
            # Review: code-reviewer F1 — prior guard `parsed[k] not in (None, "", "null")`
            # passed null-key silently; spec requires the key to be absent, not null.
            if facet_key in parsed:
                raise AssertionError(f"{name}: " + (f"facet key {facet_key!r} should be ABSENT when not passed; "
                    f"got parsed[{facet_key!r}]={parsed[facet_key]!r}"))
                return
            # Also verify the raw YAML does not start a line with the key name.
            # Line-anchored (^…MULTILINE) prevents substring false-positives
            # (e.g. "how_to_apply_notes:"). Review: code-reviewer F3 — plain substring
            # `in raw` was not line-anchored and missed null-key lines.
            raw_key = facet_key + ":"
            if re.search(r'^' + re.escape(raw_key), raw, re.MULTILINE):
                raise AssertionError(f"{name}: " + (f"raw YAML contains {raw_key!r} key line when facet was not passed. "
                    f"Raw snippet: {raw[:500]!r}"))
                return



# ---------------------------------------------------------------------------
# Test C3c — YAML-significant-char fixture: --why with colon + quote roundtrips
# ---------------------------------------------------------------------------

def test_lessons_facet_yaml_special_chars_roundtrip() -> None:
    """C3 AC4(c): write a lesson with --why containing a colon AND a quote;
    read back; assert the why value round-trips BYTE-IDENTICAL through YAML write/read.

    Proves the --why facet emit path is quoting-safe — distinct code path from body.
    (Only --why is exercised; C3a covers --trigger/--how-to-apply with plain ASCII values.)

    Spec backlink: docs/plans/2026-06-30-lesson-structured-facets-and-emit-metadata-fix.md § C3
    """
    name = "Test C3c — --why with YAML-significant chars (colon + quote) roundtrips byte-identical"
    # Value contains both a colon (inline mapping ambiguity) and a double-quote
    # (must be escaped if the emitter wraps in double-quotes).
    why_val = 'don\'t do Y: it breaks "everything" downstream'

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _lessons_required_args() + ["--why", why_val],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "lessons")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        got_why = parsed.get("why")
        if got_why != why_val:
            raise AssertionError(f"{name}: " + (f"why field did not roundtrip byte-identical: "
                f"expected {why_val!r}, got {got_why!r}"))
            return



# ---------------------------------------------------------------------------
# Test C3d — coordinator-lesson-add wrapper: facet threading through argparse
# ---------------------------------------------------------------------------

def test_lesson_add_facet_threading() -> None:
    """C3 integration: invoke coordinator-lesson-add (the dedup wrapper) directly with
    --trigger/--why/--how-to-apply; assert the output YAML carries all three facets
    with the correct values.

    C3a–c test coordinator-queue-append directly and do not cover the wrapper's
    argparse + `cmd +=` threading path. This test closes that gap.

    Review: code-reviewer F2 — add integration test for coordinator-lesson-add facet threading.
    Spec backlink: docs/plans/2026-06-30-lesson-structured-facets-and-emit-metadata-fix.md § C3
    """
    name = "Test C3d — coordinator-lesson-add wrapper threads --trigger/--why/--how-to-apply to YAML"
    trigger_val = "Wrapper argparse thread test trigger."
    why_val = "Without wrapper threading, facets silently drop when called via coordinator-lesson-add."
    how_to_apply_val = "Invoke coordinator-lesson-add with all three flags; verify output YAML."

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_lesson_add_cli(
            [
                "--title", "Wrapper facet threading test lesson",
                "--body", "Body for wrapper threading test.",
                "--scope", "project",
                "--trigger", trigger_val,
                "--why", why_val,
                "--how-to-apply", how_to_apply_val,
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"coordinator-lesson-add exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "lessons")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"output directory not created: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        for field, expected in [
            ("trigger", trigger_val),
            ("why", why_val),
            ("how_to_apply", how_to_apply_val),
        ]:
            got = parsed.get(field)
            if got != expected:
                raise AssertionError(f"{name}: " + (f"field {field!r}: expected {expected!r}, got {got!r}"))
                return



# ---------------------------------------------------------------------------
# Helpers for routing-gate tests (C2 strang-08 arm)
# ---------------------------------------------------------------------------

def _make_fake_coordinator_core(tmpdir: str, mode: str = "success", out_path: str = "") -> str:
    """Create a fake coordinator_core.invoke package under tmpdir for routing tests.

    mode (applies to any op OTHER than schema.describe/schema.validate — see below —
    i.e. in practice "queue.append", the op these routing tests actually exercise):
          "success" -> returns {out_path: <out_path>} in a JSON-RPC envelope.
          "skipped" -> returns {skipped: true, reason: "test-claude-klabauter-root-unresolvable"}.
          "capture" -> writes params+env to tmpdir/captured.json and returns success.

    Returns tmpdir (the CLAUDE_KLABAUTER_ROOT value that makes the fake seam present).

    schema.describe/schema.validate are handled UNCONDITIONALLY (regardless of mode),
    always accepting: schema.validate always returns {"ok": true, "errors": []} and
    stashes the passed fields to disk; schema.describe reads that stash back and
    returns {"required": [], "optional": <stashed field names>, "enums": {}} — enough
    for the CLI's own _validate()/_build_yaml() (which route BOTH ops through this same
    fake CLAUDE_KLABAUTER_ROOT) to accept and emit every field, without hardcoding any real
    schema's shape. Rewired (de-node cutover, 480ad8f8 / W0.5 Option B+C): schema ops
    now hard-require the native seam with no legacy fallback — this fake previously
    only modeled the single op each test cared about (typically queue.append), and any
    op it didn't recognize silently got the SAME configured response, corrupting the
    "capture" mode's captured.json (a schema op landing there before/instead of the
    intended op) and starving schema.validate of an "ok" key (validation-rejected). The
    schema-op branch below is why captured.json is now keyed by the op name in "capture"
    mode's on-disk shape — callers must select the "queue.append" entry.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2
    """
    coord_dir = os.path.join(tmpdir, "coordinator_core")
    os.makedirs(coord_dir, exist_ok=True)
    # __init__.py required for the package to be importable as a Python module.
    with open(os.path.join(coord_dir, "__init__.py"), "w", encoding="utf-8") as fh:
        fh.write("")

    stash_path_repr = repr(os.path.join(tmpdir, "_schema_fields_stash.json"))
    schema_op_preamble = (
        "import json, os, sys\n"
        "_op = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "_params_raw = sys.argv[2] if len(sys.argv) > 2 else '{}'\n"
        "_params = json.loads(_params_raw) if _params_raw else {}\n"
        "if _op == 'schema.validate':\n"
        "    with open(" + stash_path_repr + ", 'w') as _sfh:\n"
        "        json.dump(_params.get('fields', {}), _sfh)\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'ok': True, 'errors': []}}))\n"
        "    sys.exit(0)\n"
        "if _op == 'schema.describe':\n"
        "    _stashed = {}\n"
        "    if os.path.exists(" + stash_path_repr + "):\n"
        "        with open(" + stash_path_repr + ") as _sfh:\n"
        "            _stashed = json.load(_sfh)\n"
        "    _desc = {'required': [], 'optional': list(_stashed.keys()), 'enums': {}}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': _desc}))\n"
        "    sys.exit(0)\n"
    )

    if mode == "success":
        invoke_body = (
            "import json, sys\n"
            "if __name__ == '__main__':\n"
            + _indent(schema_op_preamble)
            + "    result = {'out_path': " + repr(out_path) + "}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}))\n"
            "    sys.exit(0)\n"
        )
    elif mode == "skipped":
        invoke_body = (
            "import json, sys\n"
            "if __name__ == '__main__':\n"
            + _indent(schema_op_preamble)
            + "    result = {'skipped': True, 'reason': 'test-claude-klabauter-root-unresolvable'}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}))\n"
            "    sys.exit(0)\n"
        )
    elif mode == "capture":
        capture_path_repr = repr(os.path.join(tmpdir, "captured.json"))
        out_path_repr = repr(out_path)
        invoke_body = (
            "import json, os, sys\n"
            "if __name__ == '__main__':\n"
            + _indent(schema_op_preamble)
            # Only the caller's own op (queue.append) is captured — an infra call this
            # fake doesn't otherwise recognize (e.g. --dump-op-timeouts) must NOT
            # overwrite captured.json, or a routing test asserting "captured.json was
            # never written" (proving the native op path wasn't taken) would see a
            # false positive from an unrelated non-schema, non-queue.append call.
            + "    if _op == 'queue.append':\n"
            "        capture = {\n"
            "            'params': _params,\n"
            "            'env_session_id': os.environ.get('CLAUDE_CODE_SESSION_ID', ''),\n"
            "        }\n"
            "        with open(" + capture_path_repr + ", 'w') as fh:\n"
            "            json.dump(capture, fh)\n"
            "    result = {'out_path': " + out_path_repr + "}\n"
            "    print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}))\n"
            "    sys.exit(0)\n"
        )
    else:
        raise ValueError(f"Unknown mode: {mode!r}")

    with open(os.path.join(coord_dir, "invoke.py"), "w", encoding="utf-8") as fh:
        fh.write(invoke_body)

    return tmpdir


def _indent(body: str, prefix: str = "    ") -> str:
    """Indent every (non-empty) line of `body` by `prefix` — used to nest the
    schema-op preamble inside `_make_fake_coordinator_core`'s `if __name__ ==
    '__main__':` block without hand-duplicating the indentation per call site.
    """
    return "".join(prefix + line if line.strip() else line for line in body.splitlines(keepends=True))


# ---------------------------------------------------------------------------
# Test R1 — routing seam-absent → legacy path (State-1)
# ---------------------------------------------------------------------------

def test_routing_seam_absent_uses_legacy() -> None:
    """State-1 (total seam absence): the CLI now fails loud instead of falling
    back to a legacy write.

    Rewired (de-node cutover, 480ad8f8 / W0.5 Option B+C, 2026-07-19): this test
    originally asserted that a CLAUDE_KLABAUTER_ROOT with no coordinator_core.invoke made
    the OUTER queue.append routing gate fall back to legacy_fn(), which then
    validated and wrote the YAML itself via a CLIENT-SIDE schema.describe/
    schema.validate call. That assumption no longer holds: legacy_fn()'s own
    _validate()/_build_yaml() route their schema.describe/schema.validate calls
    through the exact same CLAUDE_KLABAUTER_ROOT-keyed seam check the outer gate uses —
    and schema ops have had NO legacy fallback since schema-cli.js was deleted
    (_schema_cli_no_legacy: "no legacy fallback exists"). So a CLAUDE_KLABAUTER_ROOT with
    truly no coordinator_core now fails BEFORE legacy_fn ever reaches a write —
    there is no longer any environment shape where "outer op seam-absent" and
    "schema op seam-present" can diverge, since both resolve against the same
    CLAUDE_KLABAUTER_ROOT. This is empirically confirmed (not merely inferred): the same
    CLI invocation that used to reach `_write_out_path_excl` now exits 1 at the
    schema.validate step, naming the missing native seam. The correct assertion
    for total seam absence is now "fails loud, writes nothing" — verified below.
    Genuine legacy-fallback (fake-but-working coordinator_core) coverage lives in
    test_routing_seam_present_uses_native and friends; the truly-empty-seam case
    for schema failure specifically is covered by test_schema_load_fails_loud_via_env_override.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2
    """
    name = "Test R1 — routing: total seam absence -> fails loud, no write (State-1, post de-node cutover)"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        # Initialize a real git repo so _current_repo_root() returns git_root.
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        # claude_klabauter_dir has no coordinator_core package -> seam absent for EVERY op,
        # including the schema.describe/schema.validate calls legacy_fn's own
        # _validate()/_build_yaml() make — which now have no legacy fallback either.
        # Do NOT set QUEUE_APPEND_OUTPUT_ROOT so the live routing gate is exercised.
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode == 0:
            raise AssertionError(f"{name}: " + (f"expected non-zero exit with total seam absence; got 0. stdout={result.stdout!r}"))
            return

        combined = result.stdout + result.stderr
        if "schema" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"expected a schema-seam failure message; stderr={result.stderr!r}"))
            return

        expected_dir = os.path.join(git_root, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")] if os.path.isdir(expected_dir) else []
        if yaml_files:
            raise AssertionError(f"{name}: " + (f"expected NO YAML written on total seam absence; found {yaml_files}"))
            return



# ---------------------------------------------------------------------------
# Test R2 — routing seam-present → native path (State-2)
# ---------------------------------------------------------------------------

def test_routing_seam_present_uses_native() -> None:
    """State-2: when CLAUDE_KLABAUTER_ROOT points at a dir with a fake coordinator_core.invoke,
    the routing gate takes the native path, printing the fake out_path to stdout.

    Does NOT set QUEUE_APPEND_OUTPUT_ROOT (routing tests exercise the live routing gate,
    not the test-isolation bypass). Uses a git-initialized tmpdir as cwd.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2
    """
    name = "Test R2 — routing: seam-present -> native path, stdout = result out_path (State-2)"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        fake_out_path = os.path.join(git_root, "state", "debt-backlog", "2026-01-01-test.yaml")
        _make_fake_coordinator_core(claude_klabauter_dir, mode="success", out_path=fake_out_path)

        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        # Native path prints result['out_path'] to stdout (AC2 stdout parity).
        out_path_stdout = result.stdout.strip()
        if out_path_stdout != fake_out_path:
            raise AssertionError(f"{name}: " + (f"expected stdout={fake_out_path!r} (native out_path); "
                f"got {out_path_stdout!r}. stderr={result.stderr!r}"))
            return



# ---------------------------------------------------------------------------
# Test R3 — grep-gate: legacy_fn body still present in source (AC6)
# ---------------------------------------------------------------------------

def test_grep_gate_legacy_fn_present() -> None:
    """AC6: the legacy body must be preserved verbatim as legacy_fn in the source.

    Greps coordinator-queue-append for 'def legacy_fn' to confirm the write-core
    body is still present on the legacy path.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2 / AC6
    """
    name = "Test R3 — grep-gate: legacy_fn body present in coordinator-queue-append source (AC6)"
    script_path = _script_path()
    with open(script_path, encoding="utf-8") as fh:
        source = fh.read()

    if "def legacy_fn" not in source:
        raise AssertionError(f"{name}: " + ("source does not contain 'def legacy_fn' — legacy body may have been removed"))
        return

    # Assert the legacy write-core landmarks are present inside legacy_fn.
    for marker in ("_validate(schema_name, fields)", "_build_yaml(schema_name, fields)", "_ClaudeKlabauterUnresolvable"):
        if marker not in source:
            raise AssertionError(f"{name}: " + (f"source missing legacy_fn landmark {marker!r}"))
            return



# ---------------------------------------------------------------------------
# Test R4 — AC11: from_repo + session provenance parity on native path
# ---------------------------------------------------------------------------

def test_native_provenance_parity() -> None:
    """AC11: the native path must receive the CLI-resolved from_repo and session_id.

    Uses a capture-mode fake coordinator_core.invoke that writes received params
    to disk; verifies:
      - from_repo in params == the explicit --from-repo value (not op basename default)
      - session_id in params == the known session_id (param-first as of claude-klabauter a9f0a9e;
        no longer threaded via os.environ mutation)

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2 / AC11
    """
    name = "Test R4 — AC11: native path receives CLI-resolved from_repo and session_id"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        fake_out_path = os.path.join(git_root, "fake-entry.yaml")
        _make_fake_coordinator_core(claude_klabauter_dir, mode="capture", out_path=fake_out_path)

        expected_from_repo = "test-repo-em"
        expected_session_id = "test-session-r4-abc123"

        # Review: code-reviewer — F6: pass --created-by-agent so we can assert it threads
        # into op params. Lines 1274-1276 in the CLI add created_by_agent explicitly to
        # _op_params; this arg exercises that line and would catch its accidental removal.
        result = _run_cli(
            _debt_backlog_required_args() + ["--created-by-agent", "test-agent-em"],
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": expected_session_id,
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        capture_path = os.path.join(claude_klabauter_dir, "captured.json")
        if not os.path.isfile(capture_path):
            raise AssertionError(f"{name}: " + (f"capture file not written by fake op at {capture_path}"))
            return

        with open(capture_path, encoding="utf-8") as fh:
            captured = json.load(fh)

        params = captured.get("params", {})
        got_from_repo = params.get("from_repo")
        if got_from_repo != expected_from_repo:
            raise AssertionError(f"{name}: " + (f"from_repo in op params: expected {expected_from_repo!r}, got {got_from_repo!r}. "
                "The op basename default would diverge — must be the CLI-resolved value."))
            return

        got_session = params.get("session_id", "")
        if got_session != expected_session_id:
            raise AssertionError(f"{name}: " + (f"session_id in op params: expected {expected_session_id!r}, "
                f"got {got_session!r}. Session provenance must be passed as op param "
                f"(param-first as of claude-klabauter a9f0a9e — not via os.environ mutation)."))
            return

        # Review: code-reviewer — F6: assert created_by_agent threads into op params.
        expected_created_by_agent = "test-agent-em"
        got_agent = params.get("created_by_agent")
        if got_agent != expected_created_by_agent:
            raise AssertionError(f"{name}: " + (f"created_by_agent in op params: expected {expected_created_by_agent!r}, "
                f"got {got_agent!r}. --created-by-agent must be threaded explicitly into "
                "native op params (lines 1274-1276 in coordinator-queue-append)."))
            return



# ---------------------------------------------------------------------------
# Test R4b — AC11 gap: --queue-scope central appears in native op params (F5)
# ---------------------------------------------------------------------------

def test_native_queue_scope_param_threading() -> None:
    """AC11 gap (F5): when --queue-scope central is passed, queue_scope must appear
    in the native op params with value "central".

    Uses capture-mode fake coordinator_core.invoke. The None-stripping loop in the
    CLI includes queue_scope in _op_params when set; this test would catch accidental
    removal of that path.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2 / AC11
    """
    # Review: code-reviewer — F5: AC11 gap — queue_scope="central" on native path is untested.
    name = "Test R4b — AC11: --queue-scope central appears in native op params"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        fake_out_path = os.path.join(git_root, "fake-entry.yaml")
        _make_fake_coordinator_core(claude_klabauter_dir, mode="capture", out_path=fake_out_path)

        result = _run_cli(
            _improvement_queue_required_args(["--queue-scope", "central"]),
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        capture_path = os.path.join(claude_klabauter_dir, "captured.json")
        if not os.path.isfile(capture_path):
            raise AssertionError(f"{name}: " + (f"capture file not written by fake op at {capture_path}"))
            return

        with open(capture_path, encoding="utf-8") as fh:
            captured = json.load(fh)

        params = captured.get("params", {})
        got_queue_scope = params.get("queue_scope")
        if got_queue_scope != "central":
            raise AssertionError(f"{name}: " + (f"queue_scope in op params: expected 'central', got {got_queue_scope!r}. "
                "--queue-scope central must be threaded into native op params."))
            return



# ---------------------------------------------------------------------------
# Test R5 — AC12: skipped -> WARN to stderr, no path to stdout
# ---------------------------------------------------------------------------

def test_skipped_envelope_emits_warn_no_path() -> None:
    """AC12: when the native op returns {skipped: True, reason: ...}, the wrapper
    must emit the legacy WARN message to stderr and exit 0 without printing any path.

    Contract pt 5: skipped:true must NOT be treated as unconditional success.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2 / AC12
    """
    name = "Test R5 — AC12: skipped envelope -> WARN to stderr, no path to stdout"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        _make_fake_coordinator_core(claude_klabauter_dir, mode="skipped", out_path="")

        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"skipped path must exit 0; got {result.returncode}: {result.stderr!r}"))
            return

        # No path must appear in stdout.
        if result.stdout.strip():
            raise AssertionError(f"{name}: " + (f"skipped path must NOT print a path to stdout; got: {result.stdout!r}"))
            return

        # WARN must appear in stderr.
        # Review: code-reviewer — F4: require warn: unconditionally (case-insensitive). The prior
        # disjunction (warn: OR CLAUDE_KLABAUTER_ROOT) would pass on any error mentioning CLAUDE_KLABAUTER_ROOT without
        # a WARN line present, defeating the AC12 assertion. AC12 requires the legacy WARN message
        # parity — warn: must always be present on the skipped path.
        combined = result.stdout + result.stderr
        if "warn:" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"skipped path must emit 'warn:' to stderr; got stderr={result.stderr!r}"))
            return



# ---------------------------------------------------------------------------
# Test R6 — QUEUE_APPEND_OUTPUT_ROOT bypass skips native path when seam present (F8)
# ---------------------------------------------------------------------------

def test_output_root_bypass_skips_native() -> None:
    """When QUEUE_APPEND_OUTPUT_ROOT is set AND CLAUDE_KLABAUTER_ROOT points at a capture-mode fake,
    the early-exit bypass must fire BEFORE the routing gate so the native path is NOT
    taken. The YAML must land under QUEUE_APPEND_OUTPUT_ROOT, and captured.json must
    NOT be written.

    Without this test, moving the bypass below the route() call (or removing it) would
    not be caught.

    Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C2
    """
    # Review: code-reviewer — F8: QUEUE_APPEND_OUTPUT_ROOT bypass has no seam-present test.
    name = "Test R6 — QUEUE_APPEND_OUTPUT_ROOT bypass skips native path when seam present"
    with tempfile.TemporaryDirectory() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as output_root, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        fake_out_path = os.path.join(git_root, "fake-entry.yaml")
        _make_fake_coordinator_core(claude_klabauter_dir, mode="capture", out_path=fake_out_path)

        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "CLAUDE_KLABAUTER_ROOT": claude_klabauter_dir,
                "QUEUE_APPEND_OUTPUT_ROOT": output_root,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        # Native must NOT have been called: captured.json must be absent.
        capture_path = os.path.join(claude_klabauter_dir, "captured.json")
        if os.path.isfile(capture_path):
            raise AssertionError(f"{name}: " + ("captured.json was written (native path taken) despite QUEUE_APPEND_OUTPUT_ROOT "
                "being set. The bypass must fire before the routing gate."))
            return

        # Legacy path must have written the YAML under output_root.
        expected_dir = os.path.join(output_root, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")] if os.path.isdir(expected_dir) else []
        if not yaml_files:
            raise AssertionError(f"{name}: " + (f"legacy path did not write YAML under {expected_dir}"))
            return



# ---------------------------------------------------------------------------
# Test R7 — QUEUE_APPEND_OUTPUT_ROOT relative path rejected with clean error
# ---------------------------------------------------------------------------

def test_output_root_relative_path_rejected() -> None:
    """QUEUE_APPEND_OUTPUT_ROOT set to a relative path must exit non-zero with a
    clean one-line error message naming the bad value — not a Python traceback.

    A relative value would redirect writes to a cwd-relative location, which
    has no legitimate use-case for this test-only knob.
    """
    name = "Test R7 — QUEUE_APPEND_OUTPUT_ROOT relative path rejected with clean error"
    with tempfile.TemporaryDirectory() as cwd:
        result = _run_cli(
            _debt_backlog_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": "relative/path"},
            cwd=cwd,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for relative QUEUE_APPEND_OUTPUT_ROOT; got 0"))
        return
    # Must be a clean error line, not a Python traceback.
    if "Traceback" in result.stderr:
        raise AssertionError(f"{name}: " + (f"got a Python traceback instead of a clean error line: {result.stderr!r}"))
        return
    if "QUEUE_APPEND_OUTPUT_ROOT" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"error message should name QUEUE_APPEND_OUTPUT_ROOT. stderr: {result.stderr!r}"))
        return
    if "relative/path" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"error message should include the bad value. stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test C3b-1 — cross-repo-commitment: valid write, file exists, required fields present
# ---------------------------------------------------------------------------

# Review: code-reviewer — B-F5 (nit): keep in sync with
# coordinator/schemas/cross-repo-commitment.yaml `required:` — deriving this at test
# time via `schema-cli.js --describe cross-repo-commitment` was considered but is
# more than a small change (subprocess call + JSON parse + required-list extraction
# with no existing test-file helper for it); skipped as not-blocking per reviewer note.
_CROSS_REPO_COMMITMENT_REQUIRED_YAML_FIELDS = (
    "created",
    "title",
    "body",
    "status",
    "committed_by",
    "memo",
    "commitment",
    "observed",
)


def test_cross_repo_commitment_valid_write() -> None:
    name = "Test C3b-1 — cross-repo-commitment valid write exits 0, file has required fields"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _cross_repo_commitment_required_args(),
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(tmpdir, "state", "cross-repo-commitments")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"output directory not created: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected exactly 1 YAML file; found {len(yaml_files)}: {yaml_files}"))
            return

        yaml_path = os.path.join(expected_dir, yaml_files[0])
        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        missing = [f for f in _CROSS_REPO_COMMITMENT_REQUIRED_YAML_FIELDS if f not in parsed]
        if missing:
            raise AssertionError(f"{name}: " + (f"YAML missing required fields: {missing}. Parsed: {parsed}"))
            return

        # Negative-spec: from_repo must NOT appear — committed_by carries the
        # sibling-counterparty identity instead (schema does not declare from_repo).
        if "from_repo" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'from_repo:' (schema omits it); got {parsed['from_repo']!r}"))
            return

        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field; got id={parsed['id']!r}"))
            return



# ---------------------------------------------------------------------------
# Test C3b-2 — cross-repo-commitment: missing domain-required field exits non-zero
# ---------------------------------------------------------------------------

# Review: code-reviewer — B-F3 (nit): extracted from the C3b-2 body (was a double-call,
# double-`pair`-name comprehension) — one call, one flag/value-pair filter pass.
def _args_without_flag(args: list, flag: str) -> list:
    return [
        a for f, v in zip(args[::2], args[1::2])
        for a in (f, v)
        if f != flag
    ]


def test_cross_repo_commitment_missing_domain_field() -> None:
    name = "Test C3b-2 — cross-repo-commitment missing --committed-by exits non-zero"
    with tempfile.TemporaryDirectory() as tmpdir:
        args = _args_without_flag(_cross_repo_commitment_required_args(), "--committed-by")
        result = _run_cli(args, env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir}, cwd=tmpdir)
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit when --committed-by is omitted; got 0"))
        return
    if "committed-by" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"stderr should name the missing --committed-by flag. stderr: {result.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test C3b-3 — cross-repo-commitment: pinned status enum {open, fulfilled, withdrawn}
# ---------------------------------------------------------------------------

def test_cross_repo_commitment_status_enum() -> None:
    name = "Test C3b-3 — cross-repo-commitment status enum: fulfilled accepted, closed rejected"
    with tempfile.TemporaryDirectory() as tmpdir:
        base = [a for a in _cross_repo_commitment_required_args()]
        # Replace --status open with --status fulfilled.
        idx = base.index("--status") + 1
        base[idx] = "fulfilled"
        result_ok = _run_cli(base, env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir}, cwd=tmpdir)
    if result_ok.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--status fulfilled should be accepted; exited {result_ok.returncode}: {result_ok.stderr!r}"))
        return

    with tempfile.TemporaryDirectory() as tmpdir2:
        base2 = [a for a in _cross_repo_commitment_required_args()]
        idx2 = base2.index("--status") + 1
        # 'closed' is the BASE queue-family enum value — this schema pins its own
        # enum {open, fulfilled, withdrawn} and must reject it (negative-spec:
        # docs/wiki/cross-repo-commitments-schema.md § Status enum).
        base2[idx2] = "closed"
        result_bad = _run_cli(base2, env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir2}, cwd=tmpdir2)
    if result_bad.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected --status closed to be rejected (schema pins its own enum); got exit 0"))
        return
    # Review: code-reviewer — B-F4 (nit): assert stderr names the schema's actual pinned
    # enum values, not just that SOME non-zero exit occurred — matches this file's
    # established enum-rejection test convention (see test_invalid_enum_value_exits_nonzero).
    if "fulfilled" not in result_bad.stderr or "withdrawn" not in result_bad.stderr:
        raise AssertionError(f"{name}: " + (f"stderr should name the schema's pinned enum values (fulfilled, withdrawn). "
            f"stderr: {result_bad.stderr!r}"))
        return


# ---------------------------------------------------------------------------
# Test: path-traversal-shaped --created on --schema workstream-event exits
# non-zero, naming --created and the offending value, BEFORE any path
# construction (the value must never reach filesystem I/O).
#
# Break-class parity fix: `filename_override = f"{created}-{args.workstream}-
# {args.session}.yaml"` (main()'s workstream-event branch) already validated
# --workstream and --session via _validate_workstream_identifier, but --created
# reached the same f-string with zero validation — a "../../../evil"-shaped
# value escapes state/workstreams/events/. See _validate_created_date's
# doc-comment (coordinator-queue-append) for why the identifier allowlist is
# the wrong oracle for a date field.
# ---------------------------------------------------------------------------

def test_traversal_shaped_created_rejected_on_workstream_event() -> None:
    name = "Test — path-traversal-shaped --created rejected for --schema workstream-event"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "workstream-event",
                "--workstream", "wks-created-guard",
                "--field", "status",
                "--value", "in_progress",
                "--sequence", "1",
                "--session", "test-session",
                "--created", "../../../evil",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode == 0:
            raise AssertionError(
                f"{name}: " + (
                    "expected non-zero exit for a path-traversal-shaped --created value; got 0. "
                    f"stdout: {result.stdout!r}"
                )
            )
            return
        if "--created" not in result.stderr:
            raise AssertionError(
                f"{name}: " + (f"stderr should name --created. stderr: {result.stderr!r}")
            )
            return
        if "../../../evil" not in result.stderr:
            raise AssertionError(
                f"{name}: " + (f"stderr should show the offending value. stderr: {result.stderr!r}")
            )
            return
        # The traversal path must never have been created — proves rejection
        # happened before any directory creation / path construction.
        escaped_dir = os.path.abspath(os.path.join(tmpdir, "..", "..", "..", "evil"))
        if os.path.exists(escaped_dir):
            raise AssertionError(
                f"{name}: " + (f"traversal path must not exist on disk: {escaped_dir}")
            )
            return


