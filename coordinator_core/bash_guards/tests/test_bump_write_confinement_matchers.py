"""Reachability tests for the `bump-outside-repo-write` (C4) and
`bump-foreign-repo-write` (C5) `GuardEntry` matcher widenings
(`docs/plans/2026-08-07-liveness-seam-validates-its-repo-root.md`).

Spec backlink: `tasks/2026-08-07-liveness-seam/briefs/C4.md` (AC6/AC7/AC11),
`tasks/2026-08-07-liveness-seam/briefs/C5.md` (AC9/AC11/AC8).

negative_spec: This module does not test detection CORRECTNESS of either
PowerShell leg beyond the shapes each leg's own unit-test module already
covers (`test_bump_outside_repo_write.py`, `test_bump_foreign_repo_write.py`).
It tests only that the dispatcher's `matchers=` widening makes each leg
reachable at all (AC6/AC9), changes nothing for Bash (AC7), and that each
leg's own unparseable-input contract is SILENT, never a manufactured deny
(AC11).
"""

from __future__ import annotations

import json

import pytest

from coordinator_core.bash_guards import bump_foreign_repo_write as foreign_guard
from coordinator_core.bash_guards import bump_outside_repo_write as guard
from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._dialect import Dialect, resolve_segments_for_dialect
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards._write_bump_session_start import (
    write_session_start_record,
)


# test IS the spawn. _BASELINE is shrink-only pre-existing residue and is explicitly
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _set_anchor(monkeypatch, home_dir, anchor_dir, session_id: str) -> None:
    monkeypatch.setenv("HOME", str(home_dir))
    write_session_start_record(session_id, launch_cwd=str(anchor_dir))


def test_ac6_powershell_out_file_payload_reaches_the_powershell_leg(tmp_path, monkeypatch):
    calls = []
    original = guard._check_bump_outside_repo_write_powershell

    def _spy(cmd, session_id, cwd, payload):
        calls.append((cmd, session_id, cwd, payload))
        return original(cmd, session_id, cwd, payload)

    monkeypatch.setattr(guard, "_check_bump_outside_repo_write_powershell", _spy)

    dest = tmp_path / "out.txt"
    cmd = f"Get-Date | Out-File -FilePath {dest}"
    raw = json.dumps(
        {
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
            "session_id": "sess-ac6-reachability",
            "cwd": str(tmp_path),
        }
    )

    dispatch.evaluate_payload_json(raw)

    assert calls, (
        "the PowerShell leg was never invoked -- the dispatcher's "
        "per-entry matchers gate (dispatch.py's guard loop) skipped the "
        "bump-outside-repo-write entry before its fn() ever ran"
    )


def test_ac6_bash_payload_never_reaches_the_powershell_leg(tmp_path, monkeypatch):
    calls = []
    original = guard._check_bump_outside_repo_write_powershell

    def _spy(cmd, session_id, cwd, payload):
        calls.append((cmd, session_id, cwd, payload))
        return original(cmd, session_id, cwd, payload)

    monkeypatch.setattr(guard, "_check_bump_outside_repo_write_powershell", _spy)

    dest = tmp_path / "out.txt"
    cmd = f"echo hi > {dest.as_posix()}"
    raw = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": "sess-ac6-negative-control",
            "cwd": str(tmp_path),
        }
    )

    dispatch.evaluate_payload_json(raw)

    assert not calls


# `Set-Content` sink resolving under a DIFFERENT git root from the session's


def test_ac9_powershell_set_content_payload_reaches_the_foreign_repo_powershell_leg(
    tmp_path, monkeypatch
):
    calls = []
    original = foreign_guard._check_bump_foreign_repo_write_powershell

    def _spy(cmd, session_id, cwd, payload):
        calls.append((cmd, session_id, cwd, payload))
        return original(cmd, session_id, cwd, payload)

    monkeypatch.setattr(foreign_guard, "_check_bump_foreign_repo_write_powershell", _spy)

    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".git").mkdir()
    dest = foreign / "target.txt"

    session_id = "sess-ac9-reachability"
    _set_anchor(monkeypatch, home, anchor, session_id)

    cmd = f"Set-Content -Path {dest} -Value hello"
    raw = json.dumps(
        {
            "tool_name": "PowerShell",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": str(anchor),
        }
    )

    dispatch.evaluate_payload_json(raw)

    assert calls, (
        "the foreign-repo-write PowerShell leg was never invoked -- the "
        "dispatcher's per-entry matchers gate (dispatch.py's guard loop) "
        "skipped the bump-foreign-repo-write entry before its fn() ever ran"
    )


