"""coordinator_core.write_guards.block_subagent_grant_record_write — deny a
dispatched subagent's Write/Edit/MultiEdit/NotebookEdit to the CLAUDE.md
write-grant record itself.

Purpose: NEW per PM ruling 2026-08-08 ("Two bypass vectors, now in scope",
vector 2). Before this module, nothing in ``write_guards/`` or
``bash_guards/`` guarded a subagent ``Write``/``Edit`` directly against
``<git-common-dir>/coordinator-sessions/<sid>/claude-md-write-grant.json`` —
the record ``coordinator_core.session.claude_md_grant`` writes and
``check_claude_md_write_grant`` reads to gate
``block_unauthorized_claude_md_write``. That record is not itself
CLAUDE.md-class, so the CLAUDE.md guard never fires on it, and
``check_claude_md_write_grant`` validates only enum shape / session-id
match / liveness — never who is asking to write it. A subagent could
therefore hand-author a live-looking grant record for its own session and
walk straight through the CLAUDE.md guard's allow leg (3). This module
closes that direct-artifact-write route.

Fires-on: any ``Write``/``Edit``/``MultiEdit``/``NotebookEdit`` whose
resolved target path is
``<git-common-dir>/coordinator-sessions/<sid>/claude-md-write-grant.json``
(any ``<sid>``), attempted by a dispatched subagent (non-empty top-level
``agent_id``).

IDENTITY-GATE aspect modelled on
``block_unauthorized_claude_md_write.py``: agent_id-PRESENCE only — no
back-pointer ``subagent_type`` lookup. Any dispatched subagent, of any
type, is in scope; this deliberately does not narrow to
``coordinator:executor`` the way ``block_subagent_plan_body_write`` does,
for the same reason ``block_unauthorized_claude_md_write`` does not narrow
to a reviewer class: a type-scoped gate here would leave the exact write
path this module exists to close open to every other subagent type.

PATH-RESOLUTION aspect modelled on ``block_memo_status_hand_edit.py``, NOT
on ``block_unauthorized_claude_md_write.py`` — that guard's own surface
(CLAUDE.md-class files) never lives under ``.git/coordinator-sessions/``,
so it has no session-hub resolution logic to copy. This module: resolves
the repo root via the shared
``coordinator_core.write_guards._repo_root.resolve_repo_root``, which
WALKS for a ``.git`` entry first and falls back to a
``git rev-parse --show-toplevel`` SPAWN only if that walk finds nothing up
to ``cwd.anchor`` (see Negative-spec below — this fallback is genuinely
reachable on the hot path, not merely theoretical), then the git COMMON
dir via ``coordinator_core.git.git_dir.resolve_git_common_dir``
(also pure path/text, no subprocess), joins ``coordinator-sessions/`` under
that resolved common dir, and gates containment on the RESOLVED path —
never a bare lexical/regex match on the raw ``file_path`` string. A ``..``
traversal segment anywhere in the candidate is COLLAPSED (lexically
resolved via ``posixpath.normpath``, no filesystem access) before the
containment/filename check runs, so the check always sees the candidate's
real resolved location — NOT ``block_memo_status_hand_edit``'s
``_TRAVERSAL_RE`` shape, which treats any traversal-bearing candidate as
out-of-scope/skip. That precedent's shape does not fit here: this guard's
allow leg needs "resolves elsewhere -> allow, resolves onto the grant
record -> deny", not "any traversal anywhere -> skip" (which would
silently reopen the original bypass) or "any traversal anywhere -> deny"
(which was this module's own prior, over-aggressive shape — see
Negative-spec below). Both sides of the containment comparison are
case-folded via ``casefold_path`` (same precedent, same rationale —
Windows and macOS APFS are case-insensitive).
The grant record's filename is imported as ``_GRANT_FILENAME`` from
``coordinator_core.session.claude_md_grant`` (verified value
``"claude-md-write-grant.json"``) rather than hand-copied, so a rename of
that constant cannot silently desync the two modules.

Concretely, why the naive lexical form fails: the real grant record is
written through ``core.session_dir``, which resolves the session hub via
this same git-common-dir logic. In a linked git worktree,
``<worktree>/.git`` is a gitdir-pointer FILE, not a directory — a bare
regex on the raw ``file_path`` (or a literal ``.git``-string join) can
miss the real write target entirely and silently fail to guard it, exactly
the trap ``coordinator_core.git.git_dir``'s own module docstring exists to
prevent.

Allow-conditions (fail-CLOSED is NOT appropriate here — this leg's allow
leg IS the EM path, mirroring ``block_unauthorized_claude_md_write``'s own
shape):
  (1) No ``agent_id`` (top-level EM write) -> always allow. The EM is the
      one who legitimately acquires and writes the grant.
  (2) Resolved path does not match
      ``<common-dir>/coordinator-sessions/<any-sid>/claude-md-write-grant.json``
      -> allow.
  (3) ``tool_name`` not in ``_INTERCEPTED_TOOLS`` -> allow (defense-in-
      depth, matches every sibling ``write_guards`` module's convention).

Design-as-offers (same posture as C1b/C4, the sibling legs of this same
plan): the deny text does NOT hand the firing subagent a self-executable
remediation — that is the exact defect class this plan closes (a subagent
that could self-serve a valid-looking grant record defeats the whole
point of gating the grant on PM ratification). It names report-BLOCKED-
upward as the subagent's own path, and that the EM is the one who
acquires and writes the grant.

Complementary channel, named honestly (not closed by this module alone):
this leg covers the Write-tool channel ONLY. It does not, and structurally
cannot, see a write performed *inside* a Bash-spawned Python process (e.g.
the grant module's own ``open()`` call under a ``python3 -c`` invocation) —
that never surfaces as a Write/Edit/MultiEdit/NotebookEdit tool call for
this module to match. The complementary Bash-channel leg is
``coordinator_core.bash_guards.block_subagent_grant_acquisition``
(plan chunks C1a/C1b), which classifies Bash invocation token shapes
instead. Neither leg subsumes the other — see
``docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md``
§ "Complementarity and the residual that survives both legs" for the full,
honest joint-residual statement: a Bash-channel route the bash-side
classifier does not recognise still reaches the file, and this module
cannot see it either, because its own matcher is the Write-tool surface,
not writes performed inside a spawned Python process.

Negative-spec:
  - Does NOT gate on ``subagent_type`` — agent_id PRESENCE only, mirroring
    ``block_unauthorized_claude_md_write``'s deliberate choice not to
    reproduce ``block_subagent_plan_body_write``'s narrower, type-scoped
    pattern (see "IDENTITY-GATE aspect" above).
  - Does NOT import ``coordinator_core.session.core.sessions_dir`` (nor
    call it indirectly). That resolver unconditionally SPAWNS ``git
    rev-parse --git-common-dir`` and returns ``""`` on failure. This
    module's ``git_dir.resolve_git_common_dir`` seam itself never spawns
    (pure path/text). Its upstream ``resolve_repo_root`` call, however, is
    NOT unconditionally spawn-free either — see the next bullet for the
    honest residual.
  - KNOWN RESIDUAL: ``resolve_repo_root`` (this module's repo-root input)
    delegates to ``coordinator_core.git.repo_root.show_toplevel``, which
    WALKS for a ``.git`` entry first (no spawn) but falls back to a
    ``git rev-parse --show-toplevel`` SPAWN if that walk finds nothing up
    to ``cwd.anchor``. That fallback IS reachable on this guard's
    PreToolUse hot path — e.g. a payload ``cwd`` outside any git worktree,
    or one whose ancestor chain to the filesystem root contains no ``.git``
    entry — and this module does nothing to prevent it; it is not a case
    this guard can special-case away, since a ``cwd`` with no discoverable
    repo root also has no resolvable ``coordinator-sessions`` hub for this
    guard to gate. Recorded here as a known residual rather than papered
    over — see ``coordinator_core.write_guards._repo_root`` and
    ``coordinator_core.git.repo_root`` for the resolver's own fuller
    accounting.
  - The two resolvers (this module's ``resolve_repo_root``-based seam vs.
    ``session.core.sessions_dir``'s spawning one) agree on the plain-clone
    and linked-worktree paths — both resolve to the same real session hub
    in both cases. They diverge only in the unresolvable-repo case (neither
    ``.git`` directory nor parseable gitdir-pointer file present, AND the
    walk's own spawn fallback also fails to find a toplevel): the spawning
    ``sessions_dir`` resolver returns ``""`` there, while this module's
    resolver fails open to a literal ``<repo_root>/.git`` join. That
    divergence is harmless — an unresolvable repo has no real grant-record
    write happening under it for this guard to miss.
  - Does NOT preserve a UNC path's ``//server/share`` root as a lexically
    distinguishable form THROUGH ``_normalize_path``'s slash-collapse — the
    collapse detects a leading ``//`` before collapsing and re-establishes
    it afterward, so a UNC-rooted candidate and a UNC-rooted
    ``sessions_root`` are collapsed identically and remain comparable
    (regression-pinned in ``TestUNCPathHandling`` in this module's test
    file). This is a normalization-symmetry property, not a claim that
    every UNC path variant (e.g. an alternate share alias for the same
    physical host) is deduplicated — string-identity containment still
    cannot see through two different UNC strings that resolve to the same
    physical file.
  - Does NOT hand-copy the grant filename literal — imports
    ``_GRANT_FILENAME`` from ``coordinator_core.session.claude_md_grant``
    so a rename there cannot silently desync the two modules.
  - Does NOT offer the firing subagent a self-executable remediation
    (see "Design-as-offers" above) — the whole point of this leg is that
    the subagent cannot itself acquire what it is being denied.
  - Does NOT allow a ``..`` traversal segment to bypass the segment-count
    check, AND does NOT treat "contains ``..``" as sufficient proof of "is
    the grant record" either — a candidate containing ``..`` is first
    LEXICALLY COLLAPSED via ``_collapse_traversal``/``posixpath.normpath``,
    and only the resolved result is containment/segment-count checked. An
    earlier integration pass on this module used the second (over-broad)
    shape — treating any ``..``-bearing candidate as an automatic in-scope
    match regardless of where it actually resolved — which denied every
    unrelated ``..``-bearing write anywhere in the repo (an unscoped
    false-positive on a hard-deny guard). That was a genuine regression,
    corrected by this collapse-then-check shape: a traversal-bearing
    candidate that resolves ONTO the grant record still denies (AC13, no
    NARROWER), and one that resolves anywhere else now correctly allows
    (AC13, no WIDER).
  - Does NOT compare candidate and sessions-root strings case-sensitively —
    both go through ``casefold_path`` before containment/filename matching,
    so a differently-cased candidate that names the same on-disk grant
    record on a case-insensitive filesystem (Windows, macOS/APFS) is still
    caught.

Spec backlink:
  docs/plans/2026-08-08-discriminate-the-caller-on-the-write-grant.md
  § chunk C9, § "Two bypass vectors, now in scope", § "Complementarity and
  the residual that survives both legs".
Precedent (identity-gate shape):
  coordinator_core/write_guards/block_unauthorized_claude_md_write.py
Precedent (path-resolution shape):
  coordinator_core/write_guards/block_memo_status_hand_edit.py
Complementary leg (Bash channel):
  coordinator_core/bash_guards/block_subagent_grant_acquisition.py
  (plan chunks C1a/C1b)
"""

