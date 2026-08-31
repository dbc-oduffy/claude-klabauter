"""
coordinator_core.subagent_sandbox.provision_report -- spawn-time
report-sidecar provisioner, the DR-047 engine's sibling seam to the
PreToolUse deny/allow enforcement envelope (engine.py / __main__.py).

Where the enforcement engine intercepts a subagent's *write attempts*
mid-session (PreToolUse), this module runs once at *spawn time* and, for
agent/subagent types the policy has opted into ``report_sidecar``,
provisions a per-session run-report markdown doc under
``state/subagent-share/<session_id>/`` and emits its repo-relative path on
stdout as ``{"report_sidecar": "<path>"}``. It shares the engine's policy
loader, git-root resolver, and OR-resolved agent/subagent type legs
(imported verbatim, never re-derived) but owns its own single-segment path
sanitizer -- the engine's traversal/display helpers are shaped for a
different check (multi-segment ``..`` detection and control-char display
stripping, respectively) and do not cover this seam's needs.

Fail-open everywhere: any parse failure, ineligible type, missing
session_id, or unexpected exception yields empty stdout and exit 0 --
this module must never brick a spawn.

Additive second seam (canonical spec
state/subagent-share/conductor/seam-adjudication.md § 2.3, DoE-claude):
when the stdin payload carries a ``contract_blocks`` JSON list of
``coordinator/snippets/<name>.md`` block names (resolved DoE-side from
``subagent-sandbox-policy.yaml`` -- this module never re-reads that
policy file itself), ``assemble_contract_blocks_for_payload`` assembles
their ``header_style``-aware extracted, placeholder-resolved bodies into a
single ``injected_prompt_blocks`` string, emitted as an additive second
stdout key alongside ``report_sidecar``. All-or-nothing per spec §2.6.1:
any block's extraction or placeholder-resolution failure omits the key
entirely rather than emitting a partial contract. Independent of the
``report_sidecar`` leg per spec §2.6.2: failure in either leg must never
suppress the other.

Plan-derivable ``report_sidecar`` for the four G2 plan-pipeline emitters
(canonical spec § 2.7, absorbed from G2's D0/Z2 -- this is NOT part of the
injection seam above; it changes the *value* an existing eligible
``subagent_type`` resolves for ``report_sidecar``, not the grammar of a new
key). For ``prior-art-checker``/``plan-coverage-checker``/
``external-pattern-checker``/``docs-checker`` ONLY, and only when the spawn
payload also carries a non-empty ``plan_path``, ``_provision`` writes the
sidecar to the deterministic ``state/plan-sidecars/<plan-stem>.<lens>.md``
path instead of the session-keyed ``state/subagent-share/<session_id>/``
home. Every other eligible ``subagent_type`` -- reviewer personas, and
these same four agents' non-plan dispatch shapes (e.g. docs-checker's
code-review dispatch, which carries no ``plan_path``) -- is unaffected and
keeps the session-keyed home.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from coordinator_core._settings_home import claude_config_dir, machine_local_dir
from coordinator_core.frontmatter.sentinel_blocks import extract_block as _extract_sentinel_block
from coordinator_core.git.repo_root import show_toplevel as _show_toplevel_no_spawn
from coordinator_core.session import scope as session_scope
from coordinator_core.snippet_sync.registry import (
    RegistryError,
    get_snippet_entry,
    load_registry,
)
from coordinator_core.subagent_sandbox.engine import (
    _NAMED_TEAMMATE_RE,
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)

#: Whitelist for a single path SEGMENT (label or session_id) -- everything
#: outside this set is dropped, never escaped/encoded.
#:
#: ``@`` is admitted so the EM-side canonical agent id
#: ``<name>@session-<short8>`` survives sanitization UNCHANGED. The
#: derived-key call site gates on ``_sanitize_segment(agent_id) == agent_id``;
#: with ``@`` dropped the canonical form would silently fail that equality and fall
#: through to the random-nonce path, discarding the stable collision-proof
#: key for exactly the named-dispatch population the key exists to serve.
#: ``@`` is neither a directory separator nor a component of ``.``/``..``,
#: so admitting it widens no traversal surface.
_SEGMENT_WHITELIST_RE = re.compile(r"[^A-Za-z0-9._@-]")

#: Sanitized segments that must still be rejected even though the
#: whitelist above preserves dots -- a bare '..' (or '.') survives the
#: character whitelist untouched and must be caught explicitly.
_REJECTED_SEGMENTS = {"", ".", ".."}


def _sanitize_segment(seg: str) -> Optional[str]:
    """Reduce ``seg`` to a single safe path segment, or ``None`` if unsafe.

    Whitelists ``[A-Za-z0-9._-]`` (dropping '/', '\\', and everything else
    that could smuggle a directory separator), then rejects the
    degenerate '.'/'..'/empty results the whitelist alone would let
    through. Never calls ``Path.resolve()`` -- mirrors the engine's
    normalized-but-unresolved-string discipline (engine.normalize_file_path).
    """
    sanitized = _SEGMENT_WHITELIST_RE.sub("", seg)
    if sanitized in _REJECTED_SEGMENTS:
        return None
    return sanitized


#: Leaf of the sidecar POINTER index, under the same
#: ``.git/coordinator-sessions/`` home the ``.agents/`` back-pointer chain
#: already occupies (``engine._read_backpointer_subagent_type``). Untracked and
#: machine-local on purpose: it is a rebuildable cache of "where did this
#: agent's sidecar go", not a record, so it must never enter the tree the
#: reaper sweeps or add commit churn.
_SIDECAR_POINTER_DIRNAME = ".agent-sidecars"

#: The one prefix a pointer is allowed to name. A pointer file is the only
#: input to this module that is neither the policy nor the spawn payload, so
#: it is read as UNTRUSTED: a value that does not start with this, or that
#: carries any ``..`` component, is discarded rather than followed.
_SIDECAR_POINTER_PREFIX = "state/subagent-share/"


def _sidecar_pointer_path(git_root: str, raw_agent_id: str, kind: str = "report") -> Optional[Path]:
    """Pointer file for ``raw_agent_id``, or ``None`` if it cannot key one.

    ``kind`` namespaces the index by PRODUCER, because two producers write
    into ``state/subagent-share/`` for the same agent with different leaf
    suffixes -- this module (``<key>.md``) and ``coordinator-doc-new --type
    subagent-sidecar`` (``<key>.subagent-sidecar.md``). A single flat key
    would hand one producer's spawn the other's document.

    Keyed by the RAW payload ``agent_id``, never the canonicalized one, and
    this is the whole point of the index rather than an implementation detail.
    ``engine.resolve_effective_types`` canonicalizes through
    ``session.identity.resolve_subagent_identity``, whose named-teammate leg
    returns ``build_canonical_agent_id(name, session_id[:8])`` -- so the
    canonical id has the SESSION ID baked into it and moves whenever the
    session id does. An index keyed on it would move in exact lockstep with
    the instability it exists to survive. The raw id is what the harness hands
    the same agent across a resume.
    """
    seg = _sanitize_segment(raw_agent_id)
    # Same gate the derived provision_key uses: an id that does not survive
    # sanitization UNCHANGED is not keyed at all, rather than keyed under a
    # silently different name that would never be found again.
    if seg is None or seg != raw_agent_id:
        return None
    kind_seg = _sanitize_segment(kind)
    if kind_seg is None or kind_seg != kind:
        return None
    return (
        Path(git_root)
        / ".git"
        / "coordinator-sessions"
        / _SIDECAR_POINTER_DIRNAME
        / kind_seg
        / seg
    )


def _read_sidecar_pointer(git_root: str, raw_agent_id: str, kind: str = "report") -> Optional[str]:
    """Repo-relative path of this agent's EXISTING sidecar, or ``None``.

    Every failure mode -- no pointer, unreadable, malformed, naming a path
    outside ``state/subagent-share/``, or naming a file that is no longer
    there (the reaper is entitled to have swept it) -- returns ``None``, which
    the caller reads as "nothing to adopt" and provisions normally. A stale
    pointer can therefore only ever cost a wasted read.
    """
    pointer = _sidecar_pointer_path(git_root, raw_agent_id, kind)
    if pointer is None:
        return None
    try:
        content = pointer.read_text(encoding="utf-8")
    except OSError:
        return None
    rel = content.splitlines()[0].strip() if content else ""
    if not rel.startswith(_SIDECAR_POINTER_PREFIX):
        return None
    if ".." in rel.split("/"):
        return None
    if not (Path(git_root) / rel).is_file():
        return None
    return rel


def _write_sidecar_pointer(
    git_root: str, raw_agent_id: str, rel_path: str, kind: str = "report"
) -> None:
    """Record where this agent's sidecar went. Best-effort by contract.

    Never raises and never reports: the pointer is a cache, and a spawn that
    cannot write one must still get its sidecar. Rewritten (not created-once)
    on every provision, so the pointer tracks the newest home if a sidecar is
    ever legitimately re-provisioned elsewhere.
    """
    pointer = _sidecar_pointer_path(git_root, raw_agent_id, kind)
    if pointer is None:
        return
    try:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(rel_path + "\n", encoding="utf-8", newline="\n")
    except OSError:
        return


#: Plan-derivable ``subagent_type``/``agent_type`` -> lens suffix map
#: (canonical spec § 2.7). The membership test is **plan-scoped-durable vs
#: session-scoped-ephemeral**, not "does the type review something": every
#: key here emits a finding-set whose identity is the PLAN, consumed against
#: that plan by ``review-integrator`` in a possibly-later session, so a
#: session-keyed home is precisely where the next session will not look. The
#: Opus reviewer PERSONAS (``staff-eng``, ``vp-product``, ``eng-director``,
#: ...) are excluded not for having "review" in the role but because their
#: output is a session judgment on work in flight, keyed to the session that
#: asked -- as is every other ``report_sidecar``-eligible type, which keeps
#: the session-keyed ``state/subagent-share/<session_id>/`` home.
#: A key here is only followed when the spawn payload ALSO carries a
#: non-empty ``plan_path`` -- this is what keeps e.g. docs-checker's
#: code-review dispatch (no ``plan_path`` on that payload shape) on the
#: untouched session-keyed path while its plan-side dispatch (``plan_path``
#: present) resolves to the deterministic plan-sidecar home instead.
_PLAN_DERIVABLE_LENS: Dict[str, str] = {
    "coordinator:prior-art-checker": "prior-art-check",
    "coordinator:plan-coverage-checker": "plan-coverage-check",
    "coordinator:external-pattern-checker": "external-pattern",
    "coordinator:docs-checker": "docs-check",
    "coordinator:plan-reviewer": "plan-review-check",
}


def _resolve_plan_sidecar_stem(plan_path: str) -> Optional[str]:
    """Reduce a ``plan_path`` payload value to a safe plan-stem segment.

    ``Path(plan_path).stem`` discards directory components by construction
    -- a traversal-laden ``plan_path`` (``../../etc/passwd``) can never
    smuggle a directory separator through this leg, since only the final
    component's stem is taken. The result is then run through the same
    single-segment whitelist every other path fragment this module writes
    goes through (``_sanitize_segment``), so a plan filename with unusual
    characters degrades to ``None`` (caller fails open) rather than
    producing a surprising filename.
    """
    return _sanitize_segment(Path(plan_path).stem)


#: Top-level ``plan:`` frontmatter key of a plan-derived sidecar -- anchored at
#: column 0 so ``_frontmatter``'s indented sub-keys (``divergence:``'s
#: ``diverged``, ``dispatch_feed:``'s members) can never match it.
_PLAN_FRONTMATTER_RE = re.compile(r"^plan:[ \t]*(.*?)[ \t]*$")


def _plan_frontmatter_value(doc_text: str) -> Optional[str]:
    """The leading frontmatter block's ``plan:`` value, or ``None``.

    Scans only between the opening ``---`` and the first closing ``---``: a
    ``plan:`` line in the BODY of a filled-in sidecar (a findings entry
    quoting the plan, say) is not an identity claim and must not be read as
    one. Hand-scanned rather than parsed -- this is a PreToolUse-Agent hook,
    cold on every dispatch fleet-wide, and the module's stdlib-only,
    no-PyYAML discipline (``_yaml_quoted_scalar``) applies to the read side
    for the same reason it applies to the write side.
    """
    lines = doc_text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        match = _PLAN_FRONTMATTER_RE.match(line)
        if match:
            return match.group(1).strip("'\"") or None
    return None


def _declared_plan_disagrees_with_stem(declared_plan: Optional[str], plan_stem: str) -> bool:
    """True iff ``declared_plan`` names a plan whose own stem is not
    ``plan_stem`` -- the mechanically-detectable shape of the 2026-08-07
    clobber (state/bug-backlog/2026-08-07-lens-sidecar-provisioning-clobbers-
    a-peer-plans-sidecar.yaml), where a sidecar's filename said one plan and
    its ``plan:`` frontmatter said another.

    Compared UNSANITIZED on purpose: ``plan_stem`` has already been through
    ``_sanitize_segment``, so a plan filename the whitelist silently REWROTE
    (rather than rejected) lands under a stem naming a different plan than
    the payload asked for, and that rewrite is exactly a disagreement this
    must catch, not tolerate.

    An absent or empty ``plan:`` cannot disagree -- four of the five lenses
    stamp no such key -- so it is not a refusal; this guard only ever fires
    on a positively contradictory identity claim.
    """
    if not declared_plan:
        return False
    return Path(declared_plan).stem != plan_stem


def _read_stdin() -> str:
    return sys.stdin.read()


#: Valid ``--type``/payload ``type`` values. Keys double as argparse
#: choices for --type below -- keep both in lockstep.
TEMPLATE_TYPES = ("run-report", "review-findings", "assessment", "staff-eng-review")

#: Superset frontmatter field set shared by every template -- unchanged
#: field NAMES/ORDER across all types (status/agent_type/spawned_at/
#: lead_session_id/divergence/commits/dispatch_feed); only the body
#: sections vary by type. See CONTRACT.md's provision-and-emit contract
#: before touching field names/order here -- this is a pinned wire shape.
def _frontmatter(agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None) -> str:
    """``lead_session_id`` is the REQUESTING EM's session id -- distinct
    from the spawned agent's own ``agent_id`` (resolve_effective_types'
    first return leg, never written into frontmatter itself). Sourced
    straight off the stdin payload's ``session_id`` field (raw, not the
    path-sanitized segment used for the directory leaf) -- see
    ``_provision``'s call site. Falls back to the literal ``null`` when
    absent -- in practice this is never absent when reached via
    ``_provision`` since ``session_id`` is required for eligibility.

    ``dispatch_feed``'s sub-properties in run-report.schema.json
    (``label``/``agent_type``/``model``/``effort``/``schema_ref``/
    ``brief_ref`` = ``type: string``, ``est_min`` = ``type: number``) admit
    NO null -- only the field itself is ``["object", "null"]``. There is no
    ``required`` list and ``additionalProperties: true``, so a valid
    block-style object may simply OMIT every field it has no real value
    for. This emits ONLY the two fields with a real value at scaffold time
    (``gate_kind: none`` -- a valid enum member -- and ``write_files: []``)
    and drops the seven null-valued lines a prior revision emitted, which
    were schema-invalid (staff review + coordinator/tests/
    test_flight_recorder_scaffolder.py Case 3/6h caught it downstream, see
    C6b)."""
    return (
        "---\n"
        "status: open\n"
        f"agent_type: {agent_type}\n"
        f"spawned_at: {spawned_at}\n"
        f"lead_session_id: {lead_session_id if lead_session_id is not None else 'null'}\n"
        "divergence:\n"
        "  diverged: false\n"
        "commits: []\n"
        "dispatch_feed:  # forward-declared, INERT until pcli-04 emitter\n"
        "  gate_kind: none\n"
        "  write_files: []\n"
        "---\n\n"
    )


#: AC1 (docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md):
#: the review receipt is stamped HERE, at dispatch, never resolved from
#: `directives_review.py` or any close-time reader -- see this module's own
#: docstring's "Additive second seam" discussion and C4's join at close for
#: why dispatch-time is the load-bearing choice (a blank-vs-filled
#: distinction the close-time join depends on). Reuses `review_trail_write`'s
#: own closed `_DELEGATE_REVIEWERS` vocabulary BY NAME, never a re-derived
#: second classifier (chunk brief, state/dispatch-briefs/2026-08-27-the-
#: review-gate-measures-the-whole-session/C2.md) -- the same vocabulary
#: `hooks/subagent_review_mark.py::_is_reviewer` already reuses this way, so
#: a third independent copy of "what counts as a delegate reviewer" never
#: gets a chance to drift from the other two.
def _bare_agent_type(value: str) -> str:
    """Strip a ``coordinator:``/``agent:``-shaped namespace prefix -- mirrors
    ``review_trail_write.normalize_reviewer``'s own stripping convention
    (the reviewer vocabulary is spelled bare, never namespaced), and is the
    exact same stripping ``hooks/subagent_review_mark.py::_bare_type``
    performs for the identical purpose on the SubagentStop side of this
    receipt's lifecycle."""
    _prefix, sep, bare = value.rpartition(":")
    return bare if sep else value