def test_ac9_bash_payload_never_reaches_the_foreign_repo_powershell_leg(tmp_path, monkeypatch):
    calls = []
    original = foreign_guard._check_bump_foreign_repo_write_powershell

    def _spy(cmd, session_id, cwd, payload):
        calls.append((cmd, session_id, cwd, payload))
        return original(cmd, session_id, cwd, payload)

    monkeypatch.setattr(foreign_guard, "_check_bump_foreign_repo_write_powershell", _spy)

    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".git").mkdir()
    dest = foreign / "target.txt"

    session_id = "sess-ac9-negative-control"
    _set_anchor(monkeypatch, home, anchor, session_id)

    cmd = f"echo hi > {dest.as_posix()}"
    raw = json.dumps(
        {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": session_id,
            "cwd": str(anchor),
        }
    )

    dispatch.evaluate_payload_json(raw)

    assert not calls


# AC7 -- no Bash regression. The widened `matchers=COMMAND_TOOL_NAMES` still

_OLD_MATCHERS = ("Bash",)

_BASH_CORPUS = [
    pytest.param("cp {src} {dest}", id="cp-outside-repo"),
    pytest.param("echo hi > {dest}", id="redirect-outside-repo"),
    pytest.param("mkdir -p {dest}", id="mkdir-outside-repo"),
    pytest.param("git status", id="git-status-no-write-sink"),
]


def test_ac7_bash_still_admitted_by_the_real_registered_matchers(tmp_path, monkeypatch):
    """
    the prior version of this test compared `"Bash" in ("Bash",)` against
    `"Bash" in COMMAND_TOOL_NAMES`, both trivially True regardless of what
    the dispatcher actually registers. This introspects the REAL guard
    chain built by `dispatch._build_guard_chain` (the same structural
    posture `test_guard_band_membership.py` uses -- registration only,
    never the `fn` closures) and asserts against the live
    `bump-outside-repo-write` entry's `matchers`, not a hand-copied
    constant, so a regression that narrowed the registered matchers back
    to `("Bash",)`-only-minus-Bash (or dropped Bash entirely) would fail
    this test even though `COMMAND_TOOL_NAMES` itself never changed."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    session_id = "sess-ac7-registration"
    _set_anchor(monkeypatch, home, anchor, session_id)

    chain = dispatch._build_guard_chain(
        "git status", session_id, str(anchor), {"tool_name": "Bash"}, None, None
    )
    entries = {entry.name: entry for entry in chain}

    assert "bump-outside-repo-write" in entries
    entry = entries["bump-outside-repo-write"]
    assert "Bash" in entry.matchers, (
        "the real registered bump-outside-repo-write GuardEntry no longer "
        "admits Bash -- AC7's no-regression guarantee is broken: "
        "matchers=%r" % (entry.matchers,)
    )
    assert set(_OLD_MATCHERS) <= set(entry.matchers), (
        "the widened matchers must be a superset of the pre-widening "
        "pin, not just happen to still include Bash"
    )


@pytest.mark.parametrize("template", _BASH_CORPUS)
def test_ac7_bash_payload_verdict_unchanged_by_tool_name_widening(
    template, tmp_path, monkeypatch
):
    """The detection FUNCTION itself (`check_bump_outside_repo_write`) never
    reads `matchers` -- it reads `payload["tool_name"]` only to pick a
    dialect. Widening the registration cannot change what this function
    returns for a Bash-dialect payload: asserted here by calling it with
    `tool_name` absent (the pre-widening production shape, since every
    caller before this chain existed left it unset for Bash) and with
    `tool_name="Bash"` explicit (the post-widening shape a real Bash tool
    call now carries) and requiring byte-identical verdicts."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    dest = outside / "target.txt"
    src = anchor / "src.txt"
    src.write_text("x\n", encoding="utf-8")

    cmd = template.format(src=src.as_posix(), dest=dest.as_posix())

    session_id = "sess-ac7-%s" % abs(hash(cmd))
    _set_anchor(monkeypatch, home, anchor, session_id)

    result_no_tool_name = guard.check_bump_outside_repo_write(cmd, session_id, str(anchor), {})
    result_explicit_bash = guard.check_bump_outside_repo_write(
        cmd, session_id, str(anchor), {"tool_name": "Bash"}
    )

    assert (result_no_tool_name is None) == (result_explicit_bash is None)
    if result_no_tool_name is not None:
        assert result_no_tool_name == result_explicit_bash


_FOREIGN_BASH_CORPUS = [
    pytest.param("cp {src} {dest}", id="cp-foreign-repo"),
    pytest.param("echo hi > {dest}", id="redirect-foreign-repo"),
    pytest.param("git status", id="git-status-no-write-sink"),
]


