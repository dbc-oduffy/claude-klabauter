"""`_SESSION_ID_UUID_RE` is replicated in three modules; this asserts they agree.

The pattern is a session-id SHAPE validator, not a canonical-UUID validator:
it gates whether a `Session-Id:` trailer's value is even worth treating as a
session id before any attribution decision rests on it. Three modules carry
their own copy rather than importing one, deliberately -- `archive_stamp`,
`ops/handoff_correct_body`, and `workstream_complete/directives_commit_tail`
sit in different dependency strata and a shared constant would couple them.

Negative-spec: this does NOT assert the pattern's VALUE, only that the copies
have not diverged. Tightening the shape (say, to require canonical 8-4-4-4-12
dash placement) is a deliberate change -- make it in all three and this test
follows you there. What it refuses is the silent case: one copy tightened, the
others left behind, two validators now disagreeing about the same trailer while
both claim to implement "the same shape."

Raised as a P2 by the code-reviewer against `5e66def27`
(`state/subagent-share/cb57af40-4874-4b6c-b192-10b2afe1c517/
coordinatorcode-reviewer-5a0b4e00.md`), which could not verify byte-equality
within its dispatch budget and named the drift risk rather than leaving it
unchecked. The copies were byte-identical when this landed; this is the
artifact that keeps them so.
"""

import re
from pathlib import Path

_REPLICA_RELPATHS = (
    "coordinator_core/archive_stamp.py",
    "coordinator_core/ops/handoff_correct_body.py",
    "coordinator_core/workstream_complete/directives_commit_tail.py",
)

# Captures only the pattern-string argument (group "pattern"), never a trailing
# `, re.IGNORECASE`-style flags argument -- (.+) greedy-to-last-")" would swallow
# flags whole, making a one-arg/two-arg mismatch fail as a spurious "diverged"
# rather than naming what actually differs. `(?:#.*)?` tolerates a trailing
# end-of-line comment, which a bare `\s*$` would otherwise miss entirely
# (raising "found 0" instead of surfacing real drift).
_ASSIGNMENT_RE = re.compile(
    r"^_SESSION_ID_UUID_RE\s*=\s*re\.compile\("
    r"(?P<pattern>r?\"[^\"]*\"|r?'[^']*')"
    r"(?:\s*,\s*(?P<flags>[\w.| ]+?))?"
    r"\)\s*(?:#.*)?$",
    re.MULTILINE,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _replica_literals() -> dict[str, str]:
    literals: dict[str, str] = {}
    for relpath in _REPLICA_RELPATHS:
        source = (_REPO_ROOT / relpath).read_text(encoding="utf-8")
        matches = list(_ASSIGNMENT_RE.finditer(source))
        assert len(matches) == 1, f"{relpath}: expected exactly one _SESSION_ID_UUID_RE assignment, found {len(matches)}"
        match = matches[0]
        pattern = match.group("pattern").strip()
        flags = match.group("flags")
        literals[relpath] = f"{pattern} [flags={flags.strip()}]" if flags else pattern
    return literals


def test_replicas_are_byte_identical() -> None:
    literals = _replica_literals()
    distinct = set(literals.values())
    assert len(distinct) == 1, (
        "_SESSION_ID_UUID_RE has diverged across its replica sites -- one copy was "
        "changed without the others. Reconcile all three, then update this test's "
        "docstring if the shape itself was meant to change:\n"
        + "\n".join(f"  {path}: {literal}" for path, literal in sorted(literals.items()))
    )