def _is_close_receipt_reviewer(agent_type: str, subagent_type: str) -> bool:
    """True iff either resolved label's bare form is a member of the closed
    close-receipt-reviewer vocabulary (``CLOSE_RECEIPT_REVIEWERS`` -- the
    delegate-reviewer set plus the close-floor's own mandatory reviewer,
    see ``reviewer_vocabulary.py``'s module docstring) -- checked against
    BOTH labels, mirroring ``_provision``'s own eligibility test just above
    (``agent_type in policy.report_sidecar or subagent_type in
    policy.report_sidecar``), since which of the two carries the
    reviewer-shaped value is not fixed across callers.

    Reads the vocabulary from ``ops.reviewer_vocabulary``, a stdlib-only leaf,
    NOT from ``ops.review_trail_write`` which re-exports it (C9). Both spell the
    same one set, so this is still reuse-by-name and not a second classifier --
    but the import cost differs by two orders of magnitude, and this function is
    called on every ``_provision``, which is a PreToolUse-Agent hook and
    therefore cold on every agent dispatch fleet-wide. Measured: reaching it via
    ``review_trail_write`` costs 34.4ms marginal here; via the leaf, 0.21ms.
    ``review_trail_write`` is also the surface C5 gravestones, so a hot hook
    must not hard-depend on it.

    Local import retained (not merged into the module header): it avoids a
    module-init-order cycle with ``coordinator_core.ops``, which imports modules
    that in turn reach back into ``coordinator_core.hooks``/
    ``coordinator_core.subagent_sandbox`` -- the same reason
    ``subagent_review_mark.py::_is_reviewer`` gives for its own local import."""
    from coordinator_core.reviewer_vocabulary import CLOSE_RECEIPT_REVIEWERS as _CLOSE_RECEIPT_REVIEWERS

    return bool(
        (agent_type and _bare_agent_type(agent_type) in _CLOSE_RECEIPT_REVIEWERS)
        or (subagent_type and _bare_agent_type(subagent_type) in _CLOSE_RECEIPT_REVIEWERS)
    )


