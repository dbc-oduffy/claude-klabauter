"""coordinator_core.bash_guards._dc_git_probe -- git-probe layer and owner-sentence / peer-claim text shared by every guard family.

The git-probe layer shared by every dispatch_checks guard family: the deny envelope,
the per-dispatch probe budget and deadline, the fail-open record, `_run_git` and
`_batch_show_index_blobs`. Patchable seams are called module-qualified so a patch on
this module reaches every caller.

Peer-claim and dirty-path helpers for the dispatch_checks destructive-rm and
validate-commit guards: porcelain parsing, the dirty-path memo and `_rm_peer_claim_of`.

Owner-sentence and strict-scope denial text shared by the dispatch_checks guard
families: byte-budgeted owner clauses, writer-name resolution, provenance notes and
the batched scope-denial formatter.
"""

from __future__ import annotations

import contextvars
import os
import re
import subprocess
import sys
import time
from typing import Any, Callable, Dict, FrozenSet, List, Optional, TYPE_CHECKING, Tuple
from coordinator_core.bash_guards._command_tokenizer import (
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._override_log_path import _override_log_path
from coordinator_core.bash_guards._rewrite_support import _bt_git_resolved_subcommand
from coordinator_core.git.correction_note import SENTINEL as _NOTE_SENTINEL

if TYPE_CHECKING:
    from coordinator_core.session.scope import OwnerFact


# Generator-provenance declaration (coordinator_core/ops/generator_census). The one real
# write in this module (guard-fail-open.log) resolves under
# <git_root>/.git/coordinator-sessions/<session_id>/ -- untracked guard bookkeeping,
# never a tracked repo artifact.
GENERATES = []


_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


#: Wall-clock a single dispatch may spend inside `_run_git` subprocesses
#: before further probes are declined unspawned. Derived from the harness's
#: own PreToolUse hook window (15 000 ms, read off a `hook_cancelled`
#: attachment in a live transcript -- the harness owns that number, this
#: repo does not), minus the rest of one invocation: interpreter boot, the
#: `coordinator_core` import, and every non-probing guard's in-process work.
#: Measured idle end-to-end for the whole hook, real payload, warm index:
#: ~0.55 s, of which ~0.95 s of git time is the tail under contention -- so
#: this budget is not felt on an unloaded box and only binds when the machine
#: is at its documented 50-70-concurrent-LLM norm (`docs/wiki/machine-load-
#: norm.md`).
#:
#: WHY A CEILING AT ALL. Per-probe `timeout=2.0` bounds ONE spawn; nothing
#: bounded their SUM. A commit-shaped command spawns six git processes on
#: this path, so the engine's own worst case (12 s of git, plus boot and
#: import) sat ABOVE the harness window -- and a hook that overruns its
#: window is cancelled outright, which loses every guard's verdict, not just
#: the slow one's. That is the delivery failure recorded in
#: `state/bug-backlog/2026-08-15-bare-commit-deny-never-reached-the-
#: operator.yaml`: no deny, no advisory, nothing printed at all.
_GIT_PROBE_BUDGET_SECONDS = 6.0

#: Deadline (a `time.monotonic()` stamp) for the dispatch currently in
#: flight, or `None` when no budget is armed. INERT BY DEFAULT and armed at
#: exactly one seam (`dispatch.evaluate_payload_json`), which is the only
#: caller running inside the harness's cancellable window: a check invoked
#: directly -- every test in this package, `_alternative_liveness`'s own
#: liveness harness -- sees `None` and behaves byte-identically to before
#: this budget existed. Do not arm it from a check; a check does not know
#: whether it is one guard in a chain or the whole call.
#:
#: `contextvars.ContextVar` rather than a bare module global (C8,
#: docs/plans/2026-08-15-warm-engine-retires-the-per-invocation-cold-start.md):
#: under a warm engine two dispatches can be interleaved, and a bare global
#: lets a second dispatch's `_arm_git_probe_deadline` silently resurrect a
#: first dispatch's already-exhausted budget (or vice versa) -- the
#: characterization test this fix flips,
#: `coordinator_core/warm/tests/test_process_global_characterization.py`
#: Site 6. Each dispatch runs in its own Task/thread Context, so arm/disarm
#: in one dispatch is invisible to a concurrently-interleaved one.
_git_probe_deadline: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "_dispatch_checks_git_probe_deadline", default=None
)

#: Returned instead of spawning once the budget is spent. Deliberately 127
#: (`_run_git`'s existing unresolvable-`git` code) and NOT -1: -1 is the
#: timeout sentinel four oracle branches in this file treat as fail-CLOSED
#: (`check_destructive_git_clean`, and the revert/reset/stash oracles), and
#: a budget-exhausted probe is a guard-process resource condition, not
#: evidence the command is unsafe -- denying on it would block real work on
#: a loaded machine, the same argument `_bt_solo_bare_commit_index_nonempty`
#: already makes for its own fail-open posture. Every `rc != 0` call site in
#: this file therefore degrades to its own already-specified fail-open
#: default, which is strictly better than what the overrun produced: a
#: cancelled hook delivers NO guard's verdict, including the many that need
#: no probe at all.
_GIT_PROBE_BUDGET_SPENT_RC = 127

#: Precise cause of the most recent `_run_git` failure, `None` after a
#: successful probe. `rc` alone cannot separate "git could not be executed"
#: from "the budget was already spent" -- both are 127 by construction (see
#: `_GIT_PROBE_BUDGET_SPENT_RC`) -- and a fail-open record that cannot name
#: which of the two happened does not answer the question it exists to
#: answer. A ContextVar, not a module global, for the reason
#: `_git_probe_deadline` states: two dispatches interleave under a warm
#: engine.
_git_probe_last_failure: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_dispatch_checks_git_probe_last_failure", default=None
)

#: Fail-open reasons recorded by the index probes of the dispatch in flight:
#: appended by `_record_probe_fail_open`, drained by
#: `_take_probe_fail_open_reasons`. A tuple (immutable) so the ContextVar
#: default cannot be mutated across contexts.
_git_probe_fail_open_reasons: contextvars.ContextVar[Tuple[str, ...]] = (
    contextvars.ContextVar(
        "_dispatch_checks_git_probe_fail_open_reasons", default=()
    )
)


def _persist_probe_fail_open(
    reasons: Tuple[str, ...],
    cmd: str,
    session_id: str,
    git_root: Optional[str],
) -> None:
    """Append a fail-open record to disk so "was this commit guarded?" stays
    answerable after the dispatch ends.

    `_record_probe_fail_open` made the degradation legible IN FLIGHT — the
    reason reaches the operator through the advisory. That is not enough to
    close `state/bug-backlog/2026-08-21-bare-commit-guard-likely-fails-open-
    unde-0d2276775068.yaml`, whose whole subject is a commit that swept a
    peer's staged blob past this guard with nothing in the record able to
    establish whether the probe had degraded. The reasons live in a
    ContextVar drained into one advisory; nothing survives the process. So
    the question the row exists to answer is undecidable the moment the
    advisory scrolls past — which is exactly where the 2026-08-21 reporter
    stood, and where this session stood again for `38383479f1`.

    Recording only. The fail-open POSTURE is unchanged and deliberately so:
    a degraded probe must not manufacture a deny.

    Never raises. A guard that crashes because it could not write its own
    audit line is worse than one that loses the line, so every failure here
    is swallowed after one stderr breadcrumb — the same posture the override
    audit log takes on an unwritable path.
    """
    if not reasons or not git_root:
        return
    try:
        log_path = _override_log_path(git_root, session_id)
        if log_path is None:
            return
        log_path = os.path.join(os.path.dirname(log_path), "guard-fail-open.log")
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "%s | %s | GIT-COMMIT-SCOPE-PROBE-FAIL-OPEN | %s | %s\n"
                % (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    session_id or "no-session",
                    "; ".join(reasons),
                    cmd[:200].replace("\n", " "),
                )
            )
    except Exception as exc:  # noqa: BLE001 -- never crash the dispatcher
        print(
            "git-commit-safe-commit-advise: failed to persist fail-open "
            "record: %s" % (exc,),
            file=sys.stderr,
        )


