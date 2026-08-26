"""test_sweep_boot.py — contract suite for sweep-boot.py, now a GRAVESTONE.

sweep-boot.py used to trampoline `session.boot_sweep`. That op is KILLED, not
suspended (30017ms measured against a 2000ms bar, 8 of 8 calls ending in
caller_timeout), so the dial could only ever refuse — 49 times in 28 hours in
this repo, 32 in DoE-claude, once per session boot, each refusal arriving
through a warm client that blocks up to 15s for a respawn first.

The dispatch is gone and nothing replaces it: across the same 28 hours in which
the composite was 100% dead, the repo landed 368 archival commits through the
per-artifact lifecycle ops (`handoff.archive_transition`,
`handoff.ship_and_archive`, `handoff.close_origin_stub`,
`fleet.archive_completed_handoffs`, `fleet.archive_terminal_sizings`). The
requirement the kill bar demands be named is discharged there.

What this suite protects is therefore the ABSENCE of the dispatch plus the two
contracts the SessionStart hook (owned in the coordinator-claude plane) still
depends on byte-for-byte: one integer on stdout, exit 0 always.

The prior suite's dispatch-path tests (`test_native_ok_sums_four_buckets`,
`test_route_mutation_error_partial`, `test_route_mutation_runtime_error`,
`test_refused_op_writes_failure_record`, and the rest) are deliberately NOT
carried forward — they asserted the behaviour of a call site that no longer
exists, and a green test over a deleted dispatch is worse than no test.

Spec backlink: pln-strang-11-b8-session-init-boot-f78455 § C2 / AC7
→ docs/research/2026-08-26-the-ceremony-budget-is-spent-on-one-git-status.md
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

_SWEEP_BOOT_PATH = os.path.join(SCRIPT_DIR, "sweep-boot.py")

#: The op this file exists to prove is no longer dialled.
KILLED_OP = "session.boot_sweep"


def _source() -> str:
    with open(_SWEEP_BOOT_PATH, encoding="utf-8") as fh:
        return fh.read()


def _load_sweep_boot():
    """Import sweep-boot.py as a fresh module object (hyphenated -> importlib)."""
    spec = importlib.util.spec_from_file_location("sweep_boot_under_test", _SWEEP_BOOT_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def _run_main_capturing(mod, argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = mod.main(argv)
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# The two contracts the SessionStart hook depends on.
# ---------------------------------------------------------------------------


def test_stdout_is_one_integer():
    """Byte-parity with the retired bash oracle: exactly one integer, nothing
    else. The hook parses this."""
    mod = _load_sweep_boot()
    _rc, out, _err = _run_main_capturing(mod, [])
    assert out.strip() == "0", f"expected a bare integer, got {out!r}"
    assert re.fullmatch(r"\d+\s*", out), f"stdout must be one integer: {out!r}"


def test_exit_is_always_zero():
    """Best-effort ceremony: this never blocks session boot, and a gravestone
    has even less standing to than the dispatch did."""
    mod = _load_sweep_boot()
    rc, _out, _err = _run_main_capturing(mod, [])
    assert rc == 0


def test_no_warning_on_stderr():
    """The old refusal path WARNed on every boot about an op that cannot come
    back. Silence is the fix: a dead op refusing is not news."""
    mod = _load_sweep_boot()
    _rc, _out, err = _run_main_capturing(mod, [])
    assert err.strip() == "", f"gravestone must be silent, got {err!r}"


# ---------------------------------------------------------------------------
# The absence this file exists to protect.
# ---------------------------------------------------------------------------


def test_the_killed_op_is_never_dispatched():
    """The whole point. A future edit that repoints this trampoline at any op
    -- the killed one or a replacement -- must fail here and go write a plan
    instead: the kill bar says a new boot sweep is a fresh spike, never a
    repoint of this file."""
    src = _source()
    dispatching = [
        ln for ln in src.splitlines()
        if "route_mutation(" in ln and not ln.lstrip().startswith("#")
    ]
    assert not dispatching, f"sweep-boot.py must not dispatch: {dispatching}"


def test_killed_op_name_survives_only_as_prose():
    """The name may appear in the gravestone's explanation -- it must not
    appear as a live `_OP`-style dispatch target."""
    src = _source()
    live = [
        ln for ln in src.splitlines()
        if KILLED_OP in ln
        and not ln.lstrip().startswith("#")
        and not ln.lstrip().startswith('"')
        and "=" in ln
        and "_OP" in ln
    ]
    assert not live, f"{KILLED_OP} is still a live dispatch target: {live}"


def test_writes_no_housekeeping_failure_record():
    """Recording a failure every boot, for a dead op, is what made the real
    housekeeping signal unreadable."""
    src = _source()
    calls = [
        ln for ln in src.splitlines()
        if "_record_housekeeping_failure(" in ln
        and not ln.lstrip().startswith("#")
        and not ln.lstrip().startswith("def ")
    ]
    assert not calls, f"gravestone must record no failure: {calls}"


def test_names_its_successors():
    """The kill bar requires naming the requirement, not assuming it. If the
    successor list ever stops being written down here, the next reader cannot
    tell a discharged requirement from a dropped one."""
    src = _source()
    for successor in ("handoff.archive_transition", "fleet.archive_completed_handoffs"):
        assert successor in src, f"gravestone must name {successor}"


# ---------------------------------------------------------------------------
# Invariants carried forward unchanged from the prior suite.
# ---------------------------------------------------------------------------


def test_no_stage_commit_in_source():
    """Never stages, never commits — the bash oracle's invariant, and now
    trivially true."""
    src = _source()
    for forbidden in ("git add", "git commit", '"add"', '"commit"'):
        offenders = [
            ln for ln in src.splitlines()
            if forbidden in ln and not ln.lstrip().startswith("#") and '"""' not in ln
        ]
        assert not offenders, f"{forbidden!r} present: {offenders}"


def test_help_still_exits_cleanly():
    """argv handling is shared plumbing (`sweep_argv.parse_repo_root_argv`) and
    is not part of what the gravestone retired."""
    mod = _load_sweep_boot()
    rc, _out, _err = _run_main_capturing(mod, ["-h"])
    assert rc == 0