#: Single-quote-doubling is the one YAML scalar quoting style this module
#: needs and the cheapest to get right by hand -- stdlib-only, no PyYAML
#: dependency on this cold, fleet-wide PreToolUse-Agent hook path (see
#: module docstring's fail-open framing and ``_is_close_receipt_reviewer``'s own
#: reviewer_vocabulary-leaf rationale for why a heavier import is a defect
#: here, not a style choice).
def _yaml_quoted_scalar(value: str) -> str:
    """Render ``value`` as a YAML single-quoted scalar: embedded ``'``
    doubled to escape (the single-quoted-style escape rule), and any
    CR/LF folded to a single space -- a receipt field is an identifier or
    an ISO-8601 timestamp, never legitimately multi-line, and a literal
    newline inside a single-quoted YAML scalar is illegal syntax that
    would corrupt every key after it in the block. Quoting closes F5's
    hazard: `agent_type`/`subagent_type` are dispatch-payload-controlled
    strings, and an unquoted colon-space, leading `-`/`#`/`[`/`{`, or
    embedded newline would either corrupt the frontmatter block or get
    parsed back as something other than a plain string, defeating
    `_bare_agent_type`'s `rpartition(":")`-based stripping in the gate.
    """
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    return "'" + text.replace("'", "''") + "'"


#: AC1/AC2's four-field receipt block, spliced into the sidecar's OWN
#: frontmatter (never a sibling file, never a close-time write) -- dispatch
#: session id, agent id, agent type, and a UTC timestamp, under a key
#: distinguishable per receipt kind (``review_receipt``/``integrator_receipt``
#: -- see ``_is_close_receipt_reviewer``/``_is_review_integrator`` below for the
#: mutually-exclusive callers). C4 (constraint 8, AC2b) joins this against
#: the covering baton's claim window with a plain directory listing +
#: frontmatter read; nothing here resolves a baton or touches
#: `commit_ledger.resolve_owner`/`baton_assemble` (AC12b legs i/ii). All four
#: fields are routed through ``_yaml_quoted_scalar`` -- see that function's
#: docstring for the corruption hazard this closes.
def _receipt_block(key: str, session_id: str, agent_id: str, agent_type: str, stamped_at: str) -> str:
    return (
        f"{key}:\n"
        f"  session_id: {_yaml_quoted_scalar(session_id)}\n"
        f"  agent_id: {_yaml_quoted_scalar(agent_id)}\n"
        f"  agent_type: {_yaml_quoted_scalar(agent_type)}\n"
        f"  stamped_at: {_yaml_quoted_scalar(stamped_at)}\n"
    )


def _review_receipt_block(session_id: str, agent_id: str, agent_type: str, stamped_at: str) -> str:
    """AC1's reviewer-keyed receipt block -- thin named wrapper over
    ``_receipt_block`` so call sites still read as "the reviewer receipt",
    not a bare key-string literal."""
    return _receipt_block("review_receipt", session_id, agent_id, agent_type, stamped_at)


def _integrator_receipt_block(session_id: str, agent_id: str, agent_type: str, stamped_at: str) -> str:
    """AC2's integrator-keyed counterpart to ``_review_receipt_block`` --
    same four fields, distinguishable key (``integrator_receipt:``) so
    "review ran" (AC1) and "findings were applied" (AC2) stay separately
    legible on the same sidecar shape family rather than colliding on one
    key that could only ever mean one of the two."""
    return _receipt_block("integrator_receipt", session_id, agent_id, agent_type, stamped_at)


def _splice_receipt_block(
    doc_text: str, key: str, session_id: str, agent_id: str, agent_type: str, stamped_at: str
) -> str:
    """Insert a ``key``-headed receipt block immediately before the
    frontmatter's closing ``---\\n\\n`` delimiter -- the SAME splice point
    ``_append_lens_frontmatter_keys`` uses for the plan-derivable lens keys,
    but applied to the already-assembled ``doc_text`` (frontmatter + body)
    rather than to a bare frontmatter string, since every ``_build_*_doc_text``
    builder returns the two concatenated with no clean seam to split on.

    ``str.find`` (not ``index``): on a missing ``---\\n\\n`` fence -- a future
    template builder emitting frontmatter without the exact double-newline
    fence -- this returns ``doc_text`` UNSPLICED rather than raising. This is
    a PreToolUse-Agent hook, cold on every dispatch fleet-wide; an uncaught
    ``ValueError`` here would hard-fail every reviewer/integrator dispatch on
    the box. Degrading to no receipt is the safe direction: C4's gate then
    blocks on "no review dispatched" (constraint 4 -- ambiguity favors more
    review), which is a normal, nameable gate outcome, not a bricked spawn.
    Does not mutate ``_frontmatter`` itself (same pin
    ``_append_lens_frontmatter_keys`` already documents)."""
    marker = "---\n\n"
    idx = doc_text.find(marker)
    if idx == -1:
        return doc_text
    return doc_text[:idx] + _receipt_block(key, session_id, agent_id, agent_type, stamped_at) + doc_text[idx:]


def _splice_review_receipt(doc_text: str, session_id: str, agent_id: str, agent_type: str, stamped_at: str) -> str:
    """Reviewer-keyed wrapper over ``_splice_receipt_block`` -- kept so
    ``_provision``'s reviewer branch reads as "splice the review receipt",
    not a bare key-string literal."""
    return _splice_receipt_block(doc_text, "review_receipt", session_id, agent_id, agent_type, stamped_at)


#: AC2 (docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md,
#: C3): the bare (namespace-stripped) ``agent_type``/``subagent_type`` this
#: repo's routing table registers the review-integrator persona under
#: (`coordinator:review-integrator`) -- see this module's docstring cross-
#: refs into `coordinator_core.ops.append_integrator_dispositions` for the
#: sibling doctrine. Deliberately its own single-member check, not folded
#: into `_DELEGATE_REVIEWERS`: the integrator is not a delegate REVIEWER
#: (it applies findings, it does not render a verdict) and C3's whole point
#: is a receipt DISTINGUISHABLE from AC1's reviewer receipt, which a shared
#: vocabulary membership test would blur.
_INTEGRATOR_AGENT_TYPE = "review-integrator"


def _receipt_agent_type(agent_type: str, subagent_type: str, vocabulary) -> str:
    """The label to STAMP into a receipt: whichever of the two resolves to a
    member of `vocabulary`, preferring `agent_type` when both do.

    WHY THIS EXISTS. `_is_close_receipt_reviewer`/`_is_review_integrator` check
    BOTH labels because which one carries the persona is not fixed across
    callers -- but the stamp beneath them wrote raw `agent_type`, so a
    dispatch detected via `subagent_type` was stamped with whatever
    `agent_type` happened to hold. For a NAMED (Agent-teams teammate)
    dispatch that is the teammate's own `name` string, not a
    `coordinator:*` type -- documented at
    `bash_guards/block_reviewer_bash_outside_allowlist.py`'s Divergence 16,
    which fixed the identical one-leg/two-leg asymmetry in its own
    `effective_type` selection.

    The consequence was a receipt that could never be credited:
    `receipt_credit._counting_receipt_stamps` requires the stamped
    `agent_type`'s bare form to be a `DELEGATE_REVIEWERS` member, and a
    teammate name is not one. Measured 2026-08-28 over 2899 sidecars, this
    was the only receipt failing any condition -- a genuine `coordinator:
    staff-eng` plan review dispatched as `the Staff Engineer-gate`, whose own filename
    (`coordinatorstaff-eng.the Staff Engineer-gate@session-...`) still recorded the real
    type while the receipt did not. One in 2899 today because most dispatches
    are unnamed; it scales with the habit of naming agents, not with anything
    that stays constant.

    Negative spec: this NEVER invents a type. Both labels failing the
    vocabulary returns `agent_type` unchanged, so the pre-existing bytes are
    reproduced exactly for every dispatch the gates above would not have
    admitted anyway -- and the vocabulary is never widened to admit a label
    (adding `the Staff Engineer-gate` would ratify the mislabel and grow the closed set
    by one for every name anyone ever picks).
    """
    for candidate in (agent_type, subagent_type):
        if candidate and _bare_agent_type(candidate) in vocabulary:
            return candidate
    return agent_type


def _is_review_integrator(agent_type: str, subagent_type: str) -> bool:
    """True iff either resolved label's bare form is the review-integrator
    persona -- checked against BOTH labels, mirroring
    ``_is_close_receipt_reviewer``'s own both-labels check just above, since
    which of the two carries the persona value is not fixed across callers."""
    return bool(
        (agent_type and _bare_agent_type(agent_type) == _INTEGRATOR_AGENT_TYPE)
        or (subagent_type and _bare_agent_type(subagent_type) == _INTEGRATOR_AGENT_TYPE)
    )


def _splice_integrator_receipt(doc_text: str, session_id: str, agent_id: str, agent_type: str, stamped_at: str) -> str:
    """Integrator-keyed wrapper over ``_splice_receipt_block`` -- kept so
    ``_provision``'s integrator branch reads as "splice the integrator
    receipt", not a bare key-string literal. Same splice point (the
    frontmatter's closing ``---\\n\\n`` fence, first occurrence), same
    never-mutate-``_frontmatter`` discipline, distinguishable block
    (``integrator_receipt:`` rather than ``review_receipt:``)."""
    return _splice_receipt_block(doc_text, "integrator_receipt", session_id, agent_id, agent_type, stamped_at)


def _exit_interview_section() -> str:
    """The universal closing section every template inherits (commit
    c50cf8ac) -- do not vary its questions or position by type."""
    return (
        "## Exit interview\n\n"
        "- What did you have to work out that the brief could have told you?\n\n"
        "- What did you grep, read, or probe that turned out to be a dead end — and what were you actually looking for?\n\n"
        "- Where did your tool access, permissions, or output contract fight you? What was missing that isn't deliberately withheld from this role — a guard denial is not a gap.\n\n"
        "- Anything you wanted to say and had nowhere to put?\n\n"
    )


