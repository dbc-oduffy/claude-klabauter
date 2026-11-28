"""coordinator_core.bash_guards.stale_write_guard -- ``check_stale_write``, the
deny leg for a whole-file write over content this session never read, and its
shape-candidate and worker-claim helpers.

Imports only shared guard support, never the ``dispatch_checks`` facade.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._rewrite_support import _alt_record, _crlf_strip, _override, _ready
from coordinator_core.bash_guards.destructive_rm_support import _is_within


def _stale_write_shape_candidates(
    cmd: str, cwd: Optional[str]
) -> List[Tuple[str, str, bool]]:
    """C2 (docs/plans/2026-09-02-a-write-that-discards-what-you-never-saw.md):
    ``(resolved_target, raw_target, whole_file)`` for every depth-0 write-
    sink candidate this command carries via a bare ``>`` redirect or a bare
    ``tee`` invocation -- the two WHOLE-FILE shapes C2's own body names
    (``cat > P``, ``> P``, ``tee P``). ``whole_file`` is always ``True``
    here; a target reached only through a SURGICAL shape (``sed -i``,
    ``>>``, ``tee -a``/``--append``) is never yielded at all, never yielded
    with ``whole_file=False`` -- C2's body: ambiguity resolves toward "not
    whole-file", so this function under-claims rather than guesses.

    Deliberately narrower than ``bump_outside_repo_write._iter_write_sink_
    candidates`` (C1's own write-target resolver, consumed by ``write_claim_
    record.record_write_claims``): that resolver's ``cp``/``mv``/``install``/
    ``mkdir``/``rsync``/``tar``/interpreter-payload branches are never
    whole-file shapes in C2's own enumeration and are excluded here
    entirely, and ``sed`` (any form, including ``-i``) is excluded
    unconditionally -- C2's body states a surgical write "destroys nothing
    outside the region it edits" and must never be compared, regardless of
    whether its target's whole-file fingerprint has diverged. This is not a
    second write-shape TABLE (NEGATIVE SPEC, ``write_claim_record``'s own
    module docstring) -- it reuses the same resolve-once tokenizer
    (``_command_tokenizer.resolve_command_positions``) and the same
    ``bump_outside_repo_write._resolve_relative`` cwd-join every other
    write-target resolver in this package already uses, and introduces no
    new interpreter/binary table of its own.

    Never raises: any tokenizer failure yields ``[]``. No git spawn.
    """
    from coordinator_core.bash_guards._command_tokenizer import (
        ResolutionConfidence,
        normalize_executable_basename,
        resolve_command_positions,
    )
    from coordinator_core.bash_guards.bump_outside_repo_write import (
        _resolve_relative,
    )

    try:
        segments = resolve_command_positions(
            cmd, preserve_windows_backslashes=(os.name == "nt")
        )
    except Exception:
        return []

    out: List[Tuple[str, str, bool]] = []
    effective_cwd = cwd or os.getcwd()
    for rc in segments:
        if rc.confidence == ResolutionConfidence.UNRESOLVED or not rc.tokens:
            continue
        tokens = rc.tokens
        head_base = normalize_executable_basename(tokens[0])

        if rc.depth == 0 and head_base == "cd":
            positional = [t for t in tokens[1:] if not t.startswith("-")]
            if len(positional) == 1:
                resolved = _resolve_relative(effective_cwd, positional[0])
                if resolved is not None:
                    effective_cwd = resolved
            continue

        # Bare `>` redirect only -- `>>` is append-shaped and is never a
        # whole-file write, regardless of `head_base`.
        for i, tok in enumerate(tokens):
            if tok == ">" and i + 1 < len(tokens):
                raw_target = tokens[i + 1]
                resolved_target = _resolve_relative(effective_cwd, raw_target)
                if resolved_target is not None:
                    out.append((resolved_target, raw_target, True))

        # Bare `tee` only -- any `-a`/`--append` on the invocation makes
        # every one of its targets append-shaped; C2 never asks for a
        # per-target split within one `tee` call.
        if head_base == "tee":
            args = tokens[1:]
            has_append = any(a in ("-a", "--append") for a in args)
            if not has_append:
                for a in args:
                    if a.startswith("-"):
                        continue
                    resolved_target = _resolve_relative(effective_cwd, a)
                    if resolved_target is not None:
                        out.append((resolved_target, a, True))

    return out


def _stale_write_family_claim(
    rel_path: str, session_id: str, root: str, cwd: Optional[str]
) -> bool:
    """Best-effort: does a dispatched sub-agent of THIS session (an
    ``.git/coordinator-agents/<agent-id>`` directory whose own
    ``em-session-id.txt`` back-pointer names ``session_id``) hold the
    newest LIVE TOUCH claim on ``rel_path``? Consulted ONLY to pick the
    right WORDING for ``check_stale_write``'s deny message (see that
    function's own docstring and C2's body) -- NEVER to suppress the
    deny. Observation and ownership are different questions: a worker's
    write is this session's own for ATTRIBUTION, but the dispatching
    session did not read the bytes the worker wrote, so the deny still
    stands regardless of what this returns.

    Deliberately does NOT use ``session.claim_index.lookup`` -- that
    index's own per-path claimant list is already keyed by the OWNING
    session id for both a session's OWN direct claim and a dispatched
    worker's folded-through claim (``_agent_owner_sid``), so the two are
    indistinguishable through that seam: ``session_id`` would appear in
    the claimant list for the parent's own baseline read too, and this
    function would answer "worker" on every ordinary self-write. Instead
    it enumerates ``root/.git/coordinator-agents`` directly (the SAME
    ``em-session-id.txt`` back-pointer shape ``claim_index._agent_owner_
    sid`` reads, and the SAME agent-claim-dir shape ``session.scope``'s
    Step 3b already walks for peer sub-agent claims), keeps only the
    entries whose back-pointer names ``session_id``, and asks
    ``touch_record.project_live_claims`` (the shared read seam, C3)
    whether any of THOSE agent sinks alone -- never a bare session sink --
    hold the live winning TOUCH for ``rel_path``.

    Bounded to one directory scan plus one read seam over that session's
    own agent dirs only (never every agent dir on disk). Returns ``False``
    (never raises) on any I/O failure, a missing/unreadable
    ``coordinator-agents`` directory, an empty ``session_id``, or no
    matching agent sink -- the generic message is always the safe
    fallback.
    """
    if not session_id or not rel_path or not root:
        return False
    try:
        from coordinator_core.session.touch_record import (
            project_live_claims,
            sink_path,
        )

        agents_root = os.path.join(str(root), ".git", "coordinator-agents")
        if not os.path.isdir(agents_root):
            return False

        agent_sinks = []
        with os.scandir(agents_root) as it:
            for entry in it:
                if not entry.is_dir():
                    continue
                backptr = os.path.join(entry.path, "em-session-id.txt")
                try:
                    with open(backptr, "r", encoding="utf-8") as fh:
                        owner = fh.readline().strip()
                except OSError:
                    continue
                if owner == session_id:
                    agent_sinks.append(sink_path(entry.path))

        if not agent_sinks:
            return False

        proj = project_live_claims(*agent_sinks, cwd=cwd)
        return rel_path in proj.claims
    except Exception:
        return False


def check_stale_write(
    cmd: str,
    session_id: str = "",
    cwd: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """C2 (docs/plans/2026-09-02-a-write-that-discards-what-you-never-saw.md):
    the DENY leg. A pre-verdict check -- unlike ``dispatch.
    _record_bash_write_claims`` (C1's post-verdict RECORD leg, which "must
    never influence the verdict already decided" per its own docstring),
    THIS function's whole job is to produce one -- registered as its own
    ``GuardEntry`` in the guard chain (``dispatch._build_guard_chain``),
    never folded into the recorder's seam. See this chunk's own plan body
    for why recording and denying cannot share one seam.

    For every WHOLE-FILE write-target candidate ``_stale_write_shape_
    candidates`` resolves (a bare ``>`` redirect or a bare ``tee`` -- see
    that function's own docstring for the shape boundary and why a
    surgical write, e.g. ``sed -i``/``>>``/``tee -a``, is never even a
    candidate here), compares THIS session's own last-recorded fingerprint
    for that path (``touch_record.last_seen_hash``) against the file's
    CURRENT disk content (``touch_record.is_stale``). A genuine mismatch --
    both hashes exist and disagree -- denies before the write reaches disk.
    Any unprovable state (no recorded hash, an unreadable target) is
    treated as NOT stale, never promoted to a deny -- ``is_stale``'s own
    docstring is the whole safety argument here, inherited verbatim.

    The message is a guard message (``docs/wiki/guard-messaging.md`` §
    Register): one fact, once, plus a terse alternative. It never asserts
    WHO changed the file -- the guard knows only that the content moved.
    ``git log --oneline -3 -- <path>`` is the discriminator named, and
    ``operator_override_note`` is the pointer route. When ``_stale_write_
    family_claim`` finds a dispatched-worker claimant behind the staleness,
    the message names that specific, actually-known fact ("a worker this
    session dispatched changed this file after your last read") rather
    than a generic staleness line, so a routine dispatch-then-rewrite deny
    does not read as a bug in the guard -- it never asserts WHICH worker or
    WHEN, only the fact the family lookup actually has.

    Root is resolved via ``git.repo_root.show_toplevel`` -- a walk, never a
    spawn (same resolver ``dispatch._record_bash_write_claims`` already
    uses for the identical reason) -- so this check spends no git process
    at all on its own hot path. Never raises: any resolver/import failure
    anywhere in this function returns ``None`` (allow), matching the
    fail-toward-not-denying posture every comparator here already commits
    to.
    """
    command = _crlf_strip(cmd) if cmd else ""
    if not command or not session_id:
        return None
    if _override("COORDINATOR_OVERRIDE_STALE_WRITE", payload=payload):
        return None

    try:
        from coordinator_core.git.repo_root import show_toplevel as _stale_show_toplevel
    except Exception:
        return None
    root = _stale_show_toplevel(cwd or None)
    if not root:
        return None

    candidates = _stale_write_shape_candidates(command, cwd or None)
    if not candidates:
        return None

    try:
        from coordinator_core.session.touch_record import is_stale, last_seen_hash
    except Exception:
        return None

    for resolved_target, _raw_target, _whole in candidates:
        if not _is_within(resolved_target, root):
            continue
        rel = os.path.relpath(resolved_target, root).replace(os.sep, "/")
        try:
            recorded = last_seen_hash(session_id, rel, root)
        except Exception:
            recorded = None
        try:
            stale = is_stale(recorded, resolved_target)
        except Exception:
            stale = False
        if not stale:
            continue

        family_worker = _stale_write_family_claim(rel, session_id, root, cwd or None)
        if family_worker:
            fact = (
                "a worker this session dispatched changed this file after "
                "your last read"
            )
        else:
            fact = "this file changed since your session last read it"

        note = operator_override_note(
            "COORDINATOR_OVERRIDE_STALE_WRITE", payload=payload, git_root=root
        )
        return _alt_record(
            "deny", "stale-write", "BLOCKED: %s -- %s." % (rel, fact),
            (_ready("git log --oneline -3 -- %s" % rel),),
            "COORDINATOR_OVERRIDE_STALE_WRITE",
            report="the write you need to make", authorize="overwriting the changed file",
            em_text=(
                "BLOCKED: %s -- %s. `git log --oneline -3 -- %s` shows what "
                "changed."
                % (rel, fact, rel)
            )
            + (" %s" % note if note else ""),
        )

    return None
