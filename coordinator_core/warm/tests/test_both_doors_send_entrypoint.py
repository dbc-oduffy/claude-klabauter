"""Both door implementations put `entrypoint` on the WARM request, inside
`params` -- the cross-leg parity `door.c` alone cannot establish.

WHAT REGRESSED, AND WHY THIS FILE IS SOURCE-LEVEL RATHER THAN BEHAVIOURAL.
C0 gave the door a second name-aware leg: `fall_through` resolves
`coordinator/bin/<own basename>.py` COLD, and the warm request carries the
same basename as `params["entrypoint"]` so the server dispatches that CLI
instead of `coordinator-invoke`'s own `main()`. `door.c` got both halves.
`door_posix.c` got only the cold one -- its request builder sent argv, cwd
and stdin and stopped -- so on POSIX every image hardlinked under a
non-default name mis-dispatched to the `coordinator_core.invoke` grammar
for as long as a warm server was up, which on this box is always. The cold
leg being correct is exactly what hid it: every per-leg test on either side
passed, and the single-name install that shipped first never exercised the
difference. It surfaced the day 372 names were hardlinked at once.

That is a PARITY defect, not a behavioural one on either platform alone:
`door.c` is unbuildable and unrunnable here, so no test that executes a
door can compare the two legs on the machine where the divergence appears.
Reading the two sources is the only instrument that covers both, and the
property it asserts -- the field is emitted, and emitted before `params`
closes -- is the exact pair of facts whose absence produced both bugs in
this family (the 2026-08-27 envelope-level misplacement, and this one).
"""

from __future__ import annotations

from pathlib import Path

import pytest

_DOOR_DIR = Path(__file__).resolve().parents[1] / "door"

#: The append that closes `params` and opens the transport envelope. Both
#: sources spell it identically, and everything appended before it is an op
#: argument while everything after is envelope metadata the server pops.
_PARAMS_CLOSE = r'},\"_engine_token\":\"'

#: The `entrypoint` field's own append. Both sources spell this identically
#: too -- the wide-string half of `door.c` is in the VALUE it escapes, never
#: in this key literal.
_ENTRYPOINT_KEY = r',\"entrypoint\":\"'


@pytest.mark.parametrize("source_name", ["door.c", "door_posix.c"])
def test_the_warm_request_carries_entrypoint_inside_params(source_name):
    """Present at all, and appended BEFORE `params` closes.

    Ordering is half the property, not a nicety: `ops/invoke_from_argv.py ::
    _invoke_from_argv` reads `params["entrypoint"]`, so the same field one
    level up in the envelope is silently unread -- the shape that shipped in
    `door.c` until 2026-08-27 and dispatched a renamed image under
    `coordinator-invoke`'s argument grammar while returning exit 0."""
    text = (_DOOR_DIR / source_name).read_text(encoding="utf-8")

    assert _ENTRYPOINT_KEY in text, (
        f"{source_name} never appends an `entrypoint` field to the warm "
        "request. Its cold leg may still be name-aware -- that is what made "
        "this invisible on POSIX -- but every image hardlinked under a "
        "non-default name will mis-dispatch to the coordinator-invoke "
        "grammar for as long as a warm server is up."
    )
    assert text.index(_ENTRYPOINT_KEY) < text.index(_PARAMS_CLOSE), (
        f"{source_name} appends `entrypoint` AFTER the `}}` that closes "
        "`params`, putting it at the envelope top level where "
        "`_invoke_from_argv` cannot see it."
    )


@pytest.mark.parametrize("source_name", ["door.c", "door_posix.c"])
def test_the_default_name_still_omits_entrypoint(source_name):
    """BACKWARD COMPATIBILITY IS AN AC. A door installed under the default
    name must produce the request the server already handled, byte for byte,
    so the field is emitted only when this image's resolved basename differs
    from `coordinator-invoke` -- which means the append is guarded by a
    comparison against the default-name constant."""
    text = (_DOOR_DIR / source_name).read_text(encoding="utf-8")
    guard_region = text[: text.index(_ENTRYPOINT_KEY)]
    tail = guard_region[-600:]

    assert "DOOR_DEFAULT_ENTRYPOINT" in tail, (
        f"{source_name} appends `entrypoint` with no visible comparison "
        "against DOOR_DEFAULT_ENTRYPOINT immediately above it -- a door "
        "under the default name would then send a field the pre-C0 server "
        "never received."
    )