def _build_run_report_legacy_doc_text(
    agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None
) -> str:
    """The ORIGINAL, pre-``--type`` run-report shape, frozen verbatim.

    This is the byte-for-byte back-compat target for direct ``_provision``
    callers whose payload carries no ``type`` key at all -- chiefly
    ``coordinator/bin/fan-out-dispatch.py``, which calls ``_provision``
    directly (not ``main()``) and predates the ``--type`` axis. Do not
    add sections here; new shapes belong in the ``TEMPLATE_TYPES``
    registry below, reachable only when a payload/CLI actually opts into
    a ``type``. ``lead_session_id`` threads through to ``_frontmatter``
    only -- it is a frontmatter-only addition (SUBSUME), not a body-shape
    change, so it does not disturb this shape's frozen-legacy-body pin.
    """
    return (
        _frontmatter(agent_type, spawned_at, lead_session_id)
        + "## Run notes\n\n"
        + "## Observations\n\n"
        + _exit_interview_section()
    )


def _build_run_report_doc_text(
    agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None
) -> str:
    """``--type run-report`` (explicit, or the CLI's own default): the
    legacy run-report body plus a ``## Divergence from plan`` section and a
    trackable ``## Completion`` checklist marker. Layered on top of the
    EXISTING ``divergence``/``commits``/``lead_session_id`` frontmatter --
    no new frontmatter fields introduced at this layer."""
    return (
        _frontmatter(agent_type, spawned_at, lead_session_id)
        + "## Run notes\n\n"
        + "## Observations\n\n"
        + "## Execution capability\n\n"
        + "<!-- Name what you actually ran to reach these conclusions (tests, a probe, the CLI under review), or the literal `none — this verdict rests on reading only`. Resting on reading alone is a legitimate answer; an unstated basis is not. -->\n\n"
        + "## Divergence from plan\n\n"
        + "<!-- Prose companion to the `divergence` frontmatter field -- leave blank while divergence.diverged is false. -->\n\n"
        + "## Completion\n\n"
        + "- [ ] Complete — flip this box when done; the frontmatter `status:` field remains authoritative.\n\n"
        + _exit_interview_section()
    )


def _build_review_findings_doc_text(
    agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None
) -> str:
    """``--type review-findings``: per-finding disposition slots."""
    return (
        _frontmatter(agent_type, spawned_at, lead_session_id)
        + "## Execution capability\n\n"
        + "<!-- Name what you actually ran to reach this verdict (tests, a probe, the CLI under review), or the literal `none — this verdict rests on reading only`. A verdict resting on reading alone is a legitimate answer; an unstated one is not. -->\n\n"
        + "## Findings\n\n"
        + "<!-- One entry per finding: `- [severity] <finding> — disposition: accepted | rejected | deferred — rationale: ...` -->\n\n"
        + _exit_interview_section()
    )


def _build_assessment_doc_text(
    agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None
) -> str:
    """``--type assessment``: question/answer shape."""
    return (
        _frontmatter(agent_type, spawned_at, lead_session_id)
        + "## Questions\n\n"
        + "<!-- One entry per question: `- Q: ... / A: ...` -->\n\n"
        + _exit_interview_section()
    )


def _build_staff_eng_review_doc_text(
    agent_type: str, spawned_at: str, lead_session_id: Optional[str] = None
) -> str:
    """``--type staff-eng-review``: verdict + rationale + per-finding slots.

    ``## Findings`` is the canonical heading for review output across every
    consumer (DoE-claude ruling, 2026-08-10 memo): a type whose name promises
    review output must emit a sidecar
    ``ops.append_integrator_dispositions.append_dispositions`` can write into.
    A verdict is not a finding, so ``## Verdict``/``## Rationale`` stay and the
    three coexist.

    SECTION ORDER IS LOAD-BEARING, not cosmetic. That module's
    ``_extract_findings_section`` carves from ``## Findings`` to whichever of
    ``## Exit interview`` / ``## Integrator Dispositions`` comes first, and
    deliberately does NOT stop at an intervening ``## `` heading (the reviewer
    layout nests ``## Summary``/``### Finding N`` inside the findings body).
    Emitting ``## Verdict``/``## Rationale`` AFTER ``## Findings`` would fold
    them into the findings body — so they are emitted BEFORE it, and
    ``## Findings`` is last before the exit interview. Do not reorder.

    The scaffold comment is the ``review-findings`` template's sentinel
    verbatim, because ``_findings_section_is_empty`` strips that exact string
    to tell a pristine scaffold from a filled body; a paraphrase would read as
    filled-in content and defeat the empty-scaffold refusal.
    """
    return (
        _frontmatter(agent_type, spawned_at, lead_session_id)
        + "## Verdict\n\n"
        + "## Rationale\n\n"
        + "## Execution capability\n\n"
        + "<!-- Name what you actually ran to reach this verdict (tests, a probe, the CLI under review), or the literal `none — this verdict rests on reading only`. A verdict resting on reading alone is a legitimate answer; an unstated one is not. -->\n\n"
        + "## Findings\n\n"
        + "<!-- One entry per finding: `- [severity] <finding> — disposition: accepted | rejected | deferred — rationale: ...` -->\n\n"
        + _exit_interview_section()
    )


def _append_lens_frontmatter_keys(frontmatter_text: str, lens_keys: str) -> str:
    """Splice ``lens_keys`` in immediately before ``_frontmatter``'s closing
    delimiter, without editing ``_frontmatter`` itself (CONTRACT.md pin: its
    emitted field names and order are a wire shape with external consumers).
    ``_frontmatter`` always returns a string ending in the literal
    ``"---\n\n"`` -- this appends strictly AFTER the seven pinned keys and
    before that closing ``---``, per AC2/AC3."""
    marker = "---\n\n"
    assert frontmatter_text.endswith(marker)
    return frontmatter_text[: -len(marker)] + lens_keys + marker


#: DoE-claude coordinator/agents/plan-coverage-checker.md \u00a7 Sidecar Format
#: skeleton (## Plan Coverage Verification through the tenth ### heading),
#: copied byte-exact -- see _build_plan_coverage_check_doc_text's docstring.
_PLAN_COVERAGE_CHECK_SKELETON = '## Plan Coverage Verification\n\n**Plan:** <path>\n**Verdict:** COMPLETE | INCOMPLETE | BLOCKED-SURFACE-TO-PM | SCOPE-MISMATCH | DEGRADED\n**Sub-label:** INCOMPLETE — Mechanical: N, Judgment: M  *(INCOMPLETE only; omit otherwise)*\n**Oracle items:** N (source: <heading|table|ratified problem-set:`<path>`|inline ratified problem-set|sizing object:`<path>`>)\n**Slate items:** M\n**Missed:** X | **Ambiguous:** A | **OOS-weak:** Y | **Hedges:** Z | **Unratified-deferrals:** U | **Malformed-rows:** R | **Missing-writes:** V | **Open-on-landed:** O | **Substrate-drift:** W | **Deferral-args:** G | **Spine-emittability:** E | **Vehicle-in-anti-scope:** H | **Unregistered-hooks:** K\n**Advisory:** <finding line if applicable, else omit>\n\n### Missed audit items (no slate entry, no architectural OOS)\n\n*Action: one of the three resolutions in § Identity (add-to-slate | architectural-OOS | oracle-was-wrong).*\n\n### Ambiguous audit items (signal-partial — informational only)\n\n*Action: "verify covered or promote to explicit slate citation" — plus the reason (stopword-only overlap OR uncited consolidation). Never gates INCOMPLETE.*\n\n### Weak OOS / hedges (appetite-based deferrals)\n\n*Action: "promote to slate OR rewrite OOS reason as architectural" — quote ±5 lines of context.*\n\n### Task-spine: unratified deferrals and malformed rows\n\n*Action: LEGACY → "PM ratifies (pm_approved: true) OR EM moves the row back into scope." GOVERNED → **"ask the PM to approve the `<grouping>` grouping (or supply the missing pm_utterance/disposition_detail) — never set a field yourself, approval is a PM act."** Malformed row → "author fixes the required field(s)." Zero/>1 spine blocks → FAIL-LOUD note, verdict DEGRADED.*\n\n**Deferral-argument lenses** (`case_against` vacuity; >4 candidate cuts): see `docs/wiki/plan-coverage-checker.md` § Lens 2b checks 3–4 — a cut counts as candidate while `open`, not only once closed. Emit above as **Deferral-args**.\n\n### Task-spine: rows missing declared writes\n\n*Action: "author adds `writes:` — the row\'s `surface:` plus its body name the write targets."*\n\n### Task-spine: open rows on landed plans (resolution-completeness)\n\n*Action: "EM resolves via `plan_tasks.mutate resolve` (PM approval if disposition is non-`coded`) OR investigates why it was missed."*\n\n### Spine emittability (would `dispatch.emit` refuse this spine — AC9)\n\n*Action: "add a `writes:` path with a co-located test, OR architecturally justify why this row ships with no test coverage" — quote the offending row `id`s (the engine-defect case is Advisory-only, see Phase 3.7).*\n\n### Substrate drift (in-repo paths/symbols cited that don\'t match disk)\n\n*Action: "amend plan to current substrate OR explain drift" — plus current disk state (file absent / symbol not found within ±50 lines).*\n\n### Anti-scope vehicle-naming (Phase 4.5, Lens 4)\n\n*Action: the tripwire token and correction from Phase 4.5.*\n\n### Hook registration liveness (Phase 4.6, Lens 5)\n\n*Action: "amend plan to a currently-registered hook, or confirm with the EM whether the citation is stale" — plus the on-disk existence note and roster citation.*'


