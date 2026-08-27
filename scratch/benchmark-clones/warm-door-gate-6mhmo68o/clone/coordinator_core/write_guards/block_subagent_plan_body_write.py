"""
coordinator_core.write_guards.block_subagent_plan_body_write — Python
engine-ification of DoE's retired
``coordinator/hooks/scripts/block-subagent-plan-body-write.sh`` PreToolUse
hook (deleted 2026-07-16, DoE ``2f8b8450``).

Purpose: makes ``docs/plans/**/*.md`` plan bodies AND ``docs/problems/**/*.md``
ratified problem-sets immutable to ``coordinator:executor`` subagents.
Executors under the Write-Ahead Status protocol repeatedly stamp "Execution
in progress / complete" into the plan body even when the dispatch brief
says not to — the impulse is structural (agent prompt MUST wins over brief
text in the majority of observed cases). The fix is structural too: this
guard, not calibration alone.

Widened 2026-07-24 to also cover ``docs/problems/**`` (ratified problem-sets
are EM-authored spec artifacts, same immutability rationale as a plan body)
per DoE's agent-citizenship provisioning plan C9(c) — see spec backlink
below. ``docs/wiki/**`` and ``docs/decisions/**`` are deliberately EXCLUDED:
executors are routinely dispatched to author wikis/DRs as their deliverable,
and blocking those would break legitimate delegated authoring.

This is a faithful engine-ification, not a redesign: it ports the reference
hook's identity resolver (bash ``resolve_subagent_identity`` /
``cs_build_canonical_agent_id``), the back-pointer subagent_type lookup, and
the AMBIGUOUS collision-sentinel fail-closed branch. The deny-reason strings
were originally ported verbatim; both were subsequently compressed to fit
the 220-byte runtime prose cap (2026-08-02), so they are prose-trimmed, not
byte-for-byte, while still naming the blocked file path and (for the
AMBIGUOUS branch) the colliding canonical agent id.

Spec backlink: docs/plans/2026-06-09-executor-sidecar-flight-recorder.md § C3a
  (superseded 2026-07-13 by docs/plans/2026-07-13-subagent-run-report-subsume.md
  § C8 — the flight-recorder sidecar carve-out is RETIRED; there is no
  sidecar-path carve-out in this port either.)
Spec backlink (2026-07-24 widening to docs/problems/**):
  cross-repo/inbox/2026-07-24-doe-claude-em-executor-spec-surface-widening.md
  (actioning C9(c) of DoE's agent-citizenship provisioning plan).
Ported from the retired DoE bash guard ``block-subagent-plan-body-write.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).
Identity resolver — Port of: DoE coordinator/lib/coordinator-session.sh
  (DoE ``e34f2484``, 2026-07-22) ``resolve_subagent_identity`` /
  ``cs_build_canonical_agent_id``

Fires on Write|Edit|MultiEdit|NotebookEdit when:
  (1) the top-level ``agent_id`` field is present (subagent write), AND
  (2) the normalized ``file_path`` matches
      ``(^|/)docs/(plans|problems)/.+\\.md$``, AND
  (3) the subagent_type (looked up via agent_id -> em-session-id ->
      dispatched-agents.txt) is ``coordinator:executor`` (or the
      ``AMBIGUOUS`` collision sentinel — see below).

Narrowed 2026-08-06 (docs/plans/2026-08-06-apply-guard-class-census.md
§ C16 — census verdict: "deny only the plan body the executor is CURRENTLY
executing; everything else advises"). Prior to this the guard was a
uniform deny across ALL of docs/plans/** and docs/problems/** for any
coordinator:executor, regardless of which chunk it was actually
dispatched to run. That over-broad shape is what produced the guard's
sharpest incident (state/lessons/2026-07-03-plan-body-edits-are-em-inline-
work-not-e.yaml): an executor whose legitimate DELIVERABLE was a plan-doc
edit hit the uniform deny, self-authorized
``COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY=1``, and completed the write via
a raw ``python3`` subprocess — routing around every other guard in both
packages. Narrowing the deny to only the plan body an executor's OWN
run-report sidecar names as its ``plan:`` is what makes that override
unnecessary for legitimate work: a coordinator:enricher/review-integrator
editing a DIFFERENT plan as its stated deliverable is no longer forced
into a self-authorized bypass, it just gets an advisory nudge instead.

"Currently executing" is read from the dispatching session's own
run-report sidecars (``state/subagent-share/<session_id>/*.md``,
frontmatter ``plan:`` field — the same convention this guard's own
callers write to), NOT from the write target alone. See
``_resolve_executing_plan_keys``/``_plan_body_key`` below.

For a resolved ``coordinator:executor`` subagent_type (non-AMBIGUOUS):
  (a) the write target's plan/problem-set key IS one of that session's
      sidecar-declared ``plan:`` values -> HARD DENY (unchanged deny
      reason/log/emit-log path below).
  (b) otherwise (a different plan body, or no resolvable sidecar) ->
      ADVISORY allow — an ``additionalContext``-only envelope, no
      ``permissionDecision`` field, so the write proceeds.
The AMBIGUOUS collision sentinel is UNAFFECTED by this narrowing — it is
an identity-resolution failure, not a scope call, and stays the
guard's unconditional fail-closed branch.

Repaired 2026-08-18 (docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-
emits.md § C3): the C16 narrowing above made the invented-kind deny leg
(AC6/C3 of docs/plans/2026-08-10-deny-unenumerated-agent-types-at-
dispatch.md) structurally unreachable for anything but
``coordinator:executor``, because it routed a resolved-but-unenumerated
kind through the SAME ``executing_keys`` sidecar check written for a
legitimately-dispatched executor scoping its own plan. An invented kind
has no sidecar and no "currently executing plan" concept to scope
against, so C3 splits the two: a genuinely INVENTED kind (resolved,
non-AMBIGUOUS, absent from the roster, and not the manufactured
``PLACEHOLDER_TYPE`` placeholder — see below) now hard-denies
unconditionally across the whole ``docs/plans/**``/``docs/problems/**``
surface, ahead of and independent from the C16 narrowing.
``coordinator:executor`` itself is untouched — it still only hard-denies
via the C16 executing-plan-key narrowing above.

``PLACEHOLDER_TYPE`` (``"unknown"``, the value
``track_dispatched_agents._handler`` manufactures from an absent
``subagent_type`` field) is routed onto the ``kind_unresolved`` allow leg
alongside ``""`` rather than falling into the invented-kind deny above —
it is the same unresolved-identity event as an empty string, not a
resolved invented kind. This is what keeps a legitimate
``coordinator:enricher``/``coordinator:review-integrator`` dispatch from
hard-denying during the window documented in
``state/bug-backlog/2026-08-10-posttooluse-agent-does-not-fire-for-
some-*`` where the PostToolUse enrich fire never lands and the dispatch
record is left carrying the placeholder — the exact 2026-07-03 incident
shape (``state/lessons/2026-07-03-plan-body-edits-are-em-inline-work-
not-e.yaml``) that drove a self-authorized override bypass last time.

Allow conditions (pass through):
  (1) No agent_id (top-level EM write) -> always allow.
  (2) Path not under docs/plans/ or docs/problems/ -> allow. This
      deliberately does NOT cover docs/wiki/** or docs/decisions/** —
      those are legitimate executor authoring targets (see negative-spec
      below).
  (3) subagent_type lookup fails (missing back-pointer, unreadable file)
      -> allow (PM-directed 2026-06-09: don't punish legitimate
      integrator/enricher work on infra noise).
  (4) subagent_type is anything other than ``coordinator:executor`` (and
      not the ``AMBIGUOUS`` sentinel) -> allow.
  (5) Override env COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY=1 -> allow.
      Read verbatim (not flipped to advisory) despite its bash-side
      sibling ``block_subagent_plan_body_bash_write`` moving to advisory
      class in the same census wave (C14c) — the two legs share this key
      but diverge in class after that wave; the key's semantics on THIS
      side are unchanged: it still short-circuits straight to allow
      before any of the narrowing logic above runs.

Negative-spec:
  - Does NOT reuse ``subagent_sandbox.engine``'s simplified
    ``_canonical_agent_id`` for named-teammate resolution — that helper
    (built for a DIFFERENT reference hook,
    ``block-reviewer-write-outside-sidecar.sh`` — DoE ``8b29fa14``,
    2026-07-12) intentionally returns the
    RAW ``a<name>-<16hex>`` agent_id unchanged, whereas THIS guard's
    reference hook resolves named teammates to the canonical
    ``<name>@session-<short-session-id>`` form via
    ``coordinator-session.sh``'s ``resolve_subagent_identity`` before doing
    the back-pointer directory lookup. Reusing the other module's resolver
    here would look up the wrong back-pointer directory. This module uses
    the shared ``_subagent_identity.py`` port of ``resolve_subagent_identity``
    instead (factored out 2026-08-03 so ``block_subagent_archive_write`` can
    share it — see that module's own docstring).
  - Does NOT convert an absolute ``file_path`` to a repo-relative path via
    ``git ls-files`` — matches the archive-write guard's simpler
    normalize-and-regex-match-as-is approach.
  - Does NOT carve out any sidecar path (the former
    ``tasks/<plan-slug>/flight/<chunk-id>.md`` carve-out is retired per the
    reference hook's own 2026-07-13 note).
  - Does NOT extend the immutable surface to ``docs/wiki/**`` or
    ``docs/decisions/**``. Executors are routinely dispatched to author
    wikis and DRs as their deliverable — those directories are legitimate
    executor authoring targets, not plan/problem-set bodies, and widening
    the regex to cover them would break legitimate delegated authoring.
  - Does NOT resolve "currently executing" via the ``dispatched-agents.txt``
    back-pointer chain (that file carries no ``plan:`` column) or via a new
    env var — it reads the SAME session-keyed run-report sidecar directory
    (``state/subagent-share/<session_id>/``) every other fleet convention
    already treats as this session's provisioned-artifact home, so no new
    lookup surface is introduced for this one guard.
  - Does NOT narrow the AMBIGUOUS branch. An identity collision means the
    guard cannot trust WHICH executor dispatch it is looking at, so it
    cannot trust a plan-key comparison either — narrowing that branch would
    convert a fail-closed identity guard into a fail-open one on exactly
    the input it exists to distrust.
  - Does NOT gate the invented-kind hard deny on a sidecar-declared
    "currently executing plan" — that concept exists only for a
    legitimately-dispatched ``coordinator:executor`` narrowing its OWN
    plan scope (C16 above); an invented kind has no legitimate dispatch to
    scope against, so gating it the same way would leave the deny leg
    unreachable again (the exact defect C3 repairs).
  - Does NOT treat ``PLACEHOLDER_TYPE`` ("unknown") as an invented kind.
    It is folded into ``kind_unresolved`` alongside ``""`` because both
    represent the SAME "identity never resolved" event
    (``track_dispatched_agents._handler`` manufactures the placeholder
    from an absent field) — see the C16-narrowing docstring section above
    for the incident this prevents.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._helpers import (
    emit_kind_resolution_failure_signal,
    operator_override_note,
)
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.git.git_dir import resolve_git_dir
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.bash_guards._override_log_path import (
    NO_SESSION_BUCKET,
    session_audit_log_dir,
)
from coordinator_core.write_guards._subagent_identity import (
    _read_backpointer_subagent_type,
    _resolve_subagent_identity,
)

# DEFERRED, NOT imported at module level (2026-08-13 hot-path import-budget
# fix): a module-level `from coordinator_core.hooks.block_unenumerated_agent_type
# import resolve_roster` drags in `coordinator_core.hooks`'s package `__init__`
# and its full eager registration (18 submodules). This module is reachable
# from `bash_guards.dispatch` via `block_subagent_plan_body_bash_write`'s
# `_resolve_subagent_identity` import, so that eager pull lands on the
# `bash_guards.dispatch` hot path measured by
# `coordinator_core/tests/test_hot_path_hook_import_budget.py`. Third site of
# the same defect shape as `bash_guards/_helpers.py` and its
# `block_subagent_plan_body_bash_write.py` twin -- see the import-budget
# manifest's `write_guards.engine` note for the 2026-08-10 regression this
# closes. `_resolve_roster_accessor()` below imports lazily, ONLY when the
# roster is actually needed. Caches on this module's own `resolve_roster`
# attribute -- same shape as `coordinator_core.session.core._psutil()` -- which
# is what keeps `monkeypatch.setattr(guard, "resolve_roster", ...)` working
# unmodified (see `tests/test_block_subagent_plan_body_write.py`); a private
# `_resolve_roster_mod` cache would silently break that patch point. DO NOT
# re-flatten this back to a module-level import.
_UNRESOLVED = object()
resolve_roster = _UNRESOLVED  # type: ignore[assignment]


def _resolve_roster_accessor():
    """Lazily import and cache ``resolve_roster`` on this module's own
    attribute (see the negative-spec comment above this cache's
    declaration). Returns the callable; never calls it.
    """
    global resolve_roster
    if resolve_roster is _UNRESOLVED:
        from coordinator_core.hooks.block_unenumerated_agent_type import (
            resolve_roster as _imported_resolve_roster,
        )

        resolve_roster = _imported_resolve_roster
    return resolve_roster


CLASS = "hard-deny"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 40

#: Generator-provenance declaration (coordinator_core/ops/generator_provenance.py).
#: This module's only writes are _write_block_log()'s and _write_hook_emit_log()'s
#: best-effort appends under <git_root>/.git/coordinator-sessions/<session_id>/ --
#: inside .git/, never a tracked repo artifact.
GENERATES = []

#: Reference hook — tool-name guard.
_INTERCEPTED_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

#: Reference hook — fast-exit "path under docs/plans/ or docs/problems/,
#: .md suffix" match. Widened 2026-07-24 to also cover docs/problems/
#: (ratified problem-sets) — see module docstring. Deliberately does NOT
#: match docs/wiki/** or docs/decisions/** (excluded executor authoring
#: targets).
_PLAN_BODY_RE = re.compile(r"(^|/)docs/(plans|problems)/.+\.md$")

#: Reference hook — test-fixture exemption. ``_PLAN_BODY_RE`` matches
#: ``docs/plans/`` at ANY depth, so a static parity fixture living at
#: ``<pkg>/tests/fixtures/root/docs/plans/*.md`` reads as a ratified plan
#: body and denies. Such a file is test data, not a plan: no EM authored
#: it, no execution consumes it, and editing it in lockstep with its golden
#: slice is ordinary fixture maintenance. The protected asset is the live
#: ``docs/plans/`` tree at repo root, which no ``tests/fixtures/`` segment
#: can reach. Negative-spec: this does NOT exempt ``tests/`` generally —
#: only a path with a literal ``tests/fixtures/`` segment.
_FIXTURE_PATH_RE = re.compile(r"(^|/)tests/fixtures/")

#: Control-whitespace/C0-control sanitization before interpolating an
#: attacker-influenced file_path into a deny reason.
_CONTROL_WHITESPACE_RE = re.compile(r"[\t\r\n\f\v]")
_C0_CONTROL_RE = re.compile(r"[\x00-\x1f]")

#: Escape-hatch env var named in the deny message.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_SUBAGENT_PLAN_BODY"

#: subagent_type value that is the sole block target.
_EXECUTOR_TYPE = "coordinator:executor"

#: AC14 collision sentinel — track-dispatched-agents.sh writes this when two
#: dispatches share a canonical id but carry different subagent_types.
#: Fails closed (unconditional block).
_AMBIGUOUS_SENTINEL = "AMBIGUOUS"

#: Manufactured-identity sentinel — mirrors
#: ``coordinator_core.hooks.track_dispatched_agents.PLACEHOLDER_TYPE``
#: verbatim (kept as a local literal, not an import, for the same hot-path
#: import-budget reason ``_resolve_roster_accessor`` above defers its own
#: import: importing anything from ``coordinator_core.hooks`` eagerly pulls
#: that package's ``__init__`` and its 18-submodule registration onto this
#: guard's hot path). ``track_dispatched_agents._handler`` writes this value
#: when the dispatching event carried no ``subagent_type`` field at all —
#: the SAME "identity never resolved" event as an empty string, not an
#: invented kind. See ``kind_unresolved`` below.
_PLACEHOLDER_TYPE = "unknown"


def _normalize_path(file_path: str) -> str:
    """Backslash -> slash, collapse repeated slashes."""
    normalized = file_path.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _extract_file_path(payload: Dict[str, Any]) -> str:
    """file_path, falling back to notebook_path for NotebookEdit."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _sanitize_file_path_for_reason(file_path: str) -> str:
    """Port of FILE_PATH_SAFE."""
    safe = _CONTROL_WHITESPACE_RE.sub(" ", file_path)
    return _C0_CONTROL_RE.sub("", safe)


