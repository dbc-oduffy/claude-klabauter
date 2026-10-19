"""coordinator_core.ops.dispatch_emit.em_only_act -- is a plan row the EM's to perform.

INVARIANT: one predicate answers every em-performance decision. A row is em-performed when it
declares `performer: em`, or when its only act is a command dispatch forbids every subagent
(`guard_subagent_heavy_ue_launch.launches_heavy`, the single forbidden-command list; never copied).

TRAP: a row with any write act (`writes` non-empty, `writes_under`, `appends`) stays agent-performed
even when its body runs a heavy command: that is the needs_slot author lane, where the agent writes
and the EM runs the slot leg. Deriving em there would strand the authoring. A row without a
declared `writes` key is unknown and never derived.

Rows carry no commands field, so the command is read from the body's code spans (fenced blocks
line by line, then inline single-backtick spans). Prose naming a script is a mention, not a run.
A code span quoting a forbidden command in negation ("never run `build-plugin.ps1 ...`") is
indistinguishable from a run and derives em.
Pure Python: no spawn, no I/O.

INVARIANT: `executor_commit_act` reuses the Commit Gate's own matchers
(`block_subagent_commit._has_git_commit`, `_has_committing_op_invoke`, `_has_coordinator_safe_commit`),
imported lazily after a substring prefilter, so the plan bar and the guard agree on what a commit is.
The deploy list `_DEPLOY_ACT` is closed: each entry ships a committed tree.

TRAP: `executor_commit_act` flags a backticked commit or deploy quoted in negation ("never run
`git commit`"); drop the backticks to make it a mention.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterator, Optional

from coordinator_core.bash_guards.guard_subagent_heavy_ue_launch import launches_heavy

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE = re.compile(r"`([^`\n]+)`")


def _code_spans(body: str) -> Iterator[str]:
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            yield line
        else:
            for match in _INLINE.finditer(line):
                yield match.group(1)


def forbidden_sole_act(raw: Dict[str, Any]) -> Optional[str]:
    """The first body code span dispatch forbids a subagent to run, or None.

    None unless the row declares `writes: []` and carries no `writes_under` or `appends`.
    """
    if raw.get("writes") != [] or raw.get("writes_under") or raw.get("appends"):
        return None
    body = raw.get("body")
    if not isinstance(body, str):
        return None
    for span in _code_spans(body):
        span = span.strip()
        if span and launches_heavy(span):
            return span
    return None


# Closed list of commands that publish a committed tree, so an executor (who never commits) cannot
# finish them: firebase/gcloud deploy upload the checked-in sources; npm/pnpm/yarn/bun deploy[:target]
# scripts wrap those. Tolerates a path-prefixed argv0 and .cmd/.exe spellings.
_DEPLOY_ACT = re.compile(
    r"(?:^|[\s;&|(])(?:[^\s;&|]*[\\/])?(?:"
    r"(?:firebase|gcloud)(?:\.cmd|\.exe)?\s+(?:[^;&|\n]*\s)?deploy\b"
    r"|(?:npm|pnpm|yarn|bun)(?:\.cmd|\.exe)?\s+(?:run\s+)?deploy(?::[\w-]+)?(?=\s|$)"
    r")",
    re.IGNORECASE,
)


def _span_commits_or_deploys(span: str) -> bool:
    lowered = span.lower()
    if "commit" in lowered:
        from coordinator_core.bash_guards.block_subagent_commit import (
            _has_committing_op_invoke,
            _has_coordinator_safe_commit,
            _has_git_commit,
        )

        if (
            _has_git_commit(span)
            or _has_committing_op_invoke(span)
            or _has_coordinator_safe_commit(span)
        ):
            return True
    return "deploy" in lowered and _DEPLOY_ACT.search(span) is not None


def executor_commit_act(raw: Dict[str, Any]) -> Optional[str]:
    """The first body code span an executor would run to commit or deploy, or None.

    None when row_is_em_performed(raw) (performer: em, or an agent-forbidden sole act).
    """
    if row_is_em_performed(raw):
        return None
    body = raw.get("body")
    if not isinstance(body, str):
        return None
    for span in _code_spans(body):
        span = span.strip()
        if span and _span_commits_or_deploys(span):
            return span
    return None


def row_is_em_performed(raw: Dict[str, Any]) -> bool:
    """True for a declared `performer: em` row or one whose only act is agent-forbidden."""
    return raw.get("performer") == "em" or forbidden_sole_act(raw) is not None