def _build_plan_coverage_check_doc_text(
    agent_type: str,
    spawned_at: str,
    lead_session_id: Optional[str] = None,
    *,
    plan_path: Optional[str] = None,
) -> str:
    """``"plan-coverage-check"``: the plan-coverage-checker lens's sidecar
    skeleton (docs/plans/2026-08-21-the-provisioner-writes-the-sidecar-
    skeleton.md, C1) -- reached only via the plan-derivable leg's lens-wins
    rule in ``_provision`` (AC4), never through the ordinary ``type``
    string axis a caller might set directly.

    The lens owns the plan-sidecar's body the same way it already owns its
    path (``_PLAN_DERIVABLE_LENS``): ``_PLAN_COVERAGE_CHECK_SKELETON`` above
    -- the ``## Plan Coverage Verification`` section, its ten ``### ``
    findings headings, and the thirteen-bucket counts line -- is copied
    BYTE-EXACT from DoE-claude's ``coordinator/agents/plan-coverage-
    checker.md`` \u00a7 Sidecar Format -- never retyped or paraphrased,
    since a near-miss classifies the sidecar DEGRADED. A DoE-side edit to
    that section is a drift this module does NOT auto-follow (no runtime
    dependency on the sibling checkout); re-sync the constant by hand when
    DoE's \u00a7 Sidecar Format changes.

    Lens-owned frontmatter keys (``title``/``created``/``author``/``kind``/
    ``plan``) are appended AFTER the seven pinned ``_frontmatter`` keys via
    ``_append_lens_frontmatter_keys`` -- ``_frontmatter`` itself is never
    edited (AC2). ``status:`` stays the pinned ``open`` (AC3): a completion
    claim is the agent's to make, never the scaffolder's. ``plan_path`` is
    the raw, already-repo-relative payload value (not the sanitized
    filesystem stem used for the path leaf) -- ``Path(...).stem`` is used
    only for the human-facing ``title:`` slug, never for anything
    filesystem-facing here.
    """
    created = spawned_at[:10] if spawned_at else ""
    plan_slug = Path(plan_path).stem if plan_path else ""
    lens_keys = (
        f"title: Plan Coverage Check \u2014 {plan_slug}\n"
        f"created: {created}\n"
        "author: plan-coverage-checker\n"
        "kind: plan-coverage-check\n"
        f"plan: {plan_path or ''}\n"
    )
    frontmatter = _append_lens_frontmatter_keys(
        _frontmatter(agent_type, spawned_at, lead_session_id), lens_keys
    )
    return frontmatter + _PLAN_COVERAGE_CHECK_SKELETON + "\n\n" + _exit_interview_section()


#: Template registry keyed by explicit ``type`` value. Reached only when a
#: payload/CLI invocation actually names a ``type`` -- an ABSENT ``type``
#: key never touches this registry (see ``_build_doc_text`` below), which
#: is what makes the no-type-key back-compat guarantee possible alongside
#: the "run-report" default's added sections.
_TEMPLATE_REGISTRY: Dict[str, Callable[[str, str, Optional[str]], str]] = {
    "run-report": _build_run_report_doc_text,
    "review-findings": _build_review_findings_doc_text,
    "assessment": _build_assessment_doc_text,
    "staff-eng-review": _build_staff_eng_review_doc_text,
    "plan-coverage-check": _build_plan_coverage_check_doc_text,
}


def _build_doc_text(
    agent_type: str,
    spawned_at: str,
    doc_type: Optional[str] = None,
    lead_session_id: Optional[str] = None,
    *,
    plan_path: Optional[str] = None,
) -> str:
    """Dispatch to the type-keyed template registry, or the frozen legacy
    run-report shape when ``doc_type`` is ``None`` (payload carried no
    ``type`` key -- the fan-out-dispatch.py back-compat path). An unknown
    ``doc_type`` string falls back to the run-report template rather than
    raising, consistent with this module's fail-open posture.
    ``lead_session_id`` (SUBSUME: the requesting EM's session id) is
    optional here purely so existing low-level callers that predate this
    field keep working unmodified -- ``_provision`` always supplies it.

    ``plan_path`` is a keyword-only addition (the provisioner writes the
    plan-coverage-check sidecar skeleton, C1): threaded through ONLY to
    ``"plan-coverage-check"``'s builder, which needs it for the lens-owned
    ``plan:`` frontmatter key -- every other registry entry keeps the exact
    3-positional-arg call shape it always had, so this addition cannot
    disturb their dispatch or the legacy no-type path above."""
    if not doc_type:
        return _build_run_report_legacy_doc_text(agent_type, spawned_at, lead_session_id)
    builder = _TEMPLATE_REGISTRY.get(doc_type, _build_run_report_doc_text)
    if doc_type == "plan-coverage-check":
        return builder(agent_type, spawned_at, lead_session_id, plan_path=plan_path)
    return builder(agent_type, spawned_at, lead_session_id)


#: header_style dialects the assembler knows how to extract, mirroring
#: coordinator_core.snippet_sync.verify._extract_snippet_body's taxonomy
#: (that function is DoE-consumer-facing verification tooling and stays
#: import-independent from this spawn-time seam; this module owns its own
#: copy of the same four-dialect switch against the shared registry reader
#: + sentinel_blocks primitive).
_KNOWN_HEADER_STYLES = {
    "sentinel-embedded",
    "fixed-2-line",
    "fixed-2-line-strip-end-sentinel",
    "comment-block",
}

#: Closed placeholder set (canonical spec §2.5) a contract block body may
#: reference. No others, no expression language -- an unresolvable
#: placeholder is an assembly failure (see _resolve_block_placeholders).
_KNOWN_CONTRACT_PLACEHOLDERS = {"kind", "sidecar_path", "subagent_type"}

_CONTRACT_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")


def resolve_plugin_root() -> Optional[str]:
    """Resolve the coordinator-claude plugin's CONTENT root -- the directory
    that directly contains ``snippets/``, ``subagent-sandbox-policy.yaml``,
    and ``registry.toml`` -- independent of the spawning SESSION's git root.

    Shared by this module's contract-block assembly leg and
    ``coordinator_core.hooks.cater_subagent_start._resolve_role_append_snippet_path``
    (C4/C5, state/dispatch-briefs/2026-08-21-catering-costs-what-the-work-
    costs/): both legs need "where does the coordinator-claude plugin's
    content live", not "where is the session's own repo" -- conflating the
    two is the defect this resolver exists to fix. ``_assemble_contract_blocks``
    used to join ``coordinator/snippets`` under the session's ``git_root``,
    which only composes when the session happens to be running inside the
    DoE-claude checkout itself and yields an empty snippets dir everywhere
    else.

    Resolution order:
      1. ``CLAUDE_PLUGIN_ROOT`` env var -- harness-injected, already shaped
         at content-root level (a DoE-side clone's ``coordinator/`` subdir,
         never the one-level-up repo root; see
         ``coordinator_core.install.check_install_singularity._to_plugin_root``
         for the same shape documented from the installer side).
      2. ``claude_config_dir()/plugins/coordinator-claude``, probed in both
         of its known on-disk shapes -- a DoE dev-clone (content nested
         under a ``coordinator/`` subdir) and a marketplace/OSS-mirror clone
         (content directly at that root) -- mirroring
         ``coordinator_root._resolve_plugin_root_for_machine_local``'s same
         two-shape probe for a different artifact.
      3. ``<machine_local_dir()>/.doe-root`` + ``coordinator`` -- the fleet's
         own pointer file, an in-process read with no spawn. Required because
         on a dev-clone box the live plugin root is a checkout OUTSIDE
         ``.claude`` entirely (``X:\\DoE-claude\\coordinator``), which rungs 1
         and 2 cannot see: rung 2's directory EXISTS there but holds only
         ``coordinator/bin``.

    EVERY rung PROBES FOR THIS RESOLVER'S OWN ARTIFACT (``snippets/``), never
    for mere directory existence. That distinction is the whole correctness of
    this function and it is not a style preference: an earlier revision
    returned the first candidate that ``is_dir()``, which on this fleet's
    dev-clone install returned ``~/.claude/plugins/coordinator-claude/
    coordinator`` -- a real directory containing only ``bin`` -- and thereby
    composed EMPTY contract blocks in every repo including DoE-claude itself,
    which had worked before. A stand-in probe is exactly the failure
    ``cater_subagent_start._resolve_role_append_snippet_path``'s own docstring
    already warned against for the sibling artifact.

    Returns ``None`` on a miss at every candidate -- callers fail open
    (empty contract-block assembly / "" role-append path), never raise.
    """

    def _has_content(candidate: Path) -> bool:
        return (candidate / "snippets").is_dir()

    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if env_root:
        candidate = Path(env_root)
        if _has_content(candidate):
            return str(candidate)

    plugin_base = claude_config_dir() / "plugins" / "coordinator-claude"
    for candidate in (plugin_base / "coordinator", plugin_base):
        if _has_content(candidate):
            return str(candidate)

    try:
        pointer = machine_local_dir() / ".doe-root"
        doe_root = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        doe_root = ""
    if doe_root:
        candidate = Path(doe_root) / "coordinator"
        if _has_content(candidate):
            return str(candidate)

    return None


def _extract_contract_block_body(
    snippet_text: str, header_style: str, sentinel_begin: str, sentinel_end: str
) -> Optional[str]:
    """header_style-aware extraction of a contract block's body from its
    canonical ``coordinator/snippets/<name>.md`` source text.

    Returns ``None`` on any extraction failure (unknown/missing markers) --
    the caller folds that into the all-or-nothing assembly contract, never a
    partial-body guess.
    """
    if header_style == "sentinel-embedded":
        result = _extract_sentinel_block(snippet_text, sentinel_begin, sentinel_end)
        return result.block if result is not None else None

    lines = snippet_text.split("\n")
    if len(lines) < 2:
        return None

    if header_style == "fixed-2-line":
        return "\n".join(lines[2:])

    if header_style == "fixed-2-line-strip-end-sentinel":
        return "\n".join(line for line in lines[2:] if line != sentinel_end)

    if header_style == "comment-block":
        skip = True
        out: list[str] = []
        for line in lines:
            if skip and line.startswith("<!--"):
                continue
            if skip and line.strip() == "":
                continue
            skip = False
            out.append(line)
        return "\n".join(out)

    return None


