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
state/subagent-share/conductor/seam-adjudication.md § 2.3, example-doctrine-repo):
when the stdin payload carries a ``contract_blocks`` JSON list of
``coordinator/snippets/<name>.md`` block names (resolved example-doctrine-repo-side from
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
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from coordinator_core.frontmatter.sentinel_blocks import extract_block as _extract_sentinel_block
from coordinator_core.session import scope as session_scope
from coordinator_core.snippet_sync.registry import (
    RegistryError,
    get_snippet_entry,
    load_registry,
)
from coordinator_core.subagent_sandbox.engine import (
    load_policy,
    resolve_effective_types,
    resolve_git_root,
)

#: Whitelist for a single path SEGMENT (label or session_id) -- everything
#: outside this set is dropped, never escaped/encoded.
_SEGMENT_WHITELIST_RE = re.compile(r"[^A-Za-z0-9._-]")

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
    absent, matching the existing ``dispatch_feed: null`` placeholder
    convention; in practice this is never absent when reached via
    ``_provision`` since ``session_id`` is required for eligibility."""
    return (
        "---\n"
        "status: open\n"
        f"agent_type: {agent_type}\n"
        f"spawned_at: {spawned_at}\n"
        f"lead_session_id: {lead_session_id if lead_session_id is not None else 'null'}\n"
        "divergence:\n"
        "  diverged: false\n"
        "commits: []\n"
        "dispatch_feed: null  # forward-declared, INERT until pcli-04 emitter\n"
        "---\n\n"
    )


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
    consumer (example-doctrine-repo ruling, 2026-08-10 memo): a type whose name promises
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
}


def _build_doc_text(
    agent_type: str,
    spawned_at: str,
    doc_type: Optional[str] = None,
    lead_session_id: Optional[str] = None,
) -> str:
    """Dispatch to the type-keyed template registry, or the frozen legacy
    run-report shape when ``doc_type`` is ``None`` (payload carried no
    ``type`` key -- the fan-out-dispatch.py back-compat path). An unknown
    ``doc_type`` string falls back to the run-report template rather than
    raising, consistent with this module's fail-open posture.
    ``lead_session_id`` (SUBSUME: the requesting EM's session id) is
    optional here purely so existing low-level callers that predate this
    field keep working unmodified -- ``_provision`` always supplies it."""
    if not doc_type:
        return _build_run_report_legacy_doc_text(agent_type, spawned_at, lead_session_id)
    builder = _TEMPLATE_REGISTRY.get(doc_type, _build_run_report_doc_text)
    return builder(agent_type, spawned_at, lead_session_id)


#: header_style dialects the assembler knows how to extract, mirroring
#: coordinator_core.snippet_sync.verify._extract_snippet_body's taxonomy
#: (that function is example-doctrine-repo-consumer-facing verification tooling and stays
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
    git_root: str,
    subagent_type: str,
    sidecar_path: Optional[str],
    doc_type: Optional[str],
) -> Optional[str]:
    """Assemble ``block_names`` (registry.toml snippet names, in order) into
    a single pre-joined ``injected_prompt_blocks`` string, or ``None`` on any
    assembly failure (canonical spec §2.6.1 -- all-or-nothing, never a
    partial contract).

    Bodies are extracted ``header_style``-aware off
    ``coordinator/snippets/<name>.md`` under ``git_root`` (never off the
    example-doctrine-repo policy file -- this module never re-reads
    ``subagent-sandbox-policy.yaml`` for the block-name list itself, only
    the already-resolved list the caller supplies), then have the closed
    three-placeholder set resolved. No wrapper/header/delimiter text is
    added around individual blocks; the join separator is exactly ``"\\n\\n"``.
    """
    snippets_dir = Path(git_root) / "coordinator" / "snippets"
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

    git_root = resolve_git_root(cwd)
    if not git_root:
        return None

    _agent_id, _agent_type, subagent_type = resolve_effective_types(payload, git_root)
    doc_type = payload.get("type") or None

    return _assemble_contract_blocks(
        block_names,
        git_root=git_root,
        subagent_type=subagent_type,
        sidecar_path=report_sidecar_path,
        doc_type=doc_type,
    )


def _provision_plan_derivable_doc(
    *,
    git_root: str,
    plan_stem: str,
    lens: str,
    agent_type: str,
    doc_type: Optional[str],
    session_id: str,
) -> str:
    """Write (or idempotently reuse) the deterministic plan-derivable
    sidecar for one of the four G2 emitters; return its repo-relative path.

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
    """
    plan_sidecars_dir = Path(git_root) / "state" / "plan-sidecars"
    plan_sidecars_dir.mkdir(parents=True, exist_ok=True)

    doc_path = plan_sidecars_dir / f"{plan_stem}.{lens}.md"
    spawned_at = datetime.now(timezone.utc).isoformat()
    doc_text = _build_doc_text(agent_type, spawned_at, doc_type, lead_session_id=session_id)

    try:
        with open(doc_path, "x", encoding="utf-8") as handle:
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
        pass

    return f"state/plan-sidecars/{plan_stem}.{lens}.md"


def _provision(payload: Dict[str, Any], policy_path: Optional[str], cwd: Optional[str]) -> Optional[str]:
    """Compute + write the report-sidecar doc; return its repo-relative path, or ``None``."""
    git_root = resolve_git_root(cwd)
    policy = load_policy(policy_path)

    agent_id, agent_type, subagent_type = resolve_effective_types(payload, git_root)

    is_eligible = agent_type in policy.report_sidecar or subagent_type in policy.report_sidecar
    if not is_eligible:
        return None

    effective_label = agent_type if agent_type in policy.report_sidecar else subagent_type

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
            doc_type = payload.get("type") or None
            return _provision_plan_derivable_doc(
                git_root=git_root,
                plan_stem=plan_stem,
                lens=lens,
                agent_type=agent_type,
                doc_type=doc_type,
                session_id=str(session_id),
            )

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

    session_dir = Path(git_root) / "state" / "subagent-share" / sanitized_session_id
    # Review: the Staff Engineer -- sanitized_session_id is guaranteed separator-free by
    # _sanitize_segment, so this can only ever mkdir a direct child of
    # subagent-share/ (confinement invariant -- do not relax the sanitizer
    # without revisiting this).
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
    doc_text = _build_doc_text(agent_type, spawned_at, doc_type, lead_session_id=session_id)

    if sanitized_provision_key is not None:
        # SUBSUME: deterministic + idempotent path mode (provision_key present).
        doc_path = session_dir / f"{sanitized_provision_key}.md"
        try:
            with open(doc_path, "x", encoding="utf-8") as handle:
                handle.write(doc_text)
            # SUBSUME: touch-claim (docs/plans/2026-08-05-in-process-writers-
            # declare-their-writes.md C2). `session_id` here is the RAW
            # payload["session_id"] (the dispatching session) -- see
            # session_scope.touch_written_path's docstring for the full
            # rationale and the phantom-live-peer guard it applies.
            session_scope.touch_written_path(
                str(session_id),
                f"state/subagent-share/{sanitized_session_id}/{sanitized_provision_key}.md",
                git_root,
            )
        except FileExistsError:
            # Intended idempotent hit, not an error: a chunk re-dispatch with
            # the same provision_key re-opens the SAME doc rather than
            # clobbering it -- preserve existing content, just return its
            # path. No touch-claim here: this call did not actually write
            # the bytes.
            pass

        return f"state/subagent-share/{sanitized_session_id}/{sanitized_provision_key}.md"

    nonce = secrets.token_hex(4)
    doc_path = session_dir / f"{sanitized_label}-{nonce}.md"
    try:
        with open(doc_path, "x", encoding="utf-8") as handle:
            handle.write(doc_text)
    except FileExistsError:
        # Review: the Staff Engineer -- second collision (astronomically unlikely at 32
        # bits) or any other OSError on this retry (e.g. ENOSPC, EACCES, a
        # mkdir race) is intentionally left unguarded here and falls through
        # to main()'s blanket except -- fail-open, emit nothing, never brick
        # the spawn.
        nonce = secrets.token_hex(4)
        doc_path = session_dir / f"{sanitized_label}-{nonce}.md"
        with open(doc_path, "x", encoding="utf-8") as handle:
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