from __future__ import annotations

import posixpath
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.session.claude_md_grant import _GRANT_FILENAME
from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]

#: PRIORITY 46 -- unique within the HARD-DENY phase (checked against the
#: full set of hard-deny modules' PRIORITY values at HEAD this session: 10,
#: 20, 30, 40, 45, 50, 56, 65, 125, 130, 132 taken). Ordering rationale:
#: this module governs the SAME artifact family (the write-grant surface)
#: as ``block_unauthorized_claude_md_write`` (PRIORITY 45) -- slotting
#: immediately after it keeps the two grant-adjacent hard-deny legs
#: co-located in the phase's evaluation order, ahead of the unrelated
#: ``block_consumed_handoff_edit`` (50) and ``block_memo_status_hand_edit``
#: (56) legs that follow. The phase runs first-non-None-wins, so relative
#: order among non-overlapping-path guards has no behavioral effect here --
#: this is a readability/grouping choice, not a correctness requirement.
PRIORITY = 46

#: Reference-shape tool-name guard (mirrors every sibling write_guards
#: module's defense-in-depth tool_name check).
_INTERCEPTED_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

#: Rare-use escape hatch — read the module docstring before invoking.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_GRANT_RECORD_WRITE"



def _normalize_path(file_path: str) -> str:
    """Backslash -> slash, collapse repeated slashes — same helper shape
    as ``block_subagent_plan_body_write._normalize_path``, PLUS a UNC-root
    preservation step that sibling does not need (no sibling call site
    handles a network-hosted repo).

    A leading ``//`` (or ``\\\\``, already backslash-converted above) marks
    a UNC root (``\\\\server\\share\\...`` -> ``//server/share/...``) — the
    doubled slash is semantically load-bearing there, not accidental
    repetition. Detect it BEFORE the collapse and re-establish exactly one
    extra leading slash afterward, so a UNC-rooted path and its
    ``coordinator-sessions``-joined sibling both keep the same UNC marker
    through this function rather than one silently losing it to
    ``/server/share/...`` (see module docstring Negative-spec, UNC bullet).
    """
    normalized = file_path.replace("\\", "/")
    is_unc = normalized.startswith("//")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if is_unc and not normalized.startswith("//"):
        normalized = "/" + normalized
    return normalized


