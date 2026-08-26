"""
coordinator_core.ops.tests.test_queue_parity — byte-parity harness for queue.append + queue.promote.

Purpose: Assert the Python ops (queue.append / queue.promote) produce byte-identical YAML output
to their legacy bash oracles (coordinator-queue-append / coordinator-lesson-promote) for the same
inputs. This is the strangler invariant (F10/AC2/AC5): if the byte output drifts, the DoE facade
routing will silently produce different on-disk entries.

Coverage:
  (a) AC2/F10  — byte-parity for queue.append across all five schemas (debt-backlog, bug-backlog,
                  improvement-queue, lessons, cross-repo-commitment), including system: block
                  field order
  (b) F8       — env-pin: CLAUDE_CODE_SESSION_ID identical for both op and bash oracle;
                  both provenance_completeness branches exercised (session-present→complete,
                  absent→unknown)
  (c) AC12     — created: emitted unquoted (bare YYYY-MM-DD) on the lessons path
  (d) AC5      — byte-parity for queue.promote (lessons-outbox: uuid id, ISO-ts filename,
                  no system block)
  (e) AC1/AC3/AC4 — content-keyed idempotency: identical re-run → one file; distinct
                  content, same title+date → two files (both survive)
  (f) AC13     — caller_worktree=X writes X/state/<queue>/, NOT claude-klabauter's own tree
  (g) AC6      — _ClaudeKlabauterUnresolvable graceful-degrade: WARN+skip, exit 0, no cwd fallback
  (h) F9/AC14  — node + schema-cli.js reachability asserted as explicit precondition;
                  ≥1 parseable-reject case confirming typed-error mapping

Spec backlink: pln-strang-08-queue-append-strangl-2a3499 § C5
Oracle: [DoE-claude] coordinator/bin/coordinator-queue-append
Oracle: [DoE-claude] coordinator/bin/coordinator-lesson-promote
"""

from __future__ import annotations

import asyncio
import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest

# ---------------------------------------------------------------------------
# Dispatchability guard — MUST precede all test functions.
# Lesson: universal-registry-completeness-tests-ov — import coordinator_core.ops
# FIRST, then guard the ops this module exercises BEFORE any per-op assertion.
#
# The guard is `resolves(...)`, not a non-empty `_REGISTRY`. Registration went
# LAZY-ONLY on 2026-08-22 (docs/plans/2026-08-22-the-import-path-costs-nothing.md
# § C3): importing `coordinator_core.ops` fires no `@register_op` decorator, so
# `_REGISTRY` is legitimately EMPTY at this point and the old
# `assert len(_REGISTRY) > 0` — written for eager mode — failed at COLLECTION,
# taking this whole module down with a message ("all @register_op decorators
# must have fired at module import time") stating a contract that had been
# deliberately retired. `resolves()` is the lazy-mode equivalent and is exactly
# what that plan's § C3 names as the correct assertion here; the empty-registry
# read it excludes is this one.
# ---------------------------------------------------------------------------
import coordinator_core.ops  # noqa: F401 — kept so an import failure surfaces here; registers nothing (lazy-only since 2026-08-22)

from coordinator_core.ipc import _REGISTRY  # noqa: F401 — retained for per-test reads
from coordinator_core.ops._registry_map import resolves

assert resolves("queue.append"), (
    "dispatchability guard failed: 'queue.append' not in _REGISTRY — "
    "coordinator_core.ops.queue_append is neither in OP_MODULE_MAP nor already registered"
)
assert resolves("queue.promote"), (
    "dispatchability guard failed: 'queue.promote' not in _REGISTRY — "
    "coordinator_core.ops.queue_promote is neither in OP_MODULE_MAP nor already registered"
)

import coordinator_core.ops.queue_append as _qa_mod  # noqa: E402
from coordinator_core.ops import coordinator_doe_root as _doe_root_mod  # noqa: E402
from coordinator_core.ops.queue_append import (  # noqa: E402
    _ClaudeKlabauterUnresolvable as _AppendUnresolvable,
    _queue_append_handler,
    append_queue_entry,
)
from coordinator_core.ops.queue_promote import (  # noqa: E402
    _DoeUnresolvable as _PromoteUnresolvable,
    _queue_promote_handler,
    promote_lesson,
)

# Byte-parity assertions compare op output against the legacy bash oracles'
# real output for the same real repo/session inputs — the env-pin (F8) and
# caller_worktree (F13) checks specifically assert against real git/session
# state, which a mocked git would only prove the mock's own return value for.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

# ---------------------------------------------------------------------------
# Oracle / schema-cli path resolution (reads ~/.claude/.doe-root sentinel)
# ---------------------------------------------------------------------------

_DOE_ROOT_SENTINEL = Path.home() / ".claude" / ".doe-root"
_DOE_QUEUE_APPEND_CLI: Optional[Path] = None
_DOE_LESSON_PROMOTE_CLI: Optional[Path] = None
_DOE_SCHEMA_CLI: Optional[Path] = None

