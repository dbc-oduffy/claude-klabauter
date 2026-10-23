"""coordinator_core.bash_guards._dc_rm -- guard family 3 of the
``dispatch_checks`` split: ``check_destructive_rm`` (block-destructive-rm.sh),
``check_stale_write`` and their target-resolution / touch-record helpers.

Imports only the shared ``_dc_*`` layer and pre-existing sibling modules,
never the ``dispatch_checks`` facade or another family module.

CLOSED PORTING GAP (2026-07-31, incident + code-reviewer findings
1/2/3/4/5/6): the 2026-07-31 `~/.claude` destruction (see
`check_destructive_rm`'s own root-deny comment and
`test_check_destructive_rm_repo_root.py`) turned up four further gaps in the
same function beyond the root-resolution/identity fix itself: a bare
repository target bypassed the guard entirely (no `--show-toplevel` output
and no dirty-state fallback for a bare repo -- now probed via
`--is-bare-repository`); the root-deny and dirty-work branches spawned an
identical `git -C <dir> rev-parse --show-toplevel` twice per directory
target (now computed once and threaded through); the incident's own literal
command (`rm -rf ~/.Claude`) was STILL allowed post-fix because the
target-token loop never tilde-expanded `tgt` before its `os.path.exists`
gate, and unconditionally skipped any token containing `$` -- including
`$HOME`-relative targets, the same incident class under a different spelling
(now: `~` expanded via `os.path.expanduser`, and `$HOME`/`${HOME}` narrowly
resolved via `_expand_home_var`; other `$VAR`/glob tokens remain a known,
unchanged bound, see the token-loop comment and finding 8); and the deny
message overclaimed full history loss for a linked `git worktree add` target
(whose store lives in the main repo), now branched by `is_linked_worktree`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.bash_guards import _dc_git_probe, _unborn_scratch_repo
from coordinator_core.bash_guards._command_tokenizer import (
    split_unquoted_newlines as _bt_split_unquoted_newlines,
)
from coordinator_core.bash_guards._dc_git_probe import (
    _new_dirty_paths_memo,
    _rm_peer_claim_of,
)
from coordinator_core.bash_guards._dc_git_shared import _new_git_memo
from coordinator_core.bash_guards._dc_shell_text import (
    _RM_WRAPPER_WORDS,
    _join_backslash_newlines,
    _shell_c_unwrap_payloads,
    _split_segments,
    _strip_heredoc_bodies,
    _strip_q,
    _word_present,
)
from coordinator_core.bash_guards._dialect import (
    Dialect,
    resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._helpers import operator_override_note
from coordinator_core.bash_guards._rewrite_support import _alt_record, _crlf_strip, _offer, _override, _ready
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _PS_REMOVE_VERBS,
    _ps_normalize_verb_token,
    _ps_resolve_head_verb,
)


# ---------------------------------------------------------------------------
# 3. check_destructive_rm -- block-destructive-rm.sh
# F0: per-target repo resolution (git -C "$(dirname TGT_ABS)"), never a
# single dispatcher-level root.
# ---------------------------------------------------------------------------

_RM_CMD_RE = re.compile(
    r"^\s*((sudo|command|time|exec|nice|nohup)\s+|env\s+(\S+=\S*\s+)*|"
    r"[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*(/\S*/)?rm(\s|$)"
)
def _rm_is_rm_segment(seg: str) -> bool:
    prev = None
    s = seg
    while s != prev:
        prev = s
        s = s.lstrip()
        if s.startswith("$("):
            s = s[2:]
        elif s.startswith("("):
            s = s[1:]
        elif s.startswith("{"):
            s = s[1:]
        elif s.startswith("!"):
            s = s[1:]
        elif s.startswith("`"):
            s = s[1:]
        elif s.startswith("\\"):
            s = s[1:]
    probe = s.replace("'", "").replace('"', "")
    # Bundled `-c` (2026-07-29, EM-run second-pass integration, confirmed
    # live): `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact `-c`
    # token requirement missed it -- `sh -ic "rm -rf ..."` / `sh -ic "git
    # clean -fdx"` / `sh -ic "git <revert-verb>..."` all silently sailed
    # through as prose. Widened to `-[a-zA-Z]*c[a-zA-Z]*` (same tolerance as
    # `_BUNDLED_C_FLAG_RE` elsewhere in this file/package).
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        return bool(re.search(r"\brm\b", probe))
    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        # Searched on `probe` as well as `s`: the non-wrapper branch below
        # already reads the quote-stripped form, and reading only `s` here let
        # `sudo 'r''m' -rf <path>` past while the bare `'r''m' -rf <path>` was
        # caught (measured 2026-08-29). Quote-stripping does not disturb the
        # `(){}` and backtick boundaries this pattern anchors on -- it removes
        # only quote characters.
        return bool(
            re.search(r"(^|[\s({`])(/\S*/)?rm([\s)}`]|$)", s)
            or re.search(r"(^|[\s({`])(/\S*/)?rm([\s)}`]|$)", probe)
        )
    return bool(_RM_CMD_RE.search(probe))


def _abs_path(p: str, base: Optional[str] = None) -> str:
    """Resolve p to an absolute path, forward-slash-normalized.

    Windows pathlib.Path.resolve() renders backslashes; the legacy bash
    checks this ports (block-destructive-rm.sh and siblings) run under
    Git-Bash / git own path plumbing, which emits forward slashes
    unconditionally (git itself always prints forward-slash-separated
    paths, even for Windows targets). A raw backslash rendering here is a
    PARITY BREAK against the golden corpus (deny-message paths and the
    git -C / status / stash command lines it prints), not an intentional
    platform difference -- normalize before returning so every caller
    (including the verbatim-quoted git -C "<path>" lines in the deny
    message) matches legacy byte-for-byte.
    """
    try:
        resolved = str(Path(p if os.path.isabs(p) else os.path.join(base or os.getcwd(), p)).resolve())
    except OSError:
        # Resolution failure (e.g. broken symlink loop, permission denied on an
        # ancestor dir) -- fall back to the raw input unresolved/unnormalized;
        # callers treat this the same as any other unresolved-but-present path.
        return p
    return resolved.replace("\\", "/")


_HOME_VAR_RE = re.compile(r"\$(?:HOME\b|\{HOME\})")


def _expand_home_var(tok: str) -> Optional[str]:
    """Expand a ``$HOME``/``${HOME}`` reference in ``tok`` using this
    process's own ``HOME`` env var.

    Narrow and deliberate, not a general shell-variable expander: this hook
    runs out-of-band from the invoking shell and has no access to its
    environment or expansion rules, so expanding an ARBITRARY `$VAR` would
    be guessing, not resolving. `$HOME`/`${HOME}` is the one exception --
    it is well-known, essentially always matches the current process's own
    HOME, and skipping a recursive-rm target solely because it spells the
    home directory as ``$HOME/.claude`` instead of ``~/.claude`` is exactly
    the 2026-07-31 incident class (a target silently bypassing every check
    below because the token filter gave up on it).

    Returns ``None`` (never raises) when the token still contains an
    unresolved ``$`` afterward -- an unknown variable, or ``HOME`` unset --
    so the caller can fall back to skipping the token, same as before this
    fix, rather than acting on a partially-resolved path.
    """
    if "$" not in tok:
        return tok
    home = os.environ.get("HOME")
    if not home:
        return None
    # negative-spec: the replacement MUST stay a callable. Passing `home` as a
    # plain string makes re.sub parse it as a replacement TEMPLATE, in which
    # backslash sequences are escapes -- so a Windows HOME of `C:\Users\<name>`
    # raises `PatternError: bad escape \U`, breaking this function's own
    # "never raises" contract three lines up in the docstring. Observed live on
    # win32: every `rm` naming `$HOME` crashed the destructive-rm guard, which
    # fails CLOSED, so the guard denied the command having never inspected it.
    # A callable replacement is returned verbatim with no escape processing.
    expanded = _HOME_VAR_RE.sub(lambda _m: home, tok)
    if "$" in expanded:
        return None
    return expanded


def _is_same_dir(a: str, b: str) -> bool:
    """True when a and b name the SAME directory on disk.

    Identity is st_dev/st_ino via os.path.samefile, not a string compare,
    because the two are not equivalent on the filesystems this runs on:
      - case-insensitive APFS/HFS+ (macOS default) and NTFS make
        `~/.Claude` and `~/.claude` one directory, while os.path.normcase
        is an identity function off Windows and reports them different;
      - symlinks and bind-style mounts alias two spellings onto one inode.
    Both aliasing modes matter to a caller deciding whether a path about to
    be irreversibly deleted is a repository root.

    Returns False (never raises) when either side is missing or unstattable
    -- callers use this to ARM a deny, so an unresolvable path must not
    assert an identity it cannot prove. NOTE this is not an equivalent
    backstop for every caller: for `check_destructive_rm`'s root-deny branch
    on a CLEAN repo root, the only other leg left once this returns False is
    the dirty-work check a few lines down, which denies nothing when `git
    status --porcelain` is empty -- an unstattable path on an otherwise-clean
    root therefore gets no protection at all here, not a softer one.

    Also note: because `os.path.samefile` follows symlinks (as does the
    `_abs_path` resolution callers run first), a target that is a SYMLINK
    pointing at a repo root is reported identical to the root itself, even
    though `rm -rf <symlink>` (no trailing slash forcing traversal) would
    only unlink the symlink on POSIX, not delete the store. Accepted
    over-blocking (safe direction, not a regression) -- not narrowed here.
    """
    try:
        return os.path.samefile(a, b)
    except (OSError, ValueError):
        return False


def _is_within(path: str, root: str) -> bool:
    """True when `path` is `root` or lies underneath it. Pure string/normcase work --
    no filesystem probe and no spawn, so it is safe to run over every target."""
    p = os.path.normcase(os.path.normpath(path))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r.rstrip(os.sep) + os.sep)


def _attribute_porcelain(
    out: str, root: str, targets: List[str]
) -> Optional[Dict[str, str]]:
    """Split one batched `git status --porcelain` over many pathspecs back into
    per-target status text, or return None if that cannot be done with certainty.

    NEGATIVE SPEC -- THIS FUNCTION MAY NOT GUESS. Its caller uses the result to
    decide whether `rm` is about to destroy a peer's uncommitted work, so an
    unattributable line is not a clean target, it is an unanswered question. Every
    shape below that this cannot parse exactly returns None, and the caller then
    pays the un-batched per-target spawn it was trying to avoid. Batching is an
    optimisation; the deny is not.

    Declines on: a quoted path (`core.quotepath` escapes non-ASCII, and unescaping
    it here would be a second, divergent implementation of git's own quoting), a
    rename/copy arrow (`XY orig -> new`, where BOTH sides matter and the entry is
    not one path), and any line too short to carry the two-column status plus a
    path. Accepts only the plain `XY <path>` form."""
    table: Dict[str, str] = {os.path.normcase(os.path.normpath(t)): "" for t in targets}
    for line in out.splitlines():
        if not line.strip():
            continue
        if len(line) < 4:
            return None
        rel = line[3:]
        if rel.startswith('"') or " -> " in rel:
            return None
        abs_path = os.path.normpath(os.path.join(root, rel))
        owner = None
        for t in targets:
            if _is_within(abs_path, t):
                # Longest match wins: a nested target must not be absorbed by its
                # own ancestor when both were named on the same command line.
                if owner is None or len(t) > len(owner):
                    owner = t
        if owner is None:
            return None
        key = os.path.normcase(os.path.normpath(owner))
        table[key] = (table[key] + "\n" + line) if table[key] else line
    return table


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

    try:
        from coordinator_core.session.touch_record import is_stale, last_seen_hash
    except Exception:
        return None

    candidates = _stale_write_shape_candidates(command, cwd or None)
    if not candidates:
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


def _rm_flush_touch(paths: List[str], session_id: str, root: Optional[str]) -> None:
    """C9 (2026-08-27, docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md):
    best-effort recording of a plain TOUCH event for every ``paths`` entry
    THIS session's own allowed ``rm``/``git rm`` is about to remove.

    Closes the residue filed at ``state/bug-backlog/2026-08-27-git-rm-
    through-bash-leaves-no-touch-clai-d0a400871a9f.yaml``: `rm`/`git rm`
    run through Bash fired no Write/Edit hook, so the deleting session left
    no claim behind and its own legitimate deletion rendered
    `owner:orphan`. A plain TOUCH (never a delete-flavored verb -- none
    exists in this record's schema, see ``touch_record.VERB_TOUCH``/
    ``VERB_RELEASE``) is exactly what closes it: `compute_scope` Step 1
    (`session/scope.py`) already admits a touched-then-deleted path to
    `my_scope` with NO existence check, so a TOUCH recorded here is
    sufficient -- this function does not need to, and must not, invent a
    second verb or touch `compute_scope` itself.

    Deliberately does NOT record a delete for a path this session did NOT
    itself remove -- callers pass only targets THIS `rm`/`git rm` command
    resolved and is about to act on, never a peer's or an unrelated path.
    An unattributable staged deletion (no claim from anyone) is untouched
    by this function on purpose -- that is the signal C7's flip acts on,
    and widening it here would be exactly the forgiveness-rule widening
    this plan's Anti-scope forbids.

    Fails toward recording NOTHING, never toward raising: this sits on the
    `rm` hot path (``check_destructive_rm``'s own budget: one record-
    append, no spawn, no filesystem walk beyond what its caller already
    resolved), and a recording failure here must never turn an otherwise-
    ALLOWED `rm` into a denied one, nor add a spawn/directory-walk of its
    own -- ``session_id``/``root`` are the caller's own already-resolved
    values (``session_id`` is this check's own parameter; ``root`` is
    ``cur_repo``, the one `rev-parse --show-toplevel` this function's
    caller already spawned for its own purposes), not re-derived here.

    The append tail itself lives in ``session.touch_record.append_touch_
    claims``, shared with ``bash_guards.write_claim_record.record_write_
    claims`` (2026-08-30). What stays HERE is the half the two callers
    legitimately differ on: this one is handed ABSOLUTE targets an `rm`
    resolved and relpaths them; the write side receives them relative
    already. That appender carries the same never-raise posture, so the
    outer ``try`` is belt-and-braces for the import, not for the writes.

    Containment against ``root`` is load-bearing here, not tidy -- it is
    the write side's own gate (``write_claim_record.record_write_claims``,
    via that module's local ``_is_within`` twin, kept local rather than
    imported from here on purpose -- see that function's docstring), and
    this deletion-side twin had none until now. ``claim_index.commit_set``'s
    own NEGATIVE SPEC (PM ruling 2026-08-21) covers dirtiness only -- it
    says nothing about containment one way or the other. The containment
    consequence below is real, but it follows from `commit_set` simply
    having no containment check at all, not from a documented ruling there:
    an out-of-repo claim that slips past this function reaches
    ``safe_commit_offer``'s commit pathspec undetected regardless.
    That is the exact failure mode ``write_claim_record._SED_SCRIPT_RE``'s
    own note already measured: `git add -- <junk>` exits 128 and the real
    change is NOT committed, so one bad claim destroys the session's whole
    commit. Sighted for real in cross-repo memo ``2026-08-30-coordinator-content-repo-em-
    safe-commit-drops-subagent-written-paths.md``, which reported a bare
    ``holder.json`` in ``reconciliation.claimed_absent``, sourced from a
    pytest ``tmp_path`` outside the repo -- benign there only because an
    ABSENT out-of-repo path routes to ``deleted_paths`` and commits
    nothing; a PRESENT one is not benign. Skip before relpathing, not
    after: a skipped target is simply never handed to ``os.path.relpath``
    or the appender at all, which is also why the bare
    ``except ValueError: continue`` that used to wrap the relpath call was
    deleted rather than kept -- this gate rejects every input that call
    could raise on (cross-drive on Windows; POSIX's relpath has no drive
    concept to raise over), so that branch was unreachable dead code, not
    defense-in-depth.
    """
    if not paths or not session_id or not root:
        return
    try:
        from coordinator_core.session.touch_record import KIND_WRITE, append_touch_claims

        rels = []
        for tgt_abs in paths:
            if not _is_within(tgt_abs, root):
                # The `_is_within` gate above already
                # rejects any target `os.path.relpath` would raise `ValueError`
                # on (cross-drive on Windows; POSIX has no drive concept for
                # relpath to raise over), so a bare `except ValueError: continue`
                # around the relpath call below is unreachable from this call
                # site and was deleted rather than kept as belt-and-braces.
                continue
            rels.append(os.path.relpath(tgt_abs, root).replace(os.sep, "/"))
        # KIND_WRITE: a deletion is the most mutating thing on this record.
        append_touch_claims(rels, session_id, root, kind=KIND_WRITE)
    except Exception:
        return


#: A PowerShell parameter binds on any unambiguous prefix of its name, so
#: `-Recurse` also arrives as `-Recurs`, `-rec`, or `-r`. Among `Remove-Item`'s
#: parameters only `Recurse` begins with `r`, so any `-r...` prefix of it is
#: unambiguous and nothing else can claim those spellings.
_PS_RECURSE_NAME = "recurse"


def _ps_recurse_flag(tokens):
    """The token that asked for recursion, or None if none did.

    Returned rather than a bool because the refusal names it: a caller told
    their delete was treated as recursive can see WHICH word did it, and a
    misparse then reads as a misparse instead of as policy.
    """
    for tok in tokens:
        if not tok.startswith("-") or tok.startswith("--"):
            continue
        name = tok[1:].split(":", 1)[0].lower()
        if name and _PS_RECURSE_NAME.startswith(name):
            return tok
    return None


def check_destructive_rm(
    cmd: str,
    session_id: str = "",
    payload: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """F0-b (2026-08-05, spawn-count defect): the per-TARGET loop below
    resolves `git -C <parent-dir> rev-parse --show-toplevel` (scratch-
    allowlist probe) and, for a file target, `git -C <parent-dir> rev-parse
    --show-toplevel` again (dirty-work root probe) once PER TARGET, with
    byte-identical argv whenever two targets share a parent directory --
    the overwhelmingly common case for a plain `rm a.py b.py c.py`. ~1 extra
    spawn per target, linear (measured: 1 target -> 4 spawns, 3 targets ->
    5, 10 targets -> 12, 30 targets -> 32; 15 targets across 3 different
    dirs -> 17, confirming the memo below still resolves each distinct dir
    independently). Fixed via `_new_git_memo`'s per-call, exact-(cwd, args)-
    keyed memo -- see that factory's docstring for why this is sound and
    why it is deliberately NOT module-level.
    """
    if not cmd:
        return None
    cmd = _crlf_strip(cmd)
    cmd = _join_backslash_newlines(cmd)
    cmd = _strip_heredoc_bodies(cmd)

    # C3 (pln-the-destructive-core-learns-the-she): the pre-existing
    # `\brm\b` gate is BASH-shaped -- none of PowerShell's `Remove-Item`
    # spelling or its `ri`/`rd`/`del`/`erase` aliases contains the substring
    # `rm`, so a PowerShell-native invocation returned None here before ever
    # reaching a target check. Widen the gate (cheap pre-filter only, never
    # the real detection -- that is `_PS_REMOVE_VERBS` membership below).
    #
    # Deliberately NOT gated on `dialect_from_tool_name(payload["tool_name"])
    # is Dialect.POWERSHELL`. Per this plan's own "Row 2 is the load-bearing
    # row": coordinator-content-repo's `_rearm_command_tool_name` rewrites a genuine
    # `PowerShell` tool call to `tool_name: "Bash"` BEFORE this payload ever
    # reaches the dispatcher, so a real `Remove-Item -Recurse -Force .git`
    # issued from PowerShell arrives here labeled `"Bash"` -- the exact shape
    # AC5's oracle pins (`Remove-Item ...` must deny under BOTH tool_name
    # values, not only under a `tool_name` that happens to say
    # `"PowerShell"`). Trusting the declared `tool_name` to gate PowerShell
    # vocabulary detection would silently keep this guard blind on that
    # relabeled row -- the one case the plan's § Problem measured as the
    # load-bearing gap. The PowerShell verb scan below therefore always
    # runs, independent of `tool_name`; the risk this widens is a literal
    # `Remove-Item`/`ri`/`rd`/`del`/`erase` WORD appearing in an otherwise
    # unrelated bash command, which is not a realistic false-positive shape
    # (none of those words collides with ordinary bash vocabulary the way a
    # narrower alias might).
    # Scanned on the raw command AND on a quote-stripped copy. The shell
    # resolves `'r''m'` to `rm`, but `\brm\b` matched neither the raw text nor
    # anything downstream, so this fast path returned None and the whole guard
    # never ran: measured 2026-08-29, `rm -rf <path>` denied while
    # `'r''m' -rf <path>` was ALLOWED against the same path with the same
    # uncommitted work under it. Same shape as the doctrine-surface guard's
    # prefilter bypass closed the same day -- a correct analyzer gated behind a
    # raw-text probe that shell quoting walks straight past.
    #
    # WIDENING ONLY, and this gate decides solely whether to LOOK: every
    # downstream check still requires a real rm segment, a target that exists,
    # and uncommitted work under it, so a spurious word here cannot produce a
    # deny on its own. `_rm_is_rm_segment` already reads segments this way; the
    # gate in front of it did not.
    _has_bash_rm_word = _word_present(r"\brm\b", cmd)
    _has_ps_remove_word = _word_present(r"(?i)\b(remove-item|ri|rd|del|erase)\b", cmd)
    if not _has_bash_rm_word and not _has_ps_remove_word:
        return None

    rm_override = _override("COORDINATOR_ALLOW_RM", payload=payload)
    # A relative target is relative to the session's cwd: on the warm engine the process
    # cwd is wherever the server booted, and an unresolved target is silently skipped.
    _rm_cwd = (payload or {}).get("cwd") or os.getcwd()

    # Per-call memo, keyed on the exact (cwd, args) pair actually run -- see
    # `_new_git_memo` docstring for why this is sound and why it is
    # deliberately NOT module-level.
    _memo_run_git = _new_git_memo()

    # `_rm_peer_claim_of`'s uncommitted-delta gate, shared across every
    # target this call's loop below checks -- see `_new_dirty_paths_memo`'s
    # own docstring for why this MUST be built once here and threaded
    # through, rather than left for that function's own one-shot fallback
    # (which would spawn `git status` once per rm target).
    _dirty_paths_lookup = _new_dirty_paths_memo()

    # Per-call, per-ROOT batched `git status --porcelain`. One spawn per distinct
    # repo root instead of one per TARGET (`rm a.py b.py c.py` was three).
    #
    # WHY LAZY, AND WHY NOT A PRE-PASS. Resolving every target's root up front to
    # group them would spawn for targets the loop below `continue`s past long
    # before it ever needs a status -- a scratch-allowlisted path, a nonexistent
    # path, a claims-dir path. That trades an N-spawn tail for an N-spawn head.
    # This fills on FIRST need for a root and only then, so every early exit above
    # still costs nothing.
    #
    # WHY IT FALLS BACK RATHER THAN GUESSING. This guard's deny leg is the thing
    # standing between a peer's uncommitted work and `rm -rf`. Porcelain paths are
    # root-relative, may be renames (`XY orig -> new`), and are quoted when
    # `core.quotepath` fires on non-ASCII. If a line cannot be attributed to a
    # target with certainty, this does NOT assume clean -- it returns None and the
    # caller re-issues that target's own un-batched status call. A missed spawn is
    # a cost; a missed deny is a peer's lost work.
    _status_by_root: Dict[str, Optional[Dict[str, str]]] = {}

    def _batched_status_for(root: str, tgt_abs: str, all_targets: List[str]) -> Optional[str]:
        if root not in _status_by_root:
            under = [t for t in all_targets if _is_within(t, root)]
            if not under:
                _status_by_root[root] = None
            else:
                rc_b, out_b = _dc_git_probe._run_git(
                    ["-C", root, "--no-optional-locks", "status", "--porcelain", "--"] + under
                )
                _status_by_root[root] = (
                    _attribute_porcelain(out_b, root, under) if rc_b == 0 else None
                )
        table = _status_by_root[root]
        if table is None:
            return None
        return table.get(os.path.normcase(tgt_abs), "")

    cur_repo = None
    rc, out = _dc_git_probe._run_git(["rev-parse", "--show-toplevel"])
    if rc == 0:
        cur_repo = out.strip()

    # C9 (2026-08-27, docs/plans/2026-08-27-a-pathspec-is-not-a-scope.md):
    # every real target this call ends up letting through -- bash `rm` or
    # `git rm` alike -- is collected here (as `tgt_abs`) and flushed as a
    # TOUCH record ONLY at this function's final `return None`, never
    # inline where a target is found. That ordering is load-bearing: any
    # earlier `return _deny(...)` anywhere below means the whole command
    # never executes, so nothing in this list may have been recorded yet.
    # See `_rm_flush_touch`'s own docstring for why this is a plain TOUCH
    # (not a delete-flavored verb) and what it closes.
    _pending_rm_touch_paths: List[str] = []

    # A bare newline separates commands; unsplit, `rm a` + newline + `cd <repo>` reads
    # `cd` and `<repo>` as rm targets and denies the repo root.
    _rm_cmd = _bt_split_unquoted_newlines(cmd)
    _rm_segments = list(_split_segments(_rm_cmd))
    #: Segment string -> the flag token that declared recursion, or None when
    #: the segment was parsed and declared none. Only PowerShell-synthesized
    #: segments get an entry; a bash segment is absent and falls through to the
    #: free-text scan below, which is correct for bash and only for bash.
    _parsed_recursion = {}
    if _has_ps_remove_word:
        # AC3: alias/flag resolution is TABLE-DRIVEN AND SHARED, reusing
        # `block_subagent_destructive_action`'s own `_PS_REMOVE_VERBS`/
        # `_ps_resolve_head_verb`/`_ps_normalize_verb_token` (which already
        # unwind the `&('Remove-Item') ...` / `&(Get-Command Remove-Item)
        # ...` call-operator forms) rather than a second, per-check alias
        # list. `resolve_segments_for_dialect` itself already expands
        # `Start-Process`/`saps`/`start` invocations before segmentation
        # (`_dialect.expand_start_process_invocations`) and its
        # tree-sitter-pwsh parser natively resolves backtick escapes and
        # `@'...'@`/`@"..."@` here-strings at the token level -- the
        # anti-bypass surface this chunk's brief names is inherited from
        # that seam, not reinvented here.
        #
        # Each recognized invocation is rewritten to an EQUIVALENT
        # bash-shaped `rm -rf <remaining argv>` segment string and appended
        # to the same segment list the loop below already walks -- this
        # reuses the ~300-line target-resolution/deny ladder byte-for-byte
        # for both dialects instead of forking a parallel PowerShell copy of
        # it. `-Recurse`/`-Force` (any order, abbreviated `-r`/`-fo`) and a
        # named `-Path`/`-LiteralPath` flag all survive into `rest` and are
        # filtered by the SAME `startswith("-")` flag test the bash leg
        # already applies when it walks `after.split()` below -- a
        # positional target (`Remove-Item .git -Recurse -Force`) or a
        # named-flag target (`Remove-Item -Path .git`) both leave exactly
        # the target token as the sole non-flag operand. Gating is
        # deliberately NOT conditioned on `-Recurse`/`-Force` presence: the
        # bash leg denies a `.git`/repo-root target regardless of `-r`,
        # since the irreversibility is a property of the TARGET, not of
        # whether the caller also passed a flag -- matching that posture is
        # the safe direction, not a widening of it.
        _ps_segments = resolve_segments_for_dialect(
            _rm_cmd, Dialect.POWERSHELL, guard_name="destructive-rm"
        )
        if _ps_segments:
            for _ps_tokens, _ps_pipe_before in _ps_segments:
                if not _ps_tokens:
                    continue
                _ps_clean = [_ps_normalize_verb_token(t) for t in _ps_tokens]
                _ps_verb, _ps_rest = _ps_resolve_head_verb(_ps_clean)
                if _ps_verb not in _PS_REMOVE_VERBS:
                    continue
                # `-rf` is NOT stamped on unconditionally any more. It was,
                # and any PowerShell flag CONTAINING an `r` then re-matched the
                # bash short-flag scan below -- `-Force` most of all. Measured
                # 2026-09-11 across three repos: `Remove-Item -Force <file>` on
                # a single tracked file was refused as a recursive delete of
                # uncommitted work, and the refusal named neither the flag it
                # had read nor the dialect it had read it in, so three sessions
                # in turn read a parse bug as a policy they had to argue with.
                #
                # The target-irreversibility posture the old comment defended
                # is untouched: a `.git`/repo-root target is denied on
                # `tgt_is_dir` above, which never consulted this flag.
                _ps_recurse = _ps_recurse_flag(_ps_rest)
                _ps_seg = ("rm -rf " if _ps_recurse else "rm ") + " ".join(_ps_rest)
                _rm_segments.append(_ps_seg)
                _parsed_recursion[_ps_seg] = _ps_recurse
        # `_ps_segments is None` (unparseable PowerShell) -- SILENT already
        # recorded by `resolve_segments_for_dialect`; no synthetic segment
        # is added, preserving today's fail-open posture (AC4) rather than
        # falling through to the bash-shaped free-text scan below, which
        # would misattribute a PowerShell parse failure as a bash
        # unparseable-command fail-close.

    for seg in _rm_segments:
        if not seg.strip():
            continue

        # `git ... rm` (staged removal, git-recoverable) never matches
        # `_rm_is_rm_segment` below -- that gate requires the segment's OWN
        # head word (past wrappers/env) to be `rm`, and `git` is not in
        # `_RM_WRAPPER_WORDS` -- so this detection must run BEFORE that
        # gate, not after it (a `git rm` segment would otherwise `continue`
        # past this whole block, silently, on the very next line). The deny
        # ladder below still never runs for it (staged removal is
        # git-recoverable, unlike a bare `rm`), but (C9) it still needs its
        # own TOUCH: `git rm` (without `--cached`) deletes the working-tree
        # file too, not just the index entry, and previously left no touch
        # claim behind at all.
        if re.search(
            r"\bgit(\s+(-C\s+\S+|-c\s+\S+|--(git-dir|work-tree|namespace)(=\S+|\s+\S+)|"
            r"--exec-path(=\S+|\s+\S+)?|-p|--paginate|--no-pager|--bare|"
            r"--no-replace-objects|--literal-pathspecs|--glob-pathspecs|"
            r"--noglob-pathspecs|--icase-pathspecs|--no-optional-locks))*\s+rm\b",
            seg,
        ):
            _gr_after = re.sub(r".*\bgit\b.*?\brm(\s|$)", " ", seg, count=1)
            if "--cached" not in _gr_after.split():
                for _gr_tok in _gr_after.split():
                    if _gr_tok == "--" or _gr_tok.startswith("-"):
                        continue
                    if "$" in _gr_tok or any(c in _gr_tok for c in ("*", "?", "[")):
                        continue
                    _gr_t = _strip_q(_gr_tok)
                    if _gr_t.endswith(")"):
                        _gr_t = _gr_t[:-1]
                    if _gr_t.endswith("}"):
                        _gr_t = _gr_t[:-1]
                    _gr_resolved = os.path.join(_rm_cwd, os.path.expanduser(_gr_t)) if _gr_t else ""
                    if not _gr_resolved or not os.path.exists(_gr_resolved):
                        continue
                    _pending_rm_touch_paths.append(_abs_path(_gr_resolved))
            continue

        if not _rm_is_rm_segment(seg):
            continue
        # Quote-stripped, for the SAME reason `_rm_is_rm_segment` builds its
        # own `probe` that way: the shell resolves `'r''m'` to `rm`, so the
        # segment reaches here correctly identified as an rm -- but this
        # verb-strip needed a LITERAL `rm` token, found none, and left `after`
        # as the whole segment. Measured 2026-08-29: `rm -rf <path>` denied
        # while `'r''m' -rf <path>` was ALLOWED on the same path with the same
        # uncommitted work under it. The two must read the segment the same
        # way or the detector and the extractor disagree about what they are
        # looking at. Whitespace-splitting a quoted path was already lossy
        # here (`rm -rf 'my dir'` split to `'my` + `dir'` before this), so
        # this changes no case that previously resolved.
        seg_unquoted = seg.replace("'", "").replace('"', "")
        after = re.sub(r".*(^|\s)rm(\s|$)", " ", seg_unquoted, count=1)
        if seg in _parsed_recursion:
            recursion_flag = _parsed_recursion[seg]
        else:
            _rec_m = re.search(r"(^|\s)(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(\s|$)", after)
            recursion_flag = _rec_m.group(2) if _rec_m else None
        recursive = recursion_flag is not None

        if recursive and re.search(r"\$\(|`", after) and not rm_override:
            return _alt_record(
                "deny", "destructive-rm",
                "BLOCKED: 'rm' has a recursive flag and a subshell-resolved "
                "target ($(...) or backticks) — cannot verify what it would "
                "delete. Resolve it to a literal path first.",
                (_offer("git status --porcelain -- <resolved-path>", "the resolved path"),),
                report="the rm you need", authorize="a recursive rm of an unverifiable target",
                em_text=(
                    "BLOCKED: 'rm' has a recursive flag and a subshell-resolved "
                    "target ($(...) or backticks) — cannot verify what it would "
                    "delete.\n\n"
                    "Resolve it to a literal path first; use instead:\n"
                    "  git status --porcelain -- <resolved-path>"
                ),
            )

        targets: List[Tuple[str, str]] = []
        for tok in after.split():
            if tok == "--" or tok.startswith("-"):
                continue
            raw_tok = tok
            if "$" in tok:
                expanded = _expand_home_var(tok)
                if expanded is None or any(c in expanded for c in ("*", "?", "[")):
                    # 2026-07-31: a bare `$`/`${...}` reference the hook
                    # cannot resolve in-process still gets skipped here --
                    # this is finding 8, an inherited/known bound (no shell
                    # environment access, no expansion rules) unchanged by
                    # this fix. Only the narrow, well-known $HOME/${HOME}
                    # case below is resolved, because that is exactly the
                    # incident class (a recursive rm on a $HOME-relative
                    # target silently bypassing every check here).
                    continue
                tok = expanded
            elif any(c in tok for c in ("*", "?", "[")):
                continue
            t = _strip_q(tok)
            if t.endswith(")"):
                t = t[:-1]
            if t.endswith("}"):
                t = t[:-1]
            # `t_raw` is the AS-TYPED
            # token (captured BEFORE any `$HOME`/`${HOME}` expansion above),
            # kept separate from `t` (which is already expanded for the
            # $HOME leg). For a plain or `~`-spelled token the two are
            # identical (expansion never touched `tok` in that case). Both
            # are threaded through so deny MESSAGES can show the raw, as-
            # typed spelling for EVERY target shape, not just tilde-spelled
            # ones.
            t_raw = _strip_q(raw_tok)
            if t_raw.endswith(")"):
                t_raw = t_raw[:-1]
            if t_raw.endswith("}"):
                t_raw = t_raw[:-1]
            targets.append((t_raw, t))

        for tgt, tgt_expanded in targets:
            # 2026-07-31: `os.path.exists`/`os.path.isdir` do NOT expand a
            # leading `~` themselves -- `rm -rf ~/.Claude` was tokenized
            # correctly but then silently `continue`d here because
            # `os.path.exists("~/.claude")` is False, before ANY guard leg
            # below got a chance to run. Expand a leading `~` the same way
            # guard_offer_git_c.py:_offer_normalize_path already does, and
            # use the expanded form for every filesystem probe below; `tgt`
            # itself stays the raw, as-typed string so deny MESSAGES still
            # show the user what they wrote. `tgt_expanded` is `tgt` for a
            # plain or `~`-spelled token, and the already-`$HOME`-expanded
            # path for a `$HOME`/`${HOME}`-spelled token (Finding 1 fix --
            # this used to overwrite `tgt` itself for that spelling, so
            # `tgt` was already absolute by message-build time despite the
            # comment above claiming otherwise for every spelling).
            tgt_resolved = os.path.join(_rm_cwd, os.path.expanduser(tgt) if tgt.startswith("~") else tgt_expanded)
            if not tgt or not os.path.exists(tgt_resolved):
                continue
            tgt_abs = _abs_path(tgt_resolved)
            # C9: collected now, flushed only at this function's final
            # `return None` -- see the list's own comment at this
            # function's top for why an early `return _deny(...)` below
            # must never let this target reach the touch record.
            _pending_rm_touch_paths.append(tgt_abs)

            norm = tgt_abs.replace("\\", "/")
            if (
                "/coordinator-sessions/" in norm and "-claims/" in norm.split("/coordinator-sessions/", 1)[-1]
            ) or re.search(r"/coordinator-sessions/[^/]*-claims$", norm):
                if rm_override:
                    continue
                return _alt_record(
                    "deny", "destructive-rm",
                    "BLOCKED: '%s' is a coordinator claim lock dir — "
                    "regenerable session scratch, NOT git history. Do not rm "
                    "it by hand: the manual-rm path is how a live peer gets "
                    "stomped (CLAIM-CLEAR-LIVENESS in coordinator-tripwires.md)." % tgt,
                    (_offer(
                        "cs_clear_claim_if_dead <class> <basename> (lib/coordinator-session.sh; "
                        "refuses to clear a LIVE peer's claim)", "the class and basename"),),
                    report="the claim dir you need cleared", authorize="removing the claim lock dir",
                    em_text=(
                        "BLOCKED: '%s' is a coordinator claim lock dir — "
                        "regenerable session scratch, NOT git history. Do not rm "
                        "it by hand.\n\n"
                        "Did you mean: cs_clear_claim_if_dead <class> <basename>\n\n"
                        "  cs_clear_claim_if_dead calls cs_claim_holder_live first "
                        "and refuses to clear a LIVE peer's claim. The manual-rm "
                        "path is exactly how a live peer gets stomped (see "
                        "CLAIM-CLEAR-LIVENESS in coordinator-tripwires.md).\n\n"
                        "  cs_clear_claim_if_dead is the canonical safe path for "
                        "clearing a stale claim.\n"
                        "  (Available in lib/coordinator-session.sh — source it "
                        "first if not already loaded.)" % tgt
                    ),
                )

            # A `*.lock` file under the git store holds no committed data --
            # git writes a new index to `index.lock` and renames it onto
            # `index`; unlink the lock mid-flight and that rename FAILS and
            # the git command errors out loudly, leaving the on-disk index
            # untouched and still consistent. A second git that acquires a
            # fresh lock reads that same consistent index. The realistic
            # worst case is a failed git command, not a damaged repository
            # -- and the index is regenerable from HEAD plus the working
            # tree regardless. Hard blocks in this guard are reserved for
            # irreversible harm; this is not irreversible, so the prior
            # deny was a wall around the wrong path rather than a cheaper
            # right one -- an EM must be able to clear a git lock without
            # human escalation. See cross-repo/inbox/
            # 2026-08-12-example-retrieval-repo-em-git-index-lock-reaper.md. This leg
            # must fire FIRST and narrowly (it does not replace the branch
            # below, which still denies every non-lock git-store path).
            #
            # NEGATIVE SPEC -- do NOT turn this into an advisory that names
            # the reaper, however helpful that reads. `dispatch.py`'s guard
            # loop returns on the FIRST non-None envelope, so returning
            # anything here short-circuits every guard registered after this
            # one for the same command. A silent `continue` is the required
            # shape; the allow IS the message.
            is_git_store_target = norm.endswith("/.git") or "/.git/" in norm or os.path.basename(tgt_abs) == ".git"
            # The
            # allow is scoped to a lock FILE only; a `.lock`-suffixed
            # directory has no place in the rename-onto-index safety
            # argument above and must fall through to the general git-store
            # deny below.
            if is_git_store_target and os.path.basename(tgt_abs).endswith(".lock") and os.path.isfile(tgt_abs):
                continue

            # The write-confinement bump's own clear marker
            # (`allow-xrepo-write-<session-id>`, `_write_bump_marker.py`) is
            # a zero-byte sentinel directly under the gitdir, not objects,
            # refs, or logs -- removing it only re-arms the bump for the
            # rest of the session, per that module's own docstring. Denying
            # its removal leaves a session that cleared the bump for one
            # deliberate operation with no way to put it back
            # (state/bug-backlog/2026-09-02-git-store-rm-guard-blocks-
            # removing-the-w-d2194cff6d30.yaml). Same shape as the `.lock`
            # allow above: scoped to a FILE whose basename matches, so a
            # directory merely named with that prefix still falls through
            # to the general git-store deny below.
            if (
                is_git_store_target
                and os.path.basename(tgt_abs).startswith("allow-xrepo-write-")
                and os.path.isfile(tgt_abs)
            ):
                continue

            # The engine's own `claude-klabauter-*.json` caches sit directly under the
            # gitdir, keyed on (size, mtime_ns) and rebuilt on a miss -- not
            # objects, refs, or logs. A file only, directly under `.git`, so a
            # nested path or a directory of that name still meets the deny.
            if (
                is_git_store_target
                and os.path.basename(os.path.dirname(tgt_abs)) == ".git"
                and os.path.basename(tgt_abs).startswith("claude-klabauter-")
                and os.path.basename(tgt_abs).endswith(".json")
                and os.path.isfile(tgt_abs)
            ):
                continue

            # A target whose basename is
            # literally `.git` is caught HERE and denied with the generic
            # git-store message below, before it ever reaches the bare-repo
            # probe further down (`tgt_is_bare` / `--is-bare-repository`).
            # A `.git`-named bare repo is therefore denied via a different,
            # less specific message than the root-deny branch's bare-repo
            # wording -- the bare-repo leg is not exercised for every
            # bare-repo shape.
            if is_git_store_target:
                if rm_override:
                    continue
                return _alt_record(
                    "deny", "destructive-rm",
                    "BLOCKED: 'rm' would delete part of the git store at "
                    "'%s'. This corrupts/destroys repository history "
                    "irreversibly — no checkout, reflog, or stash recovers it." % tgt, (),
                    report="the git-store removal you need", authorize="deleting part of the git store",
                    em_text=(
                        "BLOCKED: 'rm' would delete part of the git store at "
                        "'%s'. This corrupts/destroys repository history "
                        "irreversibly — no checkout, reflog, or stash recovers it." % tgt
                    ),
                )

            # A repo ROOT is the same irreversibility class as the `.git`
            # target above -- deleting it takes the store with it -- but it
            # reaches this code as an ordinary directory, so the check above
            # never sees it. It is denied on IDENTITY (the target resolves to
            # a toplevel, OR the target IS a bare repo -- a bare repo has no
            # separate worktree to distinguish it from its own store), not on
            # dirty-state: by the time the store is gone there is no status
            # to consult, and every clean/dirty distinction downstream is
            # moot.
            #
            # Resolution is by st_dev/st_ino (`_is_same_dir`), never by string
            # compare, because a case-insensitive filesystem makes
            # `~/.Claude` and `~/.claude` the SAME directory while
            # `os.path.normcase` (a no-op off Windows) reports them distinct.
            # That exact aliasing deleted a live `~/.claude` on 2026-07-31
            # via a probe-cleanup `rm -rf` whose author believed it named a
            # separate case-variant directory.
            #
            # `tgt_top`/`tgt_is_bare` are computed ONCE here (a single
            # `rev-parse --show-toplevel` plus, only when that comes back
            # empty, a single `--is-bare-repository` probe) and reused by the
            # dirty-work branch below instead of re-invoking `_run_git` with
            # the same `-C <dir> rev-parse --show-toplevel` args a second
            # time on this hot path.
            tgt_is_dir = os.path.isdir(tgt_resolved)
            tgt_top = ""
            tgt_is_bare = False
            if tgt_is_dir:
                # F0-b: memoized -- safe to reuse if the same dir target
                # repeats (e.g. `rm -rf dirA dirA`); see `_new_git_memo`.
                rc_top, out_top = _memo_run_git(["-C", tgt_abs, "rev-parse", "--show-toplevel"])
                tgt_top = out_top.strip() if rc_top == 0 else ""
                if not tgt_top:
                    # `rev-parse --show-toplevel` fails inside a BARE repo
                    # (no working tree to name) -- without this probe a
                    # bare repo target would fall straight through both this
                    # branch and the dirty-work branch below (which also has
                    # no dirty-state fallback for a bare repo) undenied.
                    #
                    # `--show-toplevel`
                    # also fails for the OVERWHELMINGLY common case of "a
                    # plain, non-repo scratch directory" -- not just for a
                    # bare repo. Spawning a second git process to rule out
                    # "is this a bare repo" on EVERY such target doubled
                    # subprocess cost on the single most frequent shape of
                    # this check. A real (bare or non-bare) git directory
                    # always has a `HEAD` file and an `objects/` subdir at
                    # its root -- cheap `os.path` checks that can rule out
                    # the common non-repo case without a spawn. This can
                    # only narrow when the spawn happens, never skip a
                    # genuine bare repo: a bare repo's toplevel dir IS its
                    # git dir, so it always has both.
                    looks_like_git_dir = os.path.isfile(
                        os.path.join(tgt_abs, "HEAD")
                    ) and os.path.isdir(os.path.join(tgt_abs, "objects"))
                    if looks_like_git_dir:
                        # F0-b: memoized, same rationale as `tgt_top` above.
                        rc_bare, out_bare = _memo_run_git(
                            ["-C", tgt_abs, "rev-parse", "--is-bare-repository"]
                        )
                        tgt_is_bare = rc_bare == 0 and out_bare.strip() == "true"
                if tgt_is_bare or (tgt_top and _is_same_dir(tgt_top, tgt_abs)):
                    if rm_override or _unborn_scratch_repo.is_unborn_scratch_repo(tgt_abs):
                        continue
                    clone_failed = (
                        None
                        if tgt_is_bare
                        else _unborn_scratch_repo.scratch_clone_failures(tgt_abs, _memo_run_git)
                    )
                    if clone_failed == []:
                        continue
                    store_desc = tgt_top if tgt_top else tgt_abs
                    # A LINKED `git worktree add` worktree's actual .git
                    # store lives in the MAIN repo (`.git/worktrees/<name>`)
                    # -- deleting the worktree directory loses its local
                    # uncommitted changes, not the repo's full history.
                    # Branch the claim so it's true for both target shapes.
                    is_linked_worktree = False
                    if tgt_top and not tgt_is_bare:
                        # F0-b: memoized, same rationale as `tgt_top` above.
                        rc_gd, out_gd = _memo_run_git(["-C", tgt_abs, "rev-parse", "--git-dir"])
                        if rc_gd == 0 and "/worktrees/" in out_gd.strip().replace("\\", "/"):
                            is_linked_worktree = True
                    if is_linked_worktree:
                        loss_desc = (
                            "removes this worktree's local uncommitted "
                            "changes and its link to the repository's git "
                            "metadata -- commits, branches, stashes, and "
                            "reflog entries live in the MAIN repo's .git "
                            "store, not here, but this worktree's own "
                            "uncommitted work"
                        )
                    else:
                        loss_desc = (
                            "deletes the .git store along with the "
                            "worktree — every commit, branch, stash, and "
                            "reflog entry that exists only here"
                        )
                    failed_note = (
                        "Scratch clone not recoverable by re-clone, failed: %s.\n\n"
                        % ", ".join(clone_failed)
                        if clone_failed
                        else ""
                    )
                    return _alt_record(
                        "deny", "destructive-rm",
                        "BLOCKED: '%s' is the ROOT of a git repository (%s). "
                        "'rm' on it %s "
                        "is gone irreversibly, "
                        "plus all gitignored content (settings, caches, "
                        "credentials, session state) that no commit ever "
                        "held. %s"
                        "This deny is on identity, not on dirty-state: a "
                        "clean worktree does NOT make deleting the store "
                        "safe. Beware case-insensitive filesystems: a path that "
                        "LOOKS like a case-variant probe directory can be the "
                        "real one."
                        % (tgt, store_desc, loss_desc, failed_note),
                        (
                            _offer(
                                'git -C "%s" log --branches --not --remotes --oneline (empty == nothing unpushed); '
                                'git -C "%s" stash list (stashes are never pushed); '
                                'git -C "%s" status --porcelain --ignored (untracked + gitignored content)'
                                % (tgt_abs, tgt_abs, tgt_abs),
                                "nothing; run to confirm a fully pushed CLONE before removing it",
                            ),
                            _ready('python3 -c "import os;print(os.stat(%r).st_ino)"' % tgt_abs),
                        ),
                        report="the repository removal you need", authorize="deleting a repository root",
                        em_text=(
                            "BLOCKED: '%s' is the ROOT of a git repository (%s). "
                            "'rm' on it %s "
                            "is gone irreversibly, "
                            "plus all gitignored content (settings, caches, "
                            "credentials, session state) that no commit ever "
                            "held.\n\n"
                            "%s"
                            "This deny is on identity, not on dirty-state: a "
                            "clean worktree does NOT make deleting the store "
                            "safe.\n\n"
                            "If you are removing a CLONE that is fully pushed, "
                            "confirm every ref is on a remote first:\n"
                            '  git -C "%s" log --branches --not --remotes '
                            "--oneline    # empty == nothing unpushed\n"
                            '  git -C "%s" stash list                          '
                            "  # stashes are never pushed\n"
                            '  git -C "%s" status --porcelain --ignored        '
                            "  # untracked + gitignored content\n\n"
                            "Beware case-insensitive filesystems: a path that "
                            "LOOKS like a case-variant probe directory can be the "
                            "real one. Verify with:\n"
                            '  python3 -c "import os;print(os.stat(%r).st_ino)"'
                            % (tgt, store_desc, loss_desc, failed_note, tgt_abs, tgt_abs, tgt_abs, tgt_abs)
                        ),
                    )

            # Scratch allowlist.
            scratch_bn = os.path.basename(tgt_abs)
            scratch_par_abs = os.path.dirname(tgt_abs)
            # F0-b: memoized -- `scratch_par_abs` is the TARGET's parent dir,
            # byte-identical argv across every target sharing that parent
            # (the common `rm a.py b.py c.py` shape: 1 extra spawn per
            # target, linear, before this fix). Also collapses with the
            # file-target `probe_dir` call further below, which computes the
            # exact same `-C <parent-dir> rev-parse --show-toplevel` for a
            # non-directory target -- a pre-existing double-spawn on THIS
            # target alone that the shared memo now also absorbs for free.
            rc_s, out_s = _memo_run_git(["-C", scratch_par_abs, "rev-parse", "--show-toplevel"])
            scratch_repo = out_s.strip() if rc_s == 0 else None
            if (
                scratch_repo
                and cur_repo
                and os.path.normcase(scratch_repo) == os.path.normcase(cur_repo)
                and not os.path.islink(tgt_resolved.rstrip("/"))
                and os.path.isdir(os.path.join(scratch_repo, "tasks"))
                and os.path.normcase(scratch_par_abs) == os.path.normcase(os.path.join(scratch_repo, "tasks"))
                and re.match(r"^[a-zA-Z0-9_-]+-scratch$", scratch_bn)
            ):
                continue

            if tgt_is_dir or recursive:
                # Resolve the owning repo from the TARGET when the target is
                # itself a directory, and only fall back to its parent for a
                # file target (where `-C <file>` is not a valid git cwd).
                #
                # Asking the parent unconditionally -- as this did until
                # 2026-07-31 -- silently skipped (`continue`) every target
                # whose parent happened to sit outside a repo, which is the
                # normal shape of a checkout: `~/.claude`, `~/X/coordinator-content-repo`,
                # and `~/X/claude-klabauter` were all ALLOWED because `~` and
                # `~/X` are not repos, while their SUBdirectories were
                # correctly denied. The dirty-work protection existed and
                # simply never ran on the paths with the most to lose.
                #
                # For a directory target, `tgt_top` was already resolved by
                # the root-deny branch above via this exact `-C <dir>
                # rev-parse --show-toplevel` call -- reuse it rather than
                # spawning git a second time with identical args. A bare
                # repo target never reaches here (denied above), so no
                # bare-repo handling is needed on this leg.
                if tgt_is_dir:
                    root = tgt_top
                else:
                    probe_dir = os.path.dirname(tgt_abs)
                    # F0-b: memoized -- `probe_dir` is `dirname(tgt_abs)`,
                    # identical to `scratch_par_abs` above for THIS target
                    # (same computation) and to every other file target
                    # sharing this parent dir; routed through the same memo
                    # rather than spawning a second/duplicate `git` call.
                    rc_r, out_r = _memo_run_git(["-C", probe_dir, "rev-parse", "--show-toplevel"])
                    root = out_r.strip() if rc_r == 0 else ""
                if not root:
                    continue
                # BATCHED per repo root, not memoized per target. The pathspec is
                # loop-VARIANT -- `tgt_abs` differs by construction -- which is why
                # the (cwd, args) memo above cannot absorb it, and why this was one
                # spawn per target: `rm a.py b.py c.py` paid three. `git status`
                # takes MANY pathspecs, so one call answers every target sharing a
                # root, and `_batched_status_for` fills that table on first need
                # (never in a pre-pass -- see its docstring: a pre-pass would spawn
                # for targets the early `continue`s above never reach).
                #
                # `None` means the batch could not be attributed with certainty, so
                # this falls back to the original per-target call rather than
                # treating an unparsed line as a clean tree. See
                # `_attribute_porcelain`'s negative spec: a missed spawn is a cost,
                # a missed deny is a peer's lost work.
                _all_tgt_abs = [
                    _abs_path(os.path.expanduser(t) if t.startswith("~") else te)
                    for t, te in targets
                    if t and os.path.exists(os.path.expanduser(t) if t.startswith("~") else te)
                ]
                out_st = _batched_status_for(root, tgt_abs, _all_tgt_abs)
                if out_st is None:
                    _rc_st, out_st = _dc_git_probe._run_git(
                        ["-C", root, "--no-optional-locks", "status", "--porcelain", "--", tgt_abs]
                    )
                status = "\n".join(out_st.splitlines()[:9])
                if status:
                    peer_sid = _rm_peer_claim_of(tgt_abs, root, _dirty_paths_lookup)
                    if peer_sid:
                        return _alt_record(
                            "deny", "destructive-rm",
                            "BLOCKED (not overridable): '%s' holds "
                            "untracked/uncommitted work claimed by LIVE peer "
                            "session %s in this shared worktree. "
                            "Untracked files are byte-indistinguishable from "
                            "your own and git CANNOT recover them. If you "
                            "believe it is stale, confirm with the peer or "
                            "wait for their handoff — do not delete a live "
                            "peer's uncommitted work." % (tgt, peer_sid), (),
                            report="the removal you need", authorize="deleting a live peer's work",
                            em_text=(
                                "BLOCKED (not overridable): '%s' holds "
                                "untracked/uncommitted work claimed by LIVE peer "
                                "session %s in this shared worktree. "
                                "Untracked files are byte-indistinguishable from "
                                "your own and git CANNOT recover them. If you "
                                "believe it is stale, confirm with the peer or "
                                "wait for their handoff — do not delete a live "
                                "peer's uncommitted work." % (tgt, peer_sid)
                            ),
                        )
                    if rm_override:
                        continue
                    disp = "\n".join(status.splitlines()[:8])
                    more = "\n  ... and more (first 8 shown)" if len(status.splitlines()) > 8 else ""
                    # Name the flag when recursion is the ONLY reason this
                    # armed. On a directory target the reason is the target
                    # and needs no flag; on a file target the caller cannot
                    # otherwise tell a read of their command from a policy
                    # about it, which is how a misparse survived three repos.
                    why = ""
                    if not tgt_is_dir and recursion_flag:
                        why = " (read as recursive: %s)" % recursion_flag
                    return _alt_record(
                        "deny", "destructive-rm",
                        "BLOCKED: 'rm' on '%s'%s would destroy uncommitted/"
                        "untracked work that git CANNOT recover (untracked "
                        "files and unstaged edits live in no commit, no "
                        "stash, no reflog):\n%s%s\n"
                        "Re-derive what would actually be lost rather than trusting a "
                        "remembered state; reserve irreversible deletion for genuinely "
                        "disposable, self-authored, uncontested paths."
                        % (tgt, why, disp, more),
                        (
                            _ready('git -C "%s" status --porcelain -- "%s"' % (root, tgt_abs)),
                            _ready(
                                'cp -r -- "%s" "${TMPDIR:-/tmp}/preserved-$(basename "%s")"'
                                % (tgt_abs, tgt_abs)
                            ),
                        ),
                        report="the removal you need", authorize="deleting uncommitted work",
                        em_text=(
                            "BLOCKED: 'rm' on '%s'%s would destroy uncommitted/"
                            "untracked work that git CANNOT recover (untracked "
                            "files and unstaged edits live in no commit, no "
                            "stash, no reflog):\n%s%s\n\n"
                            "Before overriding, re-derive what would actually be "
                            "lost (do not trust a remembered or narrated state):\n"
                            '  git -C "%s" status --porcelain -- "%s"\n\n'
                            "To preserve the work first, COPY it aside — never "
                            "stash on this tree, which a dozen sessions share "
                            "(`stash@{0}` is whoever stashed last, not you):\n"
                            '  cp -r -- "%s" "${TMPDIR:-/tmp}/preserved-$(basename "%s")"\n\n'
                            "Reserve irreversible deletion for genuinely "
                            "disposable, self-authored, uncontested paths."
                            % (tgt, why, disp, more, root, tgt_abs, tgt_abs, tgt_abs)
                        ),
                    )

    # BX-13: a `sh -c '...'`/`bash -c "..."` (etc.) wrapper's quoted argument
    # is executed, not inert text -- unwrap and re-scan it too.
    #: The loop variable is the unwrapped COMMAND STRING, not the guard payload
    #: dict -- named apart from `payload` because binding it to that name shadowed
    #: this function's own `payload` parameter, so the recursion below silently
    #: re-scanned with caller context dropped and `_override` fell back to ambient
    #: process env. Forward the parameter explicitly (C14c).
    for unwrapped_cmd in _shell_c_unwrap_payloads(cmd):
        result = check_destructive_rm(unwrapped_cmd, session_id, payload=payload)
        if result is not None:
            return result

    # C9: reached only once every target above (bash `rm` AND `git rm`
    # alike) AND every unwrapped `sh -c`/`bash -c` sub-command cleared the
    # deny ladder with no `return _deny(...)` anywhere -- i.e. only when
    # this call is actually about to ALLOW the removal through. Flushing
    # here, and nowhere earlier, is what keeps a denied `rm` from ever
    # recording a touch for work that never actually happened.
    _rm_flush_touch(_pending_rm_touch_paths, session_id, cur_repo)
    return None
