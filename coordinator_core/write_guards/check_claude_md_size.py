"""coordinator_core.write_guards.check_claude_md_size — hard-deny SIZE leg
ported from example-doctrine-repo ``coordinator/hooks/scripts/check-claude-md-size.py``.

Purpose: that example-doctrine-repo hook carries TWO INDEPENDENT checks against a governed
(fleet-loaded) CLAUDE.md-class target -- a byte-size budget (soft exit-1
warning, hard exit-2 block) and a wholly separate C7 admission-gate check
against a per-heading-section disposition ledger. Only the byte-budget
check's HARD (exit-2) half folds into this engine. Two EM-ratified decisions
this module deliberately does NOT reopen (see
``docs/plans/2026-07-29-hook-fan-in-write-path.md`` § C8):

  - The C7 admission-gate ledger check stays example-doctrine-repo-resident, permanently.
    Porting it would require this engine to resolve the example-doctrine-repo repo root and
    read example-doctrine-repo working-data (``state/audits/2026-07-27-doctrine-envelope-
    classification.md``) at hook time -- inverting the coordinator-claude
    depends-on-claude-klabauter direction the tri-plane split establishes.
  - The byte budget's SOFT (exit-1) half does NOT port and does NOT become
    a model-facing advisory. It stays exactly as-is -- stderr to the user
    terminal only -- in the residual example-doctrine-repo hook. This engine surfaces at most
    ONE advisory per payload; a warning firing on nearly every governed
    CLAUDE.md-class edit would routinely occupy that slot and suppress
    whichever other advisory would otherwise have fired.

So the example-doctrine-repo hook's post-fold responsibility narrows to: the admission-gate
ledger check, and the soft-limit warning. Both continue to run there,
unchanged, exactly as before this port.

Thresholds and the governed-surface discriminant are consumed from
``coordinator_core.claude_md_budget`` -- the claude-klabauter-owned SSOT this repo
already defines (this module's own package, not a cross-repo import) --
rather than re-declaring local literals.

Fidelity notes:
  - The blocked CONDITION is unchanged: a governed CLAUDE.md-class target
    whose POST-EDIT simulated content exceeds ``HARD_LIMIT_BYTES``.
  - ``permissionDecisionReason`` is the legacy stderr text with trailing
    whitespace stripped, per the stderr-normalization rule this plan's C2
    map states (shared with C1's differential harness) -- no re-wrapping,
    truncation, or re-punctuation beyond that strip.
  - The token-count annotation (best-effort, via
    ``coordinator_core.ops.measure_token_envelope.estimate_tokens``) is
    preserved verbatim in the message shape; a token-estimate failure omits
    the annotation rather than failing the whole check (matches the source
    hook's own best-effort framing).
  - Simulation failures (unreadable existing file, decode error, any
    exception raised while reconstructing the proposed post-edit content)
    ALLOW (return ``None``) rather than deny -- this mirrors the source
    hook's own explicit ``except: sys.exit(0)`` around its ``simulate()``
    call. This is the source's OWN fail-open choice on this leg, preserved
    as-is -- not a general fail-open policy for this module's CLASS.

Negative-spec -- do NOT "complete" this port while reading it:
  - Do NOT add the soft-limit (exit-1) warning here as an advisory. See
    "Purpose" above for why that was an EM-ratified decision, not an
    oversight.
  - Do NOT add the C7 per-heading admission-gate ledger check here. It has
    no example-doctrine-repo-repo access from this engine and is not in scope for this module.
  - Do NOT widen ``MATCHERS`` to include ``NotebookEdit``. The source hook
    registers on ``Write|Edit|MultiEdit`` only (hooks.json, entry #2) --
    confirmed against ``docs/plans/2026-07-29-hook-fan-in-write-path.md``'s
    C2 priority map, not ``write_guards/INTERFACE.md``'s illustrative
    four-matcher example.

C7b addendum (``docs/plans/2026-07-30-boot-doctrine-cut-and-refill-gate.md``
§ C7b): this leg now ALSO enforces the AC4 per-surface ratchet watermark
(``coordinator_core.claude_md_budget.parse_watermark`` / ``ratchet_check``),
read from the SAME repo-local ledger file convention the (still example-doctrine-repo-resident,
still out of scope here) admission gate uses -- ``state/audits/<surface-
slug>-classification.md`` under the SAME working tree the edit targets. This
is not the cross-repo "resolve the example-doctrine-repo repo root" read the negative-spec
above rules out: it is a plain repo-local file read against whatever repo
``abs_file_path`` already lives in, exactly like the byte-size check below
already reads that same file's own bytes. Repo root is resolved by directory
walk-up (``_find_repo_root``), never a ``git`` subprocess spawn, to keep this
hot-path check spawn-free per this repo's standing anti-spawn ruling. A
governed surface outside any repo, or with no ledger/watermark at the
resolved path, is simply unarmed -- the flat ``HARD_LIMIT_BYTES`` above
still applies regardless. A ledger that IS present but malformed (a
``## Watermark`` section missing its ``- Bytes:``/``- Reason:`` row) is
ALSO treated as unarmed for this leg (fail OPEN, with a stderr warning
naming the ledger path) rather than denied -- this module carries no
``COORDINATOR_OVERRIDE_*`` escape hatch, so a hard deny here would be a
wedge with no in-harness way out over auxiliary-ledger corruption that
says nothing about the edit in hand. This mirrors ``_simulate``'s own
already-documented fail-open choice on parse/reconstruction failure above,
making the two failure paths on this module consistent.

CLASS/PRIORITY history: flipped from `hard-deny` @ 15 to `advisory` @ 106 by
plan chunk C2 of `docs/plans/2026-08-06-apply-guard-class-census.md`, per
DR-277 (guards are advisory by default). Both deny returns (the ratchet-
breach leg and the flat `HARD_LIMIT_BYTES` leg) became advisory
`additionalContext` envelopes -- neither carries `permissionDecision`.
106 sits below `guard_concrete_path_citations` (advisory, PRIORITY 111,
matches root `CLAUDE.md` by exact filename via `_LOUD_EXACT_FILES`), which
would otherwise win the single advisory slot on every root-CLAUDE.md write
and silently drop this guard's message. This module still carries no
`COORDINATOR_OVERRIDE_*` key -- unchanged by the flip, per
docs/wiki/write-guard-priority-bands.md.

Spec backlink: docs/plans/2026-07-29-hook-fan-in-write-path.md § C8;
  docs/plans/2026-08-06-apply-guard-class-census.md (chunk C2);
  docs/decisions/DR-277-guards-are-advisory-by-default-two-named.md;
  CLASS/MATCHERS/PRIORITY convention per
  docs/wiki/write-guard-priority-bands.md (the real SSOT -- the prior
  citation, state/plan-sidecars/2026-07-29-hook-fan-in-write-path.priority-
  map.md, named a file that was never written);
  ratchet watermark per docs/plans/2026-07-30-boot-doctrine-cut-and-refill-
  gate.md § C7b.
Reference (example-doctrine-repo source, SIZE leg only -- the per-heading admission-gate leg
  and the soft warning remain there): example-doctrine-repo
  coordinator/hooks/scripts/check-claude-md-size.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.claude_md_budget import (
    HARD_LIMIT_BYTES,
    RatchetWatermarkError,
    is_governed_claude_md,
    load_audience_manifest,
    parse_watermark,
    ratchet_check,
    resolve_ledger_path,
)
from coordinator_core.ops.measure_token_envelope import estimate_tokens


def _find_repo_root(start: str) -> Optional[str]:
    """Walk up from ``start`` looking for a ``.git`` entry (directory or
    file, for worktrees/submodules) -- no ``git`` subprocess spawn, per this
    repo's standing anti-spawn ruling on the PreToolUse hot path."""
    current = Path(start).resolve()
    if current.is_file():
        current = current.parent
    for candidate in [current, *current.parents]:
        if (candidate / ".git").exists():
            return str(candidate)
    return None

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 106

