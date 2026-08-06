"""test_lesson_promote.py — routing-branch, grep-gate, and AC12 tests for coordinator-lesson-promote.

Tests (AC4, AC6, AC12 from strang-08 plan):
  AC4  Routing to queue.promote when seam present; distinct outbox schema preserved.
  AC6  Per-verb routing-branch test (seam-present→native, seam-absent→legacy) and
       grep-gate asserting legacy body present as legacy_fn in the CLI source.
  AC12 Skipped-envelope mapping: native op returns {skipped: True, reason} →
       wrapper emits legacy WARN to stderr and exits 0, no path printed.

Converted from a hand-rolled unittest runner to collectable pytest functions.

Run: python3 -m pytest coordinator/bin/tests/test_lesson_promote.py

Spec backlink: docs/plans/2026-07-06-strang-08-arm-queue-facade-invoke-retarget.md § C4
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest.mock
import uuid as _uuid_mod
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — locate CLI relative to this test file
# test file: coordinator/bin/tests/test_lesson_promote.py
# CLI:       coordinator/bin/coordinator-lesson-promote
# ---------------------------------------------------------------------------
_TESTS_DIR = Path(__file__).resolve().parent
_BIN_DIR = _TESTS_DIR.parent
_CLI_PATH = _BIN_DIR / "coordinator-lesson-promote"

# Ensure lib is on sys.path so cc_invoke imports inside the CLI work.
_LIB_DIR = _BIN_DIR / "lib"
if str(_LIB_DIR) not in sys.path:
    sys.path.insert(0, str(_LIB_DIR))

# Load the CLI as a Python module for unit testing.
# The trampoline header ('...exec...) is inert when imported as Python.
# spec_from_file_location needs an explicit SourceFileLoader for files without .py extension.
_loader = importlib.machinery.SourceFileLoader("coordinator_lesson_promote", str(_CLI_PATH))
_spec = importlib.util.spec_from_loader("coordinator_lesson_promote", _loader)
_cli_mod = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
_loader.exec_module(_cli_mod)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_ARGV = [
    "--title", "Test lesson title",
    "--body", "Test lesson body prose",
    "--change-kind", "doctrine-edit",
    "--target-wiki", "docs/wiki/test-wiki.md",
]

_FAKE_SCHEMA_OUTPUT = {"enums": {"change_kind": ["doctrine-edit", "wiki-append", "skill-edit"]}}


# ---------------------------------------------------------------------------
# AC6 — routing-branch: seam-present → native
#
# AC4/AC6: seam-present → _cc_route returns dict with out_path → printed, exit 0.
# ---------------------------------------------------------------------------


def test_native_out_path_printed():
    """AC4: native path returns out_path → printed to stdout, exit 0."""
    fake_result = {"out_path": "/fake/claude-klabauter/state/lessons-outbox/2026-07-06T00-00-00Z-test.yaml"}

    captured_out = io.StringIO()
    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=fake_result) as mock_route,
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stdout", captured_out),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == 0, "native path must exit 0"
    assert (
        "/fake/claude-klabauter/state/lessons-outbox/2026-07-06T00-00-00Z-test.yaml"
        in captured_out.getvalue()
    ), "out_path must be printed on native success"
    mock_route.assert_called_once()


def test_native_route_op_is_queue_promote():
    """AC4: _cc_route is called with op='queue.promote'."""
    fake_result = {"out_path": "/fake/path.yaml"}

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=fake_result) as mock_route,
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stdout", io.StringIO()),
    ):
        _cli_mod.main(_MINIMAL_ARGV)

    call_op = mock_route.call_args[0][0]
    assert call_op == "queue.promote", "op must be queue.promote"


def test_native_from_repo_explicit_in_params():
    """AC11: from_repo is passed explicitly in params (provenance parity)."""
    fake_result = {"out_path": "/fake/path.yaml"}

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=fake_result) as mock_route,
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="test-repo-name"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stdout", io.StringIO()),
    ):
        _cli_mod.main(_MINIMAL_ARGV)

    params = mock_route.call_args[0][1]
    assert params.get("from_repo") == "test-repo-name", (
        "from_repo must be passed explicitly in params — not left for op to default"
    )


def test_native_params_contain_required_fields():
    """AC4: queue.promote params contain all required lesson fields.

    target_wiki must be a REAL entry in the central wiki inventory: change_kind
    "wiki-append" is one of the WIKI_TARGETING_CHANGE_KINDS (2026-07-23 A7/A9 scope
    fix), so main() runs `_validate_target_wiki` against the actual resolved
    example-doctrine-repo wiki inventory (unmocked here) before ever reaching `_cc_route` —
    a fabricated path like the prior "docs/wiki/target.md" is rejected with exit 2
    (argparse SystemExit), never reaching the params assertions below. computed-
    skills.md is the same stable, long-lived doc the sibling test
    coordinator/tests/test_lesson_promote_node_enum.py uses for this exact
    wiki-append scope-fix case.
    """
    fake_result = {"out_path": "/fake/path.yaml"}
    argv = [
        "--title", "My lesson",
        "--body", "Lesson body text",
        "--change-kind", "wiki-append",
        "--target-wiki", "docs/wiki/computed-skills.md",
        "--scope-tags", "drain,cross-repo",
        "--evidence", "abc1234",
    ]

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=fake_result) as mock_route,
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stdout", io.StringIO()),
    ):
        _cli_mod.main(argv)

    params = mock_route.call_args[0][1]
    assert params["title"] == "My lesson"
    assert params["body"] == "Lesson body text"
    assert params["change_kind"] == "wiki-append"
    assert params["target_wiki"] == "docs/wiki/computed-skills.md"
    assert params["scope_tags"] == ["drain", "cross-repo"]
    assert params["evidence"] == "abc1234"


# ---------------------------------------------------------------------------
# AC6 — routing-branch: seam-absent → legacy
#
# seam-absent → _cc_route calls legacy_fn → legacy write executes.
# ---------------------------------------------------------------------------


def test_legacy_fn_called_when_seam_absent(tmp_path):
    """AC6: seam-absent path (route calls legacy_fn) → legacy write lands on disk."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    def _fake_route(op: str, params: dict, repo_root: str, legacy_fn) -> int:
        # Simulate seam-absent: call the legacy_fn closure and return its result.
        return legacy_fn()

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", side_effect=_fake_route),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_outbox_root", return_value=str(outbox)),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == 0, "legacy path must exit 0 on success"
    entries = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
    assert len(entries) == 1, f"legacy_fn must write exactly 1 outbox file; got {entries}"