def _resolve_git_root(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --show-toplevel``; best-effort.

    Best-effort in both senses: the caller (``_write_block_log``) only ever
    uses the result to decide WHERE to append a log line, never to flip the
    ALLOW/DENY decision -- so an unresolvable root degrades to "skip the log
    line", exactly like the pre-existing OSError leg.

    AC4 (docs/plans/2026-08-07-no-window-subprocess-primitive.md, chunk C3b):
    delegates to the shared, process-lifetime-memoized
    ``write_guards._repo_root.resolve_repo_root`` instead of hand-rolling its
    own spawn -- same fail-open-to-``None`` contract as the prior inline
    ``subprocess.run``, so no verdict changes. Note the timeout is now the
    shared resolver's fixed 2.0s house value (``coordinator_core.git.
    repo_root._TIMEOUT_SECS``), which already matched this guard's own prior
    2.0s (2026-08-05 hardening pass) -- no change in practice. The prior
    inline spawn also logged a forensic diagnostic on ``OSError`` ("skipping
    deny-log"); the shared resolver swallows all failures silently (never
    raises), so that diagnostic is restored here explicitly rather than
    lost.
    """
    result = resolve_repo_root(cwd)
    if result is None:
        print(
            f"block_subagent_plan_body_write: no git root resolved for cwd="
            f"{cwd!r}, skipping deny-log (decision unaffected)",
            file=sys.stderr,
        )
    return result


def _resolve_git_dir(cwd: Optional[str]) -> Optional[str]:
    """``git rev-parse --git-dir``; best-effort.

    Same fail-open contract as ``_resolve_git_root`` above -- only feeds
    ``_write_hook_emit_log``'s diagnostic log path, never the guard's own
    ALLOW/DENY decision.

    D4 (docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md):
    delegates to the shared, non-spawning
    ``coordinator_core.git.git_dir.resolve_git_dir`` seam instead of
    hand-rolling its own ``git rev-parse --git-dir`` spawn. That resolver
    takes a REPO ROOT (not an arbitrary cwd) and builds its result via
    join/normpath with no ``.resolve()`` -- its output is absolute only when
    the ``repo_root`` argument already is. ``resolve_repo_root`` (the
    peer-landed shared seam) always returns an already-``Path.resolve()``d
    absolute string or ``None``, so that trap does not materialize here.

    Byte-shape note (best-effort log path only, never the verdict): the
    prior subprocess emitted whatever form ``git rev-parse --git-dir``
    itself chose, typically a path RELATIVE to ``cwd`` (e.g. ``.git``) when
    ``cwd`` was already the toplevel -- which ``_write_hook_emit_log`` then
    joined against the log-writing PROCESS's own cwd, not necessarily the
    hook's ``cwd``. This resolver always returns an ABSOLUTE path anchored
    at the real repo. Since this value never feeds the ALLOW/DENY decision
    (only where the best-effort diagnostic log line lands), the change is
    accepted as a side effect of the migration rather than reproduced.
    """
    repo_root = resolve_repo_root(cwd)
    if not repo_root:
        return None
    return str(resolve_git_dir(Path(repo_root)))


def _write_block_log(
    git_root: Optional[str], session_id: str, agent_id: str, file_path: str
) -> None:
    """Best-effort per-session deny log.

    Wrapped so any failure can NEVER flip the ALLOW/DENY decision — mirrors
    bash's trailing ``|| true``.
    """
    if not session_id or not git_root:
        return
    try:
        # A DENY audit line is not a session and must never mint one: this
        # used to `mkdir(parents=True, exist_ok=True)` `<hub>/<session_id>`,
        # and `liveness.live_session_ids` enumerates every non-denylisted
        # child of that hub as a SESSION -- so an audit write manufactured a
        # phantom, record-less session that claim attribution and scope
        # computation both read. `session/core.py::ensure_session` is the
        # ONE constructor. The line is not dropped (it has security content,
        # unlike `guard_doctrine_surface_edits`'s advisory log): an unknown
        # session's line lands in the denylisted `no-session` bucket.
        resolved = session_audit_log_dir(git_root, session_id)
        if resolved is None:
            return
        log_dir = Path(resolved)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log_dir / "plan-body-write-block.log", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{ts} | DENY | agent_id={agent_id} | path={file_path}\n")
    except OSError as exc:
        print(f"block_subagent_plan_body_write: deny-log write failed "
              f"(decision unaffected): {exc}", file=sys.stderr)


def _write_hook_emit_log(cwd: Optional[str], emit: str) -> None:
    """Best-effort diagnostic emit log.

    Logs the exact bytes emitted so a "hookSpecificOutput missing
    hookEventName" schema-validation error can be compared against what was
    actually sent. Wrapped so any failure can NEVER flip the decision.
    """
    try:
        git_dir = _resolve_git_dir(cwd)
        if not git_dir:
            return
        session_id = os.environ.get("CLAUDE_SESSION_ID", NO_SESSION_BUCKET)
        # Same rule as `_write_block_log` above: a diagnostic emit log never
        # mints `<hub>/<sid>` -- `ensure_session` is the one constructor, and a
        # record-less child of that hub reads as a phantom SESSION to
        # `liveness.live_session_ids`. An unknown session's line lands in the
        # denylisted `no-session` bucket rather than being dropped.
        sessions_root = Path(git_dir) / "coordinator-sessions"
        if session_id != NO_SESSION_BUCKET and not (sessions_root / session_id).is_dir():
            session_id = NO_SESSION_BUCKET
        log_dir = sessions_root / session_id / "hook-emits"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        with open(log_dir / "emits.tsv", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(f"{ts}\tblock-subagent-plan-body-write\t{emit}\n")
    except OSError as exc:
        print(f"block_subagent_plan_body_write: hook-emit-log write failed "
              f"(decision unaffected): {exc}", file=sys.stderr)


#: C16 narrowing — extracts the ``docs/plans/...`` or ``docs/problems/...``
#: comparison key from an already-normalized path (backslashes converted,
#: no repeated slashes). Deliberately mirrors ``_PLAN_BODY_RE``'s own
#: ``.+\.md$`` breadth (not flattened to a single filename segment) so a
#: sidecar's ``plan:`` value and a write target agree on the same key
#: whether or not either is nested under a subdirectory.
_PLAN_KEY_RE = re.compile(r"docs/(?:plans|problems)/.+\.md$")


def _plan_body_key(normalized_path: str) -> Optional[str]:
    """Reduce a normalized path to its ``docs/plans|problems/...`` key, or
    ``None`` if the path carries no such suffix (should not happen for a
    write target that already passed ``_PLAN_BODY_RE``, but a sidecar's
    ``plan:`` value is untrusted input and may not match at all).
    """
    match = _PLAN_KEY_RE.search(normalized_path)
    return match.group(0) if match else None


def _resolve_executing_plan_keys(git_root: Optional[str], session_id: str) -> set:
    """Plan/problem-set keys this session's run-report sidecars declare as
    ``plan:`` — i.e. the plan bodies actually being executed right now by
    dispatches under this session, per the fleet's own
    ``state/subagent-share/<session_id>/`` sidecar convention.

    Best-effort: an unresolvable git_root/session_id, missing sidecar
    directory, or any per-file read/parse failure simply omits that
    session/file from the result — never raises. A guard that cannot prove
    "this write hits the executing body" must not hard-deny it (see
    ``check()``), so an empty result here means the narrowed deny stays
    inert and every write from this branch is advisory-only, not a crash.
    """
    keys: set = set()
    if not git_root or not session_id:
        return keys
    sidecar_dir = Path(git_root) / "state" / "subagent-share" / session_id
    try:
        entries = sorted(sidecar_dir.glob("*.md"))
    except OSError:
        return keys
    for entry in entries:
        try:
            text = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        split = split_frontmatter(text)
        if split is None:
            continue
        plan_value = read_fm_field(split.fm_text, "plan")
        if not plan_value:
            continue
        key = _plan_body_key(_normalize_path(plan_value))
        if key:
            keys.add(key)
    return keys


def _advisory_reason(file_path: str, subagent_type: str = _EXECUTOR_TYPE) -> str:
    """C16 narrowing — non-blocking nudge for an executor writing a
    plan/problem-set body OTHER than the one its own sidecar names as the
    plan it is executing. Deliberately much shorter than
    ``_deny_reason_executor``: this leg never blocks, so it does not carry
    the override-env note (there is nothing to bypass).

    ``subagent_type`` names the kind that actually reached this leg. It
    defaults to ``coordinator:executor`` because that was the only caller
    before C3 (docs/plans/2026-08-10-deny-unenumerated-agent-types-at-
    dispatch.md) routed cleanly-resolved unenumerated kinds here too — an
    operator reading a nudge that names a type they did not dispatch
    debugs the wrong thing.
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    if subagent_type != _EXECUTOR_TYPE:
        return (
            f"Note: {file_path_safe} is a plan/problem-set body, and the "
            f"dispatched subagent_type {subagent_type!r} is not on coordinator's "
            "enumerated agent roster. Unenumerated kinds are treated as untrusted "
            "for plan-body writes rather than waved through — if this type is "
            "legitimate, it belongs on the roster; if editing THIS file is your "
            "stated deliverable, confirm scope with the EM."
        )
    return (
        f"Note: {file_path_safe} is a plan/problem-set body outside the plan "
        "you are currently executing. coordinator:executor dispatches "
        "normally edit only their own dispatch's plan body via the EM — if "
        "editing THIS file is your stated deliverable, confirm scope with "
        "the EM; if not, this is worth a second look before proceeding."
    )