def _resolve_block_placeholders(text: str, values: Dict[str, str]) -> Optional[str]:
    """Substitute the closed placeholder set into ``text``.

    Returns ``None`` if any ``{{...}}`` occurrence names something outside
    ``values`` -- an unresolved placeholder must never leak literal braces
    into an assembled prompt (canonical spec §2.5); this is the single
    substitution pass that both detects and resolves in one walk.
    """
    unresolved: list[str] = []

    def _sub(match: "re.Match[str]") -> str:
        name = match.group(1)
        if name not in values:
            unresolved.append(name)
            return match.group(0)
        return values[name]

    resolved = _CONTRACT_PLACEHOLDER_RE.sub(_sub, text)
    if unresolved:
        return None
    return resolved


def _assemble_contract_blocks(
    block_names: Sequence[str],
    *,
    plugin_root: str,
    subagent_type: str,
    sidecar_path: Optional[str],
    doc_type: Optional[str],
) -> Optional[str]:
    """Assemble ``block_names`` (registry.toml snippet names, in order) into
    a single pre-joined ``injected_prompt_blocks`` string, or ``None`` on any
    assembly failure (canonical spec §2.6.1 -- all-or-nothing, never a
    partial contract).

    Bodies are extracted ``header_style``-aware off
    ``<plugin_root>/snippets/<name>.md`` (never off the DoE policy file --
    this module never re-reads ``subagent-sandbox-policy.yaml`` for the
    block-name list itself, only the already-resolved list the caller
    supplies), then have the closed three-placeholder set resolved. No
    wrapper/header/delimiter text is added around individual blocks; the
    join separator is exactly ``"\\n\\n"``. ``plugin_root`` is the
    plugin's own CONTENT root (``resolve_plugin_root()``), NOT the spawning
    session's git root -- the two only coincide when the session happens to
    be running inside the DoE-claude checkout itself (see
    ``resolve_plugin_root``'s docstring).
    """
    snippets_dir = Path(plugin_root) / "snippets"
    registry_path = snippets_dir / "registry.toml"

    try:
        registry_data = load_registry(registry_path)
    except RegistryError as exc:
        print(
            f"provision_report: contract-block assembly failed loading {registry_path}: {exc}",
            file=sys.stderr,
        )
        return None

    values = {
        "kind": doc_type or TEMPLATE_TYPES[0],
        "sidecar_path": sidecar_path or "",
        "subagent_type": subagent_type or "",
    }

    assembled_parts: list[str] = []
    for name in block_names:
        snippet_path = snippets_dir / f"{name}.md"
        try:
            snippet_text = snippet_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"provision_report: contract block {name!r} unreadable at {snippet_path}: {exc}",
                file=sys.stderr,
            )
            return None

        try:
            entry = get_snippet_entry(registry_data, name)
        except RegistryError as exc:
            print(
                f"provision_report: contract block {name!r} not registered in {registry_path}: {exc}",
                file=sys.stderr,
            )
            return None

        header_style = entry.get("header_style", "sentinel-embedded")
        if header_style not in _KNOWN_HEADER_STYLES:
            print(
                f"provision_report: contract block {name!r} has unknown header_style "
                f"{header_style!r}, expected one of {sorted(_KNOWN_HEADER_STYLES)}",
                file=sys.stderr,
            )
            return None

        body = _extract_contract_block_body(
            snippet_text, header_style, entry["sentinel_begin"], entry["sentinel_end"]
        )
        if body is None:
            print(
                f"provision_report: contract block {name!r} failed extraction "
                f"(header_style={header_style!r})",
                file=sys.stderr,
            )
            return None

        resolved = _resolve_block_placeholders(body, values)
        if resolved is None:
            print(
                f"provision_report: contract block {name!r} has an unresolvable placeholder "
                f"outside {sorted(_KNOWN_CONTRACT_PLACEHOLDERS)}",
                file=sys.stderr,
            )
            return None

        assembled_parts.append(resolved)

    return "\n\n".join(assembled_parts)


def assemble_contract_blocks_for_payload(
    payload: Dict[str, Any],
    *,
    cwd: Optional[str],
    report_sidecar_path: Optional[str],
) -> Optional[str]:
    """Independent second leg of spawn-time provisioning (canonical spec
    §2.3/§2.6.2): resolve + assemble ``payload["contract_blocks"]`` into an
    ``injected_prompt_blocks`` string.

    Deliberately does NOT call ``_provision`` or gate on
    ``report_sidecar_path`` being non-``None`` -- contract-assembly failure
    must not suppress the ``report_sidecar`` offer, and sidecar-provisioning
    failure must not suppress the contract (both legs fail open
    independently; neither ever blocks or denies a spawn). ``git_root`` and
    ``subagent_type`` are re-resolved here rather than threaded through from
    ``_provision``, since that function's early returns (ineligible type,
    missing session_id, unsanitizable segment) must not short-circuit this
    leg. Consumer-agnostic: reads only the block-name list the caller
    supplies on the payload -- carries no consumer-family identifier and no
    hardcoded block-name list; the ``{{kind}}`` fallback below reuses this
    module's pre-existing generic ``TEMPLATE_TYPES[0]`` CLI default, not a
    template-type literal baked in for this seam.
    """
    block_names = payload.get("contract_blocks")
    if not block_names:
        return None
    if not isinstance(block_names, list) or not all(isinstance(name, str) for name in block_names):
        print(
            "provision_report: contract_blocks must be a JSON list of strings, "
            "skipping contract assembly",
            file=sys.stderr,
        )
        return None

    # Non-spawning root read (C2, state/dispatch-briefs/2026-08-21-
    # catering-costs-what-the-work-costs/C2.md): eligible per
    # `resolve_git_root_cheap`'s own stated rule -- this leg (module
    # docstring: "deliberately NOT threaded through from _provision")
    # fails open to `None`/no-blocks on a miss, never a wrong VERDICT.
    # `repo_root.show_toplevel` walks and never spawns.
    git_root = _show_toplevel_no_spawn(cwd)
    if not git_root:
        return None

    # `payload["plugin_root"]` is the FIRST rung (C1, hook_http.payload_from_event's
    # computed body field, carried per-call from the forwarder). `resolve_plugin_root()`'s
    # own ambient probe (env var -> plugin dir -> `.doe-root` pointer) is the FALLBACK,
    # reached only when the payload is silent -- e.g. a direct in-process caller
    # (`cater_subagent_start.py`, `fan-out-dispatch.py`) that never went through the HTTP
    # hook seam at all. Never the other way around: see this module's docstring for the
    # resident-server env-freeze hazard the payload-first rung exists to avoid.
    plugin_root = payload.get("plugin_root")
    if not isinstance(plugin_root, str) or not plugin_root:
        plugin_root = resolve_plugin_root()
    if not plugin_root:
        return None

    _agent_id, _agent_type, subagent_type = resolve_effective_types(payload, git_root)
    doc_type = payload.get("type") or None

    return _assemble_contract_blocks(
        block_names,
        plugin_root=plugin_root,
        subagent_type=subagent_type,
        sidecar_path=report_sidecar_path,
        doc_type=doc_type,
    )


def _recover_orphaned_sidecar(
    git_root: Optional[str],
    payload: Dict[str, Any],
    subagent_type: str,
    kind: str = "report",
) -> Optional[str]:
    """This agent's existing sidecar when its ELIGIBILITY -- not merely its
    path -- was lost to a session-id change. ``None`` in every other case.

    The narrower, quieter half of the same defect the pointer index closes.
    ``engine.resolve_effective_types`` resolves the second eligibility leg
    through the back-pointer directory ``.agents/<canonical_id>/``, and that
    directory is keyed by ``<name>@session-<session_id[:8]>`` -- the REQUESTING
    session's id, baked into the key by
    ``session.identity.resolve_subagent_identity``'s named-teammate leg. When
    ``/clear`` mints a new session id, a NAMED teammate's back-pointer lookup
    misses, ``subagent_type`` comes back empty, and a dispatch that was
    admitted to ``report_sidecar`` through that leg alone now fails the
    eligibility test outright. Not a second empty sidecar: NO sidecar, and no
    emitted path for the agent to write into. The adoption check further down
    never runs, because the caller has already returned.

    A pointer is only ever written AFTER the policy admitted a spawn, so a
    pointer for this agent is standing proof that this agent was eligible --
    which is why recovering eligibility from one invents nothing. The sidecar
    it names is this agent's own prior document, and handing it back is the
    same continuity the adoption path provides, reached one step earlier.

    Gated to cost nothing on the population that does not need it. The check
    only touches disk when ALL of:
      - the back-pointer leg came back EMPTY (``subagent_type`` falsy) -- a
        spawn whose eligibility resolved normally has nothing to recover;
      - the raw id is NAMED-teammate shaped -- the bare-hex leg of
        ``resolve_subagent_identity`` ignores ``session_id`` entirely, so an
        unnamed agent's canonical id never moves and its back-pointer never
        misses for this reason;
      - a git root resolved.
    Every ineligible spawn outside that intersection -- which is nearly all of
    them, since most dispatches are unnamed -- pays two in-memory tests and no
    syscall.
    """
    if not git_root or subagent_type:
        return None
    raw_agent_id = str(payload.get("agent_id") or "")
    if not raw_agent_id or not _NAMED_TEAMMATE_RE.fullmatch(raw_agent_id):
        return None
    return _read_sidecar_pointer(git_root, raw_agent_id, kind)