def test_legacy_fn_returns_int_exit_code(tmp_path):
    """AC6: legacy_fn() returns an int; main() surfaces it as exit code."""
    # Capture what legacy_fn returns when it succeeds via the legacy write.
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    captured_legacy_result: list = []

    def _fake_route(op: str, params: dict, repo_root: str, legacy_fn) -> int:
        result = legacy_fn()
        captured_legacy_result.append(result)
        return result  # pass through (as if seam absent)

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", side_effect=_fake_route),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_outbox_root", return_value=str(outbox)),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == 0
    assert captured_legacy_result == [0], "legacy_fn must return int 0 on success"


def test_legacy_fn_outbox_schema(tmp_path):
    """AC4: legacy outbox entry has uuid4 id, no system block, ISO-ts filename.

    Closes the 'distinct outbox schema preserved' clause of AC4 by opening the
    written file and asserting the three invariants at the file level.
    Review: code-reviewer strang-08-slice3 — (F2) added to verify file-level schema shape.
    """
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    def _fake_route(op: str, params: dict, repo_root: str, legacy_fn) -> int:
        return legacy_fn()

    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", side_effect=_fake_route),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_outbox_root", return_value=str(outbox)),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == 0, "legacy path must exit 0 for schema check"
    entries = [f for f in os.listdir(outbox) if f.endswith(".yaml")]
    assert len(entries) == 1, "exactly 1 outbox file must be written"
    filename = entries[0]

    # Filename must match ISO-ts-slug pattern (e.g. 2026-07-06T10-30-00-00-00-slug.yaml)
    import re
    assert re.match(
        r'^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}[^-]*-[a-z0-9-]+\.yaml$', filename
    ), "outbox filename must match ISO-ts-slug pattern"

    with open(outbox / filename, encoding="utf-8") as fh:
        content = fh.read()

    # system: block must be absent (distinct outbox schema, not lessons.md format)
    assert "system:" not in content, (
        "system: block must NOT be present in outbox entry (distinct outbox schema)"
    )

    # id: field must be a UUID4
    id_val = None
    for line in content.splitlines():
        if line.startswith("id:"):
            id_val = line.split(":", 1)[1].strip()
            break
    assert id_val is not None, "id: field must be present in outbox entry"
    try:
        parsed = _uuid_mod.UUID(id_val)
    except ValueError:
        raise AssertionError(f"id: field is not a valid UUID: {id_val!r}")
    assert parsed.version == 4, "id: field must be a UUID4 (not v1/v3/v5)"


# ---------------------------------------------------------------------------
# AC12 — skipped-envelope mapping
#
# native op returns {skipped: True, reason} → WARN on stderr, exit
# _EXIT_DOE_UNRESOLVABLE (3), no path. A13 fix: this used to exit 0 (a defect —
# silent success on a write that never happened); it now mirrors the legacy_fn
# _DoeUnresolvable handler's non-zero exit.
# ---------------------------------------------------------------------------