_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit"}


def _simulate(tool_name: str, tool_input: Dict[str, Any], abs_file_path: str) -> Optional[str]:
    """Reconstruct the FULL post-edit file content -- never just the
    proposed new fragment -- since the byte budget below is measured
    against the whole file, not the delta. Mirrors the source hook's own
    ``simulate()`` byte-for-byte (same replace-once-vs-replace-all branch,
    same read-before-replace shape for Edit/MultiEdit)."""
    if tool_name == "Write":
        return tool_input.get("content", "") or ""

    if tool_name == "Edit":
        with open(abs_file_path, "r", encoding="utf-8") as f:
            cur = f.read()
        old_s = tool_input.get("old_string", "")
        new_s = tool_input.get("new_string", "")
        if tool_input.get("replace_all"):
            return cur.replace(old_s, new_s)
        return cur.replace(old_s, new_s, 1)

    if tool_name == "MultiEdit":
        with open(abs_file_path, "r", encoding="utf-8") as f:
            buf = f.read()
        for edit in tool_input.get("edits", []) or []:
            old_s = edit.get("old_string", "")
            new_s = edit.get("new_string", "")
            if edit.get("replace_all"):
                buf = buf.replace(old_s, new_s)
            else:
                buf = buf.replace(old_s, new_s, 1)
        return buf

    return None


