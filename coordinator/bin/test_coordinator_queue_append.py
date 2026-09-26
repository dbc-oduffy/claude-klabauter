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

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REPO_ROOT_COORDINATOR_CORE = os.path.join(_REPO_ROOT, "coordinator_core")


def _resolve_doe_root_for_tests() -> str:
    """Best-effort DoE-claude sibling root, for tests that override CLAUDE_HOME.

    coordinator/bin/lib/coordinator_registry.py's manifest ladder falls back to
    a machine-local `repos.doe_claude` lookup that is itself CLAUDE_HOME/
    settings-home-anchored -- a test overriding CLAUDE_HOME to a throwaway dir
    (to prove CLAUDE_HOME is unused for project scope, e.g.) collaterally
    breaks that fallback too. Resolving it once here and forwarding it as an
    explicit DOE_ROOT env override (coordinator_registry.py's own rung 1
    override) keeps the manifest read working without touching what the test
    actually asserts on.
    """
    try:
        from coordinator_core.testing.doe_root import resolve_doe_root

        root = resolve_doe_root()
    except Exception:
        return ""
    return root if root and os.path.isdir(root) else ""


_DOE_ROOT_FOR_TESTS = _resolve_doe_root_for_tests()


def _script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-queue-append.py")


def _python() -> str:
    return sys.executable