def _collapse_traversal(abs_path: str) -> str:
    """Lexically collapse ``.``/``..`` segments in an already-absolute,
    forward-slash-normalized candidate via ``posixpath.normpath`` — pure
    string manipulation, no filesystem access (no ``stat``, no symlink
    following, no spawn), safe to run unconditionally on the PreToolUse
    hot path.

    This is the fix for a real defect a prior integration pass introduced:
    that pass rejected ANY ``..``-bearing candidate as an automatic match
    (deny), on the theory that a lexical segment-count check cannot be
    trusted once ``..`` is present. That is true, but "cannot be trusted
    lexically" is a reason to RESOLVE before deciding, not a reason to
    deny unconditionally — the prior shape denied every unrelated
    ``..``-bearing write anywhere in the repo (e.g. ``../sibling/x.py``),
    an unscoped false positive far worse than the narrow bypass it closed.
    Collapsing here first means the containment/filename check downstream
    runs against the path's real resolved location: a ``..``-bearing
    candidate that resolves ONTO the grant record still denies (the
    original bypass stays closed), and one that resolves anywhere else
    now correctly allows.
    """
    is_unc = abs_path.startswith("//")
    collapsed = posixpath.normpath(abs_path)
    if is_unc and not collapsed.startswith("//"):
        collapsed = "/" + collapsed
    return collapsed


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """``file_path``, falling back to ``notebook_path`` for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _resolve_git_common_dir(cwd: Optional[str]) -> Optional[str]:
    """Resolve ``cwd``'s repo's git COMMON dir without spawning ``git``.

    Mirrors ``block_memo_status_hand_edit._resolve_git_common_dir``: first
    resolves the repo root via the shared, non-spawning
    ``resolve_repo_root``, then the common dir via the pure-Python
    ``resolve_git_common_dir`` seam. Fails open (``None``) when the repo
    root itself cannot be resolved — see module docstring's Negative-spec
    for why that divergence from the spawning ``sessions_dir`` resolver is
    harmless.
    """
    repo_root = resolve_repo_root(cwd)
    if not repo_root:
        return None
    return str(resolve_git_common_dir(Path(repo_root)))


def _is_grant_record_path(normalized_file_path: str, common_dir: str) -> bool:
    """``True`` iff ``normalized_file_path`` (already backslash/slash-
    normalized), resolved against ``cwd``/``common_dir``, is a
    ``coordinator-sessions/<any-sid>/<_GRANT_FILENAME>`` path under the
    resolved git common dir. Containment and filename match are both
    against the RESOLVED path, never a raw-string regex.

    A ``..`` traversal segment anywhere in ``normalized_file_path`` is
    RESOLVED, not treated as an automatic match -- see
    ``_collapse_traversal``'s docstring for why a bare containment test
    (``_TRAVERSAL_RE.search(...) -> True means deny``) is the wrong shape
    here: it would deny every ``..``-bearing write anywhere in the repo,
    not just one that lexically resolves onto the grant record. Without
    SOME traversal handling, though, a candidate like
    ``<sid>/x/../claude-md-write-grant.json`` lexically resolves onto the
    real grant record but produces a THREE-segment remainder, so the naive
    ``len(parts) == 2`` check falls through to ``False`` (allow) even
    though the write lands on the guarded artifact -- that is the original
    bypass this module exists to close. Resolving the candidate via
    ``_collapse_traversal`` BEFORE the containment/segment-count check
    keeps both properties true at once: a traversal-bearing candidate that
    resolves onto the grant record still denies, and one that resolves
    anywhere else allows (AC13 scope-equals-enforcement: no wider, no
    narrower).

    Both sides of the containment comparison are case-folded via
    ``casefold_path`` -- same reason ``block_memo_status_hand_edit`` folds:
    Windows and macOS (APFS default) are case-insensitive filesystems, so
    a differently-cased candidate is the SAME on-disk file as the
    resolved sessions root but a bare ``.startswith()`` would not match
    it, letting the guard go silently inert on either platform.
    """
    if normalized_file_path.startswith("/") or (
        len(normalized_file_path) >= 2 and normalized_file_path[1] == ":"
    ):
        abs_candidate = normalized_file_path
    else:
        abs_candidate = _normalize_path(
            str(Path(common_dir) / normalized_file_path)
        )

    abs_candidate = _collapse_traversal(abs_candidate)

    abs_candidate_cf = casefold_path(abs_candidate)

    sessions_root = casefold_path(
        _normalize_path(str(Path(common_dir) / "coordinator-sessions")).rstrip("/") + "/"
    )

    if not abs_candidate_cf.startswith(sessions_root):
        return False

    remainder = abs_candidate_cf[len(sessions_root):]
    parts = remainder.split("/")
    # <sid>/<_GRANT_FILENAME> -- exactly two remaining path segments.
    return (
        len(parts) == 2
        and parts[0] != ""
        and parts[1] == casefold_path(_GRANT_FILENAME)
    )


def _deny_reason(file_path: str, payload: Optional[Dict[str, Any]] = None) -> str:
    """Design-as-offers deny text (same posture as the plan's C1b/C4
    sibling legs) -- names report-BLOCKED-upward as the subagent's path,
    and that the EM is the one who acquires/writes the grant. Deliberately
    offers no self-executable remediation to the firing subagent -- see
    module docstring "Design-as-offers".

    PARAGRAPH TRIM (C8, docs/plans/2026-08-13-guard-messages-stop-handing-
    agents-the-keys.md, PM ruling 2026-08-13): the "This is not a judgment
    on the edit -- ..." paragraph was deliberate when written and was held
    out of C8's original register sweep for a PM ruling, since trimming it
    reverses that earlier ratified design-as-offers call. The PM ruled to
    trim it fully -- this is that trim landing, applied in the same target +
    reason + route shape ``block_unauthorized_claude_md_write`` now uses.
    """
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        "BLOCKED: writing the CLAUDE.md write-grant record directly is not "
        "available to a dispatched agent.\n\n"
        "Report BLOCKED to your EM instead:\n"
        f"  Target: `{file_path}`\n"
        "  Reason: the CLAUDE.md write-grant record is EM-acquired, "
        "not subagent-writable.\n"
        "  Unblock (EM runs this, not you): acquire a live session grant "
        "via the grant CLI, then re-dispatch."
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the grant-record write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the hard-deny envelope. See module
    docstring "Allow-conditions" for the three pass-through cases.
    """
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    # (1) No agent_id -> EM-inline write -> allow.
    agent_id = payload.get("agent_id") or ""
    if not agent_id:
        return None

    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    cwd = payload.get("cwd")
    common_dir = _resolve_git_common_dir(cwd)
    if not common_dir:
        return None

    normalized = _normalize_path(file_path)
    if not _is_grant_record_path(normalized, common_dir):
        return None

    reason = _deny_reason(file_path, payload)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