def _token_note(new_content: str) -> str:
    """Best-effort token annotation -- omitted (not a failure) if the
    estimator raises, matching the source hook's own best-effort framing."""
    try:
        tokens = estimate_tokens(new_content)
    except Exception:
        return ""
    if tokens is None:
        return ""
    return f", ~{tokens} tokens (estimate)"


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the CLAUDE.md SIZE hard-deny leg against a PreToolUse
    payload. Returns ``None`` (allow) or the nested hard-deny envelope.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return None
    abs_file_path = os.path.abspath(file_path)

    repo_root = _find_repo_root(abs_file_path)
    audience_manifest = load_audience_manifest(repo_root) if repo_root else []

    if not is_governed_claude_md(
        abs_file_path, repo_root=repo_root, audience_manifest=audience_manifest
    ):
        return None

    try:
        new_content = _simulate(tool_name, tool_input, abs_file_path)
    except Exception:
        return None
    if new_content is None:
        return None

    size = len(new_content.encode("utf-8"))

    # C7b (AC4): the per-surface ratchet watermark -- unarmed (no ledger, no
    # "## Watermark" section) is a silent no-op. A malformed ("armed but
    # broken") watermark fails loud, same as the example-doctrine-repo-resident admission gate.
    if repo_root:
        surface = os.path.relpath(abs_file_path, repo_root).replace(os.sep, "/")
        ledger_path = resolve_ledger_path(repo_root, surface)
        try:
            watermark = parse_watermark(ledger_path)
        except RatchetWatermarkError as exc:
            # A malformed ledger is auxiliary-bookkeeping corruption, NOT a
            # statement about whether THIS edit is legitimate -- denying
            # every edit to the governed surface until an operator happens
            # to notice and hand-repair the ledger is a wedge with no
            # escape hatch (this module carries zero COORDINATOR_OVERRIDE_*
            # keys). Fail OPEN on the ratchet leg exactly like `_simulate`'s
            # own explicit fail-open above (this makes the two failure
            # paths on this module consistent, not merely fixes one of
            # them): treat the surface as UNARMED for this evaluation (same
            # as "no ledger" -- `watermark = None`), print a stderr warning
            # naming the malformed ledger path so the operator can repair
            # it, and fall through to the real size/ratchet evaluation
            # below. The flat `HARD_LIMIT_BYTES` check further down still
            # applies regardless of this leg's outcome.
            print(
                f"[check_claude_md_size] WARNING: ratchet watermark ledger "
                f"at {ledger_path} is malformed, ratchet leg unarmed for "
                f"this edit ({exc})",
                file=sys.stderr,
            )
            watermark = None
        ratchet_ok, ratchet_msg = ratchet_check(size, watermark)
        if not ratchet_ok:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": f"Advisory: {ratchet_msg}",
                }
            }

    if size <= HARD_LIMIT_BYTES:
        return None

    reason = (
        f"Advisory: {file_path} would be {size} bytes{_token_note(new_content)}, "
        f"over the {HARD_LIMIT_BYTES}-byte cap. Trim content or split the edit."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reason,
        }
    }