def _record_probe_fail_open(predicate: str, probe: str, rc: int) -> None:
    """Record WHY an index probe is failing open, and say so on stderr.

    A bare `False` from a fail-open probe erases the distinction between
    "probed the index and found nothing" and "never learned what the index
    held" -- and that distinction is the whole of whether this guard is
    still protecting anything. `state/bug-backlog/2026-08-21-bare-commit-
    guard-likely-fails-open-unde-0d2276775068.yaml` records the cost: a
    peer's staged blob committed under another session's subject, past a
    guard whose matcher was afterwards verified correct against the exact
    reported shape, with nothing in the record able to establish whether
    the probe had degraded under load.

    Recording only -- the fail-open POSTURE is deliberately unchanged (a
    guard-process error must not manufacture a deny; see
    `_bt_solo_bare_commit_index_nonempty`). What changes is legibility: the
    reason reaches the operator through the advisory the command falls
    through to, in `check_git_commit_safe_commit_advise`."""
    cause = _git_probe_last_failure.get()
    if cause is None:
        if rc == -1:
            cause = "the probe timed out"
        elif rc == _GIT_PROBE_BUDGET_SPENT_RC:
            cause = "git was unresolvable, or the probe budget was spent"
        else:
            cause = "git exited %d" % rc
    reason = "%s: `git %s` failed -- %s" % (predicate, probe, cause)
    _git_probe_fail_open_reasons.set(_git_probe_fail_open_reasons.get() + (reason,))
    print(
        "bash_guards.dispatch_checks: index probe failed open -- %s; the "
        "staged set is unverified, so this command is not escalated." % reason,
        file=sys.stderr,
    )


def _clear_probe_fail_open_reasons() -> None:
    """Drop reasons left by an earlier segment or dispatch, so a fall-through
    advisory can only ever name a probe failure of its own."""
    _git_probe_fail_open_reasons.set(())


def _take_probe_fail_open_reasons() -> Tuple[str, ...]:
    """Return and clear the reasons recorded since the last clear."""
    reasons = _git_probe_fail_open_reasons.get()
    _git_probe_fail_open_reasons.set(())
    return reasons


def _arm_git_probe_deadline(budget: Optional[float] = None) -> None:
    """Open a probe budget for one dispatch. Idempotent per dispatch: a
    second arm replaces the deadline rather than extending it.

    `None` reads `_GIT_PROBE_BUDGET_SECONDS` AT CALL TIME rather than
    binding it as a default argument at import: a default would freeze the
    module constant into this signature, making it unreachable to the one
    thing that needs to vary it -- a test shrinking the budget so it can
    exercise exhaustion in milliseconds instead of seconds."""
    if budget is None:
        budget = _GIT_PROBE_BUDGET_SECONDS
    _git_probe_deadline.set(time.monotonic() + budget)


def _disarm_git_probe_deadline() -> None:
    """Close the current dispatch's probe budget, restoring the unbudgeted
    default. Callers arm/disarm in a `try`/`finally` so an exception on the
    guard chain cannot leave a stale deadline armed for the next call in a
    long-lived process (a test run; anything embedding this engine)."""
    _git_probe_deadline.set(None)


def _git_probe_budget_spent() -> bool:
    """True iff a budget is armed AND already spent. `None` (unarmed) is
    never "spent" -- see `_git_probe_deadline`'s inert-by-default note."""
    deadline = _git_probe_deadline.get()
    return deadline is not None and time.monotonic() >= deadline


def _run_git(args: List[str], cwd: Optional[str] = None, timeout: float = 2.0,
             extra_env: Optional[Dict[str, Optional[str]]] = None) -> Tuple[int, str]:
    """Run `git <args>`, optionally `-C <cwd>`-scoped via subprocess `cwd=`.
    An `extra_env` value of `None` removes that variable from the child env.
    Returns (returncode, stdout). rc == -1 signals a timeout (mirrors bash's
    `rc=124` convention used by the oracle-timeout deny branches).

    Declines to spawn at all, returning `_GIT_PROBE_BUDGET_SPENT_RC`, once
    the dispatch's probe budget is spent (see `_git_probe_deadline`). The
    per-call `timeout` is deliberately NOT clamped to the remaining budget:
    a clamped timeout would push borderline probes onto the rc == -1 path,
    which four oracles in this file treat as fail-CLOSED, manufacturing
    denies out of machine load. Overshoot is therefore bounded by one call's
    own `timeout`, not by the budget."""
    if _git_probe_budget_spent():
        print(
            "bash_guards.dispatch_checks: git probe budget (%.1fs) spent; "
            "declining `git %s` unspawned -- this dispatch's remaining "
            "probes fall back to their own fail-open defaults."
            % (_GIT_PROBE_BUDGET_SECONDS, " ".join(args[:3])),
            file=sys.stderr,
        )
        _git_probe_last_failure.set(
            "the guard's %.1fs git-probe budget was already spent, so this "
            "probe was never spawned" % _GIT_PROBE_BUDGET_SECONDS
        )
        return _GIT_PROBE_BUDGET_SPENT_RC, ""
    env = None
    if extra_env:
        env = dict(os.environ)
        for _k, _v in extra_env.items():
            if _v is None:
                env.pop(_k, None)
            else:
                env[_k] = _v
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=cwd,
            timeout=timeout,
            env=env,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        _git_probe_last_failure.set("the probe timed out after %.1fs" % timeout)
        return -1, ""
    except OSError:
        _git_probe_last_failure.set("git could not be executed")
        return 127, ""
    if result.returncode != 0:
        _git_probe_last_failure.set("git exited %d" % result.returncode)
    else:
        _git_probe_last_failure.set(None)
    return result.returncode, result.stdout