def _deny_reason_ambiguous(
    agent_id: str, file_path: str, payload: Optional[Dict[str, Any]] = None
) -> str:
    """Compressed from the AMBIGUOUS-branch REASON (no longer byte-for-byte —
    see module docstring); names the blocked file and colliding canonical id
    since this is the guard's unconditional fail-closed branch.
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    return (
        f"BLOCKED {file_path_safe}: ambiguous agent identity ({agent_id}) — "
        "canonical-id collision, failing closed.\n\n"
        "Two dispatches shared one id with different subagent_types. Ask the EM to\n"
        "re-dispatch cleanly."
        + ("\n\n" + _note if _note else "")
    )


def _deny_reason_executor(
    agent_id: str,
    file_path: str,
    subagent_type: str = _EXECUTOR_TYPE,
    payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Byte-for-byte port of the coordinator:executor-branch REASON, widened
    2026-07-24 to also name ``docs/problems/**`` ratified problem-sets
    alongside plan bodies (see module docstring).

    ``subagent_type`` names the kind that actually reached this leg; the
    ``coordinator:executor`` default preserves the ported REASON byte-for-byte
    for the original caller. C3 (docs/plans/2026-08-10-deny-unenumerated-
    agent-types-at-dispatch.md) also routes cleanly-resolved unenumerated
    kinds here, and the executor-specific remediation below ("route to
    coordinator:enricher/review-integrator") is wrong advice for those — the
    fix for an unenumerated kind is the roster, not a re-route.
    """
    file_path_safe = _sanitize_file_path_for_reason(file_path)
    _note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload)
    if subagent_type != _EXECUTOR_TYPE:
        return (
            "Use instead:\n"
            f"  {file_path_safe}: the dispatched subagent_type {subagent_type!r} is "
            "not on coordinator's enumerated agent roster, so it may not write this "
            "plan/problem-set body. Unenumerated kinds are treated as untrusted here "
            "rather than waved through. If this type is legitimate, add it to the "
            "roster; if the write is your deliverable, ask the EM to dispatch an "
            "enumerated kind (coordinator:enricher/review-integrator)"
            + ("\n\n" + _note if _note else "")
        )
    return (
        "Use instead:\n"
        f"  {file_path_safe}: coordinator:executor may not write this plan/problem-set "
        "body directly. Stamping status? Use the run-report sidecar "
        "state/subagent-share/<provisioned-path>.md instead. Editing the body was your "
        "deliverable? Ask the EM to route to coordinator:enricher/review-integrator"
        + ("\n\n" + _note if _note else "")
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the plan-body-write guard against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope. Fails open
    (returns ``None``) on any missing/unparseable field or lookup-miss,
    matching the reference hook's ``-e``-omitted, fail-open-on-error
    contract and its PM-directed lookup-fail-is-allow semantics.
    """
    # Honor escape hatch first.
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Tool-name guard — defense-in-depth.
    tool_name = payload.get("tool_name") or ""
    if tool_name not in _INTERCEPTED_TOOLS:
        return None

    # Single git root resolution.
    cwd = payload.get("cwd")
    git_root = _resolve_git_root(cwd)

    # Identity resolution via the resolve_subagent_identity port.
    raw_agent_id = payload.get("agent_id") or ""

    # Empty RAW agent_id -> no subagent at all (EM main-loop) -> allow. This
    # is the EM/subagent discriminator this guard gates on -- whether the
    # harness supplied an agent_id at all, not whether it canonicalizes.
    if not raw_agent_id:
        return None

    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)

    # file_path (or notebook_path fallback).
    file_path = _extract_file_path(payload)
    if not file_path:
        return None

    normalized = _normalize_path(file_path)

    # Fast exit: path not under docs/plans/ or docs/problems/ -> allow.
    # (docs/wiki/** and docs/decisions/** are deliberately NOT matched —
    # see module docstring negative-spec.)
    if not _PLAN_BODY_RE.search(normalized):
        return None

    # Fast exit: static test fixture, not a ratified plan body -> allow.
    if _FIXTURE_PATH_RE.search(normalized):
        return None

    # Subagent-type lookup via back-pointer chain.
    # Lookup-fail semantics: allow (PM-directed 2026-06-09) -- reinstated
    # 2026-07-30 after a brief that lumped this guard in with the uniform-
    # deny identity family (block_subagent_commit,
    # block_subagent_destructive_action) mis-scoped it here. THIS guard is
    # per-kind policy (only coordinator:executor is blocked; enricher,
    # review-integrator, and every other resolved kind are legitimate plan-
    # body editors by design), so an unresolvable kind cannot be confined as
    # "executor" without directly reproducing the harm the 2026-06-09 ruling
    # was written to prevent -- punishing a legitimate integrator/enricher
    # dispatch whose backpointer chain happens to be unreadable for reasons
    # unrelated to its own identity. Reverting the VERDICT only; the
    # measurement-only instrumentation signal below is kept so a future,
    # properly-scoped PM conversation about the 2026-06-09 ruling has real
    # frequency data to work from.
    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)

    is_ambiguous = subagent_type == _AMBIGUOUS_SENTINEL

    # C3 (docs/plans/2026-08-18-claude-klabauter-fires-the-workflows-it-emits.md):
    # PLACEHOLDER_TYPE is a manufactured-absent-field identity, the same
    # "never resolved" event as an empty string (see ``_PLACEHOLDER_TYPE``
    # above) — NOT a resolved, enumerable kind. Folding it into
    # ``kind_unresolved`` is what routes it onto the allow exit below
    # alongside "", rather than falling through into the invented-kind
    # deny path a few lines down.
    is_placeholder = subagent_type == _PLACEHOLDER_TYPE

    # Emitted at the actual exit point with the actual verdict (``None``,
    # since that branch is the ONLY reachable exit for an unresolved,
    # non-ambiguous kind) rather than a hand-passed claim about the outcome.
    kind_unresolved = not is_ambiguous and (not subagent_type or is_placeholder)

    # Block when SUBAGENT_TYPE is coordinator:executor OR AMBIGUOUS (AC14
    # collision sentinel). All other types — including empty/lookup-failure/
    # placeholder — allow. AC6/C3: "all other types" is narrowed to
    # enumerated-roster types only — a lookup FAILURE or PLACEHOLDER_TYPE
    # (kind_unresolved) still allows through this same exit, unchanged from
    # the 2026-06-09 ruling above, but a type that resolves CLEANLY to
    # something absent from the roster (C1's own union-of-three roster,
    # coordinator_core.hooks. block_unenumerated_agent_type.resolve_roster)
    # is a genuinely INVENTED kind: it hard-denies unconditionally across
    # the whole protected surface below, not gated on any "currently
    # executing plan" sidecar concept — an invented kind has no legitimate
    # dispatch to protect (see module docstring negative-spec / C3 brief).
    # A roster-load error is a peer-repo hiccup, not this guard's problem to
    # newly deny on: C1's PreToolUse(Agent) deny is the primary fix, so this
    # stays defence in depth and falls back to today's allow rather than
    # denying on an unresolvable roster.
    is_invented = False
    if not is_ambiguous and subagent_type != _EXECUTOR_TYPE:
        if kind_unresolved:
            emit_kind_resolution_failure_signal(
                "block_subagent_plan_body_write", agent_id, git_root, None
            )
            return None
        roster, _roster_error = _resolve_roster_accessor()()
        if roster is None or subagent_type in roster:
            return None
        is_invented = True

    # AMBIGUOUS collision sentinel: unconditional fail-closed, unaffected by
    # the C16 plan-scoping narrowing below (identity failure, not a scope
    # call — see module docstring negative-spec).
    if is_ambiguous:
        _write_block_log(git_root, session_id, agent_id or raw_agent_id, file_path)
        reason = _deny_reason_ambiguous(agent_id or raw_agent_id, file_path, payload)
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        _write_hook_emit_log(cwd, str(result))
        return result

    # Invented kind: hard-denies unconditionally, across the WHOLE
    # docs/plans/** + docs/problems/** surface — deliberately NOT routed
    # through the C16 executing-plan-key narrowing below, which exists only
    # to scope a legitimate coordinator:executor dispatch to its OWN plan.
    # An invented kind has no sidecar and no "currently executing plan"
    # concept to narrow against.
    if is_invented:
        _write_block_log(git_root, session_id, agent_id or raw_agent_id, file_path)
        reason = _deny_reason_executor(agent_id or raw_agent_id, file_path, subagent_type, payload)
        result = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
        _write_hook_emit_log(cwd, str(result))
        return result

    # C16 narrowing: a resolved coordinator:executor only hard-denies the
    # plan/problem-set body its OWN session sidecar(s) declare as the plan
    # currently being executed. Any other plan/problem-set write from an
    # executor is advisory-only.
    target_key = _plan_body_key(normalized)
    executing_keys = _resolve_executing_plan_keys(git_root, session_id)
    if not target_key or target_key not in executing_keys:
        reason = _advisory_reason(file_path, subagent_type)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }

    # Block confirmed: this IS the plan body being executed.
    _write_block_log(git_root, session_id, agent_id or raw_agent_id, file_path)
    reason = _deny_reason_executor(agent_id or raw_agent_id, file_path, subagent_type, payload)

    result = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }

    # Diagnostic emit log — best-effort, wrapped so
    # it can never flip the decision already computed above.
    _write_hook_emit_log(cwd, str(result))

    return result