if _DOE_ROOT_SENTINEL.exists():
    try:
        _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding="utf-8").strip()
        _DOE_QUEUE_APPEND_CLI = Path(_doe_root) / "coordinator" / "bin" / "coordinator-queue-append"
        _DOE_LESSON_PROMOTE_CLI = Path(_doe_root) / "coordinator" / "bin" / "coordinator-lesson-promote"
        _DOE_SCHEMA_CLI = Path(_doe_root) / "coordinator" / "bin" / "schema-cli.js"
    except OSError:
        print(f"skip: <module>: _doe_root = _DOE_ROOT_SENTINEL.read_text(encoding=\"utf-8\").strip() failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

# ---------------------------------------------------------------------------
# Precondition checks (F9/AC14 — explicit harness guard)
# ---------------------------------------------------------------------------

_NODE_AVAILABLE = shutil.which("node") is not None
_SCHEMA_CLI_AVAILABLE = (
    _NODE_AVAILABLE
    and _DOE_SCHEMA_CLI is not None
    and _DOE_SCHEMA_CLI.is_file()
)
_ORACLE_APPEND_AVAILABLE = (
    _SCHEMA_CLI_AVAILABLE
    and _DOE_QUEUE_APPEND_CLI is not None
    and _DOE_QUEUE_APPEND_CLI.is_file()
)
_ORACLE_PROMOTE_AVAILABLE = (
    _SCHEMA_CLI_AVAILABLE
    and _DOE_LESSON_PROMOTE_CLI is not None
    and _DOE_LESSON_PROMOTE_CLI.is_file()
)


def _schema_cli_precondition_reason() -> str:
    """Human-readable reason why the schema-cli precondition is not met."""
    parts: list[str] = []
    if not _NODE_AVAILABLE:
        parts.append("'node' not found on PATH — install Node.js (https://nodejs.org/)")
    if not _DOE_ROOT_SENTINEL.exists():
        parts.append("~/.claude/.doe-root sentinel absent (DoE root not configured)")
    elif _DOE_SCHEMA_CLI is None or not _DOE_SCHEMA_CLI.is_file():
        parts.append(
            f"schema-cli.js not found at {_DOE_SCHEMA_CLI} — "
            "check that DoE coordinator clone is present at the path in ~/.claude/.doe-root"
        )
    return "; ".join(parts) if parts else "ok"


_SCHEMA_CLI_SKIP = _schema_cli_precondition_reason()

_requires_schema_cli = pytest.mark.skipif(
    not _SCHEMA_CLI_AVAILABLE,
    reason=f"schema-cli precondition not met: {_SCHEMA_CLI_SKIP}",
)
_requires_oracle_append = pytest.mark.skipif(
    not _ORACLE_APPEND_AVAILABLE,
    reason="coordinator-queue-append oracle or schema-cli not available",
)
_requires_oracle_promote = pytest.mark.skipif(
    not _ORACLE_PROMOTE_AVAILABLE,
    reason="coordinator-lesson-promote oracle or schema-cli not available",
)

# ---------------------------------------------------------------------------
# YAML scalar extraction helper
# ---------------------------------------------------------------------------


def _extract_yaml_scalar(text: str, field: str) -> Optional[str]:
    """Extract a bare or double-quoted YAML scalar value from a document string.

    Handles:
        field: bare-value           → 'bare-value'
        field: "double-quoted"      → 'double-quoted'  (strips outer quotes + unescapes)
        field: "with \\"escaped\\"" → 'with "escaped"'

    Used in promote parity test to extract 'id' and 'created' from oracle output
    without carrying YAML-serializer quoting into the value passed to the native op.
    """
    m = re.search(rf"^{re.escape(field)}: (.+)$", text, re.MULTILINE)
    if not m:
        return None
    raw = m.group(1).strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        # Double-quoted scalar: strip outer quotes and unescape.
        inner = raw[1:-1]
        inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    return raw


# ---------------------------------------------------------------------------
# Fixed test constants
# ---------------------------------------------------------------------------

_TEST_DATE = "2026-01-15"
_TEST_SESSION = "test-parity-session-0001-abc"
_TEST_FROM_REPO = "claude-klabauter-em"

# Independent expected-path oracle (op now derives these from schema-cli --describe
# applies_to; this fixture is the cross-check — deliberately NOT derived the same way,
# so a regression in the op's derivation still surfaces as a test failure).
_SCHEMA_OUTPUT_DIRS: dict[str, str] = {
    "debt-backlog": "state/debt-backlog",
    "bug-backlog": "state/bug-backlog",
    "improvement-queue": "state/improvement-queue",
    "lessons": "state/lessons",
    "cross-repo-commitment": "state/cross-repo-commitments",
}

# ---------------------------------------------------------------------------
# Schema-specific test fixtures (plain YAML params — no stray --- fences)
# Lesson: test-fidelity-seed-fixtures-in-the-real — match real on-disk entry shape.
# ---------------------------------------------------------------------------

_SCHEMA_PARAMS: dict[str, dict] = {
    "debt-backlog": {
        "title": "Parity test debt entry alpha",
        "body": "Body for debt parity test.",
        "status": "open",
        "source": "daily-review/2026-01-15",
        "risk": "Medium risk to queue parity.",
        "proposed_action": "Fix the debt item.",
        "from_repo": _TEST_FROM_REPO,
    },
    "bug-backlog": {
        "title": "Parity test bug entry beta",
        "body": "Bug description for parity test.",
        "status": "open",
        "surface": "coordinator_core/ops/queue_append.py",
        "severity": "P2",
        "from_repo": _TEST_FROM_REPO,
    },
    "improvement-queue": {
        "title": "Parity test improvement entry gamma",
        "body": "Improvement body for parity test.",
        "status": "open",
        "surface": "coordinator_core/ops/queue_append.py",
        "proposed_action": "Refactor the helper function.",
        "from_repo": _TEST_FROM_REPO,
        "change_kind": "script-edit",
    },
    "lessons": {
        "title": "Parity test lesson entry delta",
        "body": "Lesson body for parity test.",
        # status defaults to "open" for lessons
        "scope": "universal",
        "from_repo": _TEST_FROM_REPO,
    },
    "cross-repo-commitment": {
        "title": "Parity test cross-repo commitment epsilon",
        "body": "Commitment body for parity test.",
        "status": "open",
        "committed_by": "claude-central-em",
        "memo": "cross-repo/archive/2026-01-15-example-memo.md",
        "commitment": "Register cross-repo-commitment as a fifth base schema.",
        "observed": _TEST_DATE,
        # cross-repo-commitment forbids from_repo — deliberately absent here.
    },
}

# ---------------------------------------------------------------------------
# Oracle runner helpers
# ---------------------------------------------------------------------------


def _oracle_append_cli_args(schema: str, params: dict) -> list[str]:
    """Build the CLI argv for coordinator-queue-append from a params dict.

    cross-repo-commitment negative-spec: this schema forbids ``from_repo`` (it uses
    ``committed_by`` for the sibling identity instead) — ``--from-repo`` is deliberately
    omitted for this schema, mirroring the oracle CLI's own explicit strip.
    """
    assert _DOE_QUEUE_APPEND_CLI is not None
    args = [sys.executable, str(_DOE_QUEUE_APPEND_CLI)]
    args += ["--schema", schema, "--title", params["title"], "--body", params["body"]]
    if schema != "cross-repo-commitment":
        args += ["--from-repo", params.get("from_repo", _TEST_FROM_REPO)]
    args += ["--created", _TEST_DATE]

    for flag, key in [
        ("--status", "status"),
        ("--surface", "surface"),
        ("--source", "source"),
        ("--risk", "risk"),
        ("--proposed-action", "proposed_action"),
        ("--severity", "severity"),
        ("--change-kind", "change_kind"),
        ("--scope", "scope"),
        ("--evidence", "evidence"),
        ("--committed-by", "committed_by"),
        ("--memo", "memo"),
        ("--commitment", "commitment"),
        ("--observed", "observed"),
    ]:
        if params.get(key):
            args += [flag, str(params[key])]
    return args


def _run_oracle_append(
    schema: str,
    params: dict,
    out_root: Path,
    session_id: str,
    oracle_cwd: Optional[Path] = None,
) -> bytes:
    """Run the coordinator-queue-append oracle subprocess and return output file bytes.

    Sets QUEUE_APPEND_OUTPUT_ROOT for test isolation, CLAUDE_CODE_SESSION_ID for
    provenance pinning, and strips CLAUDE_KLABAUTER_ROOT to avoid oracle writing to a live tree.
    The oracle resolves schema-cli.js relative to its own __file__ — no COORDINATOR_SCHEMA_CLI
    override needed on the oracle side.

    oracle_cwd: if supplied, the oracle subprocess runs from this directory. Set to a
    non-git directory to prevent the oracle's _resolve_session_id() sentinel fallback
    (.git/coordinator-sessions/.current-session-id) when testing the session-absent path.
    """
    env = os.environ.copy()
    env["QUEUE_APPEND_OUTPUT_ROOT"] = str(out_root)
    env["CLAUDE_CODE_SESSION_ID"] = session_id
    env.pop("CLAUDE_KLABAUTER_ROOT", None)

    args = _oracle_append_cli_args(schema, params)
    result = subprocess.run(
        args,
        env=env,
        capture_output=True,
        text=True,
        cwd=str(oracle_cwd) if oracle_cwd is not None else None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, (
        f"oracle coordinator-queue-append failed (exit {result.returncode}) "
        f"for schema {schema!r}:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )

    output_dir = out_root / _SCHEMA_OUTPUT_DIRS[schema]
    files = list(output_dir.glob("*.yaml"))
    assert len(files) == 1, (
        f"expected exactly 1 oracle output file in {output_dir}, got {len(files)}: {files}"
    )
    return files[0].read_bytes()


def _run_native_append(
    schema: str,
    params: dict,
    out_root: Path,
    session_id: str,
    monkeypatch,
) -> bytes:
    """Run the native append_queue_entry op and return output file bytes.

    Pins QUEUE_APPEND_OUTPUT_ROOT and COORDINATOR_SCHEMA_CLI via monkeypatch for
    test isolation. session_id is passed explicitly (not via env) for precise control.
    """
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(out_root))
    monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
    monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

    # Collect schema-specific keyword args from params dict (exclude universal params
    # handled by positional args below).
    op_kwargs = {
        k: v for k, v in params.items()
        if k not in ("title", "body", "from_repo")
    }

    # cross-repo-commitment forbids from_repo — omit it entirely rather than passing
    # the _TEST_FROM_REPO default, mirroring the oracle CLI's explicit strip.
    from_repo_kwarg = (
        {}
        if schema == "cross-repo-commitment"
        else {"from_repo": params.get("from_repo", _TEST_FROM_REPO)}
    )

    result = append_queue_entry(
        schema=schema,
        title=params["title"],
        body=params["body"],
        created=_TEST_DATE,
        session_id=session_id,
        **from_repo_kwarg,
        **op_kwargs,
    )
    return Path(result["out_path"]).read_bytes()


# ---------------------------------------------------------------------------
# Tests: schema-cli precondition (F9/AC14)
# ---------------------------------------------------------------------------


class TestSchemaCliPrecondition:
    """schema-cli + node reachability asserted as explicit harness precondition (F9/AC14).

    Skip semantics: if node or schema-cli.js is absent, skip with an informative message
    (NOT a silent pass on ENOENT — that would hide infrastructure gaps).
    """

    def test_node_is_available(self):
        """node executable must be on PATH for schema-cli.js delegation (F9)."""
        if not _NODE_AVAILABLE:
            pytest.skip(
                "node not found on PATH — install Node.js (https://nodejs.org/) "
                "to enable schema-validation delegation tests"
            )
        assert shutil.which("node") is not None

    def test_schema_cli_js_reachable(self):
        """DoE-HEAD bin/schema-cli.js must be present at the .doe-root location (F9/AC14)."""
        if not _SCHEMA_CLI_AVAILABLE:
            pytest.skip(
                f"schema-cli.js precondition not met: {_SCHEMA_CLI_SKIP}. "
                "Tests requiring schema validation will be skipped."
            )
        assert _DOE_SCHEMA_CLI is not None and _DOE_SCHEMA_CLI.is_file(), (
            f"schema-cli.js not found at {_DOE_SCHEMA_CLI}. "
            "Ensure the DoE coordinator clone exists at the path in ~/.claude/.doe-root."
        )

    @_requires_schema_cli
    def test_parseable_reject_invalid_enum_improvement_queue(self, monkeypatch):
        """schema-cli.js returns parseable {ok: false, errors: [...]} for invalid enum (AC14).

        Confirms the typed-error-mapping path: class (b) parseable rejection from
        _schema_cli_validate → returns (False, [error_list]) rather than raising RuntimeError.
        """
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))

        from coordinator_core.ops.queue_append import _schema_cli_validate

        # invalid change_kind for improvement-queue → parseable rejection
        fields = {
            "created": _TEST_DATE,
            "title": "Validation delegation test",
            "body": "Body text.",
            "status": "open",
            "surface": "some/path.py",
            "proposed_action": "Fix it.",
            "from_repo": _TEST_FROM_REPO,
            "change_kind": "not-a-real-enum-value",  # intentionally invalid
        }
        ok, errors = _schema_cli_validate("improvement-queue", fields)

        assert ok is False, (
            "expected schema validation to reject 'not-a-real-enum-value' for change_kind"
        )
        assert isinstance(errors, list) and len(errors) > 0, (
            f"expected non-empty errors list from parseable rejection, got: {errors!r}"
        )
        # Typed-error mapping: _schema_cli_validate returns schema_validate.validate()'s
        # structured {field, error, hint} dicts, NOT schema-cli.js's pre-flattened
        # "field: error" strings. That flattening is the CLI stdout layer's job
        # (schema-cli.js:220-224, mirrored in schema_cli.py) and deliberately does not
        # happen here, so structured callers aren't forced to re-parse a string they
        # were never natively given. Assert against the dict shape.
        assert all(isinstance(e, dict) for e in errors), (
            f"expected structured {{field, error, hint}} error dicts, got: {errors!r}"
        )
        any_relevant = any(
            e.get("field") == "change_kind"
            or "invalid" in str(e.get("error", "")).lower()
            or "enum" in str(e.get("error", "")).lower()
            for e in errors
        )
        assert any_relevant, (
            f"expected an error dict naming field 'change_kind' or whose error text "
            f"mentions 'invalid'/'enum', got: {errors}"
        )

    @_requires_schema_cli
    def test_validate_raises_value_error_on_invalid_enum_via_op(self, tmp_path, monkeypatch):
        """append_queue_entry raises ValueError with typed message for invalid enum (AC14).

        Exercises the _validate → _schema_cli_validate typed-error path end-to-end:
        parseable rejection from schema-cli.js → ValueError with 'invalid value for' message.
        """
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        with pytest.raises(ValueError, match="invalid value for"):
            append_queue_entry(
                schema="improvement-queue",
                title="Invalid enum test",
                body="Test body.",
                surface="some/path.py",
                proposed_action="Fix it.",
                from_repo=_TEST_FROM_REPO,
                change_kind="totally-not-valid",  # invalid enum → ValueError
                status="open",
                created=_TEST_DATE,
                session_id="",
            )