def _batch_show_index_blobs(
    paths: List[str], cwd: Optional[str], ref: str = ""
) -> Dict[str, Optional[str]]:
    """Batched replacement for a per-file `git show :<path>` index-blob read
    (used by `check_validate_commit`'s CLAUDE.md-budget check). ONE
    `git cat-file --batch` feed (stdin, byte mode) resolves every path's
    staged content instead of one `git show` spawn per file -- the same
    amplification shape `cat_file_batch`/`cat_file_batch_objects`
    (`coordinator_core.ops.ceremony.git_native`) close elsewhere, reimplemented
    locally because THIS module's `_run_git` runs `subprocess.run(...,
    encoding="utf-8", errors="replace")` -- text mode -- while `cat-file
    --batch`'s per-record `size` field is a BYTE count; decoding first would
    desync every slice after the first multi-byte character or CRLF. Also
    honors this module's git-probe budget (`_git_probe_budget_spent`), which
    the ceremony helper has no reason to know about.

    `ref` selects which tree each `<rev>:<path>` record reads -- `""` (the
    default) reads the STAGED index blob (`:<path>`, ratchet's post-edit
    read); a caller wanting the pre-edit (last-committed) blob passes `"HEAD"`
    (`HEAD:<path>`), same as C7c's shrink-admission leg below.

    Reconciliation: every requested path is bound to an explicit slot by
    walking `paths` in order -- resolved -> blob text (utf-8, errors=
    "replace", matching `_run_git`'s own decode policy), missing/truncated/
    malformed -> None. Absence from git's output is never read as "resolved".

    Returns `{}` for empty `paths`, spawning no subprocess.
    """
    if not paths:
        return {}
    if _git_probe_budget_spent():
        print(
            "bash_guards.dispatch_checks: git probe budget (%.1fs) spent; "
            "declining `git cat-file --batch` unspawned -- claudemd-budget "
            "reads for this dispatch fall back to skipped (fail-open)."
            % _GIT_PROBE_BUDGET_SECONDS,
            file=sys.stderr,
        )
        return {p: None for p in paths}
    stdin_bytes = ("\n".join("%s:%s" % (ref, p) for p in paths) + "\n").encode("utf-8")
    try:
        proc = subprocess.run(
            ["git", "cat-file", "--batch"],
            input=stdin_bytes,
            capture_output=True,
            cwd=cwd,
            timeout=2.0,
            creationflags=_CREATIONFLAGS,
        )
    except subprocess.TimeoutExpired:
        return {p: None for p in paths}
    except OSError:
        return {p: None for p in paths}
    if proc.returncode != 0:
        # A non-zero exit can follow partial stdout writes; treating that
        # stdout as trustworthy would let the byte-offset parser slice a
        # short/garbled record and hand back a WRONG (truncated) blob
        # instead of a missing one. Fail the whole batch closed, same as
        # TimeoutExpired/OSError above -- an indeterminate read must never
        # present as a successfully-read blob on this commit-hot-path guard.
        return {p: None for p in paths}
    stdout = proc.stdout
    results: Dict[str, Optional[str]] = {}
    pos = 0
    for p in paths:
        nl = stdout.find(b"\n", pos)
        if nl == -1:
            # stdout ran out relative to the requested set -- every
            # remaining path is unresolved; never guess at a partial record.
            results[p] = None
            continue
        header = stdout[pos:nl]
        pos = nl + 1
        if header.endswith(b" missing"):
            results[p] = None
            continue
        parts = header.split(b" ")
        if len(parts) != 3:
            results[p] = None
            continue
        _sha, _type, size_field = parts
        try:
            size = int(size_field)
        except ValueError:
            results[p] = None
            continue
        content = stdout[pos:pos + size]
        pos += size
        if stdout[pos:pos + 1] == b"\n":
            pos += 1
        results[p] = content.decode("utf-8", errors="replace")
    return results


#: Sentinel distinguishing "the recency probe itself failed" (fail-CLOSED --
#: treat as within-window/contested) from "the probe ran cleanly and found
#: no eligible record" (``None`` -- genuinely unclaimed, `continue`). Used
#: only by ``_rm_peer_claim_of``'s mtime backstop below.
_UNKNOWN_MTIME = object()


def _dirty_paths_from_porcelain(porcelain_out: str) -> FrozenSet[str]:
    """Repo-relative paths carrying an uncommitted delta right NOW, parsed
    from one whole-tree ``git status --porcelain`` (no pathspec) capture --
    staged, unstaged, AND untracked all fall out of that single call, unlike
    ``session/scope.py``'s two-call ``diff --name-only HEAD`` +
    ``ls-files --others`` pair (that split exists there for
    ``core.quotepath=false`` parity with a pre-existing dialect; no such
    constraint applies here, so one call answers all three states).

    A rename line's NEW side is the live path with an outstanding delta;
    the OLD side no longer exists and carries none, so only the post-arrow
    half is kept. An unparseable short line is skipped, never guessed at --
    that only NARROWS the returned set, which is the safe direction for a
    caller using this to decide whether a claim is still contested (see
    ``_rm_peer_claim_of``): a path this function fails to recognize as dirty
    falls back to "not proven dirty", never to "proven clean"."""
    paths: set = set()
    for line in porcelain_out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1]
        path = path.strip()
        if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
            path = path[1:-1]
        if path:
            paths.add(path)
    return frozenset(paths)


def _new_dirty_paths_memo() -> Callable[[str], Optional[FrozenSet[str]]]:
    """Per-call, per-ROOT memoized ``git status --porcelain`` lookup -- the
    shared cache every caller looping ``_rm_peer_claim_of`` over several
    targets/affected-paths sharing one root MUST use, so the ONE whole-tree
    status spawn this predicate needs is paid once per root, never once per
    target (the exact per-item amplification shape `_new_git_memo` and
    `_batched_status_for` already close for their own probes -- see those
    docstrings). Returns a closure over a FRESH dict on every call,
    deliberately never module-level -- same soundness argument as
    `_new_git_memo`: the repo tree cannot mutate mid-dispatch, so exact-key
    (root) reuse within ONE guard invocation is safe, and a shared cache
    across dispatches would leak state across unrelated commands.

    The looked-up value is ``None`` on ANY git failure (fail-SAFE signal for
    ``_rm_peer_claim_of`` to consume -- see that function's own docstring
    for what it does with a ``None`` here: skip the new filter entirely and
    fall back to its pre-existing claim-membership-only behavior, never
    treat an unreadable tree as "nothing is dirty")."""
    cache: Dict[str, Optional[FrozenSet[str]]] = {}

    def _lookup(root: str) -> Optional[FrozenSet[str]]:
        if root not in cache:
            # `--untracked-files=all`: a claim is per-FILE, and plain
            # `--porcelain` collapses a wholly-untracked directory to one
            # `?? dir/` row -- every individual file a peer claimed inside
            # it would then read as "not proven dirty" even while genuinely
            # untracked, silently un-contesting a live claim. `-uall` reports
            # each file inside such a directory on its own row instead.
            rc, out = _run_git(
                ["--no-optional-locks", "status", "--porcelain", "--untracked-files=all"],
                root,
            )
            cache[root] = _dirty_paths_from_porcelain(out) if rc == 0 else None
        return cache[root]

    return _lookup


