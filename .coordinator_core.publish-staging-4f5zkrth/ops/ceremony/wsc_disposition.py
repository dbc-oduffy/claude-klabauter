"""
coordinator_core.ops.ceremony.wsc_disposition

Single source of truth for the WSC ("workstream-complete") consumed-handoff
disposition set: SINGLE_SESSION, PREDECESSOR_CONSUMED, and MEMO_PREDECESSOR.

Spec backlink: chunk C1, pln-wsc-completeness-gate-pickup-s-9793ca.
MEMO_PREDECESSOR was added by chunk C1,
docs/plans/2026-08-05-memo-predecessor-representable-outcome.md, following the
DR-084 additive-enum-only precedent already cited below: widen the recognised
set BEFORE migrating consumers, never narrow.

This is a permanent read-side alias with a write-side-only flip, following the
DR-084 dual-vocabulary precedent (coordinator_core/lifecycle_constants.py:12-22):
DR-084's standing lesson is that narrowing/retiring old-term recognition before
every consumer corpus has migrated breaks callers — the exact failure the
reverted 9d00b459 change made. `canonicalize()` here NEVER narrows what is
recognised: both the legacy spelling ("chain-terminal") and the canonical
spelling ("predecessor-consumed") are accepted on read, permanently. Only
`WRITE_TOKEN` -- which spelling this package itself emits going forward -- may
change, and that flip (C6) is a one-line edit to this module, not a second
sweep of every read site.

Negative-spec: do NOT add a narrowing step that stops recognising the legacy
spelling on read. Do NOT infer VALID/canonicalize's behaviour from the write
token's current binding -- WRITE_TOKEN governs emission only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

SINGLE_SESSION = "single-session"
PREDECESSOR_CONSUMED = "predecessor-consumed"

# The pre-rename spelling of PREDECESSOR_CONSUMED. Permanently recognised on
# read (see module docstring) even after WRITE_TOKEN flips away from it.
LEGACY_PREDECESSOR_CONSUMED = "chain-terminal"

# A memo-attributed predecessor consume -- distinct from PREDECESSOR_CONSUMED
# (which is Detector-chain-attributed). This value is new and has no history:
# it never enters the dual-vocabulary read-alias machinery above and gets no
# legacy alias of its own.
MEMO_PREDECESSOR = "memo-predecessor"

VALID = frozenset(
    {SINGLE_SESSION, PREDECESSOR_CONSUMED, LEGACY_PREDECESSOR_CONSUMED, MEMO_PREDECESSOR}
)

# Flipped by C6 to the canonical spelling. The legacy spelling stays
# permanently recognised on read via canonicalize() -- see module docstring.
# Governs emission of the PREDECESSOR_CONSUMED concept only -- MEMO_PREDECESSOR
# is a distinct disposition selector, not a spelling of this token.
WRITE_TOKEN = PREDECESSOR_CONSUMED

_CANONICAL_MAP = {
    SINGLE_SESSION: SINGLE_SESSION,
    PREDECESSOR_CONSUMED: PREDECESSOR_CONSUMED,
    LEGACY_PREDECESSOR_CONSUMED: PREDECESSOR_CONSUMED,
    MEMO_PREDECESSOR: MEMO_PREDECESSOR,
}


def canonicalize(value: str) -> str:
    """Map a disposition token (legacy or canonical spelling) to its canonical form.

    Accepts SINGLE_SESSION unchanged, and maps both PREDECESSOR_CONSUMED and
    LEGACY_PREDECESSOR_CONSUMED ("chain-terminal") to PREDECESSOR_CONSUMED --
    a receipt written before the WRITE_TOKEN flip still resolves. Accepts
    MEMO_PREDECESSOR unchanged (no legacy alias). Rejects any value outside
    VALID.
    """
    try:
        return _CANONICAL_MAP[value]
    except KeyError:
        raise ValueError(
            f"unknown WSC disposition token: {value!r} (expected one of {sorted(VALID)})"
        ) from None


# ---------------------------------------------------------------------------
# ESCALATE-ONLY env override — shared by every WSC disposition resolver
# ---------------------------------------------------------------------------
#
# Spec backlink: cross-repo memo (project-makima commit 1b07cded) fixed a
# break-class defect where every WSC_DISPOSITION/WSC_CONSUMED_HANDOFF remedy
# string told the operator to export the override, but resolve_disposition
# (coordinator/bin/wsc-session-disposition.py) never read either variable
# back — an inert remedy. That fix established the override contract this
# module now centralises for every OTHER resolver in the WSC disposition
# family (coordinator_core.ops.ceremony.branch_resolution's Branch 1), so a
# second independently-maintained copy cannot drift the same way again.
#
# The override is deliberately ESCALATE-ONLY -- a review-coverage gate an
# env var can waive is not a gate:
#   - A positive value (canonical PREDECESSOR_CONSUMED, or the permanently-
#     recognised legacy alias LEGACY_PREDECESSOR_CONSUMED; case-insensitive,
#     whitespace-stripped) wins outright, ahead of every detector.
#   - WSC_DISPOSITION=SINGLE_SESSION is REFUSED as a downgrade -- a
#     positively-detected consume is never suppressed by an env var. The
#     detector chain runs normally with a WARN explaining the refusal.
#   - An unrecognised non-empty value is ignored with a WARN naming the
#     accepted values. Never fails hard -- this is a ceremony pre-step.
#   - WSC_CONSUMED_HANDOFF set without a positive WSC_DISPOSITION does not
#     flip disposition -- NOTE and ignore.
#
# Negative-spec: do NOT add a path where the override can suppress or
# downgrade a disposition the detector chain would otherwise reach -- the
# override may only ever cause MORE gates to run, never fewer.


@dataclass(frozen=True)
class EnvOverrideResult:
    """Result of resolving the WSC_DISPOSITION/WSC_CONSUMED_HANDOFF escalate-only
    env override, independent of any particular resolver's detector chain.

    ``escalate`` is True only for a positively-recognised override (canonical
    or legacy-alias spelling) -- callers short-circuit their whole detector
    chain and adopt ``disposition``/``consumed_handoff_raw`` directly. When
    False, callers must run their detector chain exactly as if no override
    were present; ``diagnostics`` (WARN/NOTE, non-empty only for a refused
    downgrade, an unrecognised value, or a handoff-without-disposition) is
    still worth surfacing in the caller's own evidence receipt.
    """

    escalate: bool
    disposition: str
    consumed_handoff_raw: str
    diagnostics: tuple[str, ...]


def resolve_env_override(env: Optional[Mapping[str, str]] = None) -> EnvOverrideResult:
    """Resolve the escalate-only WSC_DISPOSITION/WSC_CONSUMED_HANDOFF env
    override. See the module-level "ESCALATE-ONLY env override" section
    above for the full contract; every branch below is one bullet there.

    Reuses ``canonicalize()`` for legacy-alias handling rather than
    re-hardcoding the accepted-value list, per this module's own
    negative-spec on narrowing what canonicalize() recognises.
    """
    if env is None:
        env = os.environ

    override = (env.get("WSC_DISPOSITION") or "").strip()
    handoff_raw = (env.get("WSC_CONSUMED_HANDOFF") or "").strip()

    if override:
        try:
            canon = canonicalize(override.lower())
        except ValueError:
            return EnvOverrideResult(
                escalate=False,
                disposition="",
                consumed_handoff_raw="",
                diagnostics=(
                    f"WARN: unrecognised WSC_DISPOSITION={override!r} — accepted values "
                    f"are {PREDECESSOR_CONSUMED} (canonical) or "
                    f"{LEGACY_PREDECESSOR_CONSUMED} (legacy alias). Ignoring and running "
                    "the detector chain normally.",
                ),
            )
        if canon == PREDECESSOR_CONSUMED:
            return EnvOverrideResult(
                escalate=True,
                disposition=WRITE_TOKEN,
                consumed_handoff_raw=handoff_raw,
                diagnostics=(
                    f"NOTE: disposition resolved via WSC_DISPOSITION={override!r} "
                    "override — escalate-only, takes precedence ahead of the whole "
                    "detector chain.",
                ),
            )
        # canon == SINGLE_SESSION — refused as a downgrade.
        return EnvOverrideResult(
            escalate=False,
            disposition="",
            consumed_handoff_raw="",
            diagnostics=(
                "WARN: WSC_DISPOSITION=single-session cannot downgrade a "
                "positively-detected consume — this override only escalates, never "
                "suppresses. Running the detector chain normally.",
            ),
        )

    if handoff_raw:
        return EnvOverrideResult(
            escalate=False,
            disposition="",
            consumed_handoff_raw="",
            diagnostics=(
                "NOTE: WSC_CONSUMED_HANDOFF is set without WSC_DISPOSITION — ignored; "
                "it alone cannot flip disposition.",
            ),
        )

    return EnvOverrideResult(escalate=False, disposition="", consumed_handoff_raw="", diagnostics=())


def normalize_override_handoff(repo_root: Path, raw: str, diagnostics: list[str]) -> str:
    """Normalise a raw ``WSC_CONSUMED_HANDOFF`` override value to a
    repo-relative path (whitespace already stripped by the caller). NOTEs
    (never fails, appended to ``diagnostics`` in place) when the resulting
    path does not exist on disk — e.g. an already-archived handoff, or a
    stale path — worth surfacing but not worth blocking a ceremony over.

    Mirrors coordinator/bin/wsc-session-disposition.py's
    ``_normalize_override_handoff`` (that script is deliberately
    self-contained and cannot import this package — see this module's own
    two-resolver drift test for the mechanical cross-check that keeps the
    two implementations agreeing on the same input matrix).
    """
    if not raw:
        return ""
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(repo_root.resolve()).as_posix()
        except (OSError, ValueError):
            rel = raw
    else:
        rel = raw
    if not (repo_root / rel).exists():
        diagnostics.append(
            f"NOTE: WSC_CONSUMED_HANDOFF={rel} does not exist on disk (e.g. an "
            f"already-archived handoff, or a stale path) — accepted anyway, the override "
            f"always wins."
        )
    return rel