# ---------------------------------------------------------------------------
# Tests: byte-parity for queue.append across all five schemas (AC2 / F10)
# ---------------------------------------------------------------------------


@_requires_oracle_append
@pytest.mark.parametrize(
    "schema",
    ["debt-backlog", "bug-backlog", "improvement-queue", "lessons", "cross-repo-commitment"],
)
class TestQueueAppendByteParity:
    """Byte-identical YAML parity: native op == bash oracle, all five schemas (AC2 / F10).

    This is the strangler invariant: op output bytes must match the oracle byte-for-byte,
    including the system: block field order. A yaml.safe_dump() call in the op would
    normalize quoting, reorder keys, and fail this test — the byte diff would identify
    the divergence precisely.
    """

    def test_byte_parity_with_session(self, schema, tmp_path, monkeypatch):
        """Op output is byte-identical to oracle when CLAUDE_CODE_SESSION_ID is present.

        Both paths get identical CLAUDE_CODE_SESSION_ID → provenance_completeness: complete.
        F8: session env pinned to same value on both sides.
        """
        oracle_bytes = _run_oracle_append(
            schema, _SCHEMA_PARAMS[schema], tmp_path / "oracle", _TEST_SESSION
        )
        native_bytes = _run_native_append(
            schema, _SCHEMA_PARAMS[schema], tmp_path / "native", _TEST_SESSION, monkeypatch
        )

        if oracle_bytes != native_bytes:
            oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
            native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
            )
            pytest.fail(
                f"BYTE-PARITY FAIL for schema={schema!r} (session present).\n"
                f"Diff (oracle→native):\n{diff}"
            )

    def test_byte_parity_without_session(self, schema, tmp_path, monkeypatch):
        """Op output is byte-identical to oracle when CLAUDE_CODE_SESSION_ID is absent.

        Both paths get session_id='' → provenance_completeness: unknown, no created_by_session.
        F8: absence of session env pinned identically on both sides.

        oracle_cwd isolation: oracle runs from a non-git tmp dir so its _resolve_session_id()
        sentinel fallback (.git/coordinator-sessions/.current-session-id) cannot fire —
        ensuring the oracle and native op both resolve to "" cleanly.
        """
        # Create a non-git directory for the oracle to run from.
        oracle_cwd = tmp_path / "oracle_cwd"
        oracle_cwd.mkdir()

        oracle_bytes = _run_oracle_append(
            schema, _SCHEMA_PARAMS[schema], tmp_path / "oracle", "",
            oracle_cwd=oracle_cwd,
        )
        native_bytes = _run_native_append(
            schema, _SCHEMA_PARAMS[schema], tmp_path / "native", "", monkeypatch
        )

        if oracle_bytes != native_bytes:
            oracle_lines = oracle_bytes.decode(errors="replace").splitlines(keepends=True)
            native_lines = native_bytes.decode(errors="replace").splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(oracle_lines, native_lines, fromfile="oracle", tofile="native")
            )
            pytest.fail(
                f"BYTE-PARITY FAIL for schema={schema!r} (session absent).\n"
                f"Diff (oracle→native):\n{diff}"
            )