def _rm_peer_claim_of(
    tgt_abs: str,
    root: str,
    dirty_paths_lookup: "Optional[Callable[[str], Optional[FrozenSet[str]]]]" = None,
) -> str:
    """Port of bash's `_rm_peer_claim_of` / `_gr_peer_claim_of` (identical
    trees in both source hooks; this one function serves both
    check_destructive_rm and check_destructive_git_revert, mirroring the
    bash "_gr_ names copied from _rm_ so this hook is self-contained"
    duplication as a single shared implementation instead).

    ``dirty_paths_lookup`` (uncommitted-delta gate, 2026-09-20): a touch
    record SURVIVES the commit that lands the edit -- ``project_live_claims``
    below answers "has this session ever touched this path", never "does it
    still have an uncommitted delta here right now". Left unfiltered, a live
    session goes on contesting every path it ever touched, including one it
    already committed cleanly, and a DIRECTORY target compounds this: one
    dirty file under it is enough to reach the claim scan, whose containment
    match then treats every OTHER already-committed claimed path under that
    same directory as still contested too. ``dirty_paths_lookup(root)``
    (built by ``_new_dirty_paths_memo`` -- ONE whole-tree
    ``git status --porcelain`` per root, never one per target/candidate; see
    that factory's own docstring for why a shared closure is required here)
    resolves the CURRENT dirty set, and a claim match below counts as
    contested only when the matched path is a member of it. Omitted
    (``None``, the default) makes this function build and use its own
    one-shot memo -- correct for a standalone/test call, but a caller inside
    a per-target loop MUST build one memo with ``_new_dirty_paths_memo()``
    and pass it into every call, or it pays the per-item git spawn this gate
    exists to forbid (`test_no_unbatched_per_item_git_spawn.py`).

    FAIL SAFE: ``dirty_paths_lookup`` returning ``None`` (the whole-tree
    ``git status`` itself failed) disables this filter entirely for that
    root -- every matched claim stays contested, exactly the pre-existing
    (unfiltered) behavior. An unreadable working tree must never be read as
    "nothing outstanding"; only a POSITIVELY confirmed clean path is ever
    forgiven.

    Liveness order (Q24 -- CLOSED, see module docstring "CLOSED PORTING
    GAPS"): for each candidate peer session dir under
    ``<root>/.git/coordinator-sessions/*``, canonical liveness
    (``coordinator_core.session.liveness.live_session_ids``) is consulted
    FIRST, per-sid:
      - If the sid has a ``meta.json`` file, it is within canonical's scan
        scope ("covered"). A covered sid is treated as a live peer-claim iff
        it is a member of ``live_session_ids(cwd=root)`` -- regardless of its
        touched.txt mtime (a live-but-idle peer, e.g. mtime > 30 minutes
        stale, is STILL treated as contested). A covered sid absent from the
        live-set is dead -> not contested, and is skipped entirely (no mtime
        fallback for a sid canonical has already ruled on).
      - If the sid has NO ``meta.json``, it is outside canonical's scan scope
        (not "known dead" -- simply unseen), and degrades to the 30-minute
        ``touched.txt`` mtime backstop exactly as before.
    A ``live_session_ids`` call that raises (fail-safe, mirrors the bash
    subshell-isolated failure handling) degrades every sid to the mtime
    backstop, matching the bash "empty live-set -> everything degrades"
    fail-safe posture.

    Self-identity exclusion (2026-07-16 -- CLOSED, bug-backlog
    2026-07-16-dispatch-checks-py-peer-claim-guard-omit-811b3291dd8a.yaml):
    ``cur_sid`` is resolved via the canonical
    ``coordinator_core.session.core.resolve_session_id(cwd=root)`` -- the
    same 4-tier chain bash's ``cs_resolve_session_id`` walks -- and a sid
    equal to ``cur_sid`` is skipped in the per-sid scan below (a session
    never contests its own claim). Mirrors the bash
    ``[[ -n "$cur_sid" && "$sid" == "$cur_sid" ]] && continue`` guard
    exactly: the skip only fires when ``cur_sid`` is non-empty, so an
    unresolvable/ambiguous identity (``resolve_session_id`` returns ``""``
    on Tier-4 ambiguity -- see its own docstring) degrades to "exclude
    nothing" rather than "exclude everything" -- a genuine live peer stays
    contested, never a false-ALLOW.
    """
    sess_dir = os.path.join(root, ".git", "coordinator-sessions")
    if not os.path.isdir(sess_dir):
        return ""
    try:
        tgt_rel = os.path.relpath(tgt_abs, root).replace(os.sep, "/")
    except ValueError:
        return ""
    if not tgt_rel:
        return ""

    # lazy, per-call import. An
    # ImportError here (or any other exception) degrades identically to the
    # pre-existing "unresolvable identity" path: cur_sid = "" -> the
    # self-exclusion guard below simply does not fire (never widens to
    # "exclude everything"), so a genuine live peer stays contested.
    try:
        from coordinator_core.session.core import resolve_session_id
        cur_sid = resolve_session_id(cwd=root)
    except Exception:
        cur_sid = ""

    # lazy, per-call import. An
    # ImportError here degrades identically to a raising `live_session_ids`
    # call: live_ok=False forces every sid through the mtime backstop below
    # (fail-CLOSED for the destructive guard -- never "canonical says dead,
    # allow").
    live_ok = True
    try:
        from coordinator_core.session import liveness
        live_sids = liveness.live_session_ids(cwd=root)
    except Exception:
        live_sids = frozenset()
        live_ok = False

    # Hoisted once per call rather than
    # per-sid (bash re-reads epoch per sid); skew is sub-millisecond across
    # the loop, far below the 30-minute backstop window -- intentional.
    now = time.time()
    try:
        entries = os.listdir(sess_dir)
    except OSError:
        return ""

    # Uncommitted-delta gate: resolved ONCE per call (never per-sid, never
    # per-candidate-path) -- see this function's own docstring for why a
    # shared, caller-supplied memo is required to keep this at one spawn
    # per root rather than one per target. A caller that omitted
    # ``dirty_paths_lookup`` gets a private one-shot memo here, correct for
    # a standalone call but NOT for a per-target loop -- see the docstring.
    _dirty_lookup = dirty_paths_lookup or _new_dirty_paths_memo()
    dirty_now = _dirty_lookup(root)

    for sid in entries:
        if sid in (".archive", ".agents"):
            continue
        if cur_sid and sid == cur_sid:
            continue
        sid_dir = os.path.join(sess_dir, sid)
        if not os.path.isdir(sid_dir):
            continue

        meta_path = os.path.join(sid_dir, "meta.json")
        # A raising live_session_ids call
        # must degrade EVERY sid (covered or not) to the mtime backstop, per
        # this function's own docstring; gating solely on meta.json presence
        # silently reopened a false-allow for covered sids when the try/except
        # above caught an exception.
        canon_covers = live_ok and os.path.isfile(meta_path)
        if canon_covers:
            if sid not in live_sids:
                continue  # canonical says dead -> not contested
            # canonical says live -> contested regardless of mtime; fall
            # through to the touched.txt path-match below (no mtime check).
        else:
            # Degradation backstop: sid unseen by canonical liveness.
            # AC6/C5 -- repointed onto `liveness.newest_record_mtime`, the
            # single shared recency policy (C3 brief), rather than a
            # hardcoded `touched.txt` isfile+mtime probe. Post-cutover, a
            # session writing only `touch-record.jsonl` (no `touched.txt`)
            # must still be found live by this backstop -- the old literal
            # probe treated that sid as UNCLAIMED (fail-open on this
            # destructive guard), which is the C7b failure class arriving
            # from an mtime probe instead of a content read.
            #
            # Lazy, per-call import, mirroring the `live_ok` guard above: an
            # ImportError (or any other exception) here must degrade
            # fail-CLOSED -- fall through as contested -- never silently
            # `continue` past a genuine live peer because the session
            # package failed to import.
            try:
                from coordinator_core.session.liveness import newest_record_mtime

                mtime = newest_record_mtime(sid_dir)
            except Exception:
                mtime = _UNKNOWN_MTIME
            if mtime is None:
                continue
            if mtime is not _UNKNOWN_MTIME and now - mtime > 1800:
                continue

        # The AC21 legacy union that stood here was deleted 2026-08-27 on the
        # condition this comment itself named: "until a chunk migrates that
        # writer off the old dialect (verified at HEAD, not assumed from a
        # stale premise) and deletes this union." Both halves are now true.
        # `claims.atomic_dedup_append`'s last reachable production caller --
        # the CLI `claim-path` seam -- was repointed onto
        # `touch_record.append_event` (`ab177e43f`), and the claim-reader
        # union came out of `scope.py`/`claim_index.py` in one change
        # (`227b513e7`). The on-disk corpus was drained BEFORE the read was
        # removed, not assumed empty: `legacy_touch_corpus_migrate` applied
        # across 135 dirs, and both checks then read zero -- the existence-
        # keyed `legacy_touch_corpus_drain_check` and the CONTENT-level
        # `legacy_touch_corpus_straggler_check`, the latter walking
        # `.agents/<aid>/`. Neither alone is sufficient, since the
        # existence-keyed checks can never reopen a drained dir.
        #
        # NEGATIVE SPEC -- do NOT restore this union on the shape of the
        # 2026-08-03 fail-open. That restoration (58420b380) was correct at
        # the time because a live writer still emitted the old dialect; a
        # peer whose claims arrived only through `self_claim` then projected
        # to zero claims and a destructive command against its claimed path
        # was silently allowed. No writer can emit that dialect now, so a
        # restored union would read a corpus that cannot grow, and would
        # re-admit the legacy PARSER this seam exists to retire. If a legacy
        # writer ever reappears, the defect is the writer.
        new_claimed_paths: set = set()
        new_seam_degraded = False
        try:
            from coordinator_core.session.touch_record import (
                project_live_claims,
                sink_path,
            )

            touch_record_path = sink_path(sid_dir)
            projection = project_live_claims(touch_record_path, cwd=root)
            new_claimed_paths = set(projection.claims.keys())
            new_seam_degraded = projection.degraded
        except Exception:
            new_seam_degraded = True

        if new_seam_degraded:
            return sid
        for opath in new_claimed_paths:
            if not (
                opath.startswith(tgt_rel + "/")
                or tgt_rel.startswith(opath + "/")
                or opath == tgt_rel
            ):
                continue
            # Uncommitted-delta gate: a claim on a path with NO outstanding
            # delta right now is a touch record that outlived the commit
            # that landed it, not a live contest. ``dirty_now is None``
            # means the whole-tree status probe itself failed -- fail SAFE,
            # skip this filter, keep the pre-existing (unfiltered) contested
            # verdict rather than ever reading an unreadable tree as clean.
            if dirty_now is not None and opath not in dirty_now:
                continue
            return sid
    return ""


