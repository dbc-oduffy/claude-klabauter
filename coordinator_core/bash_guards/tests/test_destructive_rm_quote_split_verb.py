"""A quote-split `rm` verb must not walk past the destructive-rm guard.

Purpose: `check_destructive_rm` gated on `\\brm\\b` over the RAW command, and
stripped the verb from its target list with a regex needing a literal `rm`
token. The shell resolves `'r''m'` to `rm`, so both missed while the command
still deleted:

    rm -rf <path>       -> denied
    'r''m' -rf <path>   -> ALLOWED, measured 2026-08-29, same path, same
                           uncommitted work underneath

Found while closing the identical shape in the doctrine-surface guard's
governed-identifier prefilter: a correct analyzer gated behind a raw-text probe
that shell quoting walks straight past. This module's own `_rm_is_rm_segment`
already read segments quote-stripped -- the gate in FRONT of it did not, so the
detector and its own fast path disagreed about what an rm command looks like.

Negative-spec: this file does NOT re-cover what `rm` does once recognised
(target resolution, override handling, peer-claim reporting all have their own
tests). It covers exactly one axis: whether the verb is RECOGNISED when spelled
through quotes. The ALLOW cases are the load-bearing other half -- a fix for an
under-denial must not become an over-denial, and the widened scan can only
decide whether to LOOK.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.bash_guards import dispatch_checks

CWD = str(Path(__file__).resolve().parents[3])
TARGET = "state/handoffs"

SPLIT_VERB_CASES = [
    ("plain, the control", f"rm -rf {TARGET}"),
    ("single-quote adjacency", f"'r''m' -rf {TARGET}"),
    ("double-quote adjacency", f'"r""m" -rf {TARGET}'),
    ("mixed quote styles", f"'r'\"m\" -rf {TARGET}"),
    ("quoted whole verb", f"'rm' -rf {TARGET}"),
    ("split with a wrapper in front", f"sudo 'r''m' -rf {TARGET}"),
]

ALLOWED_CASES = [
    ("absent target", "rm -rf /tmp/definitely-not-here-12345"),
    ("not a delete at all", f"ls -la {TARGET}"),
    ("words that merely contain the letters", "echo 'form' 'perms' > /tmp/x"),
    ("rm as prose in a commit message", 'git commit -m "drop the rm wrapper"'),
]


def _verdict(cmd: str):
    return dispatch_checks.check_destructive_rm(
        cmd, "test-session", {"cwd": CWD, "tool_input": {"command": cmd}}
    )


@pytest.mark.parametrize("label,cmd", SPLIT_VERB_CASES, ids=[c[0] for c in SPLIT_VERB_CASES])
def test_a_quote_split_rm_verb_is_still_recognised(label: str, cmd: str) -> None:
    """Every spelling the shell resolves to `rm` must reach the guard's own
    target analysis, not be dropped by the fast path in front of it.

    CALIBRATED AGAINST THE PLAIN SPELLING, never asserted absolutely: this
    guard denies only when the target carries uncommitted work, which is a
    property of the tree the suite happens to run on. If the plain `rm` does
    not deny here, the tree is clean and this oracle cannot discriminate --
    that is a SKIP, because asserting anyway would pass for the wrong reason
    on a dirty tree and fail for the wrong reason on a clean one."""
    control = _verdict(f"rm -rf {TARGET}")
    if control is None:
        pytest.skip(
            f"{TARGET} carries no uncommitted work in this tree, so the plain "
            "spelling does not deny either -- nothing for the split spelling "
            "to be compared against"
        )
    assert _verdict(cmd) is not None, (
        f"{label}: a quote-split rm verb walked past the destructive-rm guard "
        f"while the plain spelling was denied -- {cmd!r}"
    )


@pytest.mark.parametrize("label,cmd", ALLOWED_CASES, ids=[c[0] for c in ALLOWED_CASES])
def test_the_widened_scan_does_not_over_deny(label: str, cmd: str) -> None:
    assert _verdict(cmd) is None, (
        f"{label}: the widened rm scan denied a command it should not -- {cmd!r}"
    )


def test_the_detector_and_its_own_fast_path_agree() -> None:
    split = f"'r''m' -rf {TARGET}"
    assert dispatch_checks._rm_is_rm_segment(split) is True
    if _verdict(f"rm -rf {TARGET}") is None:
        pytest.skip(f"{TARGET} is clean in this tree; the end-to-end half cannot discriminate")
    assert _verdict(split) is not None
