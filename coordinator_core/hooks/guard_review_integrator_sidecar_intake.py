"""coordinator_core.hooks.guard_review_integrator_sidecar_intake —
PreToolUse(Agent) hard-deny op.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-review-integrator-sidecar-
intake.py`. Two shape changes, both forced by this row's own op contract,
neither a behaviour change: (a) stdin/stdout JSON I/O becomes the
`params`-dict-in / envelope-dict-out contract every op in this package
shares — `coordinator_core._hook_envelope.deny` replaces the source's
hand-built `hookSpecificOutput` dict; (b) the source's own `_git_root`
(a bare, zero-spawn upward walk from `cwd`) is dropped in favor of the
`repo_root` this op's caller already resolves and threads through every
handler in this package (`coordinator_core/ipc.py`'s own dispatch
contract) — a second, independent walk of the same fact would be a second
place for the two to silently disagree. Falls back to `cwd` itself
(never a bare walk) when `repo_root` is not supplied, same "no repo found"
degrade the source's own `_git_root` had.

Mechanical enforcement of `coordinator/agents/review-integrator.md`'s
"Intake precondition -- hard stop" (a DoE-claude-resident agent doc; this
guard fires on the dispatching side regardless of which repo the agent
prose lives in): review-integrator's inputs are files on disk (a reviewer
findings sidecar + the artifact path(s)), never findings inline in the
dispatch prompt. That precondition is stated in prose only, and prose lost
5/5 in the field -- a sibling EM sent five consecutive inline-findings
dispatches to review-integrator, and all five walked straight through the
prose hard stop and did the work anyway, flagging the mismatch only as a
closing concern. One instance rationalized past the stop explicitly: it
found an UNRELATED pre-existing sidecar on disk and treated the dispatch
as "a correction order against that pre-existing sidecar." A sufficiently
confident, concretely-specified brief -- real file paths, verified
measurements, unambiguous instructions -- walks straight through a
prose-only stop; the fix has to live at the dispatch seam, not in stronger
prose review-integrator itself might rationalize past again.

Fires only for a dispatched `subagent_type` that resolves to
review-integrator (`coordinator:review-integrator` or the bare
`review-integrator`, matched via the same "segment after the last `:`"
normalization DoE's `pickup-autofire.py::_normalize_command_name` used for
the analogous namespaced/bare command-name split -- both a plugin-
namespaced identity and a bare one must resolve to the same check, not
two).

A dispatch is satisfying iff its prompt text NAMES a
`state/subagent-share/<session>/<key>.md`-shaped path (see
`review-integrator.md`'s "Intake precondition" paragraph for the canonical
provisioned shape) AND that exact path exists on disk. Anything else --
inline findings with no sidecar path at all, a path that is named but not
on disk (stale/never-provisioned), or (the closed rationalization hole,
documented below) a real sidecar that exists on disk but was never named in
THIS dispatch's prompt -- denies.

NEGATIVE SPEC -- the closed rationalization hole: this guard deliberately
checks "named in this dispatch's prompt AND present on disk", never
"present on disk" alone. A "some sidecar exists somewhere under
state/subagent-share/<session>/" fallback would silently re-open the exact
failure this guard exists to close (the observed "I treated this as a
correction order against that pre-existing sidecar" rationalization) --
resist adding one even as a convenience for a genuinely-provisioned-but-
unnamed sidecar; the EM's remedy in that case is to name the path in the
prompt, not for this guard to go find it.

Deny-message shape follows the design-as-offers doctrine (global CLAUDE.md
§ Implementation Standards): leads with the correct route before stating
what was missing. No wiki anchor -- the relocated three-branch remedy
explanation and closed-rationalization-hole rationale lived at a
DoE-claude-resident wiki page not reachable from every caller of this op;
the prose carries the whole diagnosis inline instead (mirrors this row's
own `guard_repo_setup_claude_home_refusal.py` precedent for the same
reason).

Fail-open, silently, on: `tool_name` != "Agent"; `agent_id` present (a
subagent's own nested Agent call, not the dispatching EM's); no
`subagent_type`; `subagent_type` normalizes to something other than
review-integrator; no `prompt` text extractable; malformed `tool_input`. A
broken guard must never brick every review-integrator dispatch in the
fleet -- the only fail-CLOSED leg is the confirmed "no on-disk, in-prompt
sidecar path found for a genuine review-integrator dispatch" case.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

import re
from pathlib import Path, PureWindowsPath
from typing import Optional

from coordinator_core._hook_envelope import deny, no_advisory, payload_of
from coordinator_core.hooks.support.message_envelope import ALTERNATIVE_MAX_LINES, compose, render
from coordinator_core.ipc import register_op

# Repo-relative shape only, per review-integrator.md's canonical provisioned
# path (`state/subagent-share/<session>/<key>.md`) -- mirrors
# enforce-agent-dispatch-mode.py's `_PLAN_PATH_RE` widening rationale: the
# leading char-class includes both separators so an absolute brief citation
# (POSIX ".../state/subagent-share/<session>/<key>.md" or Windows
# "...\state\subagent-share\<session>\<key>.md") still matches,
# because the capture group only ever starts at the literal
# "state<sep>subagent-share<sep>" prefix -- an absolute prefix before that
# point is excluded from the match by construction.
#
# NEGATIVE SPEC -- both separators are admitted in the same alternation, never
# a POSIX-only spelling. A forward-slash-only anchor denies a correct,
# on-disk, absolute Windows citation, which is the spelling every Windows tool
# result and directory listing produces; a denial that fires on a correct
# citation trains EMs to reshape prompts until the guard stops complaining,
# which is the failure mode this guard exists to prevent. Multi-OS support is
# P0 (project CLAUDE.md § Runtime conventions) -- a separator assumption is a
# correctness defect, not a style nit. This alternation is separator-family-
# only -- do not extend it to other `state/` roots (e.g. `state/plan-
# sidecars/`) without a matching intake-contract change: widening this regex
# to also admit `state/plan-sidecars/` would erase the reap-lifecycle
# distinction between session-keyed persona-findings sidecars (reaped on an
# age/liveness floor) and the UNREAPED-BY-DESIGN plan-derivable lens
# sidecars that live there by design.
#
# The `.coordinator-local/subagent-share/` alternative is NOT that widening
# and does not reopen it. It is the SAME bucket at a relocated root -- the
# engine's `machinery_root()` moved 2026-09-02 from `state/` to
# `.coordinator-local/`, so one corpus answers to two spellings while
# readers migrate. The forbidden widening admits a DIFFERENT bucket with a
# different reap lifecycle; this admits the same bucket at the address it
# moved to. BOTH roots stay accepted: the served tree and the source tree
# diverge across a republish, and provisioning and matching can be served
# from different vintages inside that window, so both roots are legal until
# it closes and the matcher cannot tell which it is serving.
#
# An absolute citation is probed as written (never re-rooted at the session
# cwd -- that misses whenever cwd is not the sidecar's repo, the multi-repo
# cloud container's shape, whose cwd is the parent of every checkout). The
# leading char-class's own `/`/`\\` admission means the tail pattern alone
# already matches INSIDE an absolute citation; the optional captured prefix
# group below exists only to recover the absolute portion for an as-written
# probe, not to change what matches.
_SIDECAR_PATH_RE = re.compile(
    r"(?:^|[\s(\[\"'`/\\])"
    r"((?:[A-Za-z]:[/\\]|/)[^\s()\[\]\"'`,;]*?[/\\])?"
    r"((?:state|\.coordinator-local)[/\\]subagent-share[/\\][^\s()\[\]\"'`,;:]+\.md)"
)


def _extract_candidates(prompt: str) -> "list[tuple[Optional[str], str]]":
    """Every `(prefix, tail)` match -- `prefix` is the absolute lead-in
    (`C:\\...\\` or `/...`) when the citation was absolute, else `None`."""
    if not prompt:
        return []
    return [(m.group(1), m.group(2)) for m in _SIDECAR_PATH_RE.finditer(prompt)]


def _candidate_spellings(candidate: str) -> "list[str]":
    """Every repo-relative spelling of `candidate` worth an on-disk probe.

    The as-captured spelling is probed first so a POSIX host -- where a
    backslash is a legal filename character rather than a separator --
    keeps exact-match semantics; the separator-swapped spelling is the
    additional probe that makes a Windows-spelled citation of a real file
    resolve."""
    spellings = [candidate]
    swapped = PureWindowsPath(candidate).as_posix()
    if swapped != candidate:
        spellings.append(swapped)
    return spellings


def _normalize_subagent_type(raw: Optional[str]) -> str:
    """Same "segment after the last `:`" normalization DoE's
    `pickup-autofire.py::_normalize_command_name` used -- a namespaced
    `coordinator:review-integrator` and a bare `review-integrator` both
    resolve to the same bare identity before the membership test."""
    if not isinstance(raw, str):
        return ""
    return raw.rsplit(":", 1)[-1]


def _compose_no_candidates_message():
    """Pure composer for the "no sidecar path named at all" deny message.
    Keeps both remedies the original prose carried (fulfil the contract, or
    use the right agent)."""
    return compose(
        "no path under .coordinator-local/subagent-share/<session>/ named -- name the "
        "reviewer's provisioned findings sidecar, not a trail report or the plan. Or "
        "dispatch coordinator:enricher for plan-body work."
    )


def _compose_stale_candidates_message(candidates: "list[str]"):
    """Pure composer for the "named but not on disk" deny message. The
    named, stale candidate path(s) ride in `alternative` (structurally
    exempt from the char cap, per `message_envelope`) rather than the
    counted prose."""
    alternative = "\n".join(candidates[:ALTERNATIVE_MAX_LINES])
    return compose(
        "named sidecar not on disk -- provision it under "
        ".coordinator-local/subagent-share/<session>/, or dispatch "
        "coordinator:enricher for plan-body work.",
        alternative=alternative,
    )


@register_op("hooks.guard_review_integrator_sidecar_intake")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Agent) op: deny a dispatch to review-integrator whose
    prompt does not name an on-disk findings sidecar."""
    params = payload_of(params)
    if params.get("tool_name", "") != "Agent":
        return no_advisory()

    # Only the dispatching EM's own Agent call is guarded, never a
    # subagent's own nested dispatch -- mirrors the sibling Agent-matcher
    # guards' `agent_id` exclusion.
    if params.get("agent_id") is not None:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    child_subagent_type = _normalize_subagent_type(tool_input.get("subagent_type"))
    if child_subagent_type != "review-integrator":
        return no_advisory()

    prompt = tool_input.get("prompt", "")
    prompt = prompt if isinstance(prompt, str) else ""

    candidates = _extract_candidates(prompt)
    if not candidates:
        reason = render(_compose_no_candidates_message())
        return deny("PreToolUse", reason)

    cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
    root = repo_root if isinstance(repo_root, str) and repo_root else (cwd or ".")

    displayed = []
    for prefix, tail in candidates:
        # `base` is None for a relative citation (probed against `root`) or
        # the absolute prefix itself (probed as written, never re-rooted) --
        # a single loop over both shapes so a future fix can't land on only
        # one arm.
        base = prefix if prefix else None
        candidate = (prefix + tail) if prefix else tail
        displayed.append(candidate)
        for spelling in _candidate_spellings(candidate):
            probe = Path(spelling) if base is not None else Path(root) / spelling
            try:
                if probe.is_file():
                    return no_advisory()
            except Exception:
                continue  # unresolvable candidate path; try the next spelling

    reason = render(_compose_stale_candidates_message(displayed))
    return deny("PreToolUse", reason)