def _run_cli(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
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
    try:
        import yaml as _yaml
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


def _debt_backlog_required_args() -> list[str]:
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


def test_help_exits_zero() -> None:
    name = "Test 1 — --help exits 0 with non-empty stdout"
    result = _run_cli(["--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--help exited {result.returncode}, expected 0"))
        return
    if not result.stdout.strip():
        raise AssertionError(f"{name}: " + ("--help produced empty stdout"))
        return


def test_help_names_caller_quoting_contract() -> None:
    name = "Test 1b — --help tells callers to quote values containing shell metacharacters"
    result = _run_cli(["--help"])
    if result.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--help exited {result.returncode}, expected 0"))
        return
    normalized_stdout = " ".join(result.stdout.split())
    if "quoted by the caller" not in normalized_stdout:
        raise AssertionError(
            f"{name}: " + ("--help does not tell callers that metacharacter-bearing values must be quoted")
        )
        return


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
    if "debt-backlog" not in combined and "bug-backlog" not in combined and "improvement-queue" not in combined:
        raise AssertionError(f"{name}: " + (f"stderr does not name any known schema. stderr: {result.stderr!r}"))
        return


def test_missing_required_field_exits_nonzero() -> None:
    name = "Test 3 — missing required field exits non-zero, stderr names the field"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "T",
                "--body", "B",
                "--status", "open",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for missing required fields; got 0"))
        return
    combined = result.stdout + result.stderr
    missing_field_names = ("source", "risk", "proposed-action", "proposed_action")
    if not any(f in combined.lower() for f in missing_field_names):
        raise AssertionError(f"{name}: " + (f"error output does not name any missing field. stderr: {result.stderr!r}"))
        return


def test_invalid_enum_value_exits_nonzero() -> None:
    name = "Test 4 — invalid --status value exits non-zero, stderr names valid values"
    with tempfile.TemporaryDirectory() as tmpdir:
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
    if "open" not in combined:
        raise AssertionError(f"{name}: " + (f"error output should name valid status values (e.g. 'open'). stderr: {result.stderr!r}"))
        return


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
    expected_flags = ("--title", "--body", "--status", "--source", "--risk", "--proposed-action")
    missing_from_output = [flag for flag in expected_flags if flag not in combined]
    if missing_from_output:
        raise AssertionError(
            f"{name}: " + (
                f"one run did not name all required flags — missing from output: "
                f"{missing_from_output}. stderr: {result.stderr!r}"
            )
        )
    if "--status" not in combined:
        raise AssertionError(f"{name}: " + ("--status not named as required. stderr: " + repr(result.stderr)))


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


def test_invalid_lesson_scope_names_valid_set() -> None:
    name = "Test 4e — --scope global is rejected and the valid lesson-scope set is named"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "lessons",
                "--title", "scope enum probe",
                "--body", "scope enum probe",
                "--scope", "global",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit for --scope global; got 0"))
    combined = result.stdout + result.stderr
    if not combined.strip():
        raise AssertionError(f"{name}: " + ("rejection produced NO diagnostic — the silent-exit-1 regression"))
    for value in ("universal", "project", "wiki-only"):
        if value not in combined:
            raise AssertionError(f"{name}: " + (f"valid scope {value!r} missing from rejection output: {combined!r}"))


_DEBT_BACKLOG_REQUIRED_YAML_FIELDS = (
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

        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field (D2 drop); got id={parsed['id']!r}"))
            return


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

        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field (D2 drop); got id={parsed['id']!r}"))
            return

        created = parsed.get("created", "")
        if not created:
            raise AssertionError(f"{name}: " + (f"created field is empty or absent: {created!r}"))
            return


def test_mid_string_hash_roundtrips() -> None:
    name = "Test 6b — mid-string ' #' title/body survives roundtrip (no silent truncation)"
    title = "fix the bug #urgent in parser"
    body = "see PR #123 and issue #4 for context"
    risk = "has # hash mid and a\t#tab-preceded hash"

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


def test_schema_doc_not_runtime_parsed() -> None:
    name = "Test 7 — schema docs not read at runtime (the Staff Engineer F0 guard)"
    with tempfile.TemporaryDirectory() as tmpdir:
        wiki_path = os.path.join(tmpdir, "docs", "wiki", "debt-backlog-schema.md")
        if os.path.exists(wiki_path):
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

        expected_dir = os.path.join(tmpdir, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")] if os.path.isdir(expected_dir) else []
        if not yaml_files:
            raise AssertionError(f"{name}: " + ("CLI exited 0 but no YAML file was produced."))
            return


# Test 7b — schema-load fails loud (rewired: bad CLAUDE_KLABAUTER_ROOT, not COORDINATOR_SCHEMAS_DIR)

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
    with _engine_root_tmpdir() as bad_claude_klabauter_root, \
         tempfile.TemporaryDirectory() as tmpdir:
        _populate_engine_root_minus_invoke(bad_claude_klabauter_root)
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "COORDINATOR_ENGINE_ROOT": bad_claude_klabauter_root,
                **_seam_absence_env(tmpdir),
            },
            cwd=tmpdir,
        )

    if result.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected non-zero exit when CLAUDE_KLABAUTER_ROOT has no coordinator_core; got 0"))
        return

    combined = result.stdout + result.stderr
    if not any(
        token in combined
        for token in ("schema", "debt-backlog", bad_claude_klabauter_root, "remediation", "Remediation")
    ):
        raise AssertionError(f"{name}: " + (f"stderr does not contain a remediation message. stderr: {result.stderr!r}"))
        return

    written = []
    debt_dir = os.path.join(tmpdir, "state", "debt-backlog")
    if os.path.isdir(debt_dir):
        written = [f for f in os.listdir(debt_dir) if f.endswith(".yaml")]
    if written:
        raise AssertionError(f"{name}: " + (f"YAML file was written despite schema load failure: {written}"))
        return


def test_id_flag_rejected_as_unknown() -> None:
    name = "Test 8 — --id flag rejected as unknown argument (D2: id field dropped)"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--id", "DSR-2026-06-25-1",
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
    if "--id" not in combined and "unrecognized" not in combined.lower():
        raise AssertionError(f"{name}: " + (f"stderr does not mention --id or 'unrecognized'. stderr: {result.stderr!r}"))
        return


# Test 9 — central queue_scope writes to CLAUDE_KLABAUTER_ROOT, not cwd

def _improvement_queue_required_args(extra: list[str] | None = None) -> list[str]:
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
    """Build a fake_root/coordinator_core that is genuinely self-located AND
    genuinely stamped, symlinking in THIS checkout's real package otherwise.

    Post de-node-cutover (480ad8f8 / W0.5 Option B+C), schema.describe/schema.validate
    have NO legacy fallback — cc_invoke.route() checks seam presence against the exact
    same CLAUDE_KLABAUTER_ROOT the CLI uses for central-scope output routing, so a CLAUDE_KLABAUTER_ROOT
    that is merely "present but empty" (the pre-cutover test-isolation trick) now makes
    ANY write fail loud at the schema step, before the central-routing behavior this
    test targets is ever reached. Symlinking the real coordinator_core in gives the
    schema seam a working native implementation while state/ output under fake_root
    stays a disposable tmpdir, never the real checkout's own state/ tree.

    A whole-directory symlink (the pre-1268f7eab1 shape) broke the moment the
    dispatch-axis stamp gate armed: `ipc.py`'s own `_DISPATCH_ENGINE_ROOT =
    Path(__file__).resolve().parent.parent` calls `.resolve()`, which follows a
    symlinked `coordinator_core` straight through to THIS checkout's real, on-disk
    location — an unstamped dev tree — no matter what fake_root is, so the gate
    refused every dispatch regardless of what this fixture built. `ipc.py` is
    therefore copied (a real file physically under fake_root, nothing left to
    resolve away) rather than symlinked, and paired with a real, non-empty
    `_engine_stamp` — the two things `_is_dispatch_engine_stamped()` actually reads.
    Every other module stays a symlink: none of them gate on their own `__file__`.
    """
    coord_dir = os.path.join(fake_root, "coordinator_core")
    os.makedirs(coord_dir, exist_ok=True)
    for entry in os.listdir(_REPO_ROOT_COORDINATOR_CORE):
        if entry in ("ipc.py", "_engine_stamp", "__pycache__"):
            continue
        src = os.path.join(_REPO_ROOT_COORDINATOR_CORE, entry)
        dest = os.path.join(coord_dir, entry)
        if not os.path.exists(dest):
            os.symlink(src, dest, target_is_directory=os.path.isdir(src))
    shutil.copy2(os.path.join(_REPO_ROOT_COORDINATOR_CORE, "ipc.py"), os.path.join(coord_dir, "ipc.py"))
    with open(os.path.join(coord_dir, "_engine_stamp"), "w", encoding="utf-8") as fh:
        fh.write("test-fixture-stamp\n")


def test_central_scope_writes_to_claude_klabauter_root() -> None:
    """With CLAUDE_KLABAUTER_ROOT set (no QUEUE_APPEND_OUTPUT_ROOT), --queue-scope central must write
    to <CLAUDE_KLABAUTER_ROOT>/state/improvement-queue/, even when cwd is a different tmpdir.

    This is the load-bearing invariant: a session in a sibling repo must not write central
    entries into the sibling's state/improvement-queue/ — they must always land in
    claude-klabauter's own state/ tree.

    Central state routes to _claude_klabauter_root() unconditionally, per
    docs/wiki/state-placement-law.md § Taxonomy "Central/global state" (governing law:
    DoE-claude coordinator/docs/wiki/state-placement-law.md:36). The
    [DoE-claude] docs/plans/2026-07-06-gate2-w23-state-seam-caller-switch.md plan's
    proposal to instead route this branch to DoE was never ratified: that plan is
    `status: draft`, its AC1/AC2 are `pending`, and its own C3 is HELD with recorded
    disk proof the flip never took effect on the production path — see
    _output_path()'s own negative-spec docstring.

    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC1 / AC13
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
    with _engine_root_tmpdir() as claude_klabauter_root_dir, \
         tempfile.TemporaryDirectory() as sibling_cwd:
        _seed_symlinked_claude_klabauter_root(claude_klabauter_root_dir)
        init = subprocess.run(["git", "init", sibling_cwd], capture_output=True, text=True)
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return
        env = {
            "COORDINATOR_ENGINE_ROOT": claude_klabauter_root_dir,
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

        sibling_queue_dir = os.path.join(sibling_cwd, "state", "improvement-queue")
        if os.path.isdir(sibling_queue_dir):
            sibling_files = [f for f in os.listdir(sibling_queue_dir) if f.endswith(".yaml")]
            if sibling_files:
                raise AssertionError(f"{name}: " + (f"YAML written to sibling cwd (wrong): {sibling_files}"))
                return

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
         _engine_root_tmpdir() as fake_claude_klabauter_root, \
         tempfile.TemporaryDirectory() as project_cwd:
        # Set CLAUDE_HOME to a different dir to confirm it is NOT used for project scope.
        _seed_symlinked_claude_klabauter_root(fake_claude_klabauter_root)
        init = subprocess.run(["git", "init", project_cwd], capture_output=True, text=True)
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return
        env = {"CLAUDE_HOME": claude_home_dir, "COORDINATOR_ENGINE_ROOT": fake_claude_klabauter_root}
        if _DOE_ROOT_FOR_TESTS:
            env["DOE_ROOT"] = _DOE_ROOT_FOR_TESTS

        result = _run_cli(
            _improvement_queue_required_args(),
            env=env,
            cwd=project_cwd,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        expected_dir = os.path.join(project_cwd, "state", "improvement-queue")
        if not os.path.isdir(expected_dir):
            raise AssertionError(f"{name}: " + (f"expected output dir not created under cwd: {expected_dir}"))
            return

        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        if len(yaml_files) != 1:
            raise AssertionError(f"{name}: " + (f"expected 1 YAML in cwd; found {len(yaml_files)}: {yaml_files}"))
            return

        home_queue_dir = os.path.join(claude_home_dir, "state", "improvement-queue")
        if os.path.isdir(home_queue_dir):
            home_files = [f for f in os.listdir(home_queue_dir) if f.endswith(".yaml")]
            if home_files:
                raise AssertionError(f"{name}: " + (f"YAML written to CLAUDE_HOME (wrong for project scope): {home_files}"))
                return

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


def test_queue_scope_central_rejected_for_non_improvement_schemas() -> None:
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


def test_base_status_enum_closed_accepted_resolved_rejected() -> None:
    name = "Test 11 — --status closed accepted; --status resolved rejected for debt"

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


def test_unified_shape_proposed_action_no_id() -> None:
    name = "Test 12 — unified shape: proposed_action: present, id: absent, surface: present"

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


def test_wontfix_status_acceptance() -> None:
    name = "Test F6 — --status wontfix accepted for bug-backlog, rejected for debt-backlog"

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


def test_multiline_body_roundtrip() -> None:
    name = "Test F7 — multi-line body roundtrip: |- header, no trailing newline"
    body_input = "First line.\nSecond line."

    with tempfile.TemporaryDirectory() as tmpdir:
        body_path = os.path.join(tmpdir, "body.txt")
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(body_input)

        result = _run_cli(
            [
                "--schema", "debt-backlog",
                "--title", "Multiline body test entry",
                "--body-file", body_path,
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

        with open(yaml_path, encoding="utf-8") as fh:
            raw = fh.read()
        if "body: |-" not in raw:
            raise AssertionError(f"{name}: " + (f"expected 'body: |-' (strip chomping) in raw YAML; got: {raw!r}"))
            return

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


# parse, so `%*` (and `%CMDCMDLINE%`, hence `raw_cmdline_recovery`) are already

def test_body_file_carries_multiline_body() -> None:
    body_input = "First line.\n\nThird line after a blank one.\nFourth line."

    with tempfile.TemporaryDirectory() as tmpdir:
        body_path = os.path.join(tmpdir, "body.txt")
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(body_input)

        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Body file multiline entry",
                "--body-file", body_path,
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr!r}"

        expected_dir = os.path.join(tmpdir, "state", "bug-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        assert len(yaml_files) == 1, f"expected 1 YAML file; found {yaml_files}"

        parsed = _parse_yaml_file(os.path.join(expected_dir, yaml_files[0]))
        assert parsed.get("body") == body_input, (
            f"body roundtrip mismatch: expected {body_input!r}, got {parsed.get('body')!r}"
        )


def test_body_file_does_not_expand_escape_sequences() -> None:
    body_input = "A regex like \\d+\\n matches a digit run.\nSecond real line."

    with tempfile.TemporaryDirectory() as tmpdir:
        body_path = os.path.join(tmpdir, "body.txt")
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(body_input)

        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Body file literal escape entry",
                "--body-file", body_path,
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
                "--from-repo", "test-repo-em",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr!r}"

        expected_dir = os.path.join(tmpdir, "state", "bug-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        parsed = _parse_yaml_file(os.path.join(expected_dir, yaml_files[0]))
        assert parsed.get("body") == body_input, (
            f"file-borne escape sequence was expanded: got {parsed.get('body')!r}"
        )


def test_body_and_body_file_are_mutually_exclusive() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        body_path = os.path.join(tmpdir, "body.txt")
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write("from the file")

        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Both body sources",
                "--body", "from argv",
                "--body-file", body_path,
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal when both body sources are supplied"
        assert "mutually exclusive" in result.stderr, f"stderr did not name the conflict: {result.stderr!r}"


def test_body_file_unreadable_fails_loud() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Missing body file",
                "--body-file", os.path.join(tmpdir, "absent.txt"),
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal on an unreadable --body-file"
        assert "--body-file unreadable" in result.stderr, f"stderr did not name the flag: {result.stderr!r}"
        assert not os.path.isdir(os.path.join(tmpdir, "state", "bug-backlog")), (
            "a refused run must not have written anything"
        )


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

        effective_fields = {k: v for k, v in parsed.items() if v is not None and v != ""}
        try:
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
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "QUEUE_APPEND_OUTPUT_ROOT": tmpdir,
                "CLAUDE_CODE_SESSION_ID": "",
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
            raise AssertionError(f"{name}: " + (f"system.linked_sessions: expected [] for unresolved session, got {linked!r}"))
            return


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
        init_result = subprocess.run(
            ["git", "init", tmpdir],
            capture_output=True, text=True,
        )
        if init_result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init_result.stderr!r}"))
            return

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
                "CLAUDE_CODE_SESSION_ID": "",
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


def test_provenance_completeness_is_valid_by_construction() -> None:
    name = "Test C2c — provenance_completeness valid-by-construction for both session states"

    valid_values = {"complete", "unknown"}

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


def _lesson_add_script_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "coordinator-lesson-add.py")


def _installed_settings_bin_dir() -> str:
    """The settings-home `bin/` dir a bare-PATH probe resolves an installed
    CLI from -- same precedence git_hook_install.py's durable-path constant
    uses (`COORDINATOR_SETTINGS_HOME`, else `~/.coordinator-claude-settings`).
    """
    settings_home = os.environ.get("COORDINATOR_SETTINGS_HOME") or os.path.join(
        os.path.expanduser("~"), ".coordinator-claude-settings"
    )
    return os.path.join(settings_home, "bin")


def _run_lesson_add_cli(args: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Invoke coordinator-lesson-add as a subprocess (drives via python <script>).

    Inherits the full environment so QUEUE_APPEND_OUTPUT_ROOT propagates transitively
    to the coordinator-queue-append subprocess spawned by the wrapper.

    PATH is scrubbed of the installed settings-home `bin/` dir: `_queue_append_
    locator.find_cli_cmd`'s probe order tries a bare-PATH `coordinator-queue-append`
    FIRST, and on a box with a published klabauter build installed that resolves to
    a compiled native warm-door launcher -- not this checkout's Python source, and
    not a CLI `COORDINATOR_WARM=0` has any lever over (it dispatches warm by
    construction, with no cold-path env check to disable). A test measuring that
    binary measures the wrong code and reaches the box-shared warm server despite
    the cold-route pin above. Removing the installed dir from PATH makes the bare
    probe miss, so `find_cli_cmd` falls through to its sibling-path fallback and
    resolves THIS checkout's `coordinator-queue-append.py`, which does honour
    `COORDINATOR_WARM=0`.
    """
    effective_env = {**os.environ}
    if env:
        effective_env.update(env)
    installed_bin_dir = _installed_settings_bin_dir()
    path_entries = effective_env.get("PATH", "").split(os.pathsep)
    effective_env["PATH"] = os.pathsep.join(
        entry for entry in path_entries if os.path.normcase(os.path.normpath(entry or ".")) != os.path.normcase(os.path.normpath(installed_bin_dir))
    )
    return subprocess.run(
        [_python(), _lesson_add_script_path()] + args,
        env=effective_env,
        capture_output=True,
        text=True,
        cwd=cwd,
        **no_console_creationflags(),
    )


def _lessons_required_args() -> list[str]:
    return [
        "--schema", "lessons",
        "--title", "Test lesson facets roundtrip",
        "--body", "Body prose for the facets roundtrip test.",
        "--scope", "project",
        "--from-repo", "test-repo-em",
    ]


def test_lessons_facets_roundtrip() -> None:
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


def test_lessons_facets_absent_when_not_passed() -> None:
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

        try:
            parsed = _parse_yaml_file(yaml_path)
        except RuntimeError as exc:
            raise AssertionError(f"{name}: " + (f"YAML parse error: {exc}"))
            return

        for facet_key in ("trigger", "why", "how_to_apply"):
            if facet_key in parsed:
                raise AssertionError(f"{name}: " + (f"facet key {facet_key!r} should be ABSENT when not passed; "
                    f"got parsed[{facet_key!r}]={parsed[facet_key]!r}"))
                return
            # Line-anchored (^…MULTILINE) prevents substring false-positives
            raw_key = facet_key + ":"
            if re.search(r'^' + re.escape(raw_key), raw, re.MULTILINE):
                raise AssertionError(f"{name}: " + (f"raw YAML contains {raw_key!r} key line when facet was not passed. "
                    f"Raw snippet: {raw[:500]!r}"))
                return


def test_lessons_facet_yaml_special_chars_roundtrip() -> None:
    """C3 AC4(c): write a lesson with --why containing a colon AND a quote;
    read back; assert the why value round-trips BYTE-IDENTICAL through YAML write/read.

    Proves the --why facet emit path is quoting-safe — distinct code path from body.
    (Only --why is exercised; C3a covers --trigger/--how-to-apply with plain ASCII values.)

    Spec backlink: docs/plans/2026-06-30-lesson-structured-facets-and-emit-metadata-fix.md § C3
    """
    name = "Test C3c — --why with YAML-significant chars (colon + quote) roundtrips byte-identical"
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


def test_lesson_add_facet_threading() -> None:
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


def test_lesson_add_under_pytest_never_hits_warm() -> None:
    """A test-driven `coordinator-lesson-add` run must never reach the box-shared
    warm server -- regression guard for the 2026-09-18 leak (module docstring's
    "test traffic never reaches or spawns the box-shared server" fix).

    Two independent legs, both load-bearing: `COORDINATOR_WARM=0` (pinned by this
    file's `conftest.py`) disables warmth on the COLD path; scrubbing the installed
    settings-home `bin/` dir from PATH (`_run_lesson_add_cli`) stops
    `_queue_append_locator.find_cli_cmd`'s bare-PATH probe from resolving the
    published klabauter build's compiled warm-door launcher, which dispatches warm
    unconditionally and has no `COORDINATOR_WARM` lever at all. Either leg alone is
    insufficient -- this test pins their combination.

    Asserts on absence of "warm hit" in stderr/stdout (`cc_invoke.py`'s own phrase
    for a served-by-warm response, e.g. "native transport failed ... (op=queue.append,
    warm hit)") rather than a mock, since the whole point is proving the REAL
    subprocess never reached the server -- a mock would just assert on itself.
    """
    name = "Test — coordinator-lesson-add under pytest never produces a warm hit"
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_lesson_add_cli(
            [
                "--title", "Warm-hit regression guard test lesson",
                "--body", "Body for the never-hits-warm regression guard.",
                "--scope", "project",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"coordinator-lesson-add exited {result.returncode}: {result.stderr!r}"))
            return

        combined = (result.stdout or "") + (result.stderr or "")
        if "warm hit" in combined:
            raise AssertionError(f"{name}: " + (f"test-driven lesson-add reached the shared warm server: {combined!r}"))
            return


#: process's PYTHONPATH. `site` imports `sitecustomize` after it has processed
_DROP_EDITABLE_FINDER_SRC = (
    "import sys\n"
    "sys.meta_path[:] = [\n"
    "    _f for _f in sys.meta_path\n"
    "    if 'coordinator_core' not in getattr(_f, '__module__', '')\n"
    "    and 'coordinator_core' not in type(_f).__module__\n"
    "]\n"
)


def _seam_absence_env(tmp_holder: str) -> dict[str, str]:
    """Env overlay that lets `CLAUDE_KLABAUTER_ROOT` actually govern `coordinator_core`
    resolution in a spawned child.

    A seam-ABSENCE test needs `coordinator_core.invoke` to be genuinely
    unimportable from the root under test. On a box where `coordinator_core`
    is pip-installed EDITABLE, that is not achievable by `CLAUDE_KLABAUTER_ROOT` or
    `sys.path` alone: setuptools' editable install registers a
    `sys.meta_path` finder pinning the package to one hard-coded tree, and
    `sys.meta_path` is consulted BEFORE `sys.path`. So every child resolves
    that tree's `coordinator_core.invoke` no matter what root the test built,
    the seam is never absent, and the CLI's fail-loud ladder is never reached
    -- the test then measures the machine's install layout instead of the
    behaviour it names. (Measured 2026-08-19 on this box: the editable pin
    targets the LIVE working tree, so `find_spec('coordinator_core.invoke')`
    answered from it while `coordinator_core.__path__` pointed at the
    fixture's root -- two trees in one process, which is the same mixing the
    `cc_invoke` mirror-name ImportError comes from.)

    Dropping the finder in a `sitecustomize` restores `sys.path` as the
    authority for the CHILD ONLY -- no machine state is touched, and a box
    with no editable install is unaffected (the filter matches nothing).
    """
    sc_dir = os.path.join(tmp_holder, "_no_editable_finder")
    os.makedirs(sc_dir, exist_ok=True)
    with open(os.path.join(sc_dir, "sitecustomize.py"), "w", encoding="utf-8") as fh:
        fh.write(_DROP_EDITABLE_FINDER_SRC)
    existing = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": sc_dir + (os.pathsep + existing if existing else "")}


def _engine_root_tmpdir() -> "tempfile.TemporaryDirectory[str]":
    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


def _populate_engine_root_minus_invoke(root: str) -> None:
    """Build a `root/coordinator_core` package that mirrors the real one for
    every top-level entry EXCEPT `invoke`/`invoke.py`.

    require_engine_on_path (repo_identity.py's module-level side effect)
    front-inserts CLAUDE_KLABAUTER_ROOT at sys.path[0] before the real repo root is ever
    consulted, so a bare empty `root` (no coordinator_core at all) makes
    `coordinator_core.git`/`coordinator_core.pickup_assemble` unimportable --
    a spurious ModuleNotFoundError unrelated to whatever "seam absent"
    scenario the test is modeling. Symlinking everything but `invoke` gives
    those always-needed imports a real target while still leaving
    `coordinator_core.invoke` genuinely absent (find_spec-visible seam
    absence), which is the property these "seam absent"/"bad CLAUDE_KLABAUTER_ROOT"
    tests are actually asserting on.
    """
    coord_dir = os.path.join(root, "coordinator_core")
    os.makedirs(coord_dir, exist_ok=True)
    for _entry in os.listdir(_REPO_ROOT_COORDINATOR_CORE):
        if _entry in ("invoke", "invoke.py", "__pycache__"):
            continue
        _src = os.path.join(_REPO_ROOT_COORDINATOR_CORE, _entry)
        _dest = os.path.join(coord_dir, _entry)
        if not os.path.exists(_dest):
            os.symlink(_src, _dest, target_is_directory=os.path.isdir(_src))


def _make_fake_coordinator_core(tmpdir: str, mode: str = "success", out_path: str = "") -> str:
    """Create a fake coordinator_core.invoke package under tmpdir for routing tests.

    mode (applies to any op OTHER than schema.describe/schema.validate — see below —
    i.e. in practice "queue.append", the op these routing tests actually exercise):
          "success" -> returns {out_path: <out_path>} in a JSON-RPC envelope.
          "skipped" -> returns {skipped: true, reason: "test-claude-klabauter-live-root-unresolvable"}.
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

    Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C2
    """
    coord_dir = os.path.join(tmpdir, "coordinator_core")
    os.makedirs(coord_dir, exist_ok=True)

    # inserts CLAUDE_KLABAUTER_ROOT (= tmpdir here) at sys.path[0] before the real repo
    # spawned CLI -- real coordinator_core on PYTHONPATH included. Symlink
    for _entry in os.listdir(_REPO_ROOT_COORDINATOR_CORE):
        if _entry in ("invoke", "invoke.py", "__pycache__"):
            continue
        _src = os.path.join(_REPO_ROOT_COORDINATOR_CORE, _entry)
        _dest = os.path.join(coord_dir, _entry)
        if not os.path.exists(_dest):
            os.symlink(_src, _dest, target_is_directory=os.path.isdir(_src))

    stash_path_repr = repr(os.path.join(tmpdir, "_schema_fields_stash.json"))
    schema_op_preamble = (
        # "Params transport" note (ARG_MAX-immunity on Windows/msys). A fake
        "import json, os, sys\n"
        "_op = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "_params_raw = '{}'\n"
        "if '--params-file' in sys.argv:\n"
        "    with open(sys.argv[sys.argv.index('--params-file') + 1], encoding='utf-8') as _pfh:\n"
        "        _params_raw = _pfh.read()\n"
        "elif len(sys.argv) > 2 and not sys.argv[2].startswith('--'):\n"
        "    _params_raw = sys.argv[2]\n"
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
            + "    result = {'skipped': True, 'reason': 'test-claude-klabauter-live-root-unresolvable'}\n"
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
    return "".join(prefix + line if line.strip() else line for line in body.splitlines(keepends=True))


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

    Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C2
    """
    name = "Test R1 — routing: total seam absence -> fails loud, no write (State-1, post de-node cutover)"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
         tempfile.TemporaryDirectory() as git_root:
        init = subprocess.run(
            ["git", "init", git_root], capture_output=True, text=True,
        )
        if init.returncode != 0:
            raise AssertionError(f"{name}: " + (f"git init failed: {init.stderr!r}"))
            return

        # Do NOT set QUEUE_APPEND_OUTPUT_ROOT so the live routing gate is exercised.
        _populate_engine_root_minus_invoke(claude_klabauter_dir)
        result = _run_cli(
            _debt_backlog_required_args(),
            env={
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
                **_seam_absence_env(git_root),
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


def test_routing_seam_present_uses_native() -> None:
    """State-2: when CLAUDE_KLABAUTER_ROOT points at a dir with a fake coordinator_core.invoke,
    the routing gate takes the native path, printing the fake out_path to stdout.

    Does NOT set QUEUE_APPEND_OUTPUT_ROOT (routing tests exercise the live routing gate,
    not the test-isolation bypass). Uses a git-initialized tmpdir as cwd.

    Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C2
    """
    name = "Test R2 — routing: seam-present -> native path, stdout = result out_path (State-2)"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
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
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        out_path_stdout = result.stdout.strip()
        if out_path_stdout != fake_out_path:
            raise AssertionError(f"{name}: " + (f"expected stdout={fake_out_path!r} (native out_path); "
                f"got {out_path_stdout!r}. stderr={result.stderr!r}"))
            return


def test_grep_gate_legacy_fn_present() -> None:
    name = "Test R3 — grep-gate: legacy_fn body present in coordinator-queue-append source (AC6)"
    script_path = _script_path()
    with open(script_path, encoding="utf-8") as fh:
        source = fh.read()

    if "def legacy_fn" not in source:
        raise AssertionError(f"{name}: " + ("source does not contain 'def legacy_fn' — legacy body may have been removed"))
        return

    for marker in ("_validate(schema_name, fields)", "_build_yaml(schema_name, fields)", "_ClaudeKlabauterUnresolvable"):
        if marker not in source:
            raise AssertionError(f"{name}: " + (f"source missing legacy_fn landmark {marker!r}"))
            return


def test_native_provenance_parity() -> None:
    name = "Test R4 — AC11: native path receives CLI-resolved from_repo and session_id"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
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

        result = _run_cli(
            _debt_backlog_required_args() + ["--created-by-agent", "test-agent-em"],
            env={
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
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

        expected_created_by_agent = "test-agent-em"
        got_agent = params.get("created_by_agent")
        if got_agent != expected_created_by_agent:
            raise AssertionError(f"{name}: " + (f"created_by_agent in op params: expected {expected_created_by_agent!r}, "
                f"got {got_agent!r}. --created-by-agent must be threaded explicitly into "
                "native op params (lines 1274-1276 in coordinator-queue-append)."))
            return


def test_native_queue_scope_param_threading() -> None:
    name = "Test R4b — AC11: --queue-scope central appears in native op params"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
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
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
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


def test_skipped_envelope_emits_warn_no_path() -> None:
    name = "Test R5 — AC12: skipped envelope -> WARN to stderr, no path to stdout"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
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
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"skipped path must exit 0; got {result.returncode}: {result.stderr!r}"))
            return

        if result.stdout.strip():
            raise AssertionError(f"{name}: " + (f"skipped path must NOT print a path to stdout; got: {result.stdout!r}"))
            return

        # disjunction (warn: OR CLAUDE_KLABAUTER_ROOT) would pass on any error mentioning CLAUDE_KLABAUTER_ROOT without
        combined = result.stdout + result.stderr
        if "warn:" not in combined.lower():
            raise AssertionError(f"{name}: " + (f"skipped path must emit 'warn:' to stderr; got stderr={result.stderr!r}"))
            return


# Test R6 — QUEUE_APPEND_OUTPUT_ROOT bypass skips native path when seam present (F8)

def test_output_root_bypass_skips_native() -> None:
    """When QUEUE_APPEND_OUTPUT_ROOT is set AND CLAUDE_KLABAUTER_ROOT points at a capture-mode fake,
    the early-exit bypass must fire BEFORE the routing gate so the native path is NOT
    taken. The YAML must land under QUEUE_APPEND_OUTPUT_ROOT, and captured.json must
    NOT be written.

    Without this test, moving the bypass below the route() call (or removing it) would
    not be caught.

    Spec backlink: DoE-claude:pln-strang-08-arm-the-doe-queue-fa-36567b § C2
    """
    # QUEUE_APPEND_OUTPUT_ROOT bypass has no seam-present test.
    name = "Test R6 — QUEUE_APPEND_OUTPUT_ROOT bypass skips native path when seam present"
    with _engine_root_tmpdir() as claude_klabauter_dir, \
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
                "COORDINATOR_ENGINE_ROOT": claude_klabauter_dir,
                "QUEUE_APPEND_OUTPUT_ROOT": output_root,
                "CLAUDE_CODE_SESSION_ID": "",
            },
            cwd=git_root,
        )
        if result.returncode != 0:
            raise AssertionError(f"{name}: " + (f"CLI exited {result.returncode}: {result.stderr!r}"))
            return

        capture_path = os.path.join(claude_klabauter_dir, "captured.json")
        if os.path.isfile(capture_path):
            raise AssertionError(f"{name}: " + ("captured.json was written (native path taken) despite QUEUE_APPEND_OUTPUT_ROOT "
                "being set. The bypass must fire before the routing gate."))
            return

        expected_dir = os.path.join(output_root, "state", "debt-backlog")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")] if os.path.isdir(expected_dir) else []
        if not yaml_files:
            raise AssertionError(f"{name}: " + (f"legacy path did not write YAML under {expected_dir}"))
            return


# Test R7 — QUEUE_APPEND_OUTPUT_ROOT relative path rejected with clean error

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
    if "Traceback" in result.stderr:
        raise AssertionError(f"{name}: " + (f"got a Python traceback instead of a clean error line: {result.stderr!r}"))
        return
    if "QUEUE_APPEND_OUTPUT_ROOT" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"error message should name QUEUE_APPEND_OUTPUT_ROOT. stderr: {result.stderr!r}"))
        return
    if "relative/path" not in result.stderr:
        raise AssertionError(f"{name}: " + (f"error message should include the bad value. stderr: {result.stderr!r}"))
        return


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

        if "from_repo" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'from_repo:' (schema omits it); got {parsed['from_repo']!r}"))
            return

        if "id" in parsed:
            raise AssertionError(f"{name}: " + (f"YAML must NOT contain 'id:' field; got id={parsed['id']!r}"))
            return


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


def test_cross_repo_commitment_status_enum() -> None:
    name = "Test C3b-3 — cross-repo-commitment status enum: fulfilled accepted, closed rejected"
    with tempfile.TemporaryDirectory() as tmpdir:
        base = [a for a in _cross_repo_commitment_required_args()]
        idx = base.index("--status") + 1
        base[idx] = "fulfilled"
        result_ok = _run_cli(base, env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir}, cwd=tmpdir)
    if result_ok.returncode != 0:
        raise AssertionError(f"{name}: " + (f"--status fulfilled should be accepted; exited {result_ok.returncode}: {result_ok.stderr!r}"))
        return

    with tempfile.TemporaryDirectory() as tmpdir2:
        base2 = [a for a in _cross_repo_commitment_required_args()]
        idx2 = base2.index("--status") + 1
        base2[idx2] = "closed"
        result_bad = _run_cli(base2, env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir2}, cwd=tmpdir2)
    if result_bad.returncode == 0:
        raise AssertionError(f"{name}: " + ("expected --status closed to be rejected (schema pins its own enum); got exit 0"))
        return
    if "fulfilled" not in result_bad.stderr or "withdrawn" not in result_bad.stderr:
        raise AssertionError(f"{name}: " + (f"stderr should name the schema's pinned enum values (fulfilled, withdrawn). "
            f"stderr: {result_bad.stderr!r}"))
        return


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
        escaped_dir = os.path.abspath(os.path.join(tmpdir, "..", "..", "..", "evil"))
        if os.path.exists(escaped_dir):
            raise AssertionError(
                f"{name}: " + (f"traversal path must not exist on disk: {escaped_dir}")
            )
            return


def test_title_newline_argv_refused() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "First line\nSecond line",
                "--body", "Body prose.",
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal for a newline-bearing inline --title"
        assert "--title" in result.stderr, f"stderr did not name --title: {result.stderr!r}"
        assert "--title-file" not in result.stderr, (
            f"--title has no file sibling; stderr must not invent one: {result.stderr!r}"
        )


def test_body_newline_argv_refused() -> None:
    """VERIFIED GAP closure: an inline --body carrying a real newline is
    refused outright and names --body-file — it must NOT be silently
    truncated to its first line."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            [
                "--schema", "bug-backlog",
                "--title", "Body newline refusal entry",
                "--body", "First line.\nSecond line that must not be dropped.",
                "--status", "open",
                "--severity", "P3",
                "--surface", "coordinator/bin/coordinator-queue-append.py",
            ],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal for a newline-bearing inline --body"
        assert "--body-file" in result.stderr, f"stderr did not name --body-file: {result.stderr!r}"

        expected_dir = os.path.join(tmpdir, "state", "bug-backlog")
        yaml_files = os.listdir(expected_dir) if os.path.isdir(expected_dir) else []
        assert not yaml_files, f"a refused --body must not write an entry; found {yaml_files}"


def test_why_newline_argv_refused() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        result = _run_cli(
            _lessons_required_args() + ["--why", "First line\nSecond line"],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal for a newline-bearing inline --why"
        assert "--why-file" in result.stderr, f"stderr did not name --why-file: {result.stderr!r}"


def test_why_file_roundtrips_multiline_value_byte_for_byte() -> None:
    why_input = "First line of rationale.\n\nThird line after a blank one.\nFourth line."

    with tempfile.TemporaryDirectory() as tmpdir:
        why_path = os.path.join(tmpdir, "why.txt")
        with open(why_path, "w", encoding="utf-8") as fh:
            fh.write(why_input)

        result = _run_cli(
            _lessons_required_args() + ["--why-file", why_path],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr!r}"

        expected_dir = os.path.join(tmpdir, "state", "lessons")
        yaml_files = [f for f in os.listdir(expected_dir) if f.endswith(".yaml")]
        assert len(yaml_files) == 1, f"expected 1 YAML file; found {yaml_files}"

        parsed = _parse_yaml_file(os.path.join(expected_dir, yaml_files[0]))
        assert parsed.get("why") == why_input, (
            f"why roundtrip mismatch: expected {why_input!r}, got {parsed.get('why')!r}"
        )


def test_why_and_why_file_are_mutually_exclusive() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        why_path = os.path.join(tmpdir, "why.txt")
        with open(why_path, "w", encoding="utf-8") as fh:
            fh.write("from the file")

        result = _run_cli(
            _lessons_required_args() + ["--why", "from argv", "--why-file", why_path],
            env={"QUEUE_APPEND_OUTPUT_ROOT": tmpdir},
            cwd=tmpdir,
        )
        assert result.returncode != 0, "expected refusal when both --why sources are supplied"
        assert "mutually exclusive" in result.stderr, f"stderr did not name the conflict: {result.stderr!r}"


# A QUEUE_APPEND_OUTPUT_ROOT naming a temp-dir path a completed pytest run
# 0. This process's own PYTEST_CURRENT_TEST (set by pytest for the duration of


def test_swept_output_root_refuses_instead_of_writing() -> None:
    name = "Swept QUEUE_APPEND_OUTPUT_ROOT (under system temp, already gone) refuses instead of writing"
    swept = tempfile.mkdtemp()
    shutil.rmtree(swept)
    if os.path.isdir(swept):
        raise AssertionError(f"{name}: " + f"setup failed — {swept} still exists")

    result = _run_cli(
        _debt_backlog_required_args(),
        env={"QUEUE_APPEND_OUTPUT_ROOT": swept},
    )

    if result.returncode == 0:
        raise AssertionError(
            f"{name}: " + f"expected nonzero exit for a swept output root; got 0. stdout={result.stdout!r}"
        )
    if os.path.isdir(swept):
        raise AssertionError(
            f"{name}: " + f"CLI recreated the swept root at {swept!r} instead of refusing"
        )
    stderr_lower = result.stderr.lower()
    if "temp" not in stderr_lower and "tmp" not in stderr_lower:
        raise AssertionError(
            f"{name}: " + f"expected a one-line stderr message naming the temp-dir refusal; got {result.stderr!r}"
        )
