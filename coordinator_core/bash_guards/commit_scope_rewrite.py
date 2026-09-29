"""Compound add/commit scope rewrite for `git commit` advice.

Split out of `dispatch_checks.py` (hot-path line ratchet). Depends only on
`_rewrite_support` and the tokenizer, never on `dispatch_checks`.
"""

from __future__ import annotations

import shlex
from typing import List, Optional, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _bt_segments_from_tokens_with_pipe_flag,
    tokenize_full_command as _bt_tokenize_full_command,
)
from coordinator_core.bash_guards._rewrite_support import (
    _bt_git_resolved_subcommand,
    _bt_git_subcommand_start_index,
)

#: `git add` flags that leave the add's scope exactly the operands it names.
#: Any other flag (`-A`, `-u`, `-p`, `--pathspec-from-file`, a short
#: cluster) means the operands are not the whole scope, so no commit
#: pathspec can be derived from them.
_GIT_ADD_SCOPE_PRESERVING_FLAGS = frozenset(
    {"-f", "--force", "-v", "--verbose", "-N", "--intent-to-add"}
)

#: Operand characters the tokenizer hands back unexpanded -- a pathspec
#: built from them would be re-quoted into a literal the shell never meant.
_GIT_ADD_UNLITERAL_CHARS = frozenset("*?[$`~")


def _bt_add_paths_for_commit_rewrite(
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
) -> Optional[List[str]]:
    """The literal paths this compound's `git add` segments named, when the
    bare commit at `seg_index` can be scoped to exactly them; None otherwise.

    None whenever the paths are not knowable from the command text: an
    unscoped add, a whole-tree operand (`.`, `:/`), an unexpanded glob or
    variable, or git global options (`-C <dir>`) that differ between the add
    and the commit, so the same relative path would name a different file.
    """
    commit_start = _bt_git_subcommand_start_index(seg_tokens)
    if commit_start is None:
        return None
    commit_prefix = seg_tokens[: commit_start - 1]
    paths: List[str] = []
    for prior_tokens, _pipe in segments[:seg_index]:
        if _bt_git_resolved_subcommand(prior_tokens) != "add":
            continue
        add_start = _bt_git_subcommand_start_index(prior_tokens)
        if add_start is None or prior_tokens[: add_start - 1] != commit_prefix:
            return None
        after_separator = False
        for tok in prior_tokens[add_start:]:
            if not after_separator and tok == "--":
                after_separator = True
                continue
            if not after_separator and tok.startswith("-"):
                if tok not in _GIT_ADD_SCOPE_PRESERVING_FLAGS:
                    return None
                continue
            if (
                tok in (".", "./", ":/", "/")
                or tok.startswith(":")
                or _GIT_ADD_UNLITERAL_CHARS.intersection(tok)
            ):
                return None
            paths.append(tok)
    return paths or None


def _bt_scope_compound_commit_to_its_add(
    raw_cmd: str,
    seg_tokens: List[str],
    segments: List[Tuple[List[str], bool]],
    seg_index: int,
) -> Optional[str]:
    """`raw_cmd` with the bare commit scoped to its own `git add`'s paths
    (`... && git commit -m x -- <paths>`), or None when that cannot be done
    by construction.

    The pathspec is appended to the END of the command text, so the commit
    must be the last segment. The result is re-tokenized and accepted only
    if the commit segment is exactly the original tokens plus `-- <paths>`
    and every other segment is untouched -- a trailing comment, heredoc or
    redirection that would swallow the appended text fails that check and
    falls back to the deny rather than to a guessed command.
    """
    if seg_index != len(segments) - 1 or "<<" in raw_cmd:
        return None
    paths = _bt_add_paths_for_commit_rewrite(seg_tokens, segments, seg_index)
    if paths is None:
        return None
    rewritten = "%s -- %s" % (
        raw_cmd.rstrip(),
        " ".join(shlex.quote(p) for p in paths),
    )
    new_tokens = _bt_tokenize_full_command(rewritten)
    if new_tokens is None:
        return None
    new_segments = _bt_segments_from_tokens_with_pipe_flag(new_tokens)
    expected = list(segments[:seg_index]) + [
        (seg_tokens + ["--"] + paths, segments[seg_index][1])
    ]
    if [(list(t), p) for t, p in new_segments] != [
        (list(t), p) for t, p in expected
    ]:
        return None
    return rewritten
