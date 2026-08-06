"""coordinator_core.bash_guards.tests.guard_message_capture -- direct
per-guard capture seam over `dispatch.GuardEntry.fn`.

Spec backlink: docs/plans/2026-08-02-guard-message-size-discipline.md,
chunk C1, § Problem's "Correction: the capture seam must attribute bytes,
and cannot short-circuit."

Why this seam exists and not a sibling of `dispatch._decision`:
`dispatch.evaluate_payload_json`'s loop returns the first non-`None`
envelope with neither `entry.name` nor `entry.band` attached, and the loop
short-circuits -- on any input where a `CONFINEMENT_DENY` guard fires,
every `ADVISORY_REWRITE` guard registered after it in the chain is never
evaluated, so its message bytes are unmeasurable for that input (verified
live: `git commit --no-verify -m "msg"` fires both `no-verify`
(CONFINEMENT_DENY, deny) and `git-commit-safe-commit-advise`
(ADVISORY_REWRITE, allow+advisory) -- `evaluate_payload_json` would only
ever surface the first). This module invokes every `GuardEntry.fn` in the
chain unconditionally, attributing `(guard_name, band, envelope)` per cell,
modeled on `_guard_coverage.py`'s "invoke the shipped function directly"
shape (`MEASURERS`/`measure_all`), not on `evaluate_payload_json`'s
first-non-None-wins loop.

Negative spec: this module does not change, wrap, or reimplement
`dispatch.evaluate_payload_json` or `dispatch._decision` -- both stay
untouched, and every existing caller of either is unaffected. It does not
classify prose bytes, exempt spans, or bands into a corpus (C2/C3); it only
captures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from coordinator_core.bash_guards import dispatch
from coordinator_core.bash_guards._platform_verdict import (
    resolve_host_is_windows as _resolve_host_is_windows_public,
)


@dataclass(frozen=True)
class GuardCapture:
    """One `(guard_name, band, envelope)` cell -- the return shape of
    `capture_all_guards`. `envelope` is `None` for a non-firing guard on
    this input, exactly the value `GuardEntry.fn()` itself returned; this
    module performs no None-filtering, since a `None` cell is itself part
    of what C3's corpus (and C5's "non-triggering cells too" requirement)
    needs to see."""

    name: str
    band: dispatch.GuardBand
    envelope: Optional[Dict[str, Any]]


def capture_all_guards(
    cmd: str,
    session_id: str,
    cwd: str,
    payload: Dict[str, Any],
    *,
    policy_file: Optional[str] = None,
    host_is_windows: Optional[bool] = None,
) -> List[GuardCapture]:
    """Invoke every `GuardEntry.fn` in `_build_guard_chain`'s output for one
    `(cmd, session_id, cwd, payload)` fixture, unconditionally -- no
    short-circuit on the first non-`None` result, unlike
    `evaluate_payload_json`'s loop.

    `host_is_windows` is pinned explicitly per call (AC15's per-cell
    determinism requirement) via `_resolve_host_is_windows_public`, the
    same resolution `evaluate_payload_json` itself performs
    (`dispatch.py:711`) -- passing `None` here resolves against the real
    host, exactly like production; a caller measuring both host legs
    (AC15) passes `True`/`False` explicitly per call.
    """
    effective_host_is_windows = _resolve_host_is_windows_public(host_is_windows)
    chain = dispatch._build_guard_chain(
        cmd=cmd,
        session_id=session_id,
        cwd=cwd,
        payload=payload,
        policy_file=policy_file,
        host_is_windows=effective_host_is_windows,
    )
    return [GuardCapture(name=entry.name, band=entry.band, envelope=entry.fn()) for entry in chain]


def capture_one_guard(
    guard_name: str,
    cmd: str,
    session_id: str,
    cwd: str,
    payload: Dict[str, Any],
    *,
    policy_file: Optional[str] = None,
    host_is_windows: Optional[bool] = None,
) -> GuardCapture:
    """Convenience wrapper over `capture_all_guards` for a caller (e.g. C3's
    corpus rows) that only wants one named cell without re-deriving the
    lookup-by-name each time. Raises `KeyError` if `guard_name` is not a
    registered entry in this call's chain -- fails loud rather than
    returning a manufactured `None` cell for a typo'd name."""
    captures = capture_all_guards(
        cmd,
        session_id,
        cwd,
        payload,
        policy_file=policy_file,
        host_is_windows=host_is_windows,
    )
    by_name = {c.name: c for c in captures}
    return by_name[guard_name]


# ---------------------------------------------------------------------------
# Self-test surface (C1's own AC1/AC15 proof). Deliberately kept in this same
# file rather than a sibling `test_guard_message_capture.py` -- this chunk's
# dispatch stub pins exactly one surface to write. Not auto-discovered by the
# suite's `python_files = ["test_*.py"]` glob (pyproject.toml); run directly
# via `pytest coordinator_core/bash_guards/tests/guard_message_capture.py`,
# which pytest collects fine given an explicit path. Downstream chunks (C3's
# corpus) import `capture_all_guards`/`capture_one_guard` into their own
# `test_*.py` modules, which is how this seam re-enters the auto-discovered
# suite for good.
# ---------------------------------------------------------------------------


