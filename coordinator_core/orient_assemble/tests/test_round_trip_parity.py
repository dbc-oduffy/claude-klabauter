"""
coordinator_core.orient_assemble.tests.test_round_trip_parity — C3 AC(d):
round-trip parity between each ported reader and the CURRENT inline
step body it wraps, mirroring `coordinator_core.pickup_assemble`'s own
parity-test shape (`tests/test_pickup_assemble_git_readmodel_parity.py`):
call the original source function directly, call the ported reader, and
assert the reader's translation is a faithful (never lossy, never
re-derived) wrap of the original's output — not a re-implementation of
its query/format logic.

Spec backlink: DoE-claude:pln-computed-skills-b2-ceremony-st-e82420, chunk C3
"""

from __future__ import annotations

import argparse
import contextlib
import io

from coordinator_core.orient_assemble import (
    readers_branch_reconcile as rbr,
    readers_clean_ops as rco,
    readers_handoff_triage as rht,
    readers_health_reaper as rhr,
)


def test_handoff_triage_ported_functions_are_the_source_cli_objects_unmodified():
    """C2b's AS-IS import contract: the ported `_cmd_*` callables must be the
    exact function objects from the loaded source module, never
    reimplementations — a stronger guarantee than output-equality."""
    assert rht._cmd_stale_plans is rht._handoff_triage._cmd_stale_plans
    assert rht._cmd_ready is rht._handoff_triage._cmd_ready
    assert rht._cmd_awaiting_gate is rht._handoff_triage._cmd_awaiting_gate


def test_handoff_triage_ready_reader_carries_the_source_clis_captured_stdout_verbatim(
    monkeypatch,
):
    fixture_text = "- handoff-a (ready)\n- handoff-b (ready)\n"

    def fake_cmd_ready(args):
        print(fixture_text, end="")
        return 0

    monkeypatch.setattr(rht, "_cmd_ready", fake_cmd_ready)
    result = rht._read_ready()

    direct_buf = io.StringIO()
    with contextlib.redirect_stdout(direct_buf):
        fake_cmd_ready(argparse.Namespace())

    assert result.directives[0]["detail"] == direct_buf.getvalue().rstrip("\n")
    assert result.directives[0]["detail"] == fixture_text.rstrip("\n")


def test_branch_reconcile_span_assert_reader_wraps_the_ported_pure_function_unmodified():
    """The `_span_assert` comparison itself is ported as-is (never
    reimplemented) — assert identity, not merely output equality."""
    assert rbr._span_assert_compute is rbr._day_branch_resolve._span_assert


def test_branch_reconcile_span_assert_reader_matches_a_direct_call_to_the_ported_function(
    monkeypatch,
):
    from coordinator_core.daily_branch import format_span_suffix, parse_branch_span
    from coordinator_core.machine_resolver import compute_machine

    monkeypatch.setattr(
        rbr, "_current_branch", lambda repo_root=None: "work/testmachine/2026-07-01_02"
    )

    direct = rbr._span_assert_compute(
        "work/testmachine/2026-07-01_02",
        "2026-07-24",
        parse_branch_span,
        format_span_suffix,
        compute_machine,
    )
    result = rbr._read_span_assert()

    if direct is None:
        assert result.directives == []
    else:
        assert result.directives[0]["detail"] == direct


