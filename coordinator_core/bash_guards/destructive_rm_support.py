"""coordinator_core.bash_guards.destructive_rm_support -- target-resolution,
batched-status attribution and touch-record helpers behind
``_dc_rm.check_destructive_rm`` and ``stale_write_guard.check_stale_write``.

Imports only ``_dc_shell_text`` at module level (``machine_resolver`` lazily, for
``paths.scratch_root``); never the ``dispatch_checks`` facade or a guard module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.bash_guards._dc_shell_text import _RM_WRAPPER_WORDS

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
    # Bundled `-c`: `sh -ic '...'` behaves as `sh -i -c '...'`, so an exact
    # `-c` token missed it. Same tolerance as `_BUNDLED_C_FLAG_RE`.
    if re.match(r"^(bash|sh|zsh|dash)\s+(\S+\s+)*-[a-zA-Z]*c[a-zA-Z]*(\s|$)|^eval(\s|$)", probe):
        return bool(re.search(r"\brm\b", probe))
    firstword = s.split(None, 1)[0] if s.split(None, 1) else ""
    if firstword in _RM_WRAPPER_WORDS:
        # Searched on `probe` as well as `s`: reading only `s` let
        # `sudo 'r''m' -rf <path>` past. Quote-stripping leaves the `(){}` and
        # backtick boundaries this pattern anchors on intact.
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
    # negative-spec: the replacement MUST stay a callable. A plain-string `home`
    # is parsed as a re.sub TEMPLATE, so a Windows HOME of `C:\Users\<name>`
    # raises `PatternError: bad escape \U` and the fail-closed guard denies
    # every `rm` naming `$HOME` uninspected. A callable is returned verbatim.
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


def _resolves_under_machine_scratch_root(resolved: str) -> bool:
    """True when `resolved` lies strictly below the machine-local `paths.scratch_root`.
    In-process registry read, no spawn. An unset or unreadable key, or a root that is
    a filesystem root, never matches; the root itself is not exempt."""
    try:
        from coordinator_core import machine_resolver

        raw = machine_resolver.registry_get("paths.scratch_root")
        if not raw:
            return False
        root = os.path.realpath(raw)
    except Exception:  # noqa: BLE001 -- a guard that cannot resolve the root withholds the exemption
        return False
    if os.path.dirname(root) == root:
        return False
    target = os.path.normcase(os.path.normpath(resolved.replace("/", os.sep)))
    base = os.path.normcase(os.path.normpath(root))
    return target != base and target.startswith(base.rstrip(os.sep) + os.sep)


def _resolves_under_repo_scratch(resolved: str) -> bool:
    """True when `resolved` (already symlink/junction/`..`-resolved) lies strictly
    below a `<repo>/scratch` directory, `<repo>` being a directory holding a `.git`
    entry, or strictly below the machine-local `paths.scratch_root`. Filesystem
    stats and an in-process registry read only, no spawn. A scratch dir that is
    itself a link resolves elsewhere and never matches. A nested repo root or clone
    below scratch is exempt like any other path there."""
    cur = os.path.dirname(resolved.replace("/", os.sep))
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:
            return _resolves_under_machine_scratch_root(resolved)
        if os.path.normcase(os.path.basename(cur)) == "scratch" and os.path.exists(
            os.path.join(parent, ".git")
        ):
            return True
        cur = parent


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


_STATUS_UNKNOWN = "?? (status unattributed; treated as uncommitted)"


def _attribute_porcelain_z(
    out: str, root: str, targets: List[str]
) -> Optional[Dict[str, str]]:
    """Split one batched `git status --porcelain -z` over many pathspecs into
    per-target status text. `-z` carries raw (never quoted) paths and puts a
    rename/copy's origin in its own NUL field, so every shape is attributable;
    None only when an entry owns no target, which the caller reads as "unknown",
    never "clean"."""
    table: Dict[str, str] = {os.path.normcase(os.path.normpath(t)): "" for t in targets}
    fields = out.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        if len(entry) < 4:
            return None
        paths = [entry[3:]]
        if entry[0] in "RC" or entry[1] in "RC":
            if i >= len(fields) or not fields[i]:
                return None
            paths.append(fields[i])
            i += 1
        owned = False
        for rel in paths:
            abs_path = os.path.normpath(os.path.join(root, rel))
            owner = None
            for t in targets:
                if _is_within(abs_path, t) and (owner is None or len(t) > len(owner)):
                    owner = t
            if owner is None:
                continue
            owned = True
            key = os.path.normcase(os.path.normpath(owner))
            line = entry[:3] + rel
            if line not in table[key].split("\n"):
                table[key] = (table[key] + "\n" + line) if table[key] else line
        if not owned:
            return None
    return table


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


def _ps_recurse_flag(tokens: List[str]) -> Optional[str]:
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