#: BX-13 (2026-07-29, confirmed live via the real dispatcher): several
#: checks in this module scan raw/segmented command TEXT with a pattern
#: anchored at a `;`/`&`/`|`/string-start boundary, so a quoted
#: `sh -c '<payload>'` (or `bash -c`/`env sh -c`/etc.) argument -- which the
#: invoking shell WILL execute -- was never unwrapped and re-scanned; only
#: the OUTER `sh -c '...'` text was seen, and a `git`/`rm`/`find` token
#: inside the quoted payload never sat at a recognized boundary. Own-module
#: copy of the identical fix shipped in `block_subagent_commit.py`'s
#: `_wrapped_shell_c_payloads` (not shared via cross-module import -- this
#: file's own established no-cross-module-coupling precedent for small
#: tokenizer-adjacent helpers).
#: Staged-path count above which the per-file unstage remedy stops being
#: advice and becomes an instruction to destroy a peer's in-flight work.
#:
#: Measured 2026-09-02: a peer's bulk untracking left 11,534 foreign paths
#: staged on the shared branch. Every commit form in this tree refused for as
#: long as that stood -- including `git commit -- <pathspec>`, because this
#: check reads the whole index and a pathspec does not narrow it. The refusal
#: named one `git restore --staged <file>` per file, which at that size is an
#: hour of work whose successful completion is the loss of the peer's change.
_BULK_FOREIGN_INDEX_PATHS = 50


#: The one commit route that still succeeds while a peer holds the index, named
#: in both strict-scope refusals so a blocked operator has an exit that is not
#: "destroy the peer's staging".
#:
#: WHY IT IS SAFE TO NAME, since pointing at a route that escapes a guard is
#: otherwise exactly this file's anti-scope: `coordinator-safe-commit.py` routes
#: through `ceremony.commit_v2` WITHOUT `prefer_deliberate_stage`, and on that
#: default axis `coordinator_core/git/commit.py :: commit_paths` commits worktree
#: bytes for the paths the caller declared and passes over every staged path it
#: did not name. A peer's staged blob on an undeclared path is ignored rather
#: than swept -- the failure mode is prevented by construction, not by a check
#: that could be skipped. Pinned by `coordinator_core/git/tests/
#: test_action_guard_default_path.py :: test_default_axis_ignores_a_foreign_
#: staged_path`, and classified `predicate-consulted-by-construction` in
#: `docs/research/2026-08-30-the-guard-escape-table.md`.
#:
#: NEGATIVE SPEC: "literal paths, never a glob" is load-bearing, not style. The
#: substitution window is paths the caller NAMED; a glob expands in the shell
#: before the route sees it, so a peer's file inside the expansion arrives as a
#: declared path and is committed as instructed. That is the 2026-08-29 incident
#: shape (29927e684) and the one way this route can still take a peer's work.
#: Never soften the clause to just "your own paths".
_SAFE_COMMIT_ROUTE_CLAUSE = (
    'Your own paths still commit: coordinator-safe-commit.py "<subject>" '
    "-- <literal paths>; name them literally, never a glob."
)


def _bulk_foreign_index_refusal(
    staged_count: int,
    staged_file: str,
    owner_sentence: str,
    provenance_note: str,
) -> str:
    """The strict-scope refusal for an index that is another session's.

    Separate from the per-file refusal because the two say different true
    things and only one of them is true at this size. Extracted so the text
    can be asserted directly: built inline it was unreachable from a test
    without standing up git state, which is why the original wording shipped
    unexamined.

    NEGATIVE SPEC: this message must never name `git restore --staged`. That
    is the remedy the per-file branch offers, and following it here is how an
    operator destroys 11,534 paths of a peer's work one command at a time.
    """
    return (
        "BLOCKED (strict scope): the index holds %d staged paths and %s is "
        "not in this session's touch list — owned by %s.%s\n\n"
        "At this size the index is another session's in-flight change, and no "
        "`git commit` form succeeds until they land or unstage it — a pathspec "
        "does not narrow what this check reads. Ask them. %s"
        % (
            staged_count,
            staged_file,
            owner_sentence,
            provenance_note,
            _SAFE_COMMIT_ROUTE_CLAUSE,
        )
    )


def _format_batched_scope_denial(entries: List[Dict[str, str]]) -> str:
    """Combine every Check 5 deny-arm hit from one commit's staged-file walk
    into a SINGLE denial that names every offending path once, instead of
    the walk returning on the first hit and dripping one more name per
    clearing attempt (the reported defect: a 311-path plan-blitz landing
    surfaced exactly one more name per retry, with no way to tell a
    two-file problem from a fifty-file one).

    Each ``entries`` item is one arm's OWN already-composed reason/remedy
    text (foreign-hunk mismatch, vanished-from-disk, contested, or
    unclaimed) -- this function never re-derives or reformats an arm's
    finding, it only decides how many to print in full and how to
    summarize the rest, so the arms stay visibly distinct rather than
    collapsing into one undifferentiated list.

    Single offender: renders EXACTLY that offender's own ``text``,
    unprefixed -- the pre-batching shape every existing substring-pinned
    test (foreign hunk / contested / unclaimed) still expects, so a
    one-path commit reads exactly as it did before this change.

    Multiple offenders, count <= ``_BULK_FOREIGN_INDEX_PATHS``: a header
    naming the count, then each offender's own full text under its own
    path heading -- no information lost.

    Multiple offenders, count > ``_BULK_FOREIGN_INDEX_PATHS``: same cost
    problem `_bulk_foreign_index_refusal` exists for -- printing a `git
    restore --staged <file>` per path at that size is an hour of
    destructive work, not a remedy -- so beyond the cap this names only
    the offending path and its one-line kind, then an exact count of the
    remainder, without repeating each arm's own remedy paragraph.
    """
    if len(entries) == 1:
        return entries[0]["text"]

    if len(entries) <= _BULK_FOREIGN_INDEX_PATHS:
        header = "BLOCKED (scope): %d staged paths failed validation:" % len(entries)
        body = "\n\n".join("%s\n%s" % (e["path"], e["text"]) for e in entries)
        return header + "\n\n" + body

    shown = entries[:_BULK_FOREIGN_INDEX_PATHS]
    remainder = len(entries) - len(shown)
    header = (
        "BLOCKED (scope): %d staged paths failed validation. At this size "
        "a per-path remedy is not printed -- following `git restore "
        "--staged <file>` per path here risks destroying in-flight work."
        % len(entries)
    )
    listing = "\n".join("- %s (%s)" % (e["path"], e["kind"]) for e in shown)
    return (
        header
        + "\n\n"
        + listing
        + "\n... and %d more.\n\n" % remainder
        + _SAFE_COMMIT_ROUTE_CLAUSE
    )


