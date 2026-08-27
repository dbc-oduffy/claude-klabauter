"""Pins `coordinator_core.merge_assemble.cli`'s behaviour-preserving split of
`merge_assemble.main`/`merge_assemble.apply.main_apply`'s fused parse-call-
print into standalone parse/print functions (chunk C1,
docs/plans/2026-08-26-merge-assembles-entry-point-reaches-the-warm-engine.md).

Covers: same params dict out of the parse functions, byte-identical stdout
out of the print functions, same usage-error exit-2 path — and AC10's
machine-checked import-closure assertion, the property the whole leaf
design rests on.
"""
from __future__ import annotations

import json

import pytest

from coordinator_core.merge_assemble import cli


# ---------------------------------------------------------------------------
# parse_brief_argv
# ---------------------------------------------------------------------------


def test_parse_brief_argv_default_tag_prefix():
    assert cli.parse_brief_argv([]) == {"tag_prefix": "v"}


def test_parse_brief_argv_custom_tag_prefix():
    assert cli.parse_brief_argv(["--tag-prefix", "rel-"]) == {"tag_prefix": "rel-"}


def test_parse_brief_argv_missing_value_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_brief_argv(["--tag-prefix"])
    assert exc_info.value.message is None


def test_parse_brief_argv_unrecognized_argument_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_brief_argv(["--bogus"])
    assert exc_info.value.message == "merge-assemble: unrecognized argument '--bogus'"


# ---------------------------------------------------------------------------
# print_brief_result
# ---------------------------------------------------------------------------


def test_print_brief_result_byte_identical_to_prior_inline_call(capsys):
    decision_object = {"b": 1, "a": [1, 2, {"z": 9, "y": 8}]}
    cli.print_brief_result(decision_object)
    captured = capsys.readouterr()
    expected = json.dumps(decision_object, indent=2, sort_keys=True) + "\n"
    assert captured.out == expected


# ---------------------------------------------------------------------------
# parse_apply_argv
# ---------------------------------------------------------------------------


def test_parse_apply_argv_defaults():
    assert cli.parse_apply_argv([]) == {
        "session_id": None,
        "decisions": None,
        "force": False,
        "tag_prefix": "v",
    }


def test_parse_apply_argv_all_flags():
    params = cli.parse_apply_argv(
        [
            "--session-id",
            "sid-123",
            "--force",
            "--tag-prefix",
            "rel-",
            "--decisions",
            '{"version_bump_final": "v1.2.3"}',
        ]
    )
    assert params == {
        "session_id": "sid-123",
        "decisions": {"version_bump_final": "v1.2.3"},
        "force": True,
        "tag_prefix": "rel-",
    }


def test_parse_apply_argv_decisions_file_channel(tmp_path):
    payload_path = tmp_path / "decisions.json"
    payload_path.write_text('{"force": "irrelevant-key"}', encoding="utf-8")
    params = cli.parse_apply_argv(["--decisions-file", str(payload_path)])
    assert params["decisions"] == {"force": "irrelevant-key"}


def test_parse_apply_argv_conflicting_channels_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_apply_argv(["--decisions", "{}", "--decisions-file", "x.json"])
    assert "mutually exclusive" in exc_info.value.message


def test_parse_apply_argv_malformed_json_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_apply_argv(["--decisions", "{not json"])
    assert exc_info.value.message.startswith("merge-assemble apply: malformed --decisions JSON")


def test_parse_apply_argv_missing_session_id_value_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_apply_argv(["--session-id"])
    assert exc_info.value.message is None


def test_parse_apply_argv_unrecognized_argument_raises_usage_error():
    with pytest.raises(cli.UsageError) as exc_info:
        cli.parse_apply_argv(["--bogus"])
    assert exc_info.value.message == "merge-assemble apply: unrecognized argument '--bogus'"


# ---------------------------------------------------------------------------
# print_apply_result
# ---------------------------------------------------------------------------


def test_print_apply_result_byte_identical_to_prior_inline_call(capsys):
    report = {"landed": ["d0", "d1"], "gates": {"portability_sweep": "passed"}}
    cli.print_apply_result(report)
    captured = capsys.readouterr()
    expected = json.dumps(report, indent=2, sort_keys=True) + "\n"
    assert captured.out == expected


# ---------------------------------------------------------------------------
# End-to-end usage-error exit-2 path, through the cold-path entrypoints
# ---------------------------------------------------------------------------


def test_main_brief_usage_error_exits_2_and_prints_diagnostic(capsys):
    from coordinator_core.merge_assemble import main, EXIT_USAGE

    exit_code = main(["brief", "--bogus"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_USAGE
    assert "unrecognized argument" in captured.err


def test_main_apply_usage_error_exits_transport_fail(capsys):
    from coordinator_core.merge_assemble.apply import main_apply, APPLY_EXIT_TRANSPORT_FAIL

    exit_code = main_apply(["--bogus"])
    captured = capsys.readouterr()
    assert exit_code == APPLY_EXIT_TRANSPORT_FAIL
    assert "unrecognized argument" in captured.err


# ---------------------------------------------------------------------------
# AC10 — machine-checked leaf-module import-closure assertion
#
# Static (AST-based), not a runtime `sys.modules` diff: `cli.py` lives
# INSIDE the `coordinator_core.merge_assemble` package, so any dotted import
# of it (`import coordinator_core.merge_assemble.cli`) necessarily runs
# `merge_assemble/__init__.py` first as ordinary Python package machinery —
# that parent-package execution is not an import `cli.py` itself performs,
# and a runtime sys.modules inspection cannot tell the two apart. Reading
# `cli.py`'s own `import`/`from ... import` statements via `ast` is what
# actually checks the property AC10 names: what THIS FILE imports.
# ---------------------------------------------------------------------------

import ast
from pathlib import Path

_CLI_MODULE_PATH = Path(cli.__file__)
_JSON_PAYLOAD_FLAG_MODULE_PATH = (
    _CLI_MODULE_PATH.parents[1] / "ceremony_common" / "json_payload_flag.py"
)


def _coordinator_core_imports(source_path: Path) -> set[str]:
    """Returns every distinct `coordinator_core...` dotted module name
    referenced by a top-level or nested `import`/`from ... import` statement
    in the file at `source_path`, via static AST parsing — no execution."""
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "coordinator_core" or alias.name.startswith("coordinator_core."):
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "coordinator_core" or node.module.startswith("coordinator_core.")
            ):
                found.add(node.module)
    return found


def test_cli_import_closure_contains_no_forbidden_coordinator_core_modules():
    imports = _coordinator_core_imports(_CLI_MODULE_PATH)
    allowed = {"coordinator_core.ceremony_common.json_payload_flag"}
    forbidden = imports - allowed
    assert not forbidden, (
        f"cli.py imports {forbidden!r} — only {allowed!r} may appear under "
        "coordinator_core (AC10's leaf carve-out)"
    )
    assert "coordinator_core.merge_assemble" not in imports
    assert "coordinator_core.merge_assemble.apply" not in imports
    assert "coordinator_core.contract.apply_base" not in imports


def test_json_payload_flag_own_closure_is_stdlib_only():
    imports = _coordinator_core_imports(_JSON_PAYLOAD_FLAG_MODULE_PATH)
    assert not imports, (
        f"json_payload_flag.py imports {imports!r} under coordinator_core — AC10's "
        "carve-out assumes this module's own closure is stdlib-only, so it cannot "
        "widen the carve-out silently"
    )
