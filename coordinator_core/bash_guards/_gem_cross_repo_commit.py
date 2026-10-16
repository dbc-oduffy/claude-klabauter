"""coordinator_core.bash_guards._gem_cross_repo_commit -- the scoped-git-write
predicate and the in-process Group EM standing read behind the
``coordinator.feature.gem_cross_repo_commit`` branch of bump_foreign_repo_write.

Pure and spawn-free: parses already-tokenized argv and reads the nomination
records in process. It imports nothing from bump_foreign_repo_write.
"""

from __future__ import annotations

from typing import Optional, Sequence

_COMMIT_LONG_VALUE_FLAGS = frozenset({
    "--message", "--file", "--reuse-message", "--reedit-message", "--fixup",
    "--squash", "--author", "--date", "--cleanup", "--template", "--trailer",
    "--pathspec-from-file",
})
_COMMIT_SHORT_VALUE_LETTERS = frozenset("mFCct")
_COMMIT_UNSCOPED_LONG = frozenset({"--all", "--amend"})

_ADD_UNSCOPED_LONG = frozenset({"--all", "--update", "--no-ignore-removal"})
_ADD_UNSCOPED_SHORT = frozenset("Au")
_ADD_LONG_VALUE_FLAGS = frozenset({"--pathspec-from-file"})
_ADD_BROAD_PATHSPECS = frozenset({".", "./", ":/", "*"})


def _is_long_option_for(name: str, unscoped: frozenset) -> bool:
    """True when ``name`` is, or is a git-accepted unique abbreviation of (``--am``
    for ``--amend``), an unscoped long option. Any 3+ char prefix counts: an
    ambiguous abbreviation errors in git, so treating it as unscoped is safe."""
    return len(name) >= 3 and any(full.startswith(name) for full in unscoped)


def _commit_is_scoped(args: Sequence[str]) -> bool:
    pathspecs = 0
    expecting_value = False
    after_dashdash = False
    for tok in args:
        if after_dashdash:
            pathspecs += 1
            continue
        if expecting_value:
            expecting_value = False
            continue
        if tok == "--":
            after_dashdash = True
            continue
        if tok.startswith("--"):
            name, eq, _ = tok.partition("=")
            if _is_long_option_for(name, _COMMIT_UNSCOPED_LONG):
                return False
            if name == "--pathspec-from-file":
                pathspecs += 1
            if name in _COMMIT_LONG_VALUE_FLAGS and not eq:
                expecting_value = True
            continue
        if tok.startswith("-") and len(tok) > 1:
            cluster = tok[1:]
            for i, letter in enumerate(cluster):
                if letter == "a":
                    return False
                if letter == "S":
                    break
                if letter in _COMMIT_SHORT_VALUE_LETTERS:
                    if i == len(cluster) - 1:
                        expecting_value = True
                    break
            continue
        pathspecs += 1
    return pathspecs > 0


def _add_is_scoped(args: Sequence[str]) -> bool:
    pathspecs = 0
    expecting_value = False
    after_dashdash = False
    for tok in args:
        if expecting_value:
            expecting_value = False
            pathspecs += 1
            continue
        if not after_dashdash and tok == "--":
            after_dashdash = True
            continue
        if not after_dashdash and tok.startswith("--"):
            name, eq, _ = tok.partition("=")
            if _is_long_option_for(name, _ADD_UNSCOPED_LONG):
                return False
            if name in _ADD_LONG_VALUE_FLAGS and not eq:
                expecting_value = True
            continue
        if not after_dashdash and tok.startswith("-") and len(tok) > 1:
            if any(letter in _ADD_UNSCOPED_SHORT for letter in tok[1:]):
                return False
            continue
        if tok in _ADD_BROAD_PATHSPECS or tok.startswith((":/", ":(top")):
            return False
        pathspecs += 1
    return pathspecs > 0


def git_write_is_gem_scoped(subcommand: str, args: Sequence[str]) -> bool:
    """True when a git write is one a Group EM may make into a foreign repo.

    ``args`` are the tokens after ``subcommand``. Scoped means: ``mv``; ``commit``
    with at least one explicit pathspec and no ``-a``/``--all``/``--amend``
    (a pathspec-less commit takes the foreign index as it stands, possibly that
    repo's own staged work); ``add`` with explicit pathspecs only (no ``-A``,
    ``-u``, ``.``, ``:/``). Everything else is False.

    Source of the rule: DoE's guardrail "scoped pathspec commits only (never
    git add -A/./commit -a), no destructive git ops" (coordinator-content-repo
    coordinator/skills/group-em/SKILL.md, section Cross-repo commit authority).
    """
    if subcommand == "mv":
        return True
    if subcommand == "commit":
        return _commit_is_scoped(args)
    if subcommand == "add":
        return _add_is_scoped(args)
    return False


def session_holds_live_gem_standing(session_id: str, repo_root: Optional[str]) -> bool:
    """True iff the nomination read reports a live Group EM holder equal to ``session_id``.

    Empty inputs and any exception give False: fail-open here means "not a
    Group EM", so the ordinary bump applies.
    """
    if not session_id or not repo_root:
        return False
    try:
        from coordinator_core.group_em import nomination

        verdict = nomination.identify(repo_root)
        if verdict.get("state") != "live":
            return False
        holder = verdict.get("holder") or {}
        return str(holder.get("session_id") or "") == session_id
    except Exception:
        return False