def _provision_plan_derivable_doc(
    *,
    git_root: str,
    plan_stem: str,
    lens: str,
    agent_type: str,
    doc_type: Optional[str],
    session_id: str,
    plan_path: Optional[str] = None,
) -> Optional[str]:
    """Write (or idempotently reuse) the deterministic plan-derivable
    sidecar for one of the four G2 emitters; return its repo-relative path,
    or ``None`` when the stem-agreement guard refuses (caller falls open to
    the session-keyed home).

    Same exclusive-create-then-idempotent-hit discipline as the
    ``provision_key`` branch in ``_provision`` below: a re-dispatch against
    the SAME ``(plan_stem, lens)`` pair re-opens the existing doc rather
    than clobbering it -- unlike the session-keyed nonce path, this path
    is fully deterministic and has no nonce to fall back on, so
    idempotent-hit is the only collision-safe behavior available here.
    Archiving a STALE prior sidecar (rename-don't-delete, feeding the
    false-positive-arbitration feedback loop) stays an agent-side concern
    per canonical spec § 2.7 -- this module never renames or deletes an
    existing plan-sidecar file.

    Stem-agreement guard (state/bug-backlog/2026-08-07-lens-sidecar-
    provisioning-clobbers-a-peer-plans-sidecar.yaml): this refuses to write,
    or to hand back, a plan-derived sidecar whose declared plan identity
    disagrees with the filename stem it would sit under -- checked against
    the payload's own ``plan_path``, against the skeleton about to be
    written, and against an EXISTING file on the idempotent-hit path. The
    last is the one that stops a clobber COMPOUNDING: a sidecar whose
    ``plan:`` already names a different plan than its filename is a peer
    plan's artifact holding this plan's content, and handing it to another
    emitter to append to writes the same wrong artifact a second time.
    Refusal is a fail-open to the session-keyed home, never a dropped
    sidecar and never a rename -- this module still never renames or deletes
    an existing plan-sidecar file.
    """
    if _declared_plan_disagrees_with_stem(plan_path, plan_stem):
        print(
            f"provision_report: plan_path {plan_path!r} disagrees with plan-sidecar "
            f"stem {plan_stem!r}, falling back to the session-keyed home",
            file=sys.stderr,
        )
        return None

    plan_sidecars_dir = Path(git_root) / "state" / "plan-sidecars"

    doc_path = plan_sidecars_dir / f"{plan_stem}.{lens}.md"
    spawned_at = datetime.now(timezone.utc).isoformat()
    doc_text = _build_doc_text(
        agent_type, spawned_at, doc_type, lead_session_id=session_id, plan_path=plan_path
    )

    if _declared_plan_disagrees_with_stem(_plan_frontmatter_value(doc_text), plan_stem):
        print(
            f"provision_report: refusing to write {doc_path.name} -- its plan: "
            "frontmatter names a different plan than its filename stem",
            file=sys.stderr,
        )
        return None

    plan_sidecars_dir.mkdir(parents=True, exist_ok=True)

    try:
        with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)
        # SUBSUME: touch-claim (docs/plans/2026-08-05-in-process-writers-
        # declare-their-writes.md C2). `session_id` here is the RAW
        # payload["session_id"] (the dispatching session) -- see
        # session_scope.touch_written_path's docstring for the full
        # rationale and the phantom-live-peer guard it applies.
        session_scope.touch_written_path(
            session_id, f"state/plan-sidecars/{plan_stem}.{lens}.md", git_root
        )
    except FileExistsError:
        # Intended idempotent hit: a second run against the same plan +
        # lens finds its own prior sidecar and reuses it rather than
        # clobbering it -- mirrors the provision_key idempotent branch
        # below, for the same reason (deterministic path, no nonce). No
        # touch-claim here: this call did not actually write the bytes.
        try:
            existing_text = doc_path.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""
        if _declared_plan_disagrees_with_stem(
            _plan_frontmatter_value(existing_text), plan_stem
        ):
            print(
                f"provision_report: refusing to reuse {doc_path.name} -- its plan: "
                "frontmatter names a different plan than its filename stem",
                file=sys.stderr,
            )
            return None

    return f"state/plan-sidecars/{plan_stem}.{lens}.md"