# ---------------------------------------------------------------------------
# Tests: F8 provenance_completeness branches (explicit branch coverage)
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestProvenanceBranches:
    """Both provenance_completeness branches exercised (F8 env-pin)."""

    def test_session_present_yields_complete(self, tmp_path, monkeypatch):
        """CLAUDE_CODE_SESSION_ID present → system block: provenance_completeness: complete."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        result = append_queue_entry(
            schema="debt-backlog",
            title="Provenance complete branch test",
            body="Test body for provenance-complete path.",
            source="daily-review/2026-01-15",
            risk="Low.",
            proposed_action="None required.",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id=_TEST_SESSION,
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert "provenance_completeness: complete" in content, (
            f"expected 'provenance_completeness: complete' in output, got:\n{content}"
        )
        assert f"created_by_session: {_TEST_SESSION}" in content, (
            f"expected 'created_by_session: {_TEST_SESSION}' in output, got:\n{content}"
        )
        # linked_sessions should contain the session ID
        assert f"- {_TEST_SESSION}" in content, (
            f"expected '- {_TEST_SESSION}' in linked_sessions, got:\n{content}"
        )

    def test_session_absent_yields_unknown(self, tmp_path, monkeypatch):
        """CLAUDE_CODE_SESSION_ID absent → system block: provenance_completeness: unknown."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

        result = append_queue_entry(
            schema="debt-backlog",
            title="Provenance unknown branch test",
            body="Test body for provenance-unknown path.",
            source="daily-review/2026-01-15",
            risk="Low.",
            proposed_action="None required.",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",  # explicitly absent
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        assert "provenance_completeness: unknown" in content, (
            f"expected 'provenance_completeness: unknown' in output, got:\n{content}"
        )
        assert "created_by_session" not in content, (
            f"'created_by_session' must not appear when session_id is absent, got:\n{content}"
        )
        # linked_sessions empty → emitted as [] not as a list item
        assert "linked_sessions: []" in content, (
            f"expected 'linked_sessions: []' when session absent, got:\n{content}"
        )


# ---------------------------------------------------------------------------
# Tests: AC12 unquoted created: date on the lessons path
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestUnquotedCreated:
    """created: date emitted unquoted (bare YYYY-MM-DD) on the lessons path (AC12).

    The lessons path writes an unquoted YAML date object (matching the legacy
    coordinator-lesson-add output). The byte-identical parity invariant requires
    this to be replicated exactly — quoting it would silently break parity.
    Spec backlink: pln-strang-08-queue-append-strangl-2a3499 § AC12
    """

    def test_lessons_created_is_unquoted_bare_date(self, tmp_path, monkeypatch):
        """created: is a bare date (no quotes) in lessons schema output."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        result = append_queue_entry(
            schema="lessons",
            title="Unquoted created date test",
            body="Lesson body for unquoted-created test.",
            scope="universal",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")

        # Positive assertion: bare unquoted date value.
        assert f"created: {_TEST_DATE}\n" in content, (
            f"expected 'created: {_TEST_DATE}' (unquoted), found:\n{content}"
        )
        # Negative assertion: must NOT be double-quoted.
        assert f'created: "{_TEST_DATE}"' not in content, (
            f"created: date must be unquoted — found quoted form in:\n{content}"
        )
        # Negative assertion: must NOT be single-quoted.
        assert f"created: '{_TEST_DATE}'" not in content, (
            f"created: date must be unquoted — found single-quoted form in:\n{content}"
        )


# ---------------------------------------------------------------------------
# Tests: AC5 queue.promote byte-parity
# ---------------------------------------------------------------------------


@_requires_oracle_promote
class TestQueuePromoteByteParity:
    """Byte-parity for queue.promote vs coordinator-lesson-promote oracle (AC5).

    Strategy: run the bash oracle first; extract the non-deterministic id and created
    fields from its output; run the native op with those same extracted values.
    Because the native op accepts entry_id= and created= as explicit parameters, the
    resulting file is byte-identical to the oracle output.

    Named normalization: id, created, and filename-ts are non-deterministic in the oracle
    (fresh uuid4 + utcnow()) — pinned to oracle-derived values on the native side.
    All other fields (from_repo, change_kind, target_wiki, title, body, fences) are
    deterministic and compared byte-for-byte.
    """

    def test_promote_byte_parity(self, tmp_path, monkeypatch):
        """Native promote_lesson output is byte-identical to oracle for pinned inputs."""
        oracle_outbox = tmp_path / "oracle_outbox"
        oracle_outbox.mkdir()

        env = os.environ.copy()
        env["LESSON_PROMOTE_OUTBOX_ROOT"] = str(oracle_outbox)
        env.pop("CLAUDE_KLABAUTER_ROOT", None)

        assert _DOE_LESSON_PROMOTE_CLI is not None
        oracle_args = [
            sys.executable,
            str(_DOE_LESSON_PROMOTE_CLI),
            "--title", "Promote parity test lesson entry",
            "--body", "This is the lesson body for promote parity testing.",
            "--change-kind", "doctrine-edit",
            "--target-wiki", "docs/wiki/test-parity-wiki.md",
        ]
        oracle_result = subprocess.run(
            oracle_args,
            env=env,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert oracle_result.returncode == 0, (
            f"oracle coordinator-lesson-promote failed (exit {oracle_result.returncode}):\n"
            f"stdout: {oracle_result.stdout}\nstderr: {oracle_result.stderr}"
        )

        oracle_files = list(oracle_outbox.glob("*.yaml"))
        assert len(oracle_files) == 1, (
            f"expected exactly 1 oracle file, got {oracle_files}"
        )
        oracle_text = oracle_files[0].read_text(encoding="utf-8")
        oracle_bytes = oracle_files[0].read_bytes()

        # Extract non-deterministic fields from oracle output.
        # _extract_yaml_scalar handles both bare and double-quoted YAML values —
        # created: "2026-07-05T12:22:32+00:00" (quoted because of colons) → bare value.
        oracle_id = _extract_yaml_scalar(oracle_text, "id")
        oracle_created = _extract_yaml_scalar(oracle_text, "created")
        oracle_from_repo = _extract_yaml_scalar(oracle_text, "from_repo") or "unknown-sender-em"
        assert oracle_id and oracle_created, (
            f"could not extract 'id' and 'created' from oracle output:\n{oracle_text}"
        )

        # Run native op with extracted deterministic values.
        native_outbox = tmp_path / "native_outbox"
        native_outbox.mkdir()
        monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(native_outbox))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        promote_lesson(
            title="Promote parity test lesson entry",
            body="This is the lesson body for promote parity testing.",
            change_kind="doctrine-edit",
            target_wiki="docs/wiki/test-parity-wiki.md",
            from_repo=oracle_from_repo,
            entry_id=oracle_id,
            created=oracle_created,
        )

        native_files = list(native_outbox.glob("*.yaml"))
        assert len(native_files) == 1, (
            f"expected exactly 1 native file, got {native_files}"
        )
        native_bytes = native_files[0].read_bytes()

        if oracle_bytes != native_bytes:
            native_text = native_files[0].read_text(encoding="utf-8")
            oracle_lines = oracle_text.splitlines(keepends=True)
            native_lines = native_text.splitlines(keepends=True)
            diff = "".join(
                difflib.unified_diff(
                    oracle_lines, native_lines, fromfile="oracle", tofile="native"
                )
            )
            pytest.fail(f"BYTE-PARITY FAIL for queue.promote:\n{diff}")

    def test_promote_filename_uses_ts_safe_slug(self, tmp_path, monkeypatch):
        """Output filename uses <ISO-ts-safe>-<slug>-<digest12>.yaml format (colons/plus→hyphens).

        DR-213 D2(i) amendment (2026-07-08-concurrency-safe-strangled-op-writes § C1/C2):
        the trailing ``-<digest12>`` content-digest component is appended to disambiguate
        distinct same-timestamp+slug entries; this test asserts the prefix shape and the
        digest suffix pattern rather than the pre-amendment undigested filename.
        """
        monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        fixed_ts = "2026-01-15T10:30:00+00:00"
        result = promote_lesson(
            title="Filename format test lesson",
            body="Body for filename test.",
            change_kind="wiki-new",
            target_wiki="docs/wiki/some-wiki.md",
            from_repo=_TEST_FROM_REPO,
            entry_id="00000000-0000-0000-0000-000000000001",
            created=fixed_ts,
        )

        out_path = Path(result["out_path"])
        expected_ts_safe = "2026-01-15T10-30-00-00-00"
        expected_slug = "filename-format-test-lesson"
        expected_prefix = f"{expected_ts_safe}-{expected_slug}-"

        assert out_path.name.startswith(expected_prefix), (
            f"expected filename to start with {expected_prefix!r}, got {out_path.name!r}"
        )
        assert re.fullmatch(
            re.escape(expected_prefix) + r"[0-9a-f]{12}\.yaml", out_path.name
        ), (
            f"expected filename {expected_prefix}<digest12>.yaml, got {out_path.name!r}"
        )

    def test_promote_slug_truncation_matches_oracle_when_cut_hits_dash(
        self, tmp_path, monkeypatch
    ):
        """_slug_from_title parity when 40-char truncation lands exactly on a '-'.

        coordinator-lesson-promote._slug_from_title rstrips '-' left dangling by the
        40-char truncation; the native op must do the same or filenames diverge
        (op emitted a trailing '-' the oracle never would). Title chosen so
        slug[:40] == "...at-a-" (dash at the cut point).
        """
        title = "A Title Whose Slug Truncates Right At A Dash Mark"

        env = os.environ.copy()
        env["LESSON_PROMOTE_OUTBOX_ROOT"] = str(tmp_path / "oracle_outbox")
        (tmp_path / "oracle_outbox").mkdir()
        env.pop("CLAUDE_KLABAUTER_ROOT", None)

        assert _DOE_LESSON_PROMOTE_CLI is not None
        oracle_result = subprocess.run(
            [
                sys.executable,
                str(_DOE_LESSON_PROMOTE_CLI),
                "--title", title,
                "--body", "Body for slug-truncation parity test.",
                "--change-kind", "doctrine-edit",
                "--target-wiki", "docs/wiki/test-parity-wiki.md",
            ],
            env=env,
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert oracle_result.returncode == 0, (
            f"oracle coordinator-lesson-promote failed (exit {oracle_result.returncode}):\n"
            f"stdout: {oracle_result.stdout}\nstderr: {oracle_result.stderr}"
        )
        oracle_files = list((tmp_path / "oracle_outbox").glob("*.yaml"))
        assert len(oracle_files) == 1, f"expected exactly 1 oracle file, got {oracle_files}"
        # filename: <ts_safe>-<slug>-<digest12>.yaml. The oracle's own timestamp
        # is non-deterministic, so recover the slug by stripping the trailing
        # -<digest12>.yaml and the leading ISO-ts-safe prefix (read back from
        # the file's own `created:` field) rather than assuming a fixed ts.
        oracle_created = _extract_yaml_scalar(oracle_files[0].read_text(encoding="utf-8"), "created")
        assert oracle_created
        oracle_ts_safe = oracle_created.replace(":", "-").replace("+", "-")
        stripped = re.sub(r"-[0-9a-f]{12}\.yaml$", "", oracle_files[0].name)
        assert stripped.startswith(oracle_ts_safe + "-")
        oracle_slug = stripped[len(oracle_ts_safe) + 1 :]

        native_outbox = tmp_path / "native_outbox"
        native_outbox.mkdir()
        monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(native_outbox))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        fixed_ts = "2026-01-15T10:30:00+00:00"
        result = promote_lesson(
            title=title,
            body="Body for slug-truncation parity test.",
            change_kind="doctrine-edit",
            target_wiki="docs/wiki/test-parity-wiki.md",
            from_repo=_TEST_FROM_REPO,
            entry_id="00000000-0000-0000-0000-000000000002",
            created=fixed_ts,
        )
        out_path = Path(result["out_path"])
        native_ts_safe = "2026-01-15T10-30-00-00-00"
        native_stripped = re.sub(r"-[0-9a-f]{12}\.yaml$", "", out_path.name)
        assert native_stripped.startswith(native_ts_safe + "-")
        native_slug = native_stripped[len(native_ts_safe) + 1 :]

        assert native_slug == oracle_slug, (
            f"native slug {native_slug!r} != oracle slug {oracle_slug!r} "
            f"(truncation-hits-dash divergence)"
        )
        assert not native_slug.endswith("-"), (
            f"native slug {native_slug!r} retains a trailing '-' from truncation"
        )


# ---------------------------------------------------------------------------
# Tests: AC4 write-always (idempotent-by-filename-overwrite)
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestWriteAlways:
    """queue.append idempotency is content-keyed, not date+slug-keyed (AC1/AC3/AC4).

    DR-213 D2(i) amendment (2026-07-08-concurrency-safe-strangled-op-writes): the filename
    now carries a content digest (``<date>-<slug>-<digest12>.yaml``). An IDENTICAL re-run
    (same semantic payload) still dedups to one file via ``os.replace`` (idempotency
    preserved). Two DISTINCT entries sharing date+slug now produce distinct filenames and
    BOTH survive — this inverts the prior last-write-wins assertion, which assumed a fixed
    date+slug filename with no content-keying. No dedup pre-check in the op — dedup lives
    in the coordinator-lesson-add wrapper (DoE-side).

    Spec backlink: pln-concurrency-safe-writes-for-th-c7ca9f § C1
    (existing-test inversion, not a fixture refresh — see plan's the Staff Engineer F1 review note).
    """

    def test_identical_same_title_same_day_yields_exactly_one_file(self, tmp_path, monkeypatch):
        """Two append calls with IDENTICAL body, same title+date → exactly 1 file (idempotent)."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        base_kwargs: dict = dict(
            schema="debt-backlog",
            title="Write always overwrite test entry",
            source="daily-review/2026-01-15",
            risk="Low.",
            proposed_action="None required.",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",
        )

        result1 = append_queue_entry(body="Identical write body.", **base_kwargs)
        result2 = append_queue_entry(body="Identical write body.", **base_kwargs)

        assert result1["out_path"] == result2["out_path"], (
            "two calls with IDENTICAL semantic content (same title+date+body) must "
            "produce identical output paths (same content digest)"
        )
        assert result1["slug"] == result2["slug"], (
            "slug must be deterministic from title"
        )

        output_dir = tmp_path / _SCHEMA_OUTPUT_DIRS["debt-backlog"]
        yaml_files = list(output_dir.glob("*.yaml"))
        assert len(yaml_files) == 1, (
            f"expected exactly 1 file after 2 identical-content writes (content-keyed "
            f"idempotency), got {len(yaml_files)}: {yaml_files}"
        )

        content = yaml_files[0].read_text(encoding="utf-8")
        assert "Identical write body." in content, (
            f"expected write body in the deduped file, got:\n{content}"
        )

    def test_distinct_body_same_title_same_day_yields_two_files(self, tmp_path, monkeypatch):
        """Two append calls with DISTINCT bodies, same title+date → 2 files (both survive).

        Under fixed-slug filenames (pre-amendment), the second write would silently
        overwrite the first — a D2(iii) git-reversibility violation for uncommitted
        entries. Content-keying makes distinct entries additive-create instead.
        """
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        base_kwargs: dict = dict(
            schema="debt-backlog",
            title="Write always distinct-body test entry",
            source="daily-review/2026-01-15",
            risk="Low.",
            proposed_action="None required.",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",
        )

        result1 = append_queue_entry(body="First write body.", **base_kwargs)
        result2 = append_queue_entry(body="Second write body (distinct content).", **base_kwargs)

        assert result1["out_path"] != result2["out_path"], (
            "two calls with DISTINCT bodies (same title+date) must produce DIFFERENT "
            "output paths (different content digests) — no silent overwrite"
        )
        assert result1["slug"] == result2["slug"], (
            "slug must still be deterministic from title even though filenames diverge"
        )

        output_dir = tmp_path / _SCHEMA_OUTPUT_DIRS["debt-backlog"]
        yaml_files = list(output_dir.glob("*.yaml"))
        assert len(yaml_files) == 2, (
            f"expected exactly 2 files after 2 distinct-content writes (both survive), "
            f"got {len(yaml_files)}: {yaml_files}"
        )

        all_content = "".join(f.read_text(encoding="utf-8") for f in yaml_files)
        assert "First write body." in all_content, (
            f"first write's body must survive in one of the two files:\n{all_content}"
        )
        assert "Second write body (distinct content)." in all_content, (
            f"second write's body must survive in one of the two files:\n{all_content}"
        )


# ---------------------------------------------------------------------------
# Tests: AC13 caller-context (per-repo path routing)
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestCallerContext:
    """Caller repo_root=X writes X/state/<queue>/, NOT claude-klabauter's own tree (AC13).

    The daemon serves many repos; cwd-based resolution would silently target claude-klabauter's
    own tree. queue.append must resolve against main_worktree_root(caller_repo_root).
    """

    def test_caller_worktree_governs_output_path(self, tmp_path, monkeypatch):
        """append_queue_entry(caller_worktree=X) writes X/state/debt-backlog/, not elsewhere."""
        caller_repo = tmp_path / "caller_repo"
        caller_repo.mkdir()

        # No QUEUE_APPEND_OUTPUT_ROOT — exercise the real path resolution logic.
        monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        # Set CLAUDE_HOME to a dummy so meta-repo check (_same_path) does not match caller_repo.
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dummy_claude_home"))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))

        result = append_queue_entry(
            schema="debt-backlog",
            title="Caller context routing test entry",
            body="Body for caller context AC13 test.",
            source="daily-review/2026-01-15",
            risk="Low.",
            proposed_action="None required.",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",
            caller_worktree=caller_repo,
        )

        out_path = Path(result["out_path"])

        # Output must be rooted under caller_repo.
        assert out_path.is_relative_to(caller_repo), (
            f"output path {out_path} must be under caller_repo {caller_repo}, "
            "not under claude-klabauter's own tree"
        )
        # Must contain the schema's state subdirectory. `rel` is a
        # locally-computed Path.relative_to() result, not a product wire
        # value — compare via .as_posix() rather than str() so this
        # assertion isn't platform-separator-dependent (str() on a
        # WindowsPath renders backslashes; the product itself never
        # promises a forward-slash form for this local filesystem path).
        rel = out_path.relative_to(caller_repo)
        assert rel.as_posix().startswith("state/debt-backlog/"), (
            f"expected path starting with 'state/debt-backlog/', got {rel}"
        )
        assert out_path.exists(), f"output file not created at {out_path}"

    def test_caller_worktree_governs_output_path_multiple_schemas(self, tmp_path, monkeypatch):
        """Per-repo routing works for bug-backlog and improvement-queue schemas too."""
        caller_repo = tmp_path / "another_caller"
        caller_repo.mkdir()

        monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "dummy_claude_home"))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))

        result = append_queue_entry(
            schema="bug-backlog",
            title="Caller context bug routing test",
            body="Bug body for caller context test.",
            surface="some/module.py",
            severity="P2",
            status="open",
            from_repo=_TEST_FROM_REPO,
            created=_TEST_DATE,
            session_id="",
            caller_worktree=caller_repo,
        )

        out_path = Path(result["out_path"])
        assert out_path.is_relative_to(caller_repo), (
            f"bug-backlog output should be under caller_repo, got {out_path}"
        )
        # See the debt-backlog variant of this test above for why
        # .as_posix() (not str()) is the right comparison here.
        rel = out_path.relative_to(caller_repo)
        assert rel.as_posix().startswith("state/bug-backlog/"), (
            f"expected 'state/bug-backlog/', got {rel}"
        )