def test_skipped_emits_warn_to_stderr():
    """AC12: skipped:true → WARN message on stderr, non-zero exit (A13)."""
    skipped_result = {"skipped": True, "reason": "CLAUDE_KLABAUTER_ROOT unresolvable at write time"}

    captured_err = io.StringIO()
    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=skipped_result),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stderr", captured_err),
        unittest.mock.patch("sys.stdout", io.StringIO()),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == _cli_mod._EXIT_DOE_UNRESOLVABLE, (
        "skipped:true must exit _EXIT_DOE_UNRESOLVABLE (3), not silently claim success"
    )
    err = captured_err.getvalue()
    assert "warn:" in err, "must emit 'warn:' prefix on skipped"
    # Review: code-reviewer strang-08-slice3 — (F5) assert the specific reason string is
    # interpolated, not just the always-present template text; proves {reason} is wired.
    assert "CLAUDE_KLABAUTER_ROOT unresolvable at write time" in err, (
        "reason field must be interpolated into WARN message"
    )


def test_skipped_no_path_on_stdout():
    """AC12: skipped:true → no out_path emitted on stdout, non-zero exit (A13)."""
    skipped_result = {"skipped": True, "reason": "repos.claude_klabauter not configured"}

    captured_out = io.StringIO()
    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=skipped_result),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stderr", io.StringIO()),
        unittest.mock.patch("sys.stdout", captured_out),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == _cli_mod._EXIT_DOE_UNRESOLVABLE
    assert captured_out.getvalue().strip() == "", (
        "skipped:true must NOT print any path to stdout"
    )


def test_skipped_without_reason_key():
    """AC12: skipped:true with no reason key → still exits non-zero + WARN (no KeyError)."""
    skipped_result = {"skipped": True}

    captured_err = io.StringIO()
    with (
        unittest.mock.patch.object(_cli_mod, "_cc_route", return_value=skipped_result),
        unittest.mock.patch.object(_cli_mod, "_describe_schema_node", return_value=_FAKE_SCHEMA_OUTPUT),
        unittest.mock.patch.object(_cli_mod, "_resolve_from_repo", return_value="example-doctrine-repo"),
        unittest.mock.patch.object(_cli_mod, "_current_repo_root", return_value="/fake/repo"),
        unittest.mock.patch("sys.stderr", captured_err),
        unittest.mock.patch("sys.stdout", io.StringIO()),
    ):
        rc = _cli_mod.main(_MINIMAL_ARGV)

    assert rc == _cli_mod._EXIT_DOE_UNRESOLVABLE
    # Review: code-reviewer strang-08-slice3 — (F6) assert the default fallback string is
    # used when reason key is absent; "warn:" alone is nearly unconditional.
    # C1 (2026-07-06): lesson-promote now routes central writes to example-doctrine-repo, so the
    # default fallback is "DOE_ROOT unresolvable" (was "CLAUDE_KLABAUTER_ROOT unresolvable").
    assert "DOE_ROOT unresolvable" in captured_err.getvalue(), (
        "default fallback string must appear when reason key absent"
    )


# ---------------------------------------------------------------------------
# AC6 — grep-gate: legacy body present as legacy_fn in CLI source
# ---------------------------------------------------------------------------


def _source() -> str:
    return _CLI_PATH.read_text(encoding="utf-8")


def test_legacy_fn_defined():
    """Grep-gate: 'def legacy_fn()' is present in the CLI source."""
    assert "def legacy_fn(" in _source(), (
        "legacy_fn closure must be defined in coordinator-lesson-promote"
    )


def test_write_entry_inside_legacy_fn():
    """Grep-gate: _write_entry() call is inside the legacy_fn body."""
    source = _source()
    fn_start = source.index("def legacy_fn(")
    # Review: code-reviewer strang-08-slice3 — (F7) widened from 1500 to 3000 to avoid
    # latent false-negative if legacy_fn body grows.
    fn_body = source[fn_start:fn_start + 3000]
    assert "_write_entry(" in fn_body, (
        "_write_entry() call must be inside the legacy_fn closure body"
    )


def test_doe_unresolvable_inside_legacy_fn():
    """Grep-gate: _DoeUnresolvable handler is inside the legacy_fn body.

    C1 (2026-07-06): lesson-promote central writes route to example-doctrine-repo, so legacy_fn
    catches _DoeUnresolvable (was _ClaudeKlabauterUnresolvable pre-C1).
    """
    source = _source()
    fn_start = source.index("def legacy_fn(")
    fn_body = source[fn_start:fn_start + 3000]
    assert "_DoeUnresolvable" in fn_body, (
        "_DoeUnresolvable handler must be in the legacy_fn closure body"
    )


def test_cc_route_import_present():
    """Grep-gate: cc_invoke route import is present (routing gate wired)."""
    assert "from cc_invoke import route as _cc_route" in _source(), (
        "routing import must be present in coordinator-lesson-promote"
    )


def test_no_retired_transport():
    """AC9: coordinator-lesson-promote must not reference retired transport patterns."""
    source = _source()
    for pattern in ("coordinator_core.client", "AF_UNIX", "auth_token", "three-state"):
        assert pattern not in source, (
            f"retired transport pattern '{pattern}' must not appear in coordinator-lesson-promote"
        )