def test_branch_reconcile_current_branch_passes_repo_root_as_cwd(monkeypatch, tmp_path):
    """DR-382: scan scope is an explicit parameter threaded to the git
    subprocess, never re-derived from process cwd inside the reader."""
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "work/example/2026-08-29\n"

    def fake_run(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeCompleted()

    monkeypatch.setattr(rbr.subprocess, "run", fake_run)

    result = rbr._current_branch(str(tmp_path))

    assert captured["cwd"] == str(tmp_path)
    assert result == "work/example/2026-08-29"


def test_branch_reconcile_current_branch_defaults_cwd_to_none_when_no_repo_root(monkeypatch):
    """Omitting `repo_root` preserves the prior ambient-cwd behaviour
    byte-for-byte — `cwd=None` is `subprocess.run`'s own "use process cwd"
    default, unchanged from before this fix."""
    captured = {}

    class _FakeCompleted:
        returncode = 0
        stdout = "main\n"

    def fake_run(cmd, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return _FakeCompleted()

    monkeypatch.setattr(rbr.subprocess, "run", fake_run)

    result = rbr._current_branch()

    assert captured["cwd"] is None
    assert result == "main"


def test_branch_reconcile_read_span_assert_accepts_and_forwards_repo_root(monkeypatch):
    """`_read_span_assert(repo_root=...)` forwards to `_current_branch` —
    the callable seam a future threading chunk wires into `collect()`.
    `collect()` itself does not yet forward `repo_root` here (see
    `collect`'s own docstring); this asserts the seam exists and works."""
    captured = {}

    def fake_current_branch(repo_root=None):
        captured["repo_root"] = repo_root
        return ""

    monkeypatch.setattr(rbr, "_current_branch", fake_current_branch)

    rbr._read_span_assert("/some/other/repo")

    assert captured["repo_root"] == "/some/other/repo"


def test_clean_ops_addon_health_reader_translates_scan_lines_1to1_into_directives(monkeypatch):
    fixture_lines = ["RED: plugin-a doctor probe failed", "AMBER: plugin-b addon stale"]
    monkeypatch.setattr(rco, "_scan_addon_health_run", lambda mode: (fixture_lines, 1))

    result = rco._read_addon_health("--red-only")

    assert [d["detail"] for d in result.directives] == fixture_lines
    assert len(result.directives) == len(fixture_lines)


def test_health_reaper_claude_klabauter_bin_sentinel_reader_carries_the_stderr_verbatim(monkeypatch):
    def fake_cmd(args):
        import sys

        print("claude-klabauter-bin sentinel missing at X", file=sys.stderr)
        return 1

    monkeypatch.setattr(rhr, "_cmd_claude_klabauter_bin_sentinel", fake_cmd)
    result = rhr._read_claude_klabauter_bin_sentinel()
    assert result.directives[0]["detail"] == "claude-klabauter-bin sentinel missing at X"


def test_health_reaper_ported_probe_functions_are_the_source_cli_objects_unmodified():
    # cmd_exec_bit_check was retired 2026-07-28 along with check-all-shebanged-
    # exec-bits.py -- see readers_health_reaper.py's module docstring.
    assert rhr._cmd_claude_klabauter_bin_sentinel is rhr._health_probes.cmd_claude_klabauter_bin_sentinel
    assert rhr._cmd_ceremony_hook is rhr._health_probes.cmd_ceremony_hook
    assert rhr._cmd_working_repo_registration is rhr._health_probes.cmd_working_repo_registration


def test_health_reaper_working_repo_registration_reader_carries_the_stderr_verbatim(monkeypatch):
    def fake_cmd(args):
        import sys

        print("WORKING-REPO PROBE: engine.working_repos.claude_klabauter is not registered", file=sys.stderr)
        return 1

    monkeypatch.setattr(rhr, "_cmd_working_repo_registration", fake_cmd)
    result = rhr._read_working_repo_registration()
    assert result.directives[0]["detail"] == (
        "WORKING-REPO PROBE: engine.working_repos.claude_klabauter is not registered"
    )
    assert result.directives[0]["id"] == "d-working-repo-registration"
    assert result.directives[0]["cli"] == "workday-start-health-probes"
    # The emitted directive names the --fix apply form, not the bare
    # detector this reader itself calls (which stays zero-spawn) -- see
    # _read_working_repo_registration's own docstring for why a directive
    # naming only the detector would relocate the transcription rather
    # than discharge it.
    assert result.directives[0]["args"] == ["working-repo-registration", "--fix"]
