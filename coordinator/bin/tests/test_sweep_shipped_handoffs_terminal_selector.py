"""test_sweep_shipped_handoffs_terminal_selector.py — regression suite for
sweep-shipped-handoffs.py's terminal `deployment_state` archive-candidate
selector.

Pins the DR-084 fix: the selector used to be a hardcoded pre-DR-084 tuple
`("shipped", "abandoned", "superseded")`, which silently skipped handoffs
correctly migrated to `deployment_state: closed` or `continued` — they
never became archive candidates and were retained forever. The selector
now derives from `coordinator_core.lifecycle_constants.
HANDOFF_TERMINAL_DEPLOYMENT` (shipped, abandoned, continued, closed) —
the same SSOT `fleet/_common.py` and `reconcile/gate_eval.py` consume —
so a future vocabulary widening there is picked up here automatically
instead of silently stranding handoffs again.

Exercises the pure selector helpers directly (`_terminal_deployment_states`,
`_is_archive_candidate`) rather than invoking the fleet archival op —
`sweep-shipped-handoffs.py` only finds+dispatches candidates, it does not
implement archival itself, so the op is out of scope for this suite.

Spec backlink: docs/plans/2026-07-22-handoff-lifecycle-vocabulary-overhaul-scope.md § C3
Wraps: coordinator_core.lifecycle_constants.HANDOFF_TERMINAL_DEPLOYMENT
"""
from __future__ import annotations

import importlib.util
import os
import subprocess

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"], cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True, text=True, check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "sweep-shipped-handoffs.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("sweep_shipped_handoffs", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


def test_terminal_states_rejects_every_non_member_token():
    """Review: code-reviewer — the prior version of this test asserted
    `_terminal_deployment_states() == HANDOFF_TERMINAL_DEPLOYMENT`, but
    `_terminal_deployment_states()` is a pure passthrough that imports and
    returns that exact constant unmodified — the assertion was true by
    construction and proved only that the import succeeded, not any
    independent property. Replaced with negative-space coverage: every
    token NOT in HANDOFF_TERMINAL_DEPLOYMENT must be rejected by the
    selector, so a future accidental widening (or narrowing) of the
    selector's own set — independent of what the constant says — fails
    loudly here.
    """
    from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT

    terminal_states = _mod._terminal_deployment_states()
    non_members = {
        "ready_to_fire",
        "in_flight",
        "awaiting_gate",
        "superseded",
        "open",
        "deferred",
        "",
        "SHIPPED",
    } - HANDOFF_TERMINAL_DEPLOYMENT
    for token in non_members:
        assert _mod._is_archive_candidate(token, terminal_states) is False, (
            f"selector incorrectly treats {token!r} as terminal — it is not a member of "
            "HANDOFF_TERMINAL_DEPLOYMENT"
        )


def test_closed_deployment_state_is_selected():
    """LOAD-BEARING: this is the case that fails against the pre-fix
    hardcoded tuple `("shipped", "abandoned", "superseded")` — "closed" is
    not a member of that tuple, so a migrated handoff was silently skipped.
    """
    terminal_states = _mod._terminal_deployment_states()
    assert _mod._is_archive_candidate("closed", terminal_states) is True


def test_continued_deployment_state_is_selected():
    terminal_states = _mod._terminal_deployment_states()
    assert _mod._is_archive_candidate("continued", terminal_states) is True


def test_shipped_and_abandoned_still_selected():
    terminal_states = _mod._terminal_deployment_states()
    assert _mod._is_archive_candidate("shipped", terminal_states) is True
    assert _mod._is_archive_candidate("abandoned", terminal_states) is True


def test_non_terminal_deployment_state_not_selected():
    terminal_states = _mod._terminal_deployment_states()
    assert _mod._is_archive_candidate("ready_to_fire", terminal_states) is False
    assert _mod._is_archive_candidate("in_flight", terminal_states) is False


# The pre-DR-084 selector this fix replaced — verbatim, as a named
# regression witness. Sourced from the tuple literal `("shipped",
# "abandoned", "superseded")` that lived inline in `main()` before this
# fix; kept here (not imported from the target module, which no longer
# has it) so the test file itself documents the historical bug shape.
_PRE_DR084_SELECTOR = frozenset({"shipped", "abandoned", "superseded"})


def test_pre_dr084_selector_stranded_closed_and_continued():
    """Regression witness: the OLD selector did not contain "closed" or
    "continued" — this is the exact stranding bug the fix corrects.
    Asserting against a fixed historical constant (not the SSOT the fix
    imports) means this test cannot pass for the wrong reason the way a
    check sourced from the same constant it's verifying against would.
    """
    assert not _mod._is_archive_candidate("closed", _PRE_DR084_SELECTOR)
    assert not _mod._is_archive_candidate("continued", _PRE_DR084_SELECTOR)


def test_superseded_deliberately_not_selected():
    """`superseded` lives on the status axis (HANDOFF_TERMINAL_STATUS), not
    deployment_state — see lifecycle_constants.py. A disk scan of
    state/handoffs/ and archive/handoffs/ at fix time found no handoff
    carrying `deployment_state: superseded`, and every other
    HANDOFF_TERMINAL_DEPLOYMENT consumer reads the bare constant with no
    such union — pinning that this selector does not silently reintroduce
    the pre-DR-084 status/deployment_state axis confusion.
    """
    terminal_states = _mod._terminal_deployment_states()
    assert _mod._is_archive_candidate("superseded", terminal_states) is False