def _bash_payload(command: str, session_id: str) -> Dict[str, Any]:
    return {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": session_id,
        "cwd": None,
    }


def test_seam_captures_a_firing_guard_with_name_band_envelope():
    """`no-verify` (CONFINEMENT_DENY) fires on a `--no-verify` commit and the
    seam attributes name/band alongside the envelope -- attribution
    `evaluate_payload_json`'s bare-envelope return never carries."""
    cmd = 'git commit --no-verify -m "msg"'
    sid = "guard-message-capture-firing"
    capture = capture_one_guard(
        "no-verify",
        cmd,
        sid,
        "/tmp",
        _bash_payload(cmd, sid),
        host_is_windows=False,
    )
    assert capture.name == "no-verify"
    assert capture.band == dispatch.GuardBand.CONFINEMENT_DENY
    assert capture.envelope is not None
    hso = capture.envelope["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "no-verify" not in hso  # sanity: this is the envelope, not a wrapper


def test_seam_captures_a_non_firing_guard_as_none_envelope():
    """A harmless command leaves `no-verify` silent -- the seam still
    returns a `(name, band, envelope)` triple, with `envelope is None`,
    rather than omitting the cell."""
    cmd = "git status"
    sid = "guard-message-capture-nonfiring"
    capture = capture_one_guard(
        "no-verify",
        cmd,
        sid,
        "/tmp",
        _bash_payload(cmd, sid),
        host_is_windows=False,
    )
    assert capture.name == "no-verify"
    assert capture.band == dispatch.GuardBand.CONFINEMENT_DENY
    assert capture.envelope is None


def test_seam_captures_two_different_band_guards_on_the_same_input_no_short_circuit():
    """The no-short-circuit proof this chunk exists for: `git commit
    --no-verify -m "msg"` fires BOTH `no-verify` (CONFINEMENT_DENY, deny)
    and `git-commit-safe-commit-advise` (ADVISORY_REWRITE, allow+advisory)
    -- verified live against this tree before writing this assertion.
    `evaluate_payload_json`'s first-non-None-wins loop would only ever
    surface `no-verify` for this input, since it is registered first; this
    seam must surface both."""
    cmd = 'git commit --no-verify -m "msg"'
    sid = "guard-message-capture-no-short-circuit"
    captures = capture_all_guards(
        cmd,
        sid,
        "/tmp",
        _bash_payload(cmd, sid),
        host_is_windows=False,
    )
    by_name = {c.name: c for c in captures}

    deny = by_name["no-verify"]
    assert deny.band == dispatch.GuardBand.CONFINEMENT_DENY
    assert deny.envelope is not None
    assert deny.envelope["hookSpecificOutput"]["permissionDecision"] == "deny"

    advisory = by_name["git-commit-safe-commit-advise"]
    assert advisory.band == dispatch.GuardBand.ADVISORY_REWRITE
    assert advisory.envelope is not None
    assert advisory.envelope["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "additionalContext" in advisory.envelope["hookSpecificOutput"]

    # The two bands actually differ -- otherwise this would not be a
    # different-band proof, just two guards in the same band.
    assert deny.band != advisory.band


def test_seam_pins_host_is_windows_explicitly_per_cell():
    """AC15: the seam's `host_is_windows` kwarg is honored per call, not
    re-resolved from the real host -- the same platform-conditioned guard
    fires differently depending on the pinned value, proving the pin is
    live rather than ignored."""
    # Seam-confirmed multiprobe shape (every segment a recognized single-
    # process-rewritable form) -- same fixture literal as
    # `test_guard_multiprobe_banner.py`'s own `_BANNER_CMD_CONFIRMED`, whose
    # docstring there records this exact command as the deny/advise-split
    # fixture: DENY on the Windows leg, allow+advisory on the non-Windows
    # leg for the identical command.
    cmd = 'echo "=== facts ==="; pwd; whoami; git status; git rev-parse HEAD'
    sid_win = "guard-message-capture-host-windows"
    sid_mac = "guard-message-capture-host-mac"
    win_capture = capture_one_guard(
        "multiprobe-banner",
        cmd,
        sid_win,
        "/tmp",
        _bash_payload(cmd, sid_win),
        host_is_windows=True,
    )
    mac_capture = capture_one_guard(
        "multiprobe-banner",
        cmd,
        sid_mac,
        "/tmp",
        _bash_payload(cmd, sid_mac),
        host_is_windows=False,
    )
    assert win_capture.band == dispatch.GuardBand.PLATFORM_CONDITIONED_DENY
    assert mac_capture.band == dispatch.GuardBand.PLATFORM_CONDITIONED_DENY
    # Windows leg denies; non-Windows leg is suppressed to an
    # advisory-shaped or None envelope -- either way, the two legs must not
    # be identical, proving the pin actually reached the guard.
    win_decision = (win_capture.envelope or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    mac_decision = (mac_capture.envelope or {}).get("hookSpecificOutput", {}).get("permissionDecision")
    assert win_decision == "deny"
    assert win_decision != mac_decision