def test_no_bash_regression_foreign_repo_write_admitted_by_the_real_registered_matchers(
    tmp_path, monkeypatch
):
    """Sibling sweep (coordinator-authorized, `state/lessons/2026-07-23-
    one-fail-open-in-a-file-predicts-sibling-69e0f386c861.yaml`) of
    `test_ac7_bash_still_admitted_by_the_real_registered_matchers` --
    this test had the identical tautological shape (`"Bash" in ("Bash",)`
    vs. `"Bash" in COMMAND_TOOL_NAMES`, both trivially True regardless of
    what the dispatcher actually registers). Introspects the REAL guard
    chain built by `dispatch._build_guard_chain` (same structural posture
    `test_guard_band_membership.py` uses) and asserts against the live
    `bump-foreign-repo-write` entry's `matchers`, not a hand-copied
    constant."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    session_id = "sess-foreign-bash-regression-registration"
    _set_anchor(monkeypatch, home, anchor, session_id)

    chain = dispatch._build_guard_chain(
        "git status", session_id, str(anchor), {"tool_name": "Bash"}, None, None
    )
    entries = {entry.name: entry for entry in chain}

    assert "bump-foreign-repo-write" in entries
    entry = entries["bump-foreign-repo-write"]
    assert "Bash" in entry.matchers, (
        "the real registered bump-foreign-repo-write GuardEntry no longer "
        "admits Bash -- the no-regression guarantee is broken: "
        "matchers=%r" % (entry.matchers,)
    )
    assert set(_OLD_MATCHERS) <= set(entry.matchers), (
        "the widened matchers must be a superset of the pre-widening "
        "pin, not just happen to still include Bash"
    )


@pytest.mark.parametrize("template", _FOREIGN_BASH_CORPUS)
def test_no_bash_regression_foreign_repo_write_verdict_unchanged_by_tool_name_widening(
    template, tmp_path, monkeypatch
):
    """`bump-foreign-repo-write`'s own C5 counterpart to AC7's Bash-verdict-
    identity assertion above: the detection FUNCTION (`check_bump_foreign_
    repo_write`) never reads `matchers` -- it reads `payload["tool_name"]`
    only to pick a dialect, so widening the registration cannot change what
    it returns for a Bash-dialect payload."""
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / ".git").mkdir()
    dest = foreign / "target.txt"
    src = anchor / "src.txt"
    src.write_text("x\n", encoding="utf-8")

    cmd = template.format(src=src.as_posix(), dest=dest.as_posix())

    session_id = "sess-foreign-bash-regression-%s" % abs(hash(cmd))
    _set_anchor(monkeypatch, home, anchor, session_id)

    # case (`_write_bump_message.resolve_agent_class` -> AGENT_CLASS_UNKNOWN,
    base = {"session_id": session_id}
    result_no_tool_name = foreign_guard.check_bump_foreign_repo_write(cmd, session_id, str(anchor), base)
    result_explicit_bash = foreign_guard.check_bump_foreign_repo_write(
        cmd, session_id, str(anchor), {**base, "tool_name": "Bash"}
    )

    assert (result_no_tool_name is None) == (result_explicit_bash is None)
    if result_no_tool_name is not None:
        assert result_no_tool_name == result_explicit_bash


# Verdict asserted DIRECTLY (result is/is not None, never inferred from

_GUARD_LEGS = {
    "bump-outside-repo-write": guard._check_bump_outside_repo_write_powershell,
    "bump-foreign-repo-write": foreign_guard._check_bump_foreign_repo_write_powershell,
}

#: resolves under a DIFFERENT git root, so its dest must itself be inside a
_DEST_SETUP_BY_LEG = {
    "bump-outside-repo-write": lambda tmp_path: (tmp_path / "outside").resolve(),
    "bump-foreign-repo-write": lambda tmp_path: (tmp_path / "foreign").resolve(),
}

#: the deny is a TRUE POSITIVE: the tokenizer parses around the wrapper
_NO_CANDIDATE_TEMPLATES = [
    pytest.param("$x = @'\nhello\n'@\nOut-File -FilePath {dest}", id="single-quote-here-string"),
    pytest.param('$x = @"\nhello\n"@\nOut-File -FilePath {dest}', id="double-quote-here-string"),
    pytest.param("& {{ Out-File -FilePath {dest} }}", id="ampersand-call-operator"),
]

_EXTRACTS_LITERAL_TARGET_TEMPLATES = [
    pytest.param("Out-File -FilePath {dest} `n -Append", id="backtick-escape"),
    pytest.param("$env:X = 'y'; Out-File -FilePath {dest}", id="dollar-variable-assignment"),
    pytest.param("@(1,2,3) | Out-File -FilePath {dest}", id="at-array-literal"),
]

_UNPARSEABLE_SEGMENT_TEMPLATES = _NO_CANDIDATE_TEMPLATES


@pytest.mark.parametrize("guard_leg_name", list(_GUARD_LEGS))
@pytest.mark.parametrize("segment_template", _UNPARSEABLE_SEGMENT_TEMPLATES)
def test_ac11_unparseable_segment_never_denies(
    guard_leg_name, segment_template, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    target_root = _DEST_SETUP_BY_LEG[guard_leg_name](tmp_path)
    target_root.mkdir()
    if guard_leg_name == "bump-foreign-repo-write":
        (target_root / ".git").mkdir()
    dest = target_root / "target.txt"

    cmd = segment_template.format(dest=dest.as_posix())
    session_id = "sess-ac11-%s" % abs(hash((guard_leg_name, cmd)))
    _set_anchor(monkeypatch, home, anchor, session_id)

    leg = _GUARD_LEGS[guard_leg_name]
    result = leg(cmd, session_id, str(anchor), {"tool_name": "PowerShell"})

    assert result is None, (
        "an unparseable PowerShell segment must record SILENT, never "
        "manufacture a deny: got %r for guard_leg=%s cmd=%r"
        % (result, guard_leg_name, cmd)
    )

    # finding) -- every template in `_UNPARSEABLE_SEGMENT_TEMPLATES` still
    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name=guard_leg_name)
    assert segments is not None, (
        "this corpus's templates are expected to tokenize/segment "
        "successfully (the tokenizer parses AROUND the wrapper syntax) -- "
        "if this now fails, the template no longer exercises what this "
        "corpus is documented to exercise and belongs in the fully-"
        "unparseable corpus instead: cmd=%r" % (cmd,)
    )


@pytest.mark.parametrize("segment_template", _EXTRACTS_LITERAL_TARGET_TEMPLATES)
def test_ac11_extracted_literal_target_does_bump(segment_template, tmp_path, monkeypatch):
    guard_leg_name = "bump-foreign-repo-write"
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    target_root = _DEST_SETUP_BY_LEG[guard_leg_name](tmp_path)
    target_root.mkdir()
    (target_root / ".git").mkdir()
    dest = target_root / "target.txt"

    cmd = segment_template.format(dest=dest.as_posix())
    session_id = "sess-ac11-pos-%s" % abs(hash(cmd))
    _set_anchor(monkeypatch, home, anchor, session_id)

    result = _GUARD_LEGS[guard_leg_name](
        cmd, session_id, str(anchor), {"tool_name": "PowerShell"}
    )

    assert result is not None, (
        "awkward PowerShell that still lands a literal -FilePath target "
        "inside a foreign repo must be bumped, not waved through -- a "
        "`None` here is the bypass this corpus exists to catch: cmd=%r"
        % (cmd,)
    )
    assert (
        result["hookSpecificOutput"]["permissionDecision"] == "deny"
    ), result


# `resolve_segments_for_dialect` OUTRIGHT (no candidates extracted at all),
# distinct from `_UNPARSEABLE_SEGMENT_TEMPLATES` above, which all still

_FULLY_UNPARSEABLE_SEGMENT_TEMPLATES = [
    pytest.param("Out-File -FilePath {dest} &> {dest}", id="ampersand-redirect-grammar-gap"),
]


@pytest.mark.parametrize("guard_leg_name", list(_GUARD_LEGS))
@pytest.mark.parametrize("segment_template", _FULLY_UNPARSEABLE_SEGMENT_TEMPLATES)
def test_ac11_fully_unparseable_segment_yields_no_candidates_at_all(
    guard_leg_name, segment_template, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    anchor = tmp_path / "anchor"
    anchor.mkdir()
    (anchor / ".git").mkdir()
    target_root = _DEST_SETUP_BY_LEG[guard_leg_name](tmp_path)
    target_root.mkdir()
    if guard_leg_name == "bump-foreign-repo-write":
        (target_root / ".git").mkdir()
    dest = target_root / "target.txt"

    cmd = segment_template.format(dest=dest.as_posix())
    session_id = "sess-ac11-fully-unparseable-%s" % abs(hash((guard_leg_name, cmd)))
    _set_anchor(monkeypatch, home, anchor, session_id)

    segments = resolve_segments_for_dialect(cmd, Dialect.POWERSHELL, guard_name=guard_leg_name)
    assert segments is None, (
        "this template is expected to defeat segmentation OUTRIGHT (a "
        "confirmed grammar gap per _dialect.py's module docstring) -- if "
        "it now parses, tree-sitter-pwsh closed the gap and this template "
        "no longer exercises the extraction-boundary SILENT path: cmd=%r"
        % (cmd,)
    )

    leg = _GUARD_LEGS[guard_leg_name]
    result = leg(cmd, session_id, str(anchor), {"tool_name": "PowerShell"})

    assert result is None, (
        "a segment that defeats extraction outright must still record "
        "SILENT, never manufacture a deny: got %r for guard_leg=%s cmd=%r"
        % (result, guard_leg_name, cmd)
    )
