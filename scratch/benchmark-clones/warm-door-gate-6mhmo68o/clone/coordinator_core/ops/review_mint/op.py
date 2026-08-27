"""
coordinator_core.ops.review_mint.op — JSON-RPC "review.mint_workflow" operation.

Purpose: thin RPC wrapper that mints a gated review ``.mjs`` Workflow script
straight from a plan, so `/review --surface plan` gets one dispatchable
artifact instead of an EM hand-assembling reviewer calls (see plan docstring,
``docs/plans/2026-08-19-review-mints-its-own-gated-workflow.md`` task C3).
It is the ONE place in this plan's surface that touches disk (the write) and
the ONE place that touches the sibling DoE-claude clone at runtime
(``load_fragment()``, below) — every other module in ``review_mint`` (
``roster.py``, ``compose.py``) is pure.

Pipeline: derive a review tier from ``plan_path``'s own sizing citation
(``coordinator_core.ops.dispatch_emit.emit.derive_review_tier`` — reused, not
reimplemented, per the plan's C3 body) -> load and parse the DoE-owned
review-roster fragment (``load_fragment()`` -> ``roster.parse_stages``) ->
compose script text for that tier's stages (``compose.compose``) -> write the
composed ``.mjs`` to a caller-named, path-guarded ``output_path``.

``load_fragment()`` resolves the sibling root via
``coordinator_core.doe_root_pointer.read_doe_root_pointer()`` and joins
``coordinator/contract/review-roster-fragment.json`` onto it — never a
hardcoded cross-repo path (plan Anti-scope "Do not hardcode a cross-repo
absolute path"). ``dispatch.emit`` (C4) does NOT call this function; it keeps
consuming an injected fragment dict (plan F3).

Gate policy: for a ``gate: true`` stage, this op's own ``gate_policy``
closure (``_make_gate_policy``) first checks AC12's freshness signal --
each stage agent's captured ``run_nonce`` field against the nonce this run
minted and injected into that same agent's prompt (``compose()``'s
``run_nonce`` param, threaded from this op) -- and returns a distinctly-
shaped refusal (``{gate_refused, agent, expected_nonce, received_nonce}``)
on a mismatch or absence, BEFORE ever looking at ``verdict``. Only once the
nonce checks out does it check that agent's charter-declared blocking
verdict (the fragment's top-level ``blocking_verdicts`` map) against the
captured ``verdict`` field, returning AC5's exact abort shape
(``{blocking_agent, verdict, reason, sidecar_path}``) the first time one
matches. An agent with no entry (or a ``null`` entry) in
``blocking_verdicts`` contributes no branch at all (AC6) — this op never
defines "blocked" for an agent itself; it only wires the mapping the
fragment already states (plan Anti-scope "Do not define 'blocked' for
coordinator-claude's agents").

AC12: the nonce this op mints (``secrets.token_hex(8)`` -- unguessable, never
a counter or timestamp, see plan) is per-mint, generated once in
``_review_mint_workflow`` (or accepted verbatim via the optional
``run_nonce`` param, e.g. from a test that wants to pin one), threaded to
``compose_review_script`` -> ``compose()`` (prompt injection, gate stages
only) and to ``_make_gate_policy`` (the refusal comparison). This op has no
filesystem access to the sidecar the agent wrote -- the written-this-run half
of the contract is coordinator-claude's charter obligation
(``sidecar-emission-contract.md`` / ``sidecar-frontmatter-contract.md``);
this op's leg is the echo check only. Cross-repo memo:
``cross-repo/inbox/2026-08-19-doe-claude-em-run-nonce-closes-the-gate-freshness-gap.md``.

Wire params:
    plan_path (str, required)     — plan file to derive the review tier from.
    output_path (str, required)   — path to write the emitted ``.mjs`` script
                                     to. Path-guarded under ``target_root``
                                     BEFORE the file is written (mirrors
                                     ``dispatch.emit``'s own guard shape).
    target_root (str, optional)   — explicit containment root; defaults to
                                     ``repo_root`` when the request carries
                                     one, else to ``output_path``'s own
                                     parent directory — same default-
                                     derivation ``dispatch.emit`` uses.
    name (str, optional)          — script ``meta.name``; defaults to
                                     ``plan_path``'s stem.
    description (str, optional)   — script ``meta.description``; defaults to
                                     a fixed generic description.
    run_nonce (str, optional)     — AC12: pins the per-mint freshness nonce
                                     instead of minting a fresh
                                     ``secrets.token_hex(8)`` one; a test
                                     seam, not an expected caller usage.

Reply fields:
    {"path": "<written path>", "ok": bool,
     "findings": [{"severity","code","message","line"?}, ...],
     "error_count": int, "warn_count": int, "tier": "<derived tier>",
     "run_nonce": "<this mint's nonce>"}
    ``ok := error_count == 0`` — the same ``run_checks`` verdict shape
    ``dispatch.emit`` returns. ``run_nonce`` lets the EM correlate this mint
    against whatever verdict eventually comes back (AC12).

Negative-spec:
  - Does NOT emit a commit stage or a test/pytest stage — this op composes
    review stages only (plan Anti-scope; see ``compose.py``'s own negative
    spec).
  - Does NOT reimplement the tshirt->tier mapping — delegates entirely to
    ``dispatch_emit.emit.derive_review_tier``.
  - Does NOT fork the roster consumer — calls ``roster.parse_stages`` and
    ``compose.compose`` directly, the same functions ``dispatch.emit`` (C4)
    calls.

Spec backlink: pln-the-review-skill-mints-its-own-26e933 § C3
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import List, Optional, Tuple

from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops._workflow_contract import Severity, run_checks
from coordinator_core.ops.dispatch_emit.emit import derive_review_tier
from coordinator_core.ops.review_mint.compose import compose
from coordinator_core.ops.review_mint.roster import Stage, parse_stages
from coordinator_core.ops.workflow_scaffold import _js_string_literal

# Generator-provenance: writes the emitted script to a caller-supplied,
# path-guarded output_path -- no fixed target, purely caller-named.
GENERATES = []

_REVIEW_ROSTER_FRAGMENT_RELPATH = "coordinator/contract/review-roster-fragment.json"

# Pre-execution wording -- distinct from dispatch.emit's post-execution
# "Review this plan's completed work." (plan C4 body: neither caller
# inherits the other's prompt).
_REVIEW_PROMPT = "Review this plan before any task in it executes."
_REVIEW_PHASE_TITLE = "Review"


class PathEscapeError(ValueError):
    """Raised when ``output_path`` resolves outside ``target_root``."""


class ReviewTierUndeterminedError(ValueError):
    """Raised when ``plan_path`` carries no derivable review tier.

    ``derive_review_tier`` fails soft (returns ``None``) for a caller that
    can compose no review phase at all -- this op has nothing else to
    compose, so it fails loud instead of writing an empty script.
    """


def load_fragment(repo_root: Optional[Path] = None) -> dict:
    """Resolve the sibling DoE-claude root, read its shipped review-roster
    fragment, and return the parsed dict.

    The ONLY place in this plan's surface that touches the sibling clone at
    runtime (see module docstring). Raises ``FileNotFoundError`` if the
    sibling root does not resolve or the fragment file is absent -- never a
    silently empty/fabricated fragment.
    """
    doe_root = read_doe_root_pointer()
    if not doe_root:
        raise FileNotFoundError(
            "review.mint_workflow could not resolve the DoE-claude sibling "
            "root (read_doe_root_pointer() returned empty) -- cannot load "
            f"{_REVIEW_ROSTER_FRAGMENT_RELPATH}"
        )
    fragment_path = Path(doe_root) / _REVIEW_ROSTER_FRAGMENT_RELPATH
    if not fragment_path.is_file():
        raise FileNotFoundError(
            f"review roster fragment not found at {fragment_path}"
        )
    return json.loads(fragment_path.read_text(encoding="utf-8"))


def _make_gate_policy(blocking_verdicts: dict, run_nonce: str):
    """Build a C2 ``GatePolicy`` closure over ``blocking_verdicts`` (the
    fragment's top-level agent -> charter-verdict-token map) and this run's
    ``run_nonce`` (AC12).

    For each ``(agentType, js_result_expression)`` pair in a gate stage's
    ``results``, an agent with no (or a ``null``) entry contributes no
    branch (AC6) -- unchanged by AC12, since only an agent that CAN block
    has any verdict worth checking freshness on. An agent WITH a non-null
    entry gets TWO sequential ``if`` blocks:

      1. AC12's freshness check, first: if the captured ``.run_nonce``
         does not equal THIS run's nonce (mismatched OR absent -- a plain
         ``===`` catches both, `undefined !== <string>`), a distinctly-
         shaped refusal fires -- never a fall-through to "no signal", and
         never the AC5 abort shape, so a stale ``BLOCKED`` cannot launder
         into a real one.
      2. Only once the nonce checks out: that agent's captured ``.verdict``
         against its own charter token, returning AC5's exact abort shape
         on a match.

    Multiple blocking agents in one stage compose multiple sequential pairs,
    first match (of either kind) wins. Returns ``""`` (disarmed) when no
    agent in the stage can block.
    """

    def gate_policy(stage: Stage, index: int, results: List[Tuple[str, str]]) -> str:
        branches = []
        for agent, var in results:
            token = blocking_verdicts.get(agent)
            if token is None:
                continue
            branches.append(
                f"  if ({var}.run_nonce !== {_js_string_literal(run_nonce)}) {{\n"
                f"    return {{ gate_refused: 'stale-or-missing-run-nonce', "
                f"agent: {_js_string_literal(agent)}, "
                f"expected_nonce: {_js_string_literal(run_nonce)}, "
                f"received_nonce: {var}.run_nonce }};\n"
                "  }\n"
                f"  if ({var}.verdict === {_js_string_literal(token)}) {{\n"
                f"    return {{ blocking_agent: {_js_string_literal(agent)}, "
                f"verdict: {var}.verdict, reason: {var}.reason, "
                f"sidecar_path: {var}.sidecar_path }};\n"
                "  }"
            )
        return "\n".join(branches)

    return gate_policy


def _meta_block(name: str, description: str, phase_titles: List[str]) -> str:
    phases_literal = ", ".join(_js_string_literal(t) for t in phase_titles)
    return (
        "export const meta = {\n"
        f"  name: {_js_string_literal(name)},\n"
        f"  description: {_js_string_literal(description)},\n"
        f"  phases: [{phases_literal}],\n"
        "};\n"
    )


def compose_review_script(
    plan_path,
    fragment: dict,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    repo_root: Optional[Path] = None,
    run_nonce: Optional[str] = None,
) -> Tuple[str, str, str]:
    """Derive a tier from ``plan_path``, parse ``fragment``'s stages for it,
    compose script text, and return ``(script, tier, run_nonce)``.

    ``run_nonce`` (AC12) is minted here with ``secrets.token_hex(8)`` when
    the caller does not pin one -- unguessable, never a counter or a
    timestamp (see plan/memo: a predictable value defeats the freshness
    check it exists to enforce). Threaded to ``compose()`` (prompt
    injection on GATE stages only) and to ``_make_gate_policy`` (the
    refusal comparison), and returned so the op boundary can hand it back
    to the caller for correlation.

    Raises ``ReviewTierUndeterminedError`` if no tier can be derived, and
    propagates ``roster.RosterFragmentError`` / ``compose.ComposeError``
    uncaught on a malformed fragment or empty stage list.
    """
    plan_path = Path(plan_path)
    tier = derive_review_tier(plan_path, repo_root=repo_root)
    if tier is None:
        raise ReviewTierUndeterminedError(
            f"could not derive a review tier from {plan_path} -- no "
            "sizing_object citation, or its estimate.tshirt is absent"
        )

    resolved_nonce = run_nonce if run_nonce is not None else secrets.token_hex(8)

    stages = parse_stages(fragment, tier)
    blocking_verdicts = fragment.get("blocking_verdicts")
    if not isinstance(blocking_verdicts, dict):
        blocking_verdicts = {}
    gate_policy = _make_gate_policy(blocking_verdicts, resolved_nonce)

    body_blocks = compose(
        stages, _REVIEW_PROMPT, _REVIEW_PHASE_TITLE, gate_policy, run_nonce=resolved_nonce
    )
    phase_titles = [title for title, _ in body_blocks]

    resolved_name = name or plan_path.stem
    resolved_description = description or (
        f"Gated review workflow for {plan_path.stem}"
    )
    meta_block = _meta_block(resolved_name, resolved_description, phase_titles)
    body = "\n\n".join(block for _, block in body_blocks)

    # Top-level, never `async function run(ctx) { ... }` -- same shape
    # dispatch_emit's compose_script uses.
    return f"{meta_block}\n{body}\n", tier, resolved_nonce


@register_op("review.mint_workflow")
def _review_mint_workflow(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "review.mint_workflow" handler.

    Args (via params):
        plan_path (str): plan file to derive the review tier from.
        output_path (str): path to write the emitted ``.mjs`` script to.
        target_root (str, optional): explicit containment root; defaults to
            ``repo_root`` when the request carries one, else to
            ``output_path``'s parent directory (see module docstring).
        name (str, optional): script ``meta.name``.
        description (str, optional): script ``meta.description``.
        run_nonce (str, optional): AC12 -- pins this mint's freshness nonce
            instead of minting a fresh one; a test seam.

    Returns:
        {"path": str, "ok": bool, "findings": [<finding dict>, ...],
         "error_count": int, "warn_count": int, "tier": str,
         "run_nonce": str}

    Raises:
        ValueError — if ``plan_path`` or ``output_path`` is missing.
        ReviewTierUndeterminedError — if no review tier can be derived.
        FileNotFoundError — if the sibling DoE-claude root or its fragment
        file cannot be resolved (``load_fragment()``).
        roster.RosterFragmentError — if the fragment is malformed for the
        derived tier, propagated uncaught.
        PathEscapeError — if ``output_path`` resolves outside ``target_root``.
    """
    plan_path = params.get("plan_path")
    if not plan_path:
        raise ValueError("review.mint_workflow requires param: plan_path")

    output_path = params.get("output_path")
    if not output_path:
        raise ValueError("review.mint_workflow requires param: output_path")

    target_root = (
        params.get("target_root")
        or (str(repo_root) if repo_root is not None else None)
        or str(Path(output_path).resolve().parent)
    )

    guarded_path = contained_path(Path(output_path), [Path(target_root)])
    if guarded_path is None:
        raise PathEscapeError(
            f"output_path escapes target_root: {output_path!r} not under {target_root!r}"
        )

    fragment = load_fragment(repo_root=repo_root)
    script, tier, run_nonce = compose_review_script(
        plan_path,
        fragment,
        name=params.get("name"),
        description=params.get("description"),
        repo_root=repo_root,
        run_nonce=params.get("run_nonce"),
    )

    findings = run_checks(script)
    error_count = sum(1 for f in findings if f.severity is Severity.ERROR)
    warn_count = sum(1 for f in findings if f.severity is Severity.WARN)

    # newline="" suppresses Windows' text-mode CRLF rewrite -- a CRLF-carrying
    # .mjs is rejected by the harness Workflow surface that fires it (control
    # characters in the approval payload); mirrors dispatch.emit's own write.
    guarded_path.write_text(script, encoding="utf-8", newline="")

    return {
        "path": str(guarded_path),
        "ok": error_count == 0,
        "findings": [
            {
                "severity": f.severity.value,
                "code": f.code,
                "message": f.message,
                **({"line": f.line} if f.line is not None else {}),
            }
            for f in findings
        ],
        "error_count": error_count,
        "warn_count": warn_count,
        "tier": tier,
        "run_nonce": run_nonce,
    }