def _owner_clause_budget_bytes() -> int:
    """Check 5's owner-clause byte budget -- a FRACTION of the shipped guard
    message-size-discipline prose cap (``_message_size.MESSAGE_PROSE_CAP_BYTES``),
    never a hardcoded literal (C3 spec: "read it from the ... budget CONSTANT").
    The owner clause is one ingredient of the larger SCOPE/BLOCKED sentence
    (which also carries the staged path, twice in the strict-deny form), so
    it is capped at a THIRD of the whole-message budget rather than the
    whole budget itself -- leaving headroom for the fixed template text and
    the staged file's own (unbounded) path length.

    Function-local import (not module-top): ``_message_size`` transitively
    imports ``dispatch.py``, which imports THIS module at its own top level
    (``_dc``) -- a module-level import here would deadlock that cycle on a
    fresh interpreter (proven empirically: ``_is_hazard_repo`` unresolved on
    a partially-initialized ``dispatch_checks``). By the time any
    ``check_*`` function actually runs, both modules have long finished
    loading, so deferring the import to call time is safe.
    """
    from coordinator_core.bash_guards._message_size import MESSAGE_PROSE_CAP_BYTES

    return MESSAGE_PROSE_CAP_BYTES // 3


def _truncate_to_budget(text: str, max_bytes: int) -> str:
    """Byte-safe truncation (never split a multi-byte UTF-8 codepoint) with
    a trailing ellipsis marker when truncated.

    The marker is charged AGAINST ``max_bytes``, not added on top of it.
    Measured 2026-09-03: the pre-fix form truncated to ``max_bytes`` and then
    appended a 3-byte "…", so every truncating call returned ``max_bytes + 3``
    -- a budget the function is named for enforcing and silently overshot.
    Callers that chain it (owner clause -> whole message) compounded the
    overshoot once per layer.

    NEGATIVE SPEC: do not "fix" a caller by subtracting 3 before calling. The
    reservation belongs here, once, or the next caller repeats the arithmetic
    and gets it wrong in a new place.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    marker = "…"
    room = max_bytes - len(marker.encode("utf-8"))
    if room <= 0:
        return encoded[:max_bytes].decode("utf-8", errors="ignore")
    return encoded[:room].decode("utf-8", errors="ignore") + marker


def _owner_liveness_basis(owner_id: str, live_verdicts: Dict[str, Tuple[bool, str, Optional[int]]]) -> Optional[str]:
    """The liveness BASIS (``"stable-pid"``, ``"recency-window"``, ...) C8's
    ``live_session_verdicts()`` seam recorded for ``owner_id``, or ``None``
    when that id has no verdict at all (not a live session dir this call --
    e.g. a sentinel identifier, never a real session id)."""
    verdict = live_verdicts.get(owner_id)
    return verdict[1] if verdict is not None else None


def _resolve_owner_writer_name(fact: "OwnerFact") -> Optional[str]:
    """Three-rung name resolution ladder (C3, plan
    ``2026-09-01-the-claim-record-carries-the-name``) -- POLICY now shared
    verbatim with C2 (``coordinator/bin/session-claim-cli.py``) via
    ``coordinator_core.session.name_ladder.resolve_name`` (state/debt-
    backlog/2026-09-01-shared-name-resolution-ladder-for-sessio-
    026b33fcd43d.yaml), applying ``_holder_context``'s discipline
    (``coordinator/bin/coordinator-safe-commit.py``, landed 3dcf73f06c/
    586bb605a6) VERBATIM: PROVENANCE, never ADDRESS.

    IDENTIFY-BY-SID, ADDRESS-BY-RESOLVED-NAME (DoE ruling 6,
    ``2026-09-11-coordinator-content-repo-em-rulings-owed-bundle.md``): ``fact.owner`` is
    the stable identifier Check 5's owner sentence is ABOUT; the name this
    ladder returns is the address, resolved fresh at render time, not a
    permanently stable claim on either side -- neither a sid nor a name is
    guaranteed to stay resolvable forever.

    Rung 1 -- ``fact.writer_name``, the name C1 stamped ON the claim at
    write time. Survives the writer exiting, a name re-point, and a
    cross-machine read -- the durable answer this plan exists to add.
    Rung 2 -- failing that, a live ``harness_registry.lookup(fact.owner)``.
    The cheap fallback rung: correct only for a pre-C1 record whose writer
    is still resident on THIS machine, at read time, on THIS box. See
    ``name_ladder``'s module docstring for its retirement condition.
    Rung 3 -- failing both, ``None`` -- the caller renders an explicit
    UNNAMED marker, visually distinct from a bare sid (Anti-scope: never
    print a bare sid as though it were an address -- that teaches the fleet
    to route around the guard). Check 5's own budget collapses ALL THREE of
    ``name_ladder``'s rung-3 sub-reasons into this one marker -- that
    collapse is this call site's considered choice, not a gap; see
    ``name_ladder.resolve_name``'s docstring for what those sub-reasons are.

    A missing or failing lookup (``harness_registry`` unimportable, or its
    ``lookup`` raising) degrades to rung 3, never raises -- Check 5 is
    advisory infrastructure and must not turn a lookup failure into a guard
    crash.
    """
    from coordinator_core.session import name_ladder

    try:
        from coordinator_core.session import harness_registry

        lookup = harness_registry.lookup
    except Exception:  # noqa: BLE001 - degrade to rung 3, see docstring
        lookup = None

    name, rung, _reason = name_ladder.resolve_name(fact.writer_name, fact.owner, lookup)
    if rung == name_ladder.RUNG_UNRESOLVED:
        return None
    return name


def _owner_display_id(fact: "OwnerFact", name: Optional[str]) -> str:
    """The identifier Check 5 substitutes for ``fact.owner`` inside the
    subject clause (``"session %s"`` and friends).

    C3 follow-up fix 2 (plan ``2026-09-01-the-claim-record-carries-the-
    name``, EM-adjudicated break-class): the ORIGINAL shape rendered the
    FULL 36-character sid in the subject -- on a real sid that alone spent
    over half of ``_owner_clause_budget_bytes()`` (~73 bytes) before the
    name-bearing trailing clause was even reached, so truncation routinely
    cut the name to an unusable fragment or dropped it entirely, and in the
    ``undetermined``/``agent`` classes it could cut into the liveness/basis
    suffix too.

    ONCE A NAME IS KNOWN, THE FULL UUID IS THE REDUNDANT PART (the plan's
    own reasoning): when ``name`` resolves (rung 1 or 2 -- resolved once by
    the caller via ``_resolve_owner_writer_name`` and passed in here), this
    returns only the SHORT sid (``fact.owner[:8]``, the ``_holder_context``
    precedent -- ``coordinator/bin/coordinator-safe-commit.py``) for the
    subject slot, freeing the budget the full name needs at
    ``_owner_writer_name_clause``'s trailing ``" -- w:<name>"`` clause. When
    ``name`` is falsy, this returns ``fact.owner`` UNCHANGED -- today's
    full-sid rendering, exactly as before, for that case.

    Deliberately does NOT fold the name into this return value: the name
    clause stays a TRAILING addition (as it always was) rather than moving
    ahead of the liveness verdict, so `_truncate_to_budget`'s cut-from-the-
    end still degrades the name first and the safety-relevant liveness
    verdict last, by construction -- an adversarially long name must never
    be able to push "confirmed live"/"no longer live"/"CONTESTED" out of
    the truncated result.
    """
    if not name:
        return fact.owner
    return fact.owner[:8]


def _owner_writer_name_clause(name: Optional[str]) -> str:
    """The trailing name clause -- ``" -- w:<name>"`` when a name resolved
    (rung 1 or rung 2, resolved once by the caller and passed in here), or
    ``" -- UNNAMED"`` when neither rung resolved. No rung marker: the
    former ``*``/``~`` distinction between "recorded" and "live-lookup" had
    no legend anywhere in agent-facing text, and
    ``_owner_name_provenance_note`` fires identically for both rungs, so
    the distinction was undecodable residue.
    """
    if not name:
        return " -- UNNAMED"
    return " -- w:%s" % name


def _name_clause_fits(sentence: str, writer_name: Optional[str]) -> bool:
    """Whether ``sentence`` still has room for its whole name clause inside
    the owner-clause budget. Shared by ``_with_name_clause`` (which appends
    it) and by ``_format_owner_sentence``'s live arm (which uses it to
    decide whether the liveness BASIS can be afforded at all) -- one
    predicate so the two cannot disagree about what fits.

    Negative-spec: never approximate this with a length constant. The
    clause, the subject and the budget all move independently, and a
    hardcoded threshold is how the live arm silently started dropping
    names for long repo names while every test still passed.
    """
    clause = _owner_writer_name_clause(writer_name)
    return len((sentence + clause).encode("utf-8")) <= _owner_clause_budget_bytes()


def _with_name_clause(sentence: str, writer_name: Optional[str]) -> str:
    """Append the name clause whole-or-nothing, then cap to the owner-clause
    budget. THE single exit for every owner class in
    ``_format_owner_sentence`` -- do not re-implement it per branch.

    Whole-or-nothing: if the full clause will not fit beside the
    load-bearing subject/verdict prefix, drop it ENTIRELY rather than let
    ``_truncate_to_budget`` cut it to a ``w:proj…`` fragment. A fragment is
    strictly worse than no name, because ``_owner_name_provenance_note``
    triggers on the same ``" -- w:"`` substring and would fire a staleness
    warning beside a name the reader cannot read. Whole-or-nothing keeps
    that coupling honest: the warning fires exactly when a full name shows.

    Negative-spec -- this was ONE branch's local guard and the bug was
    structural, not local: it lived inline in the ``session``/``agent`` arm
    while ``agent-race`` and ``unreadable`` appended unconditionally and
    truncated, so two of the six classes emitted exactly the fragment the
    guard existed to prevent, and every test passed. A per-branch copy is
    how that recurs; there is one implementation and three callers.
    """
    clause = _owner_writer_name_clause(writer_name)
    budget = _owner_clause_budget_bytes()
    if _name_clause_fits(sentence, writer_name):
        return _truncate_to_budget(sentence + clause, budget)

    # A RESOLVED name that does not fit must still leave a marker. Dropping
    # the clause outright renders subject + verdict and nothing else -- a
    # bare sid presented as though it were an address, which is the single
    # output this whole cluster exists to prevent and which
    # `_format_owner_sentence`'s own anti-scope forbids by name. Measured
    # 2026-09-03: the drop began at a 35-byte writer_name, with or without a
    # basis clause; the longest name in the live registry is 25 bytes, so
    # this was latent rather than firing -- and
    # `test_no_live_owner_clause_ever_ends_in_a_bare_sid` asserted the
    # universal from three names (19/25/22) that all sat under the
    # threshold, proving less than it claimed.
    #
    # Precedence, worst-last: a full name beats a truncated one, a truncated
    # one beats UNNAMED, and every one of them beats a bare sid. The old
    # whole-or-nothing rule ranked a fragment BELOW nothing on the grounds
    # that `_owner_name_provenance_note` fires on the `" -- w:"` substring
    # and would warn beside an unreadable name. That trade is real but it
    # was priced against the wrong alternative: the fallback is not "no
    # name", it is "no address at all". A marked truncation warns beside a
    # partial name; the drop warns beside nothing and reads as though no
    # name existed.
    #
    # NEGATIVE SPEC: never fall back to `" -- UNNAMED"` here. That marker is
    # rung 3's -- it states that NO name resolved -- and reusing it for a
    # name that resolved but did not fit makes the two indistinguishable to
    # every reader and to the provenance note.
    if writer_name:
        if budget - len(sentence.encode("utf-8")) - len(" -- w:") > 4:
            return _truncate_to_budget(sentence + " -- w:%s" % writer_name, budget)
    return _truncate_to_budget(sentence, budget)


def _format_owner_sentence(
    fact: Optional["OwnerFact"],
    live_verdicts: Dict[str, Tuple[bool, str, Optional[int]]],
) -> str:
    """Render the OPERATOR-FACING owner clause for Check 5's advisory AND its
    strict-mode deny -- ONE formatter so the two renderings cannot drift
    (AC10). Takes an ``OwnerFact`` (``coordinator_core.session.scope``, C1)
    and renders one of SIX distinct classes (AC6), plus the AC7 CONTESTED
    rendering for an undetermined liveness verdict:

      1. live peer               -- claim_source "session", liveness "live"
      2. dead peer                -- claim_source "session", liveness "dead"
      3. peer's dispatched agent  -- claim_source "agent" (any liveness)
      4. unresolved agent-race    -- claim_source "agent-race"
      5. claims-unreadable        -- claim_source "unreadable"
      6. no-claim-found           -- ``fact is None`` -> "unknown owner"

    Negative-spec: the word "orphan" must appear in NO string this function
    returns -- that is the machine TOKEN's vocabulary (see
    ``_format_owner_token``), not this operator sentence's. The agent-race
    and claims-unreadable classes must NEVER collapse into class 6's
    "unknown owner" -- both are a positive, recent, on-disk claim (just not
    yet resolvable to a session id), and re-rendering either as an absence
    of evidence would reproduce this plan's own defect in a new spelling.
    Class 6 alone states outright that it is NOT a safety verdict.
    """
    if fact is None:
        return _truncate_to_budget(
            "unknown owner (no claim found -- NOT a safety verdict)",
            _owner_clause_budget_bytes(),
        )

    # Resolved once here and threaded through both helpers below -- each
    # used to call `_resolve_owner_writer_name` independently, up to two
    # `harness_registry.lookup` calls per rendered sentence.
    writer_name = _resolve_owner_writer_name(fact)

    if fact.claim_source == "agent-race":
        # Terse by budget: the pre-fix wording exceeded the ~73-byte budget
        # before any name, truncating the load-bearing CONTESTED verdict
        # away. Do not re-lengthen -- and no ": agent-race" token; the
        # prefix cost dropped the name clause entirely.
        sentence = (
            "%s unattributed agent -- CONTESTED"
            % _owner_display_id(fact, writer_name)
        )
        return _with_name_clause(sentence, writer_name)

    if fact.claim_source == "unreadable":
        sibling_id = _owner_display_id(fact, writer_name) if fact.owner and fact.owner != ".agents" else None
        sentence = (
            ("sibling %s" % sibling_id if sibling_id else "an unresolved sibling")
            # "its claim record is" -> "claim record": the dropped words say
            # nothing the remaining ones do not, and they were the whole
            # difference between a readable name clause and a `w:proj…`
            # fragment on a real 36-character sid.
            + " (claim unreadable this call)"
        )
        return _with_name_clause(sentence, writer_name)

    # claim_source in ("session", "agent") from here down.
    display_id = _owner_display_id(fact, writer_name)
    subject = (
        "session %s" % display_id
        if fact.claim_source == "session"
        # C3 follow-up fix 3 (bug 616e4449f90c): NOT "session %s's dispatched
        # agent" (the pre-fix shape) -- on a real 8-char display_id plus a
        # realistic name, that subject alone (25 bytes without "session ")
        # already left too little of the ~73-byte budget for the name
        # clause (~24 bytes) once ANY liveness prose was added, so the name
        # truncated to a fragment in every agent-class rendering. Dropping
        # the redundant "session " word (the subject already says
        # "dispatched agent", which only a session dispatches) is the
        # cheapest cut available in this clause: it costs no information a
        # reader did not already have from "dispatched agent" itself, and
        # the substring `"dispatched agent"` -- the one thing
        # ``test_check_validate_commit.py`` pins outside this file -- is
        # untouched.
        else "%s dispatched agent" % display_id
    )
    if fact.liveness == "live":
        basis = _owner_liveness_basis(fact.owner, live_verdicts)
        sentence = "%s (confirmed live%s)" % (
            subject,
            " via %s" % basis if basis else "",
        )
        # The BASIS yields to the NAME, same call the CONTESTED arm below
        # already makes: " via harness-registry" is 21 bytes of diagnostic
        # provenance, and on a real sid it spent exactly the bytes the name
        # clause needed. Measured 2026-09-02 against the published engine:
        # `session b4681734 (confirmed live via harness-registry)` is 54
        # bytes of a 73-byte budget, so a `-- w:example-cockpit-repo-0f` clause
        # (24) did not fit and `_with_name_clause` dropped it WHOLE --
        # rendering a bare sid with no `-- w:` and no `-- UNNAMED`, the one
        # output this clause's anti-scope forbids, to the exact reader who
        # is blocked and needs the address. `example-game-repo-em` (11 bytes) fit
        # and `example-cockpit-repo-0f` (18) did not, so the fleet's longer repo
        # names lost the name and the shorter ones kept it.
        #
        # Basis is a debugging aid for whoever maintains the liveness
        # ladder; the name is the blocked operator's only route to the
        # peer holding their tree. Keep the basis when it is free, drop it
        # the moment it costs the name.
        if basis and not _name_clause_fits(sentence, writer_name):
            sentence = "%s (confirmed live)" % subject
    elif fact.liveness == "dead":
        sentence = "%s (no longer live)" % subject
    else:
        # C3 follow-up fix 3: was "liveness undetermined this call --
        # CONTESTED" (~44 bytes) -- said the same fact twice ("undetermined"
        # and "CONTESTED" are the same verdict) and, added to the
        # "dispatched agent" subject, left no room for the name clause at
        # all. "CONTESTED" alone is the token every test in this suite and
        # `test_check_validate_commit.py` actually asserts on (grepped
        # before this edit -- no test anywhere pins the word
        # "undetermined" appearing in the rendered sentence, only in the
        # `OwnerFact.liveness` field name); the descriptive words around it
        # were free bytes this budget cannot afford across all six classes
        # with a name attached, so they are cut rather than the name.
        sentence = "%s (CONTESTED)" % subject
    return _with_name_clause(sentence, writer_name)


_OWNER_NAME_PROVENANCE_WARNING = (
    " A name shown beside a claim is provenance recorded when the claim "
    "was written, not a live address -- verify the holder before "
    "sending to it."
)

def _owner_name_provenance_note(owner_sentence: str) -> str:
    """C3 follow-up fix 1 (plan ``2026-09-01-the-claim-record-carries-the-
    name``, EM-adjudicated break-class): the EXPLICIT staleness warning
    Check 5's owner-clause budget has no room for (see
    ``_owner_writer_name_clause``'s docstring). This function is called by
    every UNBUDGETED
    site that interpolates an ``_format_owner_sentence()`` result into a
    deny or warn message (Check 6a's two strict-mode denies and its
    warn-only advisory) -- those three templates have no
    ``_truncate_to_budget`` cap, so the warning lives there instead of in
    the budgeted clause itself, at zero cost, read exactly once by the
    human it is for.

    Deliberately does NOT say "re-resolve from the stored session UUID":
    the ``sid`` a stale name is attached to is, by hypothesis, precisely
    the one that no longer resolves (this plan's own Problem statement) --
    telling the reader to check the thing that is already known broken is
    ritual, not a remedy.

    Fires only when ``owner_sentence`` actually names someone (contains
    ``_owner_writer_name_clause``'s ``" -- w:"`` marker prefix, rung 1 or
    rung 2) -- returns ``""`` for the "unknown owner" / "UNNAMED"
    renderings, which name nobody to warn about.
    """
    if " -- w:" not in owner_sentence:
        return ""
    return _OWNER_NAME_PROVENANCE_WARNING


def _format_owner_token(fact: Optional["OwnerFact"]) -> str:
    """Render the MACHINE-STABLE ``owner:`` token for ``scope-warnings.log``
    -- a stable vocabulary distinct from ``_format_owner_sentence``'s prose
    (AC10 split), because the log column has a documented sibling-repo
    consumer.

    NEGATIVE SPEC: the no-claim-found token stays the literal ``"orphan"`` --
    do NOT change it. A cross-repo answer on this column's vocabulary is
    still pending, and ``test_check_validate_commit.py:100`` pins
    ``owner:orphan`` literally for exactly this case; changing it breaks a
    documented sibling-repo surface.
    """
    if fact is None:
        return "orphan"
    if fact.claim_source in ("session", "agent"):
        return "session %s" % fact.owner
    if fact.claim_source == "agent-race":
        return "agent-race:%s" % fact.owner
    if fact.claim_source == "unreadable":
        return "unreadable:%s" % fact.owner
    return "orphan"


#: Commands whose exit status is load-bearing: a swallowed failure here lets the
#: NEXT command in the chain run against a state that never happened.
_EXIT_STATUS_LOAD_BEARING = ("commit", "push", "merge", "rebase", "cherry-pick", "mv", "rm", "tag")


def _piped_exit_code_chain(command):
    """The offending segment when a git mutation's status is read through a pipe.

    `git commit -F msg.txt | tail -3 && git push` tests TAIL, not the commit.
    `$?` after a pipeline is the LAST stage's status, so a failed commit is
    invisible and the push runs anyway against a commit that never landed.

    Measured 2026-09-11 in two repos independently, by two sessions who were at
    that moment each telling the other to distrust unverified green signals. The
    habit that produces it is benign -- piping to `tail`/`head` to keep output
    short -- which is why it goes unnoticed: it is invisible until the first
    stage actually fails, and then it is invisible again.

    Static string work only. No spawn, no git, nothing to budget.
    """
    for segment in re.split(r"&&|[|][|]", command):
        if "|" not in segment:
            continue
        head = segment.split("|", 1)[0].strip()
        # Routed through the module's own resolver rather than a local regex:
        # `git -C <path> commit` and `git -c k=v commit` both put a VALUE between
        # the flag and the subcommand, and a hand-rolled pattern that misses them
        # is a detector with a hole exactly where a scripted call sits.
        _head_tokens = _bt_tokenize_full_command(head)
        if not _head_tokens:
            continue
        if _bt_git_resolved_subcommand(_head_tokens) not in _EXIT_STATUS_LOAD_BEARING:
            continue
        return head
    return None


_NOTE_CORRECTION = _NOTE_SENTINEL + " <subject>"
