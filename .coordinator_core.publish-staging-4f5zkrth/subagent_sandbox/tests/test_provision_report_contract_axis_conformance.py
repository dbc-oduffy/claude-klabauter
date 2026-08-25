"""
coordinator_core.subagent_sandbox.tests.test_provision_report_contract_axis_conformance
-- W0.2b red test: the set of input/output axes `provision_report.py` actually
reads/writes equals the set `CONTRACT.md`'s "Axis inventory (machine-checked)"
section documents, in BOTH directions.

This is what makes the cross-repo grammar coupling between DoE's
`subagent-sandbox-policy.yaml` and claude-klabauter's `provision_report.py` discharge
by test rather than by a human remembering to keep two files in lockstep --
canonical spec `state/subagent-share/conductor/seam-adjudication.md` § 2.3.

Both sides are derived from source, not hand-copied into this test: the
documented set is PARSED out of CONTRACT.md's literal `- <kind>: `<name>``
list; the actual set is derived by regexing `provision_report.py` for
`payload.get("...")`/`payload["..."]` reads, `output["..."]` writes, and
non-plumbing `add_argument("--...")` CLI flags, plus a small, explicitly-
justified allowlist for the axes `resolve_effective_types` (engine.py)
resolves on this module's behalf rather than a literal dict access in this
module's own source.

Module under test: coordinator_core/subagent_sandbox/provision_report.py
Spec backlink: state/subagent-share/conductor/seam-adjudication.md § 2.3
"""
from __future__ import annotations

import re
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "provision_report.py"
_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "CONTRACT.md"

#: Axes resolved indirectly via `resolve_effective_types` (engine.py) --
#: never a literal `payload.get(...)`/`payload["..."]` access in THIS
#: module's own source, so the regex sweep below cannot see them. Their
#: presence is instead confirmed by
#: `test_resolve_effective_types_import_backs_indirect_axes`, which asserts
#: the delegating import this allowlist depends on is actually present.
_INDIRECT_STDIN_AXES = {"agent_type", "subagent_type", "agent_id"}

#: CLI flags that are pure invocation plumbing (policy-file location,
#: working directory) -- not part of the DoE<->claude-klabauter wire GRAMMAR this
#: contract pins, so deliberately excluded from the axis inventory both
#: here and in CONTRACT.md.
_PLUMBING_CLI_FLAGS = {"policy", "cwd"}

_DOC_ROW_RE = re.compile(r"^- (stdin|cli|stdout): `([^`]+)`", re.MULTILINE)
_PAYLOAD_READ_RE = re.compile(r'payload\.get\("([a-zA-Z_]+)"\)|payload\["([a-zA-Z_]+)"\]')
_OUTPUT_WRITE_RE = re.compile(r'output\["([a-zA-Z_]+)"\]\s*=')
_ADD_ARGUMENT_RE = re.compile(r'add_argument\(\s*"--([a-zA-Z_]+)"')


def _documented_axes() -> set[tuple[str, str]]:
    text = _CONTRACT_PATH.read_text(encoding="utf-8")
    rows = _DOC_ROW_RE.findall(text)
    assert rows, (
        "CONTRACT.md's '### Axis inventory (machine-checked)' section is missing or "
        "its rows no longer match the expected '- <kind>: `<name>`' form -- this test "
        "cannot parse a moved/reshaped section, which is itself a drift signal"
    )
    return set(rows)


def _code_axes() -> set[tuple[str, str]]:
    source = _MODULE_PATH.read_text(encoding="utf-8")

    stdin_names: set[str] = set()
    for group_a, group_b in _PAYLOAD_READ_RE.findall(source):
        stdin_names.add(group_a or group_b)
    stdin_names |= _INDIRECT_STDIN_AXES

    cli_names = {
        name for name in _ADD_ARGUMENT_RE.findall(source) if name not in _PLUMBING_CLI_FLAGS
    }

    output_names = set(_OUTPUT_WRITE_RE.findall(source))

    axes: set[tuple[str, str]] = set()
    axes |= {("stdin", name) for name in stdin_names}
    axes |= {("cli", name) for name in cli_names}
    axes |= {("stdout", name) for name in output_names}
    return axes


def test_provision_report_and_contract_files_present() -> None:
    assert _MODULE_PATH.is_file(), f"module under test not found at {_MODULE_PATH}"
    assert _CONTRACT_PATH.is_file(), f"CONTRACT.md not found at {_CONTRACT_PATH}"


def test_resolve_effective_types_import_backs_indirect_axes() -> None:
    """Confirms the delegating import `_INDIRECT_STDIN_AXES` relies on is
    actually present -- without this, that allowlist could silently claim
    axes the module no longer resolves at all."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "resolve_effective_types" in source


def test_contract_axis_conformance_both_directions() -> None:
    documented = _documented_axes()
    actual = _code_axes()

    undocumented = actual - documented
    assert not undocumented, (
        "provision_report.py reads/writes axes CONTRACT.md's Axis inventory does not "
        f"document: {sorted(undocumented)} -- add a matching row to CONTRACT.md"
    )

    unimplemented = documented - actual
    assert not unimplemented, (
        "CONTRACT.md's Axis inventory documents axes provision_report.py does not "
        f"actually implement: {sorted(unimplemented)} -- either wire the axis or "
        "remove the stale row"
    )
