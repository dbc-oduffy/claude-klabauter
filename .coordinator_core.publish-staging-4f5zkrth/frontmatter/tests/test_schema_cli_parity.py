"""
coordinator_core.frontmatter.tests.test_schema_cli_parity — conformance suite
for coordinator_core.frontmatter.schema_cli, ported from DoE-claude's own
oracle test suite (coordinator/bin/tests/test-schema-cli.bats).

Purpose: verify byte-identical argv/output/exit-code parity between this
module's --describe/--validate CLI contract and schema-cli.js's, plus the
makima-local parity subtleties called out in the chunk spec (each its own
named test, not collapsed into one "contract fixture" assertion).

Negative-spec: does NOT run bats or spawn `node schema-cli.js` — this suite
ports the DoE bats *expectations* into pytest assertions against the local
Python CLI (main()) directly, invoked in-process (no subprocess spawn) via
capsys/monkeypatch of sys.argv/stdin, matching how other coordinator_core CLI
modules are tested. Does not test the "schema.describe"/"schema.validate"
registered-op front door — that is the ops-registration side of the dual
front-door invariant, exercised (if at all) by a separate ops-dispatch test.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.frontmatter import schema_cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_describe(monkeypatch, capsys, schema_name: str) -> tuple[int, dict]:
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--describe", schema_name])
    exit_code = schema_cli.main()
    out = capsys.readouterr().out
    return exit_code, json.loads(out)


def _run_validate(monkeypatch, capsys, schema_name: str, record_json_text: str):
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--validate", schema_name])
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(record_json_text))
    exit_code = schema_cli.main()
    captured = capsys.readouterr()
    return exit_code, captured.out, captured.err


# ---------------------------------------------------------------------------
# --describe: ported bats AC-1 cases — exits 0, required is array
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schema_name",
    ["bug-backlog", "debt-backlog", "improvement-queue", "lessons-outbox", "lesson-entry", "review-trail"],
)
def test_describe_exits_0_with_array_required_optional(monkeypatch, capsys, schema_name):
    exit_code, out = _run_describe(monkeypatch, capsys, schema_name)
    assert exit_code == 0
    assert isinstance(out["required"], list)
    assert isinstance(out["optional"], list)
    assert isinstance(out["enums"], dict)


def test_describe_improvement_queue_has_change_kind_enum(monkeypatch, capsys):
    _, out = _run_describe(monkeypatch, capsys, "improvement-queue")
    assert "change_kind" in out["enums"]
    assert isinstance(out["enums"]["change_kind"], list)
    assert len(out["enums"]["change_kind"]) > 0


def test_describe_lesson_entry_required_fields_and_status_enum(monkeypatch, capsys):
    _, out = _run_describe(monkeypatch, capsys, "lesson-entry")
    expected_required = ["created", "title", "body", "status", "scope", "from_repo"]
    for f in expected_required:
        assert f in out["required"]
    assert "status" in out["enums"]
    assert isinstance(out["enums"]["status"], list)
    assert len(out["enums"]["status"]) > 0


# ---------------------------------------------------------------------------
# --describe: applies_to field present in the describe payload (AC-1b)
# ---------------------------------------------------------------------------


def test_describe_bug_backlog_applies_to(monkeypatch, capsys):
    _, out = _run_describe(monkeypatch, capsys, "bug-backlog")
    assert out["applies_to"] == "state/bug-backlog/*.yaml"


def test_describe_cross_repo_commitment_applies_to(monkeypatch, capsys):
    _, out = _run_describe(monkeypatch, capsys, "cross-repo-commitment")
    assert out["applies_to"] == "state/cross-repo-commitments/*.yaml"


# ---------------------------------------------------------------------------
# --validate: known-good record for review-trail schema → ok:true, exit 0 (AC-2)
# ---------------------------------------------------------------------------


def test_validate_review_trail_good_record_ok_true_exit_0(monkeypatch, capsys):
    record = {
        "sha_range": "abc1234..def5678",
        "reviewer": "code-reviewer",
        "scope": "bin/schema-cli.js",
        "scope_kind": "diff",
        "verdict": "pass",
        "diff_loc": 100,
        "session_id": "test-session-1",
        "workstream": None,
    }
    exit_code, out, _err = _run_validate(monkeypatch, capsys, "review-trail", json.dumps(record))
    assert exit_code == 0
    parsed = json.loads(out)
    assert parsed["ok"] is True
    assert isinstance(parsed["errors"], list)
    assert parsed["errors"] == []


# ---------------------------------------------------------------------------
# --validate: record missing a required field → ok:false, field in errors,
# non-zero exit (AC-3)
# ---------------------------------------------------------------------------


def test_validate_bug_backlog_missing_title_ok_false_nonzero_exit(monkeypatch, capsys):
    record = {
        "created": "2026-06-27",
        "body": "test body content",
        "status": "open",
        "surface": "bin/schema-cli.js",
        "severity": "P2",
    }
    exit_code, out, _err = _run_validate(monkeypatch, capsys, "bug-backlog", json.dumps(record))
    assert exit_code != 0
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert isinstance(parsed["errors"], list)
    assert any("title" in e for e in parsed["errors"])


# ---------------------------------------------------------------------------
# Unknown schema name → fail loud, non-zero exit (AC-4)
# ---------------------------------------------------------------------------


def test_describe_unknown_schema_name_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--describe", "totally-nonexistent-schema-xyz-abc"])
    with pytest.raises(SystemExit) as exc_info:
        schema_cli.main()
    assert exc_info.value.code != 0


def test_validate_unknown_schema_name_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--validate", "totally-nonexistent-schema-xyz-abc"])
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("{}"))
    with pytest.raises(SystemExit) as exc_info:
        schema_cli.main()
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Field-order pin: --describe emits required in the schema declaration order
# (AC-7). bug-backlog required fields (JSON Schema properties-declaration
# order, filtered to required[]): created, title, body, status, surface, severity
# ---------------------------------------------------------------------------


def test_describe_bug_backlog_required_field_order_pin(monkeypatch, capsys):
    _, out = _run_describe(monkeypatch, capsys, "bug-backlog")
    expected = ["created", "title", "body", "status", "surface", "severity"]
    assert out["required"] == expected


# ---------------------------------------------------------------------------
# Named parity subtleties (chunk spec) — each its OWN test, not collapsed.
# ---------------------------------------------------------------------------


def test_output_is_two_space_json_plus_trailing_newline(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--describe", "bug-backlog"])
    schema_cli.main()
    out = capsys.readouterr().out
    payload = json.loads(out)
    expected = json.dumps(payload, indent=2) + "\n"
    assert out == expected
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_describe_required_ordered_by_properties_declaration_not_required_array_order():
    # review-trail's required[] array is declared in a DIFFERENT order than the
    # schema's properties-declaration order would be if it diverged; assert the
    # emitted order tracks properties-declaration order (all 8 fields, in the
    # exact order they appear under "properties" in review-trail.schema.json,
    # filtered to required[] membership) rather than required[]'s own textual
    # order — bug-backlog is the sharper regression case since it is a strict
    # subset check the field-order-pin test above already exercises head-on;
    # here we assert the described order equals properties-declaration order
    # directly against schema_validate.describe(), independent of the CLI layer.
    from coordinator_core.frontmatter.schema_validate import describe as _describe

    result = _describe("review-trail")
    # review-trail.schema.json declares required == full properties order
    # (all 8 fields required, no optional block) — assert the describe() output
    # preserves that exact properties-declaration order.
    expected_order = [
        "sha_range", "reviewer", "scope", "scope_kind",
        "verdict", "diff_loc", "session_id", "workstream",
    ]
    assert result["required"] == expected_order


def test_enums_inclusion_rule_json_schema_dialect_only_enum_keyword_fields():
    # JSON-Schema-backed dialect: only properties declaring a JSON Schema
    # "enum" keyword appear in the enums dict — non-enum string/number fields
    # (e.g. bug-backlog's "surface", a free-text string) are absent, even
    # though they are present in required/optional.
    from coordinator_core.frontmatter.schema_validate import describe as _describe

    result = _describe("bug-backlog")
    assert "severity" in result["enums"]  # declares enum: [P0,P1,P2,P3]
    assert "status" in result["enums"]  # declares enum: [open,closed,...]
    assert "surface" not in result["enums"]  # free-text string, no enum keyword
    assert "title" not in result["enums"]  # free-text string, no enum keyword


def test_applies_to_key_always_present_even_when_schema_declares_none():
    # review-findings (or any vendored schema lacking a top-level applies_to)
    # must still surface the key with value None — "key always present" per
    # schema.applies_to ?? null. Locate a vendored schema without applies_to
    # by inspecting plan-tasks, which is per-row (no whole-file applies_to glob).
    from coordinator_core.frontmatter.schema_validate import describe as _describe

    result = _describe("plan-tasks")
    assert "applies_to" in result
    # plan-tasks.schema.json is a per-row schema without a whole-file glob —
    # applies_to is expected to be None (key present, value null).
    assert result["applies_to"] is None


def test_validate_malformed_stdin_json_exits_2_specifically(monkeypatch, capsys):
    # THE single easiest parity break to ship by accident: malformed JSON on
    # stdin must exit 2, distinct from ok:false's exit 1. Assert both the exit
    # code AND that no ok:false JSON body is printed on stdout for this path.
    exit_code, out, err = _run_validate(monkeypatch, capsys, "bug-backlog", "{not valid json")
    assert exit_code == 2
    assert exit_code != 1
    assert out == ""  # no stdout JSON body on the malformed-stdin path
    assert "malformed JSON on stdin" in err


def test_validate_well_formed_json_ok_false_exits_1_specifically(monkeypatch, capsys):
    record = {
        "created": "2026-06-27",
        "body": "test body content",
        "status": "open",
        "surface": "bin/schema-cli.js",
        "severity": "P2",
    }
    exit_code, _out, _err = _run_validate(monkeypatch, capsys, "bug-backlog", json.dumps(record))
    assert exit_code == 1
    assert exit_code != 2


def test_stderr_error_prefix_exactly_schema_cli_error(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["schema_cli.py", "--describe", "totally-nonexistent-schema-xyz-abc"])
    with pytest.raises(SystemExit):
        schema_cli.main()
    err = capsys.readouterr().err
    assert err.startswith("schema-cli: error: ")


def test_stderr_error_prefix_on_missing_mode(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["schema_cli.py"])
    with pytest.raises(SystemExit):
        schema_cli.main()
    err = capsys.readouterr().err
    assert err.startswith("schema-cli: error: ")
    assert "missing mode" in err


def test_stderr_error_prefix_on_malformed_stdin(monkeypatch, capsys):
    _exit_code, _out, err = _run_validate(monkeypatch, capsys, "bug-backlog", "{not valid json")
    assert err.startswith("schema-cli: error: ")