# ---------------------------------------------------------------------------
# Tests: AC6 _ClaudeKlabauterUnresolvable graceful-degrade
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestClaudeKlabauterUnresolvable:
    """Unresolvable-root → WARN+skip, exit 0, no cwd fallback (AC6).

    queue.append (queue_scope='central') triggers this path when CLAUDE_KLABAUTER_ROOT cannot
    be resolved via env or machine-local registry. queue.promote triggers the DoE-side
    counterpart when the DoE-claude root cannot be resolved via
    ``coordinator_doe_root()`` (REPO_DOE_CLAUDE env / machine-local / pointer-file
    rungs) — the outbox is DoE-rooted, not claude-klabauter-rooted (see queue_promote's
    ``_outbox_root`` docstring).
    """

    def test_append_central_scope_raises_claude_klabauter_unresolvable(self, tmp_path, monkeypatch):
        """queue_scope='central' + unresolvable CLAUDE_KLABAUTER_ROOT → _ClaudeKlabauterUnresolvable raised."""
        monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        # Stub MACHINE_LOCAL_IMPL to a non-existent script so machine-local lookup fails cleanly.
        monkeypatch.setenv(
            "MACHINE_LOCAL_IMPL", str(tmp_path / "nonexistent_machine_local.py")
        )

        with pytest.raises(_AppendUnresolvable):
            append_queue_entry(
                schema="improvement-queue",
                title="Unresolvable claude-klabauter central test",
                body="Test body.",
                surface="some/path.py",
                proposed_action="Fix it.",
                from_repo=_TEST_FROM_REPO,
                change_kind="script-edit",
                status="open",
                created=_TEST_DATE,
                session_id="",
                queue_scope="central",
            )

    def test_append_handler_returns_skipped_on_unresolvable(self, tmp_path, monkeypatch):
        """Handler wraps _ClaudeKlabauterUnresolvable → returns {skipped: True, reason: str} (AC6)."""
        monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.setenv(
            "MACHINE_LOCAL_IMPL", str(tmp_path / "nonexistent_machine_local.py")
        )

        result = _queue_append_handler(
            {
                "schema": "improvement-queue",
                "title": "Unresolvable handler graceful-degrade test",
                "body": "Test body.",
                "surface": "some/path.py",
                "proposed_action": "Fix it.",
                "from_repo": _TEST_FROM_REPO,
                "change_kind": "script-edit",
                "status": "open",
                "queue_scope": "central",
            },
            repo_root=None,
        )
        assert result.get("skipped") is True, (
            f"expected result['skipped'] = True on unresolvable CLAUDE_KLABAUTER_ROOT, got: {result}"
        )
        assert "reason" in result and result["reason"], (
            f"expected non-empty 'reason' in result, got: {result}"
        )

    def _make_doe_root_unresolvable(self, tmp_path, monkeypatch):
        """Force coordinator_doe_root() to fail loud (None): empty PATH (no
        `machine-local` binary), no REPO_DOE_CLAUDE override, no pointer-file /
        flat-layout rung 3 fallback (fake, empty CLAUDE_HOME), no passthrough env.
        Mirrors coordinator_core/ops/test_coordinator_doe_root.py's
        test_negative_no_machine_local_no_pointer_file recipe.
        """
        _doe_root_mod._reset_doe_root_cache()
        empty_bin = tmp_path / "empty-bin"
        empty_bin.mkdir(exist_ok=True)
        fake_home = tmp_path / "neg-empty-home"
        fake_home.mkdir(exist_ok=True)
        monkeypatch.delenv("REPO_DOE_CLAUDE", raising=False)
        monkeypatch.delenv("COORDINATOR_CLONE", raising=False)
        monkeypatch.delenv("COORDINATOR_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.delenv("COORDINATOR_SOURCE_MODE", raising=False)
        monkeypatch.setenv("CLAUDE_HOME", str(fake_home))
        monkeypatch.setenv("PATH", str(empty_bin))

    def test_promote_raises_claude_klabauter_unresolvable_no_cwd_fallback(self, tmp_path, monkeypatch):
        """promote_lesson with unresolvable DoE-claude root → _DoeUnresolvable, no cwd fallback.

        Negative-spec: stop-the-rot C12 closes the cwd-fallback landmine.
        coordinator-lesson-promote's legacy cwd fallback is NOT replicated in the op.
        """
        monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        self._make_doe_root_unresolvable(tmp_path, monkeypatch)

        with pytest.raises(_PromoteUnresolvable):
            promote_lesson(
                title="Promote unresolvable no-cwd-fallback test",
                body="Lesson body for promote unresolvable test.",
                change_kind="doctrine-edit",
                target_wiki="docs/wiki/test.md",
                from_repo=_TEST_FROM_REPO,
                entry_id="00000000-0000-0000-0000-000000000001",
                created="2026-01-15T10:00:00+00:00",
            )

    def test_promote_handler_returns_skipped_on_unresolvable(self, tmp_path, monkeypatch):
        """queue.promote handler wraps _DoeUnresolvable → returns {skipped: True} (AC6)."""
        monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        self._make_doe_root_unresolvable(tmp_path, monkeypatch)

        result = asyncio.run(
            _queue_promote_handler(
                {
                    "title": "Promote handler unresolvable graceful-degrade test",
                    "body": "Lesson body.",
                    "change_kind": "doctrine-edit",
                    "target_wiki": "docs/wiki/test.md",
                },
                repo_root=None,
            )
        )
        assert result.get("skipped") is True, (
            f"expected result['skipped'] = True on unresolvable DoE-claude root, got: {result}"
        )
        assert "reason" in result and result["reason"], (
            f"expected non-empty 'reason' in result, got: {result}"
        )


# ---------------------------------------------------------------------------
# Tests: handler-level session_id param takes precedence over env (hardening AC)
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestHandlerSessionIdParam:
    """Handler param session_id takes precedence over CLAUDE_CODE_SESSION_ID env (hardening AC).

    Regression guard: _queue_append_handler must resolve session_id from params first;
    env is the fallback for absent-param callers only. Exercises the handler path
    distinct from the builder-level tests in TestProvenanceBranches.

    Spec backlink: hardening finding — queue.append session_id caller-authoritative param.
    """

    def test_param_session_id_wins_over_env(self, tmp_path, monkeypatch):
        """Param session_id used verbatim AND takes precedence over CLAUDE_CODE_SESSION_ID env.

        AC-1: params[session_id] → emitted system: block contains created_by_session: <param-value>.
        AC-2: env is set to a DIFFERENT value; param wins — env value must NOT appear.
        """
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        # AC-2: deliberately set env to a different value — the param must win.
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-session-should-not-appear")

        result = _queue_append_handler(
            {
                "schema": "improvement-queue",
                "title": "Handler session_id param precedence test",
                "body": "Body for handler session_id param precedence test.",
                "surface": "coordinator_core/ops/queue_append.py",
                "proposed_action": "No action required.",
                "from_repo": _TEST_FROM_REPO,
                "status": "open",
                "change_kind": "script-edit",
                "session_id": "param-session-wins-xyz456",
            },
            repo_root=None,
        )

        assert not result.get("skipped"), (
            f"handler must not skip on resolvable params; got: {result}"
        )
        content = Path(result["out_path"]).read_text(encoding="utf-8")
        # AC-1: param value is used verbatim in the system: block.
        assert "created_by_session: param-session-wins-xyz456" in content, (
            f"expected param session_id in system: block, got:\n{content}"
        )
        # AC-2: env value must NOT appear (param takes precedence).
        assert "env-session-should-not-appear" not in content, (
            f"env session_id must NOT appear when param session_id is provided, got:\n{content}"
        )


# ---------------------------------------------------------------------------
# Tests: schema_fields unrecognized-key warning (review-integrator Finding 4)
# ---------------------------------------------------------------------------


@_requires_schema_cli
class TestSchemaFieldsUnrecognizedKeyWarning:
    """The **schema_fields contract-derived-plumbing loop warns (does not raise) on a
    caller-supplied key the schema contract doesn't declare — was previously untested.

    Review: review-integrator (Finding 4) — the warn-don't-raise fix (motivated by "a
    typo like 'supercedes' vanished with no signal") shipped with zero coverage.
    """

    def test_warns_on_typo_schema_fields_key(self, tmp_path, monkeypatch, caplog):
        """A schema_fields key the contract does not declare logs a warning naming it."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        with caplog.at_level("WARNING", logger="coordinator_core.ops.queue_append"):
            append_queue_entry(
                schema="improvement-queue",
                title="Typo schema_fields key test",
                body="Body text.",
                status="open",
                surface="some/path.py",
                proposed_action="Fix it.",
                from_repo=_TEST_FROM_REPO,
                created=_TEST_DATE,
                session_id="",
                change_kind="script-edit",
                supercedes="typo-for-supersedes",  # not declared by any schema
            )

        assert any(
            "supercedes" in record.getMessage() for record in caplog.records
        ), (
            f"expected a warning naming the undeclared key 'supercedes', got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )

    def test_no_warning_for_legitimate_contract_field(self, tmp_path, monkeypatch, caplog):
        """A schema_fields key the contract DOES declare (optional) logs no warning."""
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        with caplog.at_level("WARNING", logger="coordinator_core.ops.queue_append"):
            append_queue_entry(
                schema="improvement-queue",
                title="Legitimate optional field test",
                body="Body text.",
                status="open",
                surface="some/path.py",
                proposed_action="Fix it.",
                from_repo=_TEST_FROM_REPO,
                created=_TEST_DATE,
                session_id="",
                change_kind="script-edit",
                initiative="genuine-optional-contract-field",  # declared, optional
            )

        assert not any(
            "is not declared by schema" in record.getMessage()
            for record in caplog.records
        ), (
            f"expected no 'not declared by schema' warning for a legitimate optional "
            f"field, got: {[r.getMessage() for r in caplog.records]}"
        )


@_requires_schema_cli
class TestUnknownSchema:
    """Unknown schema still fails loud after _SCHEMA_OUTPUT_DIRS removal.

    Regression guard: output-dir resolution moved from a hardcoded table lookup to
    schema-cli --describe contract derivation (_output_dir_for_schema). An unknown
    schema name must still surface as ValueError — schema-cli exits non-zero for an
    unrecognized schema, which _output_dir_for_schema maps to ValueError.
    """

    def test_append_queue_entry_unknown_schema_raises_value_error(self, tmp_path, monkeypatch):
        """append_queue_entry(schema='totally-not-a-real-schema', ...) raises ValueError.

        Review: code-reviewer — the ValueError message no longer asserts "unknown schema"
        text (that framing mislabeled infra failures like node-missing/timeout/non-JSON
        as "unknown schema" — Finding 5); it now surfaces the underlying describe-failure
        cause plus the schema name via "could not resolve output dir for schema ...".
        """
        monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
        monkeypatch.setenv("COORDINATOR_SCHEMA_CLI", str(_DOE_SCHEMA_CLI))
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)

        with pytest.raises(
            ValueError,
            match=r"could not resolve output dir for schema 'totally-not-a-real-schema'",
        ):
            append_queue_entry(
                schema="totally-not-a-real-schema",
                title="Unknown schema test",
                body="Body for unknown schema test.",
                status="open",
            )


# ---------------------------------------------------------------------------
# _machine_local_impl — settings-home repoint (AC3, C3)
#
# Spec backlink: pln-repoint-coordinator-core-claud-56d805 § C3
# ---------------------------------------------------------------------------


class TestQueueAppendMachineLocalImplSettingsHomeRepoint:
    """queue_append._machine_local_impl() prefers <settings-home>/bin/_machine_local.py,
    falling back to the legacy ~/.claude/bin path only when the settings-home impl is absent.
    """

    def test_prefers_settings_home_impl_when_present(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
        settings_home_root = tmp_path / "settings_home"
        (settings_home_root / "bin").mkdir(parents=True)
        expected_impl = settings_home_root / "bin" / "_machine_local.py"
        expected_impl.write_text("# stub\n")
        monkeypatch.setattr(_qa_mod, "settings_home", lambda: settings_home_root)

        result = _qa_mod._machine_local_impl()

        assert result == str(expected_impl)

    def test_falls_back_to_claude_home_when_settings_home_impl_absent(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("MACHINE_LOCAL_IMPL", raising=False)
        settings_home_root = tmp_path / "settings_home_missing"
        monkeypatch.setattr(_qa_mod, "settings_home", lambda: settings_home_root)
        claude_home_root = tmp_path / "dummy_claude_home"
        monkeypatch.setenv("CLAUDE_HOME", str(claude_home_root))

        result = _qa_mod._machine_local_impl()

        assert result == str(claude_home_root / "bin" / "_machine_local.py")


class TestQueuePromoteRoutesViaDoeResolver:
    """queue_promote no longer owns a private machine-local/settings-home resolution

    ladder — it delegates entirely to ``coordinator_core.ops.coordinator_doe_root``
    (shared DoE-claude resolver seam), which owns its own settings-home/PATH
    resolution and is covered by ``coordinator_core/ops/test_coordinator_doe_root.py``.
    This is a thin regression guard that the delegation itself is wired, not a
    re-test of the resolver's internals.
    """

    def test_outbox_root_delegates_to_coordinator_doe_root(self, tmp_path, monkeypatch):
        import coordinator_core.ops.queue_promote as _qp_mod

        monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
        monkeypatch.setattr(
            _qp_mod, "coordinator_doe_root", lambda: str(tmp_path / "doe-claude")
        )

        result = _qp_mod._outbox_root()

        assert result == str(tmp_path / "doe-claude" / "state" / "lessons-outbox")


# ---------------------------------------------------------------------------
# Regression guard: _outbox_root() DOES NOT fall back to claude-klabauter / cwd-relative
# state/ when the DoE root is unresolvable (C12 negative-spec).
#
# Background: for weeks _outbox_root() was claude-klabauter-rooted (resolved via
# CLAUDE_KLABAUTER_ROOT env / machine-local `repos.claude_klabauter`, mirroring
# queue_append's central-scope resolver) instead of DoE-rooted, causing ~103
# lessons-outbox entries from OTHER repos' sessions to land in claude-klabauter's own
# state/lessons-outbox/ (see queue_promote.py module docstring, corrected
# 2026-07-22/23). The prose negative-spec at queue_promote.py:117-122 was
# unguarded by any test — this class is the regression guard that would have
# caught the claude-klabauter-rooted bug before it shipped.
#
# Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § C12 / AC13
# ---------------------------------------------------------------------------


class TestOutboxRootDoeRootedNotClaudeKlabauterRooted:
    """``_outbox_root()`` resolves under ``coordinator_doe_root()`` — never under
    claude-klabauter's own root (CLAUDE_KLABAUTER_ROOT / ``main_worktree_root()``) and never with a
    silent cwd-relative fallback on an unresolvable DoE root.
    """

    def test_outbox_root_is_doe_rooted_not_claude_klabauter_rooted(self, tmp_path, monkeypatch):
        """_outbox_root() with no env override resolves under coordinator_doe_root(),
        and specifically NOT under claude-klabauter's root / CLAUDE_KLABAUTER_ROOT / main_worktree_root().

        Fixture uses DISTINCT DoE and claude-klabauter roots so the negative assertion has
        teeth — a fixture where both happen to resolve to the same directory would
        pass even if the claude-klabauter-rooted bug reappeared.

        This recreates the historical bug's exact inputs: the pre-fix _outbox_root()
        resolved via CLAUDE_KLABAUTER_ROOT env (mirroring queue_append's central-scope
        resolver) rather than coordinator_doe_root() — setting both env vars to
        distinct values here means this test would have failed under that code.
        """
        import coordinator_core.ops.queue_promote as _qp_mod

        doe_root = tmp_path / "doe-claude-root"
        claude_klabauter_root = tmp_path / "claude-klabauter-live-root"
        doe_root.mkdir()
        claude_klabauter_root.mkdir()
        assert doe_root != claude_klabauter_root

        monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
        monkeypatch.setenv("COORDINATOR_ENGINE_ROOT", str(claude_klabauter_root))
        monkeypatch.setattr(_qp_mod, "coordinator_doe_root", lambda: str(doe_root))
        monkeypatch.setattr(
            _qp_mod,
            "main_worktree_root",
            lambda common_dir: (_ for _ in ()).throw(
                AssertionError("_outbox_root() must not call main_worktree_root()")
            ),
        )

        result = _qp_mod._outbox_root()

        expected = str(doe_root / "state" / "lessons-outbox")
        assert result == expected, f"expected DoE-rooted path, got: {result}"
        assert not result.startswith(str(claude_klabauter_root)), (
            f"_outbox_root() must NOT resolve under claude-klabauter's root; got: {result}"
        )
        assert str(claude_klabauter_root) not in result, (
            f"_outbox_root() must NOT reference CLAUDE_KLABAUTER_ROOT at all; got: {result}"
        )

    def test_outbox_root_raises_when_doe_root_unresolvable_no_fallback(
        self, tmp_path, monkeypatch
    ):
        """_outbox_root() raises _DoeUnresolvable — no silent cwd/claude-klabauter fallback —
        when coordinator_doe_root() returns None and no env override is set.
        """
        import coordinator_core.ops.queue_promote as _qp_mod

        monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_KLABAUTER_ROOT", raising=False)
        monkeypatch.setattr(_qp_mod, "coordinator_doe_root", lambda: None)

        with pytest.raises(_qp_mod._DoeUnresolvable):
            _qp_mod._outbox_root()

    def test_outbox_root_env_override_takes_precedence(self, tmp_path, monkeypatch):
        """LESSON_PROMOTE_OUTBOX_ROOT env override wins over coordinator_doe_root()
        entirely (test-isolation contract, C-c) — the resolver is never even called.
        """
        import coordinator_core.ops.queue_promote as _qp_mod

        override_root = tmp_path / "explicit-test-override"
        monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(override_root))
        monkeypatch.setattr(
            _qp_mod,
            "coordinator_doe_root",
            lambda: (_ for _ in ()).throw(
                AssertionError(
                    "coordinator_doe_root() must not be called when "
                    "LESSON_PROMOTE_OUTBOX_ROOT is set"
                )
            ),
        )

        result = _qp_mod._outbox_root()

        assert result == str(override_root)
