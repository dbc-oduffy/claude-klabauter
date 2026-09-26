"""
coordinator_core.write_guards.block_unauthorized_claude_md_write — deny
dispatched-subagent Write/Edit/MultiEdit/NotebookEdit to any CLAUDE.md-class
surface absent a live session grant.

Purpose: the always-on-doctrine-surface guard DR-104 (2026-07-27) reintroduces
over DR-058. DR-058 (2026-07-15) removed the general subagent
write-outside-sandbox-confinement guard wholesale, reasoning that it
overrode EM dispatch intent with no offsetting benefit — a stray edit sits
in the branch buffer until the EM's own ``git diff`` review catches it.
That reasoning does not hold for CLAUDE.md-class surfaces: ``CLAUDE.md``
and its always-loaded companions are read into EVERY future session's
context automatically, before any diff review happens — a bloated addition
there is paid silently by every subsequent session, not caught by
after-the-fact review. DR-104's own new evidence: this exact plan's
originating audit measured +27% growth in a single dispatch wave, arriving
through many individually-plausible EXECUTOR-authored additions across
parallel dispatches — a volume/compounding failure DR-058's "the EM's diff
review missed one bad edit" framing does not fit, because review-after-the-
fact cannot hold a growth RATE down when the growth is spread across many
technically-fine-looking edits.

Detection is agent_id-PRESENCE only — modeled on
``block_subagent_plan_body_write``'s subagent-detection shape, but
DELIBERATELY simpler: this guard does NOT gate on a specific
``subagent_type`` (contrast ``block_subagent_plan_body_write``'s
``coordinator:executor``-only gate). Per DR-104 § Decision: "The
reintroduced guard is NOT scoped to the reviewer class only ... the
executor path is the entire justification for this override, so a
reviewer-scoped guard would have caught none of the +27% growth that
motivated it." Any payload carrying a non-empty top-level ``agent_id``
(i.e., a dispatched-subagent write, of ANY subagent_type) is in scope —
there is no back-pointer subagent_type lookup in this module at all.

Allow conditions (pass through):
  (1) No ``agent_id`` (top-level EM write) -> always allow. The EM's own
      CLAUDE.md edits are unaffected — this guard governs the DISPATCHED
      path DR-104's new evidence is about, not EM-inline authoring.
  (2) ``file_path`` does not match C2's ``is_claude_md_class`` (imported
      from ``coordinator_core.claude_md_budget``, the single predicate this
      guard and ``check_validate_commit``/the C3 ledger widening all
      share) -> allow.
  (3) The calling session holds a LIVE CLAUDE.md write grant
      (``coordinator_core.session.claude_md_grant.check_claude_md_write_grant``,
      C5) -> allow. Grant resolution is env-driven (same
      ``COORDINATOR_SESSION_ID`` / ``CLAUDE_SESSION_ID`` /
      ``CLAUDE_CODE_SESSION_ID`` chain the granting EM used) so a
      dispatched subagent's guard evaluation resolves to the SAME session
      id as the EM that acquired the grant, with no payload-session-id
      wiring required — see ``claude_md_grant``'s own
      SUBAGENT-RESOLVABILITY docstring section for why this holds
      structurally rather than by convention.
  (4) Override env ``COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE=1`` -> allow.
      Rare-use escape hatch, distinct from (3) — (3) is the NAMED,
      documented override path this guard's own deny text points to
      (design-as-offers: the deny leads with the better alternative before
      naming the raw bypass); this env var exists only for the same
      "read the hook source before invoking" rare-use class every sibling
      write-guard in this package carries.

DESIGN AS AN OFFER, NOT A NAG (binding — global CLAUDE.md § Implementation
Standards, ``coordinator/docs/wiki/eager-agent-calibration.md``). The deny
text leads with the better alternative — check whether the addition belongs
in a SessionStart EM channel or point-of-need doctrine first, per the
discharge hierarchy (DEC-6: mechanize / re-route-by-audience / state-in-full
/ delete-outright / accept-unenforced / relocate-to-wiki-LAST-RESORT) —
before naming the override. The deny text does NOT presuppose wiki-folding
as the default alternative: an earlier draft of this exact guard's deny
copy read "document-bloat-trim.md names the default fold target", which
inverts DEC-6 in the guard's own user-facing surface (a deny message read
on every future CLAUDE.md write is exactly where that inversion compounds
silently). This module names the FULL hierarchy instead, never presupposes
its last rung.

Self-referential scope, resolved deliberately (plan-coverage-checker
finding, DoE-claude 2026-07-27-claude-md-altitude-triage.md § C4): this
guard DOES cover DoE-claude's OWN repo-root ``coordinator/CLAUDE.md`` and
``CLAUDE.md`` — excluding the authoring repo would reproduce the exact
stated-vs-real scope gap this guard's own negative-spec calls out in
``check_blanket_git_add`` (see AC9 test module). Editing this repo's own
CLAUDE.md needs a session grant like any other CLAUDE.md-class surface;
that is intended behaviour, not an accident of the pattern.

Negative-spec — do NOT read this guard as reopening DR-058 generally:
  - DR-058 (2026-07-15, "Remove the subagent write-outside-sandbox
    confinement — it overrode EM intent for no gain") killed the GENERAL
    Write/Edit deny grammar wholesale. This guard does NOT resurrect that
    general grammar — it is scoped to exactly one path class
    (CLAUDE.md-class surfaces, per ``is_claude_md_class``), authorized by
    DR-104 as a NARROW, NAMED exception on new evidence DR-058's authors
    did not have (see module docstring "Purpose" above), never a
    reopening of DR-058's general reasoning. Every other surface a
    subagent writes (source, plans, handoffs, non-always-on docs) remains
    governed by DR-058 as written — EM intent still overrides there, and
    ``git diff`` review remains the accepted mitigation.
  - DR-047 (DoE/claude-klabauter boundary redraw, contract-vs-engine split) is why
    this guard is ENGINE-resident (``claude-klabauter
    coordinator_core/write_guards/``) rather than a DoE-side hook: the
    guard is a control-plane enforcement point, not doctrine prose, and
    DR-047's split places enforcement at the engine layer.
  - DR-050 (doctrine reversal requires new evidence) is the discipline
    DR-104 itself follows in overriding DR-058 for this one path class —
    see DR-104 § New evidence for the specific distinguishing fact (a
    volume/compounding failure through the dispatched-EXECUTOR path, not
    a corruption failure a diff review would catch) that clears DR-050's
    bar. This module's existence is DR-104's ``Consequence (accepted)``
    made structural, not a fresh, unrelated policy call.
  - Do NOT scope this guard's ``agent_id`` gate to ``coordinator:executor``
    only, mirroring ``block_subagent_plan_body_write``'s pattern. That
    would reproduce the exact defect DR-104 § Decision calls out by name:
    "a reviewer-scoped guard would have caught none of the +27% growth
    that motivated it." This guard fires on ANY dispatched subagent's
    agent_id-presence, full stop — there is deliberately no back-pointer
    subagent_type lookup anywhere in this module.
  - Do NOT widen the deny scope by hand-listing additional path patterns.
    ``is_claude_md_class`` (``coordinator_core.claude_md_budget``) is the
    SOLE class-membership predicate this guard consults — matching it by
    pattern (basename + immediate-parent-directory name), never a
    hardcoded path list, is that predicate's own stated purpose; a second,
    guard-local pattern list here would immediately re-create the kind of
    drift ``is_claude_md_class`` exists to end.

Spec backlink: DoE-claude DoE-claude:pln-claude-md-altitude-triage-earn-31f32e
  § C4
Precedent (module shape): coordinator_core/write_guards/
  block_subagent_plan_body_write.py
Precedent (real-scope-equals-stated-scope defect this guard's own AC9 test
  guards against): coordinator_core/bash_guards/dispatch_checks.py
  ``check_blanket_git_add`` — doctrine (``coordinator/docs/wiki/
  coordinator-tripwires.md`` § BLOCK-BLANKET-GIT-ADD) claims a wider deny
  scope than the guard's own code enforces (cwd-scoped to ``~/.claude``
  only); see ``coordinator_core/bash_guards/tests/
  test_check_blanket_git_add.py`` for the reusable pin shape this guard's
  own AC9 test follows.
Authorizing decisions: DoE-claude docs/decisions/DR-104 (this override),
  DR-058 (the general grammar this narrowly overrides), DR-047 (contract-
  vs-engine split — why this module is engine-resident), DR-050 (the
  reversal discipline DR-104 follows).
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, Optional

from coordinator_core.claude_md_budget import is_claude_md_class
from coordinator_core.session.claude_md_grant import check_claude_md_write_grant

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 45

#: census.md, chunk C18): the deny leg below is now DIRECTIONAL. The
#: the discharge hierarchy's first rung. CLASS/PRIORITY are unchanged --
#: not reconstruct, matching ``check_claude_md_size``'s own MATCHERS

_INTERCEPTED_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_CLAUDE_MD_WRITE"

#: remediation fail SILENTLY: the grant module is claude-klabauter-resident but the
#: unblocked, and claude-klabauter reaches the interpreter via PYTHONPATH rather than
_GRANT_CLI_INVOCATION_FALLBACK = (
    'PYTHONPATH="${REPO_CLAUDE_KLABAUTER:-${CLAUDE_KLABAUTER_ROOT:-$HOME/claude-klabauter}}" '
    'python3 -m coordinator_core.session.claude_md_grant grant pm "<verbatim PM note>"'
)


#: ``PYTHONPATH="…"`` the resolved root is interpolated into. The rendered command is
_ROOT_SHELL_UNSAFE = set('"\'`$\\\n\r')


def _grant_cli_invocation() -> str:
    """The grant-CLI command line the deny text offers, with this host's claude-klabauter
    root already resolved into it.

    The env-ladder form is only correct on a host where one of those variables
    is exported or where the repo sits at ``$HOME/claude-klabauter``. On a host
    where neither holds, every rung misses and the remediation the deny text
    names silently fails to run — a dead-end deny, which is the one thing a
    design-as-offers guard must never be. This guard executes inside the claude-klabauter
    engine, so it can resolve the root itself instead of asking the reader's
    shell to.

    Falls back to the env-ladder form whenever the resolved root cannot be used:
    a deny rendered with an imperfect remediation still beats a guard that raises
    while explaining itself. This function is called during deny RENDERING, so it
    must not propagate — an exception here converts a clean block into a crashed
    PreToolUse guard.

    The import is local rather than module-scope to keep ``engine_root``'s
    ``subprocess``/``shutil`` chain off the import path of every hook dispatch that
    never renders a deny.

    ``coordinator_engine_root()`` can reach Rung 2 (the ``machine-local``
    subprocess ladder, bounded at 2s) on this deny-render path, the opposite
    choice from ``bash_guards._helpers._resolve_override_keys_doc_display()``,
    which deliberately skips Rung 2 with the stated reason "a message
    builder must never spawn a process to render itself." The two differ
    deliberately, not by drift: this guard's deny renders only on a
    CLAUDE.md-write attempt (rare), vs. ``operator_override_note``'s pointer
    rendering on EVERY guard firing across the whole suite (hot path) --
    the render-frequency gap is what makes a bounded, occasional subprocess
    spawn here an accepted cost that would not be accepted on that hotter
    path.

    ``RuntimeError`` is the resolver's one documented failure (unresolvable
    ``repos.claude_klabauter``) and falls back silently. Anything else means this
    call site has drifted from the resolver — a rename, a signature change, a bug
    in the resolver itself — which would otherwise degrade this guard back to the
    exact defect it was written to fix, permanently and invisibly. That case still
    falls back, but says so on stderr rather than passing for normal operation.
    """
    try:
        from coordinator_core.engine_root import coordinator_engine_root

        root = coordinator_engine_root()
    except RuntimeError:
        return _GRANT_CLI_INVOCATION_FALLBACK
    except Exception as exc:  # noqa: BLE001 — deny-render path must never propagate
        print(
            "coordinator: block_unauthorized_claude_md_write could not resolve the "
            f"claude-klabauter root ({type(exc).__name__}: {exc}); the grant command below falls "
            "back to environment resolution and may not run as printed.",
            file=sys.stderr,
        )
        return _GRANT_CLI_INVOCATION_FALLBACK
    if not root or _ROOT_SHELL_UNSAFE & set(root):
        return _GRANT_CLI_INVOCATION_FALLBACK
    return (
        f'PYTHONPATH="{root}" '
        'python3 -m coordinator_core.session.claude_md_grant grant pm "<verbatim PM note>"'
    )


_CONTROL_CHARS = str.maketrans({c: " " for c in "\t\r\n\f\v"})


def _sanitize_file_path_for_reason(file_path: str) -> str:
    safe = file_path.translate(_CONTROL_CHARS)
    return "".join(ch for ch in safe if ord(ch) >= 0x20)


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _normalize_path(file_path: str) -> str:
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _simulate_new_content(
    tool_name: str, tool_input: Dict[str, Any], abs_file_path: str
) -> Optional[str]:
    if tool_name == "Write":
        return tool_input.get("content", "") or ""

    if tool_name in ("Edit", "MultiEdit"):
        try:
            with open(abs_file_path, "r", encoding="utf-8") as f:
                buf = f.read()
        except OSError:
            return None
        if tool_name == "Edit":
            old_s = tool_input.get("old_string", "")
            new_s = tool_input.get("new_string", "")
            if tool_input.get("replace_all"):
                return buf.replace(old_s, new_s)
            return buf.replace(old_s, new_s, 1)
        for edit in tool_input.get("edits", []) or []:
            old_s = edit.get("old_string", "")
            new_s = edit.get("new_string", "")
            if edit.get("replace_all"):
                buf = buf.replace(old_s, new_s)
            else:
                buf = buf.replace(old_s, new_s, 1)
        return buf

    # check_claude_md_size's own MATCHERS scope.
    return None


def _is_growth(tool_name: str, tool_input: Dict[str, Any], abs_file_path: str) -> Optional[bool]:
    """``True`` if the simulated post-edit content is STRICTLY LARGER (in
    UTF-8 bytes) than the current on-disk content (0 bytes for a file that
    does not yet exist). ``False`` for shrink or size-neutral. ``None``
    when direction cannot be determined -- see ``_simulate_new_content``.
    """
    try:
        new_content = _simulate_new_content(tool_name, tool_input, abs_file_path)
    except Exception:
        return None
    if new_content is None:
        return None

    try:
        with open(abs_file_path, "r", encoding="utf-8") as f:
            old_content = f.read()
    except OSError:
        old_content = ""
    except Exception:
        return None

    return len(new_content.encode("utf-8")) > len(old_content.encode("utf-8"))


def _advisory_reason(file_path: str) -> str:
    """The advisory text for the over-fire case (net shrink or
    size-neutral) -- design-as-offers: names the concrete alternative (the
    same session-grant path the deny leg names) rather than a bare notice,
    per the Axis-A firing-shape obligation (any emission that asks the
    agent to reconsider must name a concrete alternative).

    RESHAPE (C4, docs/plans/2026-08-08-discriminate-the-caller-on-the-
    write-grant.md): this guard's whole audience is dispatched subagents —
    a subagent cannot observe, from where it stands, whether its dispatch
    carries a PM ratification; it only hears what its EM tells it. The
    grant step is therefore attributed to the EM, not framed as something
    the reading subagent should itself run — that framing was this
    guard's original defect (the deny handed the very audience it was
    meant to bind a command that cleared its own gate).

    RESHAPE (C4(c), docs/plans/2026-08-13-guard-messages-stop-handing-
    agents-the-keys.md, AC-1/AC-2): this leg fires ONLY when ``agent_id``
    is present (``check()``'s allow condition (1) above) -- i.e. only for
    a dispatched subagent, never an EM-inline write. The prior render
    still spliced the resolved ``PYTHONPATH=... python3 -m
    coordinator_core.session.claude_md_grant grant pm ...`` invocation
    into this text, unconditionally, for that same audience -- the exact
    "shown the button and told not to press it" shape ``_deny_reason``'s
    own C4(b) reshape already closed on the DENY leg. Closed here too:
    the command is gone, no `payload=`/audience gate substitutes for it
    (this leg's audience is already known by construction, same reasoning
    as `_deny_reason`'s own docstring note on `payload=`).
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    return (
        "Advisory: this edit does not grow "
        f"`{file_path_safe}` (shrink or size-neutral), so it is allowed "
        "without a session grant -- the growth-only deny this guard "
        "enforces does not apply here. If a later edit in this same "
        "session GROWS the file instead, that edit will need a live "
        "session grant. Filing one is the EM's action, not this agent's -- "
        "report it as a dependency to your EM rather than working around it."
    )


def _deny_reason(agent_id: str, file_path: str) -> str:
    """The deny-with-offer text (design-as-offers, see module docstring).

    RESHAPE (C4, docs/plans/2026-08-08-discriminate-the-caller-on-the-
    write-grant.md): this deny only ever renders for a dispatched
    subagent (the check leg allows unconditionally when no ``agent_id``
    is present). A subagent cannot itself confirm PM ratification — it
    never speaks to the PM directly, only to its EM — so the remediation
    here is "report BLOCKED upward", never a runnable ``grant pm``
    invocation framed as the reader's own next step.

    RESHAPE (C4(b), docs/plans/2026-08-13-guard-messages-stop-handing-
    agents-the-keys.md): the rendered ``PYTHONPATH=... python3 -m ...``
    grant invocation and its cwd/session precondition (formerly rendered
    beneath "Unblock (EM runs this, not you):") are GONE. Per the
    recorded EM ruling in that plan's § Problem "The design premise (PM
    ruling, 2026-08-13)": the EM's route when blocked is "check with your
    PM", which is rung-1 familiar and needs no unfamiliar artifact — a
    message that refuses and says who to ask is complete. The text now
    hands the agent only the two pieces its BLOCKED report needs: the
    governed surface (``Target:``) and the structural reason
    (``Reason:``). No override pointer, wiki reference, or "an unlock
    exists" marker replaces the deleted lines — this guard fires only on
    a dispatched subagent's write and has no EM audience to point at.

    PARAGRAPH TRIM (C8, docs/plans/2026-08-13-guard-messages-stop-handing-
    agents-the-keys.md, PM ruling 2026-08-13): the "This is not a judgment
    on the edit -- ..." design-as-offers paragraph that used to sit ahead
    of "Report BLOCKED to your EM instead:" was deliberate when written and
    was held out of C8's original register sweep specifically for a PM
    ruling, because trimming it reverses that earlier ratified call. The PM
    ruled to trim it fully ("a PARAGRAPH is way overkill anyway") — this is
    that trim landing. The deny is now target + reason + route, same shape
    C4b/C2c already used elsewhere in this plan.

    Byte-budgeted prose (see ``docs/plans/2026-08-02-guard-message-size-
    discipline.md`` C8): leads with the BETTER ALTERNATIVE — the discharge
    hierarchy (DEC-6), named but not restated in full. ``agent_id`` is no
    longer echoed into the rendered text (the deny already reaches the
    firing subagent directly; the id added prose bytes with no reader who
    needed it there). "instead" in "Report BLOCKED to your EM instead:"
    still names what the agent does IN PLACE OF the write it was about to
    make (report upward, instead of writing), which is the correct
    AC6/AC7 framing — not a "run this instead of the thing you'd
    otherwise be blocked on" offer, which is the framing this guard
    exists to avoid handing a dispatched subagent.
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    return (
        "BLOCKED: needs a session grant -- check the discharge hierarchy "
        "(mechanize/reroute/wiki) first.\n"
        "Report BLOCKED to your EM instead:\n"
        f"  Target: `{file_path_safe}`\n"
        "  Reason: needs a live CLAUDE.md write grant for this session.\n"
    )
    # knows the answer to. `_OVERRIDE_ENV_VAR` stays wired in `check()`.
    # C4(b) removed the resolved `PYTHONPATH=... python3 -m ...` grant


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    agent_id = payload.get("agent_id") or ""
    if not agent_id:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None
    normalized = _normalize_path(file_path)
    if not is_claude_md_class(normalized):
        return None

    cwd = payload.get("cwd")
    granted, _record = check_claude_md_write_grant(cwd)
    if granted:
        return None

    # MATCHERS/PRIORITY): only a net-growth edit is denied. A shrink or
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    if os.path.isabs(file_path):
        abs_file_path = file_path
    else:
        abs_file_path = os.path.abspath(os.path.join(cwd or ".", file_path))
    growth = _is_growth(tool_name, tool_input, abs_file_path)
    if growth is False:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": _advisory_reason(file_path),
            }
        }

    reason = _deny_reason(agent_id, file_path)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