def _provision(payload: Dict[str, Any], policy_path: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """Compute + write the report-sidecar doc; return its repo-relative path, or ``None``."""
    git_root = resolve_git_root(cwd)
    policy = load_policy(policy_path)

    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)

    is_eligible = agent_type in policy.report_sidecar or subagent_type in policy.report_sidecar
    if not is_eligible:
        # A named teammate whose back-pointer moved out from under it is not
        # ineligible -- it is unrecognizable. See _recover_orphaned_sidecar.
        return _recover_orphaned_sidecar(git_root, payload, subagent_type)

    effective_label = agent_type if agent_type in policy.report_sidecar else subagent_type

    # `effective_label` is ALSO the label the frontmatter `agent_type:` field
    # is stamped with (both `_provision_plan_derivable_doc` below and the
    # session-keyed leg's `_build_doc_text`), not just the one naming the
    # file. The two were separate reads -- filename off `effective_label`,
    # header off the RAW `agent_type` leg -- and they disagree for exactly the
    # populations the OR-resolver exists to serve:
    #   - back-pointer-only eligibility (no `agent_type` on the payload at
    #     all): the header stamped an EMPTY value while the filename carried
    #     the real type.
    #   - a NAMED teammate dispatch: `agent_type` is the teammate's own name.
    #   - a RESUMED agent: the harness reports `general-purpose` for it, so
    #     the header claimed a type the agent is not, on the one field a
    #     consumer filters by. Observed live 2026-08-29 on a resumed
    #     `coordinator:review-integrator` (reported by doe-claude-6c).
    # Reading the SAME label the filename and the eligibility test already
    # read closes that by construction -- the header can no longer disagree
    # with the name of the file it sits in.
    #
    # Negative spec: this never invents a type and never costs a
    # workflow-spawned agent its only identity. `effective_label` is
    # `agent_type` VERBATIM whenever `agent_type` is what made this spawn
    # eligible, which is the entire population with no back-pointer row to
    # resolve through; `subagent_type` is only reached when `agent_type` did
    # NOT carry an eligible type, i.e. when it was not the truer of the two.
    # Distinct from `_receipt_agent_type`, which is vocabulary-gated because a
    # receipt must be creditable against a closed set; the header has no such
    # set and takes the resolved label unconditionally.

    if not git_root:
        return None

    session_id = payload.get("session_id") or None
    if not session_id:
        return None

    # Plan-derivable leg (canonical spec § 2.7): only the named
    # plan-scoped-durable emitters, and only when this specific spawn also
    # carries a plan_path -- everything else (reviewer personas, and these
    # same agents' non-plan dispatch shapes) falls through unchanged to
    # the session-keyed path below. An unsanitizable plan_path fails open
    # to the session-keyed path rather than dropping the sidecar entirely.
    lens = _PLAN_DERIVABLE_LENS.get(effective_label)
    plan_path = payload.get("plan_path") or None
    if lens is not None and plan_path and git_root:
        plan_stem = _resolve_plan_sidecar_stem(str(plan_path))
        if plan_stem is not None:
            # AC4 -- lens-wins rule: a registered lens-named template wins
            # over payload["type"] on THIS leg only. Narrow by construction:
            # only a lens name that is actually a _TEMPLATE_REGISTRY key
            # takes this branch, so today only "plan-coverage-check" is
            # affected -- the other four lenses keep resolving whatever
            # type the payload supplies, exactly as before.
            if lens in _TEMPLATE_REGISTRY:
                doc_type = lens
            else:
                doc_type = payload.get("type") or None
            # A refusal from the stem-agreement guard is NOT a dropped
            # sidecar: it falls through to the session-keyed path below,
            # the same fail-open direction an unsanitizable plan_path takes.
            plan_sidecar = _provision_plan_derivable_doc(
                git_root=git_root,
                plan_stem=plan_stem,
                lens=lens,
                agent_type=effective_label,
                doc_type=doc_type,
                session_id=str(session_id),
                plan_path=str(plan_path),
            )
            if plan_sidecar is not None:
                return plan_sidecar

    sanitized_session_id = _sanitize_segment(str(session_id))
    sanitized_label = _sanitize_segment(str(effective_label))
    if sanitized_session_id is None or sanitized_label is None:
        return None

    # SUBSUME: resolve the optional deterministic provision_key BEFORE any
    # filesystem mutation (sanitize-before-path-touch) -- a rejected key then
    # fails open cleanly without leaving a stray empty session dir behind. Same
    # sanitizer confinement invariant as session_id/label: reduced to a single
    # separator-free segment, so the resulting leaf can never escape
    # subagent-share/.
    provision_key = payload.get("provision_key") or None
    sanitized_provision_key: Optional[str] = None
    if provision_key is not None:
        sanitized_provision_key = _sanitize_segment(str(provision_key))
        if sanitized_provision_key is None:
            return None
    elif agent_id:
        # `derived_key` branch (rationale: the 2026-08-15
        # concurrent-same-agent_type incident, and the load-bearing
        # `_sanitize_segment(derived_key)` re-check even though `agent_id`
        # already looks path-shaped -- a named-teammate id's `.+` segment
        # is NOT guaranteed sanitize-stable on its own). No caller-supplied
        # provision_key -> derive one deterministically from
        # `sanitized_label` + the already-resolved, already-canonicalized
        # `agent_id`, gated on `agent_id` surviving `_sanitize_segment`
        # unchanged; a malformed id falls through to the random-nonce path
        # below exactly as if no agent_id had resolved at all.
        if _sanitize_segment(str(agent_id)) == agent_id:
            derived_key = f"{sanitized_label}.{agent_id}"
            sanitized_provision_key = _sanitize_segment(derived_key)

    session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id

    # CONTINUITY: adopt this agent's EXISTING sidecar when the session id has
    # moved out from under it.
    #
    # The home is `state/subagent-share/<session_id>/<label>.<agent_id>.md` and
    # the idempotency below is a FileExistsError catch, so the continuity key
    # is the PAIR (session_id, agent_id). `/clear` mints a fresh session id
    # WITHOUT ending the process, so a subagent that outlives one re-fires
    # SubagentStart under a new session id, misses the FileExistsError branch,
    # and gets scaffolded a second, EMPTY sidecar while its populated one is
    # orphaned under the old id. Observed live 2026-08-29 on a resumed
    # `coordinator:review-integrator` (reported by doe-claude-6c): the run
    # report read as lost work, and only survived because the agent noticed
    # and hand-copied the file across.
    #
    # The orphan is worse than a duplicate. `reap-stale-subagent-sidecars.py`
    # gates preservation PER SESSION DIRECTORY on that session being live, so
    # the populated sidecars of a very-much-live session sit under a directory
    # named for a session id that no longer exists -- the gate that exists to
    # protect in-flight work aimed at the wrong directory.
    #
    # Only the deterministic-key branch is adopted into. The nonce branch has
    # no stable identity by construction (a fresh nonce per spawn), so there is
    # no continuity there to preserve and a re-dispatch is MEANT to open a new
    # doc.
    raw_agent_id = str(payload.get("agent_id") or "")
    # Empty, not None: the deterministic branch below is the only reader and
    # only reaches it under the same `sanitized_provision_key is not None`
    # condition that fills it, so a non-optional type keeps that guarantee
    # readable at the call sites instead of restating it as a narrowing.
    rel_path = ""
    if sanitized_provision_key is not None:
        rel_path = f"state/subagent-share/{sanitized_session_id}/{sanitized_provision_key}.md"
        if raw_agent_id and not (session_dir / f"{sanitized_provision_key}.md").exists():
            adopted = _read_sidecar_pointer(git_root, raw_agent_id)
            if adopted is not None and adopted != rel_path:
                return adopted

    # Review: the Staff Engineer -- sanitized_session_id is guaranteed separator-free by
    # _sanitize_segment, so this can only ever mkdir a direct child of
    # subagent-share/ (confinement invariant -- do not relax the sanitizer
    # without revisiting this). Deferred until after the adoption check above
    # so an adopting spawn leaves no stray empty session dir behind.
    session_dir.mkdir(parents=True, exist_ok=True)

    # SUBSUME: --type axis. `type` is read straight off the payload -- the
    # direct-call caller (fan-out-dispatch.py) never sets it, so `.get`
    # returns None and _build_doc_text falls back to the frozen legacy
    # run-report shape (see _build_doc_text's docstring). main() injects a
    # "run-report"-default type into the payload before reaching here, so
    # CLI invocations always resolve to the (enhanced) registry entry.
    doc_type = payload.get("type") or None

    spawned_at = datetime.now(timezone.utc).isoformat()
    # SUBSUME: lead_session_id frontmatter stamp -- the REQUESTING EM's
    # session id, raw (not the path-sanitized `sanitized_session_id` leaf
    # used for the directory name). Distinct identity from `agent_id`
    # (resolve_effective_types' first return leg above): agent_id is the
    # SPAWNED agent's own id, session_id/lead_session_id is who dispatched
    # it. Never conflate the two when reading this doc downstream.
    doc_text = _build_doc_text(effective_label, spawned_at, doc_type, lead_session_id=session_id)

    # AC1: stamp the review receipt on an eligible reviewer's OWN sidecar,
    # at dispatch, before the very first write -- see _splice_review_receipt
    # and _is_close_receipt_reviewer's docstrings above. Never applied to the
    # plan-derivable branch above (its four emitters are not delegate
    # reviewers) and never a second write -- one compose, one write, exactly
    # as AC12b's dispatch-leg budget measures.
    # AC2: the review-integrator counterpart, same seam, distinguishable
    # block/key (see _splice_integrator_receipt's docstring) -- mutually
    # exclusive with the branch above by construction (_INTEGRATOR_AGENT_TYPE
    # is not a member of CLOSE_RECEIPT_REVIEWERS), so an elif costs nothing
    # over a second independent `if` while making that exclusivity explicit.
    # The stamped type is RESOLVED (`_receipt_agent_type`), not raw: the two
    # eligibility checks read both labels, so the stamp has to as well or a
    # dispatch admitted via `subagent_type` gets stamped with `agent_type`'s
    # unrelated value -- for a named dispatch, the teammate's own name, which
    # no reader can credit. See `_receipt_agent_type`'s docstring.
    if _is_close_receipt_reviewer(agent_type, subagent_type):
        from coordinator_core.reviewer_vocabulary import CLOSE_RECEIPT_REVIEWERS

        stamped_type = _receipt_agent_type(agent_type or "", subagent_type or "", CLOSE_RECEIPT_REVIEWERS)
        doc_text = _splice_review_receipt(doc_text, str(session_id), agent_id or "", stamped_type, spawned_at)
    elif _is_review_integrator(agent_type, subagent_type):
        stamped_type = _receipt_agent_type(
            agent_type or "", subagent_type or "", {_INTEGRATOR_AGENT_TYPE}
        )
        doc_text = _splice_integrator_receipt(doc_text, str(session_id), agent_id or "", stamped_type, spawned_at)

    if sanitized_provision_key is not None:
        # SUBSUME: deterministic + idempotent path mode (provision_key present).
        doc_path = session_dir / f"{sanitized_provision_key}.md"
        try:
            with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(doc_text)
            # SUBSUME: touch-claim (docs/plans/2026-08-05-in-process-writers-
            # declare-their-writes.md C2). `session_id` here is the RAW
            # payload["session_id"] (the dispatching session) -- see
            # session_scope.touch_written_path's docstring for the full
            # rationale and the phantom-live-peer guard it applies.
            session_scope.touch_written_path(
                str(session_id),
                rel_path,
                git_root,
            )
            # CONTINUITY: record where this agent's sidecar went, so the next
            # spawn under a DIFFERENT session id can find it (see the adoption
            # check above). Best-effort -- a pointer that cannot be written
            # costs continuity, never the sidecar.
            if raw_agent_id:
                _write_sidecar_pointer(git_root, raw_agent_id, rel_path)
        except FileExistsError:
            # Intended idempotent hit, not an error: a chunk re-dispatch with
            # the same provision_key re-opens the SAME doc rather than
            # clobbering it -- preserve existing content, just return its
            # path. No touch-claim here: this call did not actually write
            # the bytes.
            pass

        return rel_path

    nonce = secrets.token_hex(4)
    doc_path = session_dir / f"{sanitized_label}-{nonce}.md"
    try:
        with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)
    except FileExistsError:
        # Review: the Staff Engineer -- second collision (astronomically unlikely at 32
        # bits) or any other OSError on this retry (e.g. ENOSPC, EACCES, a
        # mkdir race) is intentionally left unguarded here and falls through
        # to main()'s blanket except -- fail-open, emit nothing, never brick
        # the spawn.
        nonce = secrets.token_hex(4)
        doc_path = session_dir / f"{sanitized_label}-{nonce}.md"
        with open(doc_path, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(doc_text)

    # SUBSUME: touch-claim (docs/plans/2026-08-05-in-process-writers-declare-
    # their-writes.md C2). `session_id` here is the RAW payload["session_id"]
    # (the dispatching session) -- see session_scope.touch_written_path's
    # docstring for the full rationale and the phantom-live-peer guard it
    # applies.
    session_scope.touch_written_path(
        str(session_id),
        f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-{nonce}.md",
        git_root,
    )

    return f"state/subagent-share/{sanitized_session_id}/{sanitized_label}-{nonce}.md"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="coordinator_core.subagent_sandbox.provision_report",
        description="Spawn-time report-sidecar provisioner for the subagent-sandbox engine.",
    )
    parser.add_argument(
        "--policy",
        dest="policy_path",
        default=None,
        help="Explicit path to the subagent-sandbox-policy.yaml file "
        "(overrides SUBAGENT_SANDBOX_POLICY env var).",
    )
    parser.add_argument(
        "--cwd",
        dest="cwd",
        default=None,
        help="Working directory to resolve the git root from (defaults to "
        "the process cwd).",
    )
    parser.add_argument(
        "--type",
        dest="doc_type",
        choices=list(TEMPLATE_TYPES),
        default="run-report",
        help="Run-report sidecar template type (default: run-report). Only "
        "applied when the stdin payload doesn't already carry its own "
        "'type' field -- a direct _provision() caller's payload wins.",
    )
    args = parser.parse_args(argv)

    try:
        payload_text = _read_stdin()
        try:
            payload = json.loads(payload_text)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"provision_report: malformed stdin payload, skipping report-sidecar: {exc}", file=sys.stderr)
            return 0
        if not isinstance(payload, dict):
            print("provision_report: stdin payload is not a JSON object, skipping report-sidecar", file=sys.stderr)
            return 0

        # Thread the CLI --type into the payload only when the payload
        # itself is silent on type -- a caller who already stamped a
        # "type" field (rare, but possible for a scripted stdin payload)
        # keeps that value; the CLI default never overrides it.
        if "type" not in payload:
            payload["type"] = args.doc_type

        report_sidecar_path = _provision(payload, args.policy_path, args.cwd)

        # SUBSUME: contract_blocks / injected_prompt_blocks axis (canonical
        # spec §2.3/§2.6.2). Isolated in its own try/except so an unexpected
        # failure in this leg can never suppress the report_sidecar offer
        # already computed above -- the two legs fail open independently.
        injected_prompt_blocks: Optional[str] = None
        try:
            injected_prompt_blocks = assemble_contract_blocks_for_payload(
                payload, cwd=args.cwd, report_sidecar_path=report_sidecar_path
            )
        except Exception as exc:  # noqa: BLE001 — this leg must never brick a spawn either
            print(f"provision_report: unexpected error assembling contract blocks, skipping: {exc}", file=sys.stderr)
            injected_prompt_blocks = None

        output: Dict[str, str] = {}
        if report_sidecar_path is not None:
            output["report_sidecar"] = report_sidecar_path
        if injected_prompt_blocks:
            output["injected_prompt_blocks"] = injected_prompt_blocks
        if output:
            print(json.dumps(output))
    except Exception as exc:  # noqa: BLE001 — spawn-time hook must never brick a spawn (module contract above)
        print(f"provision_report: unexpected error, skipping report-sidecar: {exc}", file=sys.stderr)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
