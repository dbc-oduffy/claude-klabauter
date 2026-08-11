"""coordinator_core.bash_guards.check_test_suite_invocation -- PreToolUse(Bash)
hard-deny guard for **test-breadth invocation authority** (example-doctrine-repo's DR-088, layers
2, 3, 5 and 6 of the six-layer test-breadth ladder).

Three ordered deny legs over one shared classifier -- identity, THEN
authority, THEN resource; a caller with no standing to run a suite at all is
told so before it is ever told to wait for the machine:

  0. PRECISION leg (layer 3, R9, fail-OPEN without a cwd) -- a NON-suite-shaped
     (Tier T) command issued by a SUBAGENT is denied when it names a DIRECTORY
     positional. DR-088 § Decision always defined Tier T as the files,
     directories, or node ids the caller *"authored or touched"*; the
     mechanism enforced path-scoped and silently dropped the relevance half,
     so ``pytest tests/workstate_store/`` from an executor that touched three
     files in it passed as a scoped run over 87 files it never touched. For a
     dispatched caller, Tier T is file-and-node-id precision. The EM is
     unaffected -- directory-level Tier T is unchanged for the top-level
     session. Runs only when the command is NOT suite-shaped, so it is
     strictly additive: it never restates a deny the identity leg below owns
     with a better diagnosis. Ruled by example-doctrine-repo 2026-07-28
     (``cross-repo/inbox/2026-07-28-example-doctrine-repo-em-dr088-tier-t-relevance-
     ruling.md``); example-doctrine-repo refused a breadth/test-count term in the same ruling
     (R8) -- a rule the classifier cannot evaluate without collecting is not
     a rule.

  1. IDENTITY leg (layer 3, fail-CLOSED) -- a *suite-shaped* test command
     issued by a SUBAGENT is denied. Tier T (a path/node-id-scoped invocation
     naming what the caller actually touched) stays ungated for the EM and,
     subject to leg 0's precision narrowing, for subagents;
     Tier F/U (the repo's configured fast/full command, or any unscoped
     runner invocation) is reserved for the top-level EM. The incident this
     exists for: a fan-out wave in which every dispatched agent runs the
     whole suite concurrently -- 10+ minutes each, N of them, degrading the
     machine. Runs FIRST: a subagent is denied on identity alone, regardless
     of grant or mutex state.

  2. GRANT leg (layer 5, fail-CLOSED on authorization / fail-OPEN on infra) --
     a Tier-U OR Tier-F command (unscoped, the repo's configured
     ``full_test_cmd``, OR the repo's configured ``fast_test_cmd``, matched by
     segment-set containment -- see ``_matches_configured_cmd``) issued by the
     top-level EM is denied unless that EM's session holds a live Tier-U
     authorization grant (``coordinator_core.session.grant.
     check_tier_u_grant`` -- the same grant record covers both tiers; there is
     no separate Tier-F grant). PM-ruled 2026-08-04 (``the tree changes
     often... I want EMs to need to ask me for permission to run test suites,
     because they hammer the machine``): Tier F is no longer exempt from this
     leg -- the ask IS the escape hatch, and no Tier-F equivalent of the R6
     declaration exit (below) is to be added. Runs only on the
     non-subagent path (the identity leg above already denies every subagent
     Tier-U/F command outright) and only once the command has already been
     established as suite-shaped, since tiering re-resolves the repo's
     configured commands. A live grant is either the implicit one a example-doctrine-repo
     ceremony writes at ceremony open, or an explicit PM grant written via
     ``tier-u-grant-cli grant pm``. ``/workday-complete``,
     ``/workweek-complete`` and ``/merging-to-main`` each write the implicit
     one. This is an AUTHORITY control -- who may ask -- distinct from the
     mutex below, which is a RESOURCE control -- how many may run at once;
     see ``grant.py``'s module docstring for the same distinction from the
     writer/reader side.

     No-implicit-grant ceremonies (Review: coordinator:code-reviewer --
     restoring content the `_GRANT_DETAIL_POINTER` cut silently dropped
     rather than relocated): ``/bug-blitz``, ``/bug-sweep``,
     ``/mise-en-place``, and ``/finishing-a-development-branch`` do NOT
     carry an implicit Tier-U grant -- ask the PM explicitly before running
     Tier U under any of them.

     Ownership split, and why this file cannot close a grant-writer gap on
     its own if one reopens: the WRITER lives in example-doctrine-repo's tree --
     ``coordinator/commands/<ceremony>.md`` OR
     ``coordinator/skills/<ceremony>/SKILL.md``, and both shapes are in live
     use, which is precisely what an earlier verification missed by grepping
     only the skills tree -- while the claim that a writer exists lives
     here. The two sides can silently diverge again (this list was wrong in
     both directions on 2026-08-07, within one day), so do not restate which
     ceremonies write the implicit grant here without re-verifying against
     example-doctrine-repo's tree first.

     Negative spec -- the override is NOT a substitute for the explicit
     grant. A session that meets this deny with no live grant may find
     ``COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION=1`` the cheapest way out;
     it is not the honest path -- ``tier-u-grant-cli grant pm`` is -- and the
     override disables the identity and mutex legs along with this one. An
     authority control whose honest path looks harder than its bypass trains
     users onto the bypass; the answer is always the explicit grant, never
     the env var.

     A grant is session-scoped and liveness-gated: one PM ask covers THIS
     session for its lifetime, but a grant left behind by a dead session
     never authorizes a different (even resumed) session. Both facts above
     are exactly what an agent gets wrong by default, which is why they are
     written down here rather than only in `_deny_reason_grant`'s deny text.

  2.5. WRAPPER leg (fail-CLOSED) -- once a Tier-U OR Tier-F command has
     cleared the grant leg above, it is denied unless it is actually routed
     through ``with-suite-mutex`` (``coordinator/bin/with-suite-mutex`` --
     see ``_command_wrapped_in_suite_mutex``), the wrapper that takes the
     mutex leg's lock for real. Closes the gap where a granted EM ran the
     bare command and held nothing, so two granted sessions still overlapped
     -- the mutex leg below only refuses a SECOND concurrent run; nothing
     previously required the FIRST one to actually take the lock. The deny
     names the wrapped form of the caller's own command. Runs strictly after
     the grant leg (an ungranted command is never told to wrap what it is
     not authorized to run) and strictly before the mutex leg (a wrapped
     command still waits its turn if another run already holds the lock).

  3. MUTEX leg (layer 6, fail-OPEN) -- a suite-shaped command is denied while
     ANOTHER suite run holds the machine-wide mutex
     (``coordinator_core.testing.suite_mutex.holder()``), including for the
     top-level EM. One suite run at a time, machine-wide. This is not
     politeness: concurrent runs against a shared tree produce *fake*
     assertion failures (mid-edit reads against constants HEAD already defines
     correctly) and can abort a pytest process outright when one session holds
     a native-store write lock. Runs LAST, after the grant leg: a session with
     no grant is told to get one, not told to wait for a mutex it was never
     entitled to take.

**Caller identity is keyed on ``payload["agent_id"]`` PRESENCE, never on a
resolved ``subagent_type``.** This is load-bearing and must not be
"improved" into a type-keyed check. Workflow-phase agents carry ``agent_id``
but have NO ``dispatched-agents.txt`` back-pointer, so
``_read_backpointer_subagent_type()`` returns empty for every one of them
(646 Workflow agent ids traced, zero present in any ``dispatched-agents.txt``;
516 of them do have ``.git/coordinator-sessions/.agents/<id>/touched.txt``,
which is written only from a non-empty top-level ``agent_id``). A type-keyed
guard would silently exempt every Workflow agent -- precisely the fan-out
shape causing the incident. It would look correct, pass review, and do
nothing.

Negative spec -- what this guard deliberately does NOT do:
  - It does NOT raw-substring-match ``agent_id`` against the stdin JSON. A
    nested ``tool_response.agent_id`` would false-positive on main-loop calls;
    only the TOP-LEVEL dict key is read.
  - It does NOT deny FILE- or NODE-ID-scoped invocations. ``pytest
    path/test_x.py``, ``pytest path/test_x.py::test_case`` and ``pytest -k
    expr`` are Tier T and are allowed for everyone, always -- a node id
    stays permitted for a subagent regardless of its touched set, which is
    what keeps pre-existing-failure verification (re-running the one failing
    test you did not author) legal under leg 0.
  - It does NOT intersect a directory argument against the caller's touched
    set. The cheaper rule -- "the named directory must contain >=1 file this
    agent touched" -- passes ``pytest tests/workstate_store/`` for an agent
    that touched one file in it, so it does not close leg 0's case at all:
    containing a touched file is not the same claim as being scoped to
    touched files. The touched set is read ONLY to phrase the deny's
    better-alternative line.
  - It does NOT fall back to the session-level ``touched.txt`` when the
    agent-keyed one is absent or unresolvable. That set belongs to the EM and
    to other agents; borrowing it to phrase "the tests you touched" would
    launder exactly the relevance leg 0 asserts.
  - It does NOT treat a path argument that IS (or is an ancestor of) a
    configured ``testpaths`` root as a scope. ``pytest coordinator_core/`` is
    the entire ~9k-test suite in this repo and equals the configured
    ``fast_test_cmd``/``full_test_cmd`` -- a scoped-*looking* command that is
    the whole suite is the named failure mode this classifier is designed
    against.
  - It does NOT own the Tier-U authorization grant's schema or registry
    manifest entry (DR-088 layer 5) -- example-doctrine-repo owns
    ``coordinator/schemas/tier-u-grant.schema.json`` and its manifest row.
    This module OWNS the guard leg that reads it (the grant leg above) and
    the writer/reader it calls into (``coordinator_core.session.grant``),
    because the liveness primitive that leg needs
    (``coordinator_core.session.liveness``) lives in this repo, not example-doctrine-repo's.

This module also exposes a payload-shape-free public classification API
(``classify_command`` / ``classify_text`` / ``SuiteMatch``) for DR-088 layer
2 -- example-doctrine-repo's ``PreToolUse(Agent|Workflow)`` dispatch-brief-prose guard, which
lives in their tree and calls back into this one classifier rather than
forking a second copy that would silently drift. The private core above
(``_classify_tokens``, ``_read_testpaths``, ``_configured_test_cmds``, etc.)
is the SAME implementation both ``check()`` and the public API run through --
there is exactly one classifier in this module.

Negative spec for the public API -- what it deliberately does NOT do:
  - It does NOT accept a ``PreToolUse`` payload dict, ``tool_name``, or
    ``agent_id``. Caller identity and the hard-deny/allow decision are
    ``check()``'s job; the public API only classifies.
  - It does NOT consult the suite mutex (``testing.suite_mutex``) or emit a
    hook deny envelope. It returns data, never a permission decision.
  - It does NOT self-apply the ``position`` label as an allow/deny policy.
    ``position`` is advisory metadata describing where a match sits
    (fenced code, inline code, negated/quoted, imperative instruction, or
    undeterminable) -- claude-klabauter classifies, the CALLING guard decides what a
    given position means for its own allow/deny policy. A brief that quotes
    the whole deny-list under a "delete this from the agent" instruction
    must still classify every match; the caller, not this module, decides
    that a negated/fenced match should be treated differently from an
    imperative one.
  - It does NOT short-circuit on the first match the way ``check()`` does --
    ``classify_command``/``classify_text`` return every match found.
  - It does NOT classify a bare-line runner-name mention as a command
    unless the line carries a command-intent signal (an execution verb, or
    the runner sitting at the line's own head) -- see
    ``_bare_line_is_command_shaped``. A dispatch brief that mentions
    "pytest" narratively ("... is in pytest `testpaths` and", "a pytest
    oracle", "a re-runnable pytest node id") is prose about a tool, not an
    instruction to run one, and is never emitted as a match.
  - It does NOT read, import, or branch on a repo's
    ``fast_tier_unscoped_reason`` declaration (DR-088 R7, standing
    prohibition, ratified 2026-07-25 --
    cross-repo/inbox/2026-07-25-example-doctrine-repo-em-dr088-marker-scope-ruling.md).
    This classifier answers "what shape is this command";
    whether a repo has declared its fast tier legitimately unscoped is an
    AUTHORITY question -- "is this caller authorized to run that shape
    here" -- answered only by ``coordinator_core.session.tier_u_gate.
    enforce_tier_u_gate``, never here. A classifier that returned Tier F
    because a repo declared an exemption would reinstate the exact
    provenance-classification laundering the R7 fast-leg fix (see the
    ``cfg_tier == "fast_test_cmd"`` branch below) was written to remove, in
    a new costume.

This module also exposes ``classify_text_precision`` / ``classify_command_
precision`` / ``PrecisionMatch`` -- DR-088 R9's public counterpart to the
suite-shape API above. ``classify_text``/``classify_command`` report only
SUITE-shaped commands; a dispatch brief that says "run pytest over
tests/acquisition/" is *scoped* by that classifier's own definition, so it
reports nothing there, even though it is precisely the shape R9's precision
leg (leg 0, above) refuses once the dispatched agent actually tries to run
it. The precision API closes that gap for example-doctrine-repo's layer-2 hook, which needs to
warn the dispatching EM at authoring time rather than let the agent
discover the refusal later.

Negative spec for the precision API -- what it deliberately does NOT do:
  - It does NOT widen R9's pytest-family scope. A match is reported only
    for a ``pytest``/``py.test`` or ``python[3] -m pytest`` invocation --
    the same restriction ``_pytest_directory_args`` already enforces (see
    the ``_PYTEST_HEADS`` comment above), and for the same reason: "this
    token is a directory on disk" is a pytest-specific claim, not one this
    module makes for ``cargo test <filter>``, ``go test <package>``, or any
    other runner.
  - It does NOT restate a ``classify_text``/``classify_command`` match. A
    command that is already suite-shaped is that pair's business; the
    precision API only ever reports a command those two report NOTHING for.
  - It does NOT fail closed without a ``cwd``. Directory-vs-filter-string
    detection needs a disk existence check exactly as
    ``_pytest_directory_args`` does, so an absent ``cwd`` returns ``[]``,
    mirroring that function's and R9's own fail-open discipline -- never a
    manufactured match on shape alone.
  - It does NOT self-apply ``position`` as a filter. Same discipline as
    ``SuiteMatch.position``: this module classifies, the calling hook
    decides what a fenced/negated/descriptive mention means for its own
    policy.
  - It does NOT return a permission decision, a hook envelope, or a mutex
    consult -- data only, same as ``classify_command``/``classify_text``.

This module also exposes ``classify_runner_footprint`` -- a third shape-only
public API, for the caller that needs to tell the two very different
commands ``classify_command`` reports NOTHING for apart: one that invokes no
test runner at all (or invokes one with a positively-parsed scope) versus an
opaque wrapper that could be running the whole suite. Its sole consumer is
``coordinator_core.session.tier_u_gate``'s unclassifiable branch, whose
fail-closed default would otherwise have to refuse both groups alike. Same
negative-spec discipline as the two APIs above -- data, not a decision, and
no knowledge of any repo declaration; see the function's own docstring.

Classifier correction, 2026-07-30 -- package-script arg forwarding:
``_classify_package_manager`` handed args forwarded past a package-script's
``--`` separator to ``_classify_js_runner`` as if the package manager
guaranteed delivery of them to the underlying runner. It does not --
Example-cockpit-repo-em measured pnpm v11 running the WHOLE suite (642-643
files / 8,346-8,359 tests) for BOTH a 9-file-scoped and a 1-file-scoped
``pnpm run test --`` invocation, because pnpm drops the forwarded args for
this script shape rather than passing them to ``vitest``. A dispatched
subagent ran the full suite four times through the guard, unblocked. Fixed
by classifying every package-script shape (``npm test``/``t``/``run
test``/``run-script test``, and the ``pnpm``/``yarn`` equivalents) as
unscoped regardless of trailing args; ``bun test`` keeps routing through
``_classify_js_runner`` because it invokes bun's own built-in runner with
no forwarding layer to lose args across. This is NOT a new DR-088 ruling
(no "R10") -- DR-088's Tier-T definition is unchanged, only claude-klabauter's scope
DERIVATION for this one shape changed. Same class as R9 (a run that NAMES
narrow scope but COVERS wide scope), in its arg-forwarding variant. Spec
backlink: cross-repo/inbox/2026-07-30-example-cockpit-repo-em-tier-u-guard-
bypassed-by-pnpm-arg-forwarding.md

Classifier correction, 2026-08-03 -- tox/nox spelling gap (both legs):
neither ``tox`` nor ``nox`` was in ``_RUNNER_PREFILTER_RE`` at all, so a bare
``tox`` or ``nox`` invocation -- which by design walks EVERY configured
environment/session and therefore the whole test surface -- skipped this
guard entirely: no classification, no subagent identity deny (leg 1), no
Tier-U grant check (leg 2). See ``_classify_tox_nox`` for the full rationale
and why no scoped branch is offered for ``-e``/``-s``/``--`` posargs.
Reconstructed from a relayed finding (relay:
cross-repo/inbox/2026-08-03-example-doctrine-repo-em-klabauter-rows-move-to-you-pm-
ruled.md's sibling chain; original ``2026-08-01-example-retrieval-repo-em-test-suite-
approval-gate-bypass.md`` not present in this tree) -- the relay named which
rung each finding hit (Layer 3 identity leg; the Tier-F/U grant-check leg)
without stating the shapes, which this fix supplies.

Classifier correction, 2026-08-03 -- R9 unexpanded-glob routearound:
``_pytest_directory_args`` decided "this positional names a directory" via a
literal ``os.path.isdir`` check, so an unexpanded glob positional (``pytest
coordinator_core/*/tests``) was never a directory ON DISK and slipped past
leg 0 even though the shell expands it to the exact breadth R9 exists to
refuse. Reported by example-retrieval-repo, relayed via
cross-repo/inbox/2026-08-03-example-doctrine-repo-em-two-rulings-plan-orphan-population-
and-dr088-antiscope.md (Finding 2; the relay's "spelling-keyed" framing is
imprecise -- the gap is isdir-keyed, not spelling-keyed). Fixed by expanding
any positional containing glob metacharacters with ``glob.iglob(...,
recursive=True)`` and refusing it if any expansion is a directory; see
``_pytest_directory_args``'s own docstring for the files-only and
zero-match posture calls this fix pins.

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-test-suite-invocation-guards.md
Spec backlink (public API): cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 1
Spec backlink (grant leg): cross-repo/inbox/2026-07-23-claude-central-em-dr088-ceremonies-write-grants-wire-the-deny.md
Spec backlink (R5-R7 amendment): cross-repo/inbox/2026-07-25-example-doctrine-repo-em-dr088-marker-scope-ruling.md
Spec backlink (R9 precision public API): cross-repo/inbox/2026-07-28-example-market-data-repo-em-dispatched-agent-scoped-test-breadth.md (example-doctrine-repo repo)
Governing decision: example-doctrine-repo docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md
Anti-evasion language adapted from example-doctrine-repo coordinator/agents/coverage-auditor.md:227.
"""

from __future__ import annotations

import configparser
import contextlib
import dataclasses
import glob
import importlib.util
import io
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

from coordinator_core.bash_guards._helpers import operator_override_note, resolve_git_root
from coordinator_core.bash_guards._command_tokenizer import (
    exceeds_tokenizable_ceiling as _exceeds_tokenizable_ceiling,
    tokenize_full_command as _tokenize_full_command,
    segments_from_tokens_simple as _segments_from_tokens_simple,
)
from coordinator_core.bash_guards._dialect import (
    Dialect as _Dialect,
    dialect_from_tool_name as _dialect_from_tool_name,
    resolve_segments_for_dialect as _resolve_segments_for_dialect,
)
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES
from coordinator_core.bash_guards.block_subagent_destructive_action import (
    _strip_heredoc_bodies,
)

CLASS = "hard-deny"
#: Widened (DR-088 ladder layers 3/5/6 PowerShell-bypass fix) from the
#: former ``["Bash"]`` literal to the package's shared command-tool-name
#: universe: this guard's three legs (identity, grant, mutex) were keyed on
#: the literal string ``"Bash"`` at three independent sites, so a suite
#: command issued through the harness's ``PowerShell`` tool sailed through
#: unclassified on a PowerShell-primary fleet. A direct reference to
#: ``COMMAND_TOOL_NAMES`` (C2 declaration-form conversion) -- never a copy
#: or re-wrap -- rather than a second hardcoded ``["Bash", "PowerShell"]``
#: (or ``list(...)``/``tuple(...)``) copy that could drift from it, or that
#: would break identity (``is``) with the shared constant.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 45

#: Escape-hatch env var. Read INLINE inside ``check()`` (never hoisted to
#: module scope), matching ``dispatch_checks._override()``'s F2 discipline.
_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION"

#: Cheap prefilter -- if none of these tokens appear anywhere in the command
#: AND the dynamic per-repo leg (``_dynamic_prefilter_hit``, below) also
#: misses, no runner this classifier knows about can be present, so the whole
#: guard (including the config-file reads and the resolver load) is skipped.
#:
#: Formerly a KNOWN LIMITATION here (removed 2026-08-10): a repo whose
#: configured test command invokes NONE of these static runner names (a
#: bespoke ``bin/run-the-suite``) matched nothing, so every leg of this guard
#: -- identity, grant, mutex -- was skipped for that whole repo, for EM and
#: dispatched subagent alike, until someone hand-added the missing token
#: (``run_tier_tests`` was one such hand-patch, now removed -- see
#: ``_dynamic_prefilter_hit``). That limitation is CLOSED, not merely
#: narrowed: ``check()`` now falls through to the dynamic leg -- which reads
#: the repo's OWN configured ``fast_test_cmd``/``full_test_cmd`` head tokens
#: (from ``coordinator.local.md`` and the ``COORDINATOR_{FAST,FULL}_TEST_CMD``
#: env vars, the same two sources ``resolve_validation_cmd`` resolves from) --
#: whenever this static regex misses, so a repo's bespoke runner is gated
#: automatically the day it is configured, with no per-repo token hand-patch
#: required. The verdict on a widened-through command still comes from
#: ``_matches_configured_cmd`` against that repo's own resolved tier strings,
#: never from a new hardcoded runner branch -- the dynamic leg only decides
#: whether to keep evaluating, exactly as this static regex already did.
#:
#: ``invoke-pester`` is matched case-insensitively (via the inline ``(?i:...)``
#: group, scoped to that one alternative only) because PowerShell cmdlet
#: names are case-insensitive by language design and are conventionally
#: written mixed-case (``Invoke-Pester``) unlike every other runner in this
#: set, which are lowercase-only shell command names by Unix convention.
_RUNNER_PREFILTER_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|npm|pnpm|yarn|bun|npx|jest|vitest|"
    r"mocha|jasmine|ava|cargo|nextest|go|make|tox|nox|"
    r"(?i:invoke-pester))\b"
)


#: Bounded walk-up depth for the CHEAP (stat-only, no subprocess) repo-root
#: probe ``_cheap_repo_root`` uses for the dynamic prefilter leg. A real
#: checkout is at most a handful of levels below its git root; this bound
#: exists only to keep a pathological ``cwd`` (a deeply nested non-repo
#: directory) from walking to the filesystem root one ``os.path.exists`` call
#: at a time.
_CHEAP_ROOT_WALK_MAX_DEPTH = 64

#: Command-prefix words that wrap the real runner without changing what it is.
#: Widened (2026-07-29, cross-guard fix -- code-reviewer Finding 3): `setsid`,
#: `strace`, `doas`, `busybox` were unrecognized, same gap the sibling
#: destructive-action/worktree/sentinel/commit guards fixed for their own
#: copies of this enumerated allowlist. Defined here (rather than near its
#: other use sites further below) because ``_DYNAMIC_PREFILTER_TOKEN_STOPWORDS``
#: composes it and must itself be defined before ``_tokens_from_cmd_value``'s
#: first use, below -- a prior forward reference here let a partial/reloaded
#: import of this module reach that use site before the name existed
#: (``NameError: _DYNAMIC_PREFILTER_TOKEN_STOPWORDS``); moving definition
#: order fixes the class of bug outright rather than relying on a
#: same-process, no-partial-import assumption holding forever.
_WRAPPER_WORDS = frozenset({
    "sudo", "command", "time", "exec", "nice", "nohup", "env", "ionice",
    "stdbuf", "npx", "bunx", "pnpx", "setsid", "strace", "doas", "busybox",
})

#: BX-13 (2026-07-29, confirmed live via the real dispatcher): a
#: `sh -c '<payload>'` (or `bash -c`/`zsh -c`/etc.) invocation was never
#: unwrapped -- the quoted `-c` argument tokenizes as ONE shlex word, so
#: `_base(tokens[i])` resolved to the shell interpreter itself, never the
#: real test-runner token inside it, and a subagent's `sh -c "pytest"`
#: sailed through unclassified while the wrapped command still ran the
#: whole suite for real. Same wrapper class this module's sibling guards
#: (`block_subagent_commit.py`'s `_C_FLAG_SHELL_INTERPRETERS`,
#: `dispatch_checks.py`'s `_SHELL_C_WRAPPER_INTERPRETERS`) already unwrap.
_SHELL_C_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: Interpreter/wrapper basenames that must never surface as a dynamic
#: prefilter token on their own -- ``python3 bin/run-fast-tests.py``'s
#: distinguishing token is ``run-fast-tests.py``, not ``python3``: crediting
#: the bare interpreter name would widen the dynamic leg open for almost
#: every Python invocation in a repo that happens to configure a Python-run
#: test script, which defeats the point of a PREFILTER (it is still SAFE --
#: over-widening only means "do more classification work", never a wrong
#: deny -- but it is not cheap). Reuses ``_WRAPPER_WORDS``/
#: ``_SHELL_C_INTERPRETERS`` (already-enumerated shell/wrapper names) plus
#: the python spellings, which neither set carries.
_DYNAMIC_PREFILTER_TOKEN_STOPWORDS = (
    _WRAPPER_WORDS | _SHELL_C_INTERPRETERS | frozenset({"python", "python3", "python2", "py"})
)


def _cheap_repo_root(cwd: Optional[str]) -> Optional[str]:
    """Find the nearest ancestor of ``cwd`` containing a ``.git`` entry, via
    ``os.path.exists`` only -- NEVER a ``git rev-parse`` subprocess spawn.

    This is deliberately a DIFFERENT (cheaper, less authoritative) resolver
    than ``resolve_git_root`` (which shells out to git and is what the rest
    of this guard uses once a command is already known to be worth
    classifying). The dynamic prefilter leg below runs on EVERY Bash/
    PowerShell call whose command misses the static regex -- i.e. on most
    calls this guard ever sees -- so it must not add a subprocess spawn to
    that population; see the module docstring's "hot path stays cheap"
    constraint. A stat-bounded walk-up is the cheap substitute: it is
    slightly less correct in exotic layouts (a ``.git`` FILE for a worktree
    still satisfies ``os.path.exists``, which is fine; a repo root reached
    via a symlinked ancestor might resolve to a different absolute path than
    ``git rev-parse`` would report, which is also fine here because this
    leg's ONLY job is to find ``coordinator.local.md`` -- a wrong-by-symlink
    root just means the dynamic leg misses and this call falls back to the
    static-regex-only behavior it already had, never a false deny).

    Returns ``None`` when ``cwd`` is falsy, unresolvable, or no ``.git`` is
    found within ``_CHEAP_ROOT_WALK_MAX_DEPTH`` levels.
    """
    if not cwd:
        return None
    try:
        current = os.path.abspath(cwd)
    except (OSError, ValueError):
        return None
    for _ in range(_CHEAP_ROOT_WALK_MAX_DEPTH):
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return current
        except OSError:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return None


#: Matches a ``fast_test_cmd``/``full_test_cmd`` flat frontmatter line inside
#: ``coordinator.local.md`` -- the SAME two keys ``resolve_validation_cmd.
#: cs_resolve_fast_test_cmd``/``cs_resolve_full_test_cmd`` read (their own
#: docstring's "Step 2 -- coordinator.local.md flat top-level ... key"),
#: matched here with a cheap regex instead of the real resolver's frontmatter
#: parse -- this leg only needs candidate TOKENS to widen a prefilter, not a
#: correctly-quoted, escape-aware command string, so a lightweight read is
#: the right trade (see ``_local_md_head_tokens``'s docstring for the full
#: cost argument against caching this in a second file).
_LOCAL_MD_CMD_LINE_RE = re.compile(
    r'^(?:fast_test_cmd|full_test_cmd)\s*:\s*(.+?)\s*$', re.MULTILINE
)


def _tokens_from_cmd_value(raw: str) -> List[str]:
    """Extract candidate PREFILTER tokens (lowercased basenames) from one
    configured command VALUE string (``coordinator.local.md``'s flat
    frontmatter value, or an env var's raw value) -- e.g. ``'"python3 bin/
    run-fast-tests.py"'`` -> ``["run-fast-tests.py"]``. Interpreter/wrapper
    basenames are dropped (``_DYNAMIC_PREFILTER_TOKEN_STOPWORDS``); a flag
    token (leading ``-``) is dropped too, since a bare flag is never what
    distinguishes one repo's bespoke runner from another's. Never raises --
    an unparseable ``raw`` degrades to a whitespace split, matching this
    module's other shlex-with-fallback call sites (``_tokens``)."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    try:
        parts = shlex.split(value, posix=True)
    except ValueError:
        parts = value.split()
    out: List[str] = []
    for part in parts:
        if not part or part.startswith("-"):
            continue
        base = _base(part).lower()
        if base and base not in _DYNAMIC_PREFILTER_TOKEN_STOPWORDS:
            out.append(base)
    return out


def _local_md_head_tokens(repo_root: str) -> frozenset:
    """Dynamic prefilter tokens sourced from ``<repo_root>/coordinator.local.
    md``'s ``fast_test_cmd``/``full_test_cmd`` frontmatter values.

    Deliberately does NOT invoke ``_configured_test_cmds`` (the real
    resolver -- an ``importlib`` module load plus, on the native leg, a
    dataclass-carrying module exec) or a TOML/YAML parser. This leg's only
    job is to decide whether the STATIC regex missing a runner name should
    reopen the gate; it needs candidate tokens, not a correctly-resolved
    command, and reads at most ~8 KB of one file with one regex pass to get
    them -- already the "stat plus a small read" cost floor a persistent
    per-repo cache file (mtime/size-keyed, as sketched in the dispatching
    brief) would also have to pay just to VALIDATE its cache is still fresh,
    before it could even return a cached value. Caching the OUTPUT of a
    computation that costs exactly what checking the cache's validity costs
    saves nothing and adds its own invalidation surface (a second file to
    keep in sync, a corrupt-cache read path, a cross-process write race on a
    machine running 50-70+ concurrent sessions against the same repo) for no
    measurable benefit -- rejected in favor of this direct read.

    Returns ``frozenset()`` (never raises) when the file is absent, unreadable,
    or carries neither key -- degrading this leg to "no dynamic match" only,
    never widening a deny into an unexpected allow.
    """
    path = os.path.join(repo_root, "coordinator.local.md")
    try:
        if not os.path.isfile(path):
            return frozenset()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(8192)
    except OSError:
        return frozenset()

    frontmatter = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            frontmatter = text[:end]

    tokens: List[str] = []
    for match in _LOCAL_MD_CMD_LINE_RE.finditer(frontmatter):
        tokens.extend(_tokens_from_cmd_value(match.group(1)))
    return frozenset(tokens)


def _env_head_tokens() -> frozenset:
    """Dynamic prefilter tokens sourced directly from the
    ``COORDINATOR_FAST_TEST_CMD``/``COORDINATOR_FULL_TEST_CMD`` env vars --
    ``resolve_validation_cmd``'s Step 1 for both tiers. Reading these costs
    nothing (no file I/O, no repo-root resolution), so unlike the
    ``coordinator.local.md`` leg this always runs, independent of ``cwd``."""
    tokens: List[str] = []
    for var in ("COORDINATOR_FAST_TEST_CMD", "COORDINATOR_FULL_TEST_CMD"):
        raw = os.environ.get(var)
        if raw:
            tokens.extend(_tokens_from_cmd_value(raw))
    return frozenset(tokens)


def _dynamic_prefilter_hit(cmd: str, cwd: Optional[str]) -> bool:
    """The dynamic counterpart to ``_RUNNER_PREFILTER_RE.search`` -- does
    ``cmd`` contain a token drawn from THIS repo's own configured
    ``fast_test_cmd``/``full_test_cmd`` (env var or ``coordinator.local.md``)?

    Called ONLY when the static regex already missed (see ``check()``), so
    this pays its own cost (a bounded stat-walk plus, at most, one small file
    read) exclusively on the population the static regex does not already
    resolve for free -- never doubling cost on a command the static path
    already recognized as suite-shaped.

    A hit here means only "keep evaluating" -- same contract as the static
    regex. The actual verdict is still ``_matches_configured_cmd`` (or the
    generic per-runner classifiers) against the repo's REAL resolved tier
    strings; this function never denies or allows on its own.
    """
    tokens = _env_head_tokens()
    repo_root = _cheap_repo_root(cwd)
    if repo_root:
        tokens = tokens | _local_md_head_tokens(repo_root)
    if not tokens:
        return False
    lowered = cmd.lower()
    return any(tok in lowered for tok in tokens)

#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): `nice`,
#: `ionice`, and `stdbuf` all took their OWN argument(s) (`-n 10`, `-c2`,
#: `-oL`) which were never consumed here -- only `timeout`'s duration
#: operand had special handling (see `_strip_command_prefix` below). A
#: subagent ran `ionice -c2 pytest` / `stdbuf -oL pytest` and the runner was
#: never recognized at all, so Tier T enforcement silently never applied.
#: Same flag-set `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` uses for
#: the identical gap in `check_no_verify` (no-cross-module-coupling
#: convention -- own copy here).
_WRAPPER_ARG_FLAGS = {
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
}

#: `nice`'s bare numeric niceness form (`nice -19 pytest`, no `-n`) -- see
#: `block_subagent_destructive_action.py`'s sibling copy (Finding 4) for the
#: full rationale.
_NICE_BARE_NUMERIC_RE = re.compile(r"^-\d+$")

#: Wrappers whose runner follows a literal ``run`` subcommand
#: (``poetry run pytest``, ``uv run pytest``, ``hatch run pytest``).
_RUN_SUBCOMMAND_WRAPPERS = frozenset({"poetry", "uv", "pdm", "hatch", "rye", "pipenv"})

#: pytest flags that consume a SEPARATE following token as their value. Needed
#: so a flag's value is never mistaken for a positional scope argument -- the
#: exact evasion the ``-m 'not cadence and not pending_fix'`` shape of this
#: repo's own configured command would otherwise open.
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-o", "-c", "-n", "-W", "-r",
    "--rootdir", "--junitxml", "--junit-xml", "--deselect", "--ignore",
    "--ignore-glob", "--maxfail", "--tb", "--log-level", "--log-cli-level",
    "--override-ini", "--import-mode", "--basetemp", "--capture", "--dist",
    "--numprocesses", "--cov", "--cov-report", "--cov-config", "--result-log",
    "--confcutdir", "--assert", "--doctest-glob", "--pdbcls", "--color",
})

#: pytest flags that are themselves a narrowing selection (Tier T).
_PYTEST_SCOPING_FLAGS = frozenset({
    "--lf", "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise",
    "--stepwise-skip",
})

#: JS-runner flags that narrow by test NAME rather than by path (Tier T).
_JS_SCOPING_FLAGS = frozenset({
    "-t", "--testNamePattern", "--testPathPattern", "--testPathPatterns",
    "-g", "--grep", "--spec", "--fgrep", "-f", "--shard",
})

#: ``make`` targets that are whole-suite by construction -- there is no
#: scoped form of ``make test``, so no allow-branch exists for them.
_MAKE_SUITE_TARGETS = frozenset({"test", "tests", "check", "test-all", "testall"})


# ---------------------------------------------------------------------------
# Command shredding
# ---------------------------------------------------------------------------

def _segments(cmd: str) -> List[str]:
    """Split a (possibly compound) command into independently-classifiable
    segments on ``;``/``&``/``|`` runs, mirroring ``dispatch_checks.
    _split_segments`` -- a suite invocation buried in a compound command
    (``cd x && pytest``, ``pytest | tee log``) must still be reached.

    QUOTE-BLIND by construction (a plain regex split over the raw string) --
    used ONLY as ``_segment_argvs``'s fallback for the rare command that
    fails to tokenize at all. Do not call this directly from a new
    classification leg; see ``_segment_argvs``."""
    flat = cmd.replace("\r", "").replace("\\\n", " ")
    return re.split(r"[;&|]+", flat)


def _tokens(segment: str) -> List[str]:
    """Tokenize one segment as argv. Falls back to whitespace splitting when
    the segment is not parseable as a shell word-list (unterminated quote),
    so a malformed segment still gets classified rather than silently
    skipped.

    Past `exceeds_tokenizable_ceiling` the same whitespace fallback runs. That
    ceiling is a DoS bound inherited from `_command_tokenizer`, not a local
    tuning knob.

    It does NOT bound the returned TOKENS. `str.split()` is whitespace-only,
    so a run with no internal whitespace survives as one token of up to the
    full segment length -- which is why `_strip_command_prefix` carries its
    OWN ceiling check before re-splitting a `sh -c` payload rather than
    inheriting one from here. The earlier claim that it could inherit one was
    false and is what left that site's quadratic `shlex` cost open.
    """
    if _exceeds_tokenizable_ceiling(segment):
        return segment.split()
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return segment.split()


def _segment_argvs(cmd: str, dialect: Optional[_Dialect] = None) -> List[List[str]]:
    """Split ``cmd`` into command-separator-bounded segments and return each
    segment's argv, QUOTE-AWARE -- a ``;``/``&``/``|`` character sitting
    inside a quoted string (a commit message, an echoed doc line, a ``-m``
    marker expression) is part of that string, not a segment boundary.

    Routes through the canonical shared tokenizer
    (``_command_tokenizer.tokenize_full_command`` + ``segments_from_tokens_
    simple`` -- the same primitives ``block_subagent_commit.py``,
    ``dispatch_checks.py``, and three other guards already share) rather
    than a second hand-rolled quote-scanner, per this package's
    single-classifier-implementation discipline.

    Why this replaced the former ``_segments``/``_tokens`` pairing: that
    pairing split the RAW string on a quote-blind regex first and only
    THEN shlex-tokenized each fragment, so a quoted argument merely
    containing one of ``;&|`` (``git commit -m "fix: guard prose; pytest
    coordinator_core/tests still catches real invocations"``) was cut in
    half mid-string. The second half re-tokenized (via the ``ValueError``
    whitespace-split fallback, since the fragment's quote was now
    unbalanced) into a bare argv starting with ``pytest`` -- a commit
    message QUOTING a test command was misclassified as RUNNING one. This
    was reproduced directly against ``check()`` before this fix: the
    invocation above denied for a dispatched agent with ``Directory arg:
    coordinator_core/tests`` even though no test runner was ever invoked.

    Falls back to the legacy quote-blind ``_segments``/``_tokens`` pairing
    ONLY when the whole command fails to tokenize (``tokenize_full_command``
    returns ``None`` -- a genuinely unbalanced quote or trailing backslash
    spanning the raw command), so a malformed command still gets classified
    rather than silently reporting zero segments.

    Heredoc-body stripping (2026-07-30, added alongside the shared
    tokenizer's unquoted-newline-to-``;`` pre-pass): a heredoc BODY is stdin
    DATA, never shell command text -- once bare newlines became segment
    boundaries too, a heredoc body's prose (e.g. a findings write-up whose
    text happens to start a line with ``pytest ...``) would otherwise
    fragment into its OWN segment with ``pytest`` sitting in command
    position, misclassifying prose as a live suite invocation. ``cmd`` is
    run through ``_strip_heredoc_bodies`` (the same helper
    ``block_worktree_creation.check()`` already relies on) before
    tokenizing, so a heredoc's body never reaches this classifier at all.

    ``dialect`` (Start-Process argv-reconstruction fix, 2026-08-07): when
    ``_Dialect.POWERSHELL``, segmentation is routed through
    ``_dialect.resolve_segments_for_dialect`` INSTEAD OF the bash-only
    ``tokenize_full_command`` leg below -- see that seam's own docstring
    for why: a bare ``shlex`` pass over PowerShell text fuses a quoted,
    comma-separated ``-ArgumentList`` array (``'-m','pytest'``) into ONE
    opaque token (``-m,pytest``), so `Start-Process python -ArgumentList
    '-m','pytest'` never exposed `pytest` as its own argv token to this
    classifier at all -- confirmed live: this shape ALLOWED for a resolved
    subagent while the byte-identical un-wrapped `python -m pytest` denied.
    Falls back to the SAME bash-shlex leg below when dialect resolution
    itself returns ``None`` (a genuine PowerShell parse failure -- already
    recorded SILENT by ``_dialect``'s own tokenizer), so a malformed
    PowerShell command still gets a best-effort classification rather than
    reporting zero segments. ``None``/``_Dialect.BASH`` (every pre-existing
    caller) takes the ORIGINAL bash-only path below, BYTE-IDENTICAL to
    before this parameter existed (AC4 -- zero behavior change on the bash
    leg).
    """
    cmd = _strip_heredoc_bodies(cmd)
    if dialect is _Dialect.POWERSHELL:
        segments = _resolve_segments_for_dialect(cmd, dialect, guard_name="check_test_suite_invocation")
        if segments is not None:
            return [seg for seg, _pipe_before in segments if seg]
    tokens = _tokenize_full_command(cmd)
    if tokens is not None:
        return [seg for seg in _segments_from_tokens_simple(tokens) if seg]
    out: List[List[str]] = []
    for segment in _segments(cmd):
        if not segment.strip():
            continue
        out.append(_tokens(segment))
    return out


def _base(token: str) -> str:
    """Command basename, path- and ``.exe``-stripped, so ``/usr/bin/pytest``,
    ``.venv/bin/pytest`` and ``pytest.exe`` all classify identically
    (Windows-first-class: both separators are handled)."""
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return name


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: BX-13's `sh -c` unwrap set (`_SHELL_C_INTERPRETERS`) is defined near
#: `_WRAPPER_WORDS` above, not here -- see that constant's own comment.

#: Bundled-or-standalone `-c` short flag, e.g. `-c`, `-ic`, `-ci` (2026-07-29
#: cross-guard fix -- confirmed live against THIS module too, via the same
#: real-dispatcher attack matrix that found it in `block_subagent_commit.py`
#: and `block_worktree_creation.py`, code-reviewer Finding 2): a shell's CLI
#: parser accepts bundled short flags, so `sh -ic 'pytest'` behaves as
#: `sh -i -c 'pytest'` -- the exact-offset `tokens[i + 1] == "-c"` check
#: below missed this entirely, letting a subagent's `sh -ic "pytest"` run
#: the whole suite unclassified by Tier T/U enforcement.
_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")


def _strip_command_prefix(tokens: Sequence[str]) -> List[str]:
    """Drop leading ``VAR=value`` assignments and wrapper commands so the
    returned list starts at the real runner. ``timeout``'s duration operand
    and the ``run`` subcommand of the python-launcher wrappers are consumed
    with their wrapper. A `sh -c '<payload>'`-shaped wrapper is unwrapped by
    re-tokenizing its quoted argument and recursing (see
    ``_SHELL_C_INTERPRETERS``'s BX-13 fix comment)."""
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        # BRACE-GROUPING FIX (2026-07-29, cross-guard fix -- confirmed live
        # against THIS module via the same real-dispatcher attack matrix
        # that found the identical gap in the sibling destructive-action/
        # commit/sentinel/worktree guards, code-reviewer Finding 1): `{
        # pytest; }` was never peeled here, so the resolved runner token was
        # `{` itself and Tier T/U enforcement never applied. Bash requires a
        # space after `{` (a reserved word, not an operator), so
        # `shlex.split` always yields it as its own token.
        if tok == "{":
            i += 1
            continue
        # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
        # confirmed live): `( pytest; )` has the exact same shape as the
        # brace fix directly above -- `(` falls out as its own token from
        # the shared tokenizer for the same reason `{` does, and was never
        # peeled here either.
        if tok == "(":
            i += 1
            continue
        if _ENV_ASSIGN_RE.match(tok):
            i += 1
            continue
        base = _base(tok)
        if base == "timeout":
            i += 1
            if i < n and re.match(r"^\d+(\.\d+)?[smhd]?$", tokens[i]):
                i += 1
            continue
        if base in _WRAPPER_WORDS:
            i += 1
            taking = _WRAPPER_ARG_FLAGS.get(base)
            if taking:
                while i < n:
                    t = tokens[i]
                    if t in taking:
                        i += 1
                        if i < n:
                            i += 1  # separate-token value, e.g. `-c 2`
                        continue
                    if len(t) > 2 and t[0] == "-" and t[1] != "-" and ("-" + t[1]) in taking:
                        i += 1  # attached-value short flag, e.g. -c2 / -oL
                        continue
                    if t.startswith("--") and "=" in t:
                        i += 1
                        continue
                    if base == "nice" and _NICE_BARE_NUMERIC_RE.match(t):
                        i += 1
                        continue
                    break
            continue
        if base in _WITH_SUITE_MUTEX_BASENAMES:
            # ``with-suite-mutex -- <real command>`` -- strip the wrapper
            # itself AND its ``--`` separator so the classifier below sees
            # the SAME argv it would see unwrapped (the WRAPPER leg above
            # already knows how to tell wrapped from bare; this is what lets
            # a wrapped command still classify as suite-shaped at all,
            # rather than reading "with-suite-mutex" as an unrecognized
            # runner and falling through as an allow).
            i += 1
            if i < n and tokens[i] == "--":
                i += 1
            continue
        if base in _RUN_SUBCOMMAND_WRAPPERS:
            i += 1
            if i < n and tokens[i] == "run":
                i += 1
            continue
        if base in _SHELL_C_INTERPRETERS:
            c_flag_positions = [
                j for j in range(i + 1, n) if _BUNDLED_C_FLAG_RE.match(tokens[j])
            ]
            if c_flag_positions:
                idx = c_flag_positions[0]
                if idx + 1 < n:
                    payload = tokens[idx + 1]
                    if _exceeds_tokenizable_ceiling(payload):
                        # `_tokens`'s over-ceiling fallback is `segment.split()`
                        # -- whitespace-only, so it bounds the SEGMENT, never
                        # the token: a run with no internal whitespace survives
                        # whole and lands here at full length. `shlex.split` on
                        # it is the quadratic `read_token` cost the ceiling
                        # exists to bound (measured at HEAD, through `check()`:
                        # 200 KB -> 0.8 s, 800 KB -> 15.4 s), reachable from raw
                        # Bash `tool_input`.
                        #
                        # Fail direction, measured rather than assumed: this
                        # site's sibling `ValueError` branch (`break`) is
                        # fail-OPEN for detection -- it leaves the interpreter
                        # in command position, so nothing classifies. Routing
                        # the ceiling here would have flipped a live DENY to an
                        # allow: `bash -c '<200 KB>/pytest'` is one over-ceiling
                        # token whose basename IS `pytest`, and denies today.
                        # An over-ceiling token has no internal whitespace by
                        # construction (see above), so quote-stripping and
                        # whitespace-splitting is linear, is a no-op on shape,
                        # and keeps every runner basename `shlex.split` would
                        # have exposed here visible to the classifier.
                        inner = payload.strip("'\"").split()
                    else:
                        try:
                            inner = shlex.split(payload, posix=True)
                        except ValueError:
                            break
                    return _strip_command_prefix(inner)
        break
    return list(tokens[i:])


# ---------------------------------------------------------------------------
# testpaths resolution -- the "scoped-looking but actually the whole suite" leg
# ---------------------------------------------------------------------------

def _norm_path(raw: str) -> str:
    """Normalize a path argument for testpaths comparison: forward slashes,
    no ``./`` prefix, no trailing separator."""
    p = raw.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p


def _read_testpaths(repo_root: Optional[str]) -> List[str]:
    """Configured pytest ``testpaths`` roots for ``repo_root``.

    A path argument equal to -- or an ancestor of -- one of these is NOT a
    real scope: ``pytest coordinator_core/`` is this repo's entire suite. Reads
    ``pyproject.toml`` (``[tool.pytest.ini_options]``) and ``pytest.ini``
    (``[pytest]``); returns ``[]`` when neither is readable, which degrades
    only the ancestor test, never the rest of the classifier.
    """
    if not repo_root:
        return []
    found: List[str] = []

    pyproject = os.path.join(repo_root, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            import tomllib

            with open(pyproject, "rb") as fh:
                data = tomllib.load(fh)
            raw = data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("testpaths")
            if isinstance(raw, str):
                found.extend(raw.split())
            elif isinstance(raw, list):
                found.extend(str(x) for x in raw)
        except Exception:
            pass

    for ini_name in ("pytest.ini", "tox.ini", "setup.cfg"):
        ini_path = os.path.join(repo_root, ini_name)
        if not os.path.isfile(ini_path):
            continue
        try:
            parser = configparser.ConfigParser()
            parser.read(ini_path, encoding="utf-8")
            for section in ("pytest", "tool:pytest"):
                if parser.has_option(section, "testpaths"):
                    found.extend(parser.get(section, "testpaths").split())
        except Exception:
            pass

    return [_norm_path(p) for p in found if _norm_path(p)]


def _is_real_scope(arg: str, testpaths: Sequence[str], cwd: Optional[str]) -> bool:
    """Does this positional argument narrow the run to something smaller than
    the suite?

    True for a node id (``path::test_name``) or a path that is a strict
    DESCENDANT of the configured testpaths roots. False for ``.``, for a
    testpaths root itself, and for any ancestor of one -- those select the
    whole suite while wearing a scope's clothing.
    """
    token = arg.replace("\\", "/")
    norm = _norm_path(token.split("::", 1)[0])
    if not norm or norm in (".", ".."):
        return False

    for tp in testpaths:
        if norm == tp:
            return False
        if tp == norm or tp.startswith(norm + "/"):
            return False

    if "::" in token:
        return True
    if "/" in norm or norm.endswith(".py"):
        return True
    # A bare name with no separator is a scope only if it actually names
    # something on disk (a `tests` directory in a repo without a testpaths
    # pin); otherwise it is far more likely a mis-parsed flag operand, and
    # crediting it as a scope is exactly how this guard gets evaded.
    if cwd:
        try:
            return os.path.exists(os.path.join(cwd, norm))
        except OSError:
            return False
    return False


# ---------------------------------------------------------------------------
# Per-runner classifiers -- each returns a human-readable runner label when
# the invocation is SUITE-shaped, else None.
# ---------------------------------------------------------------------------

def _walk_pytest_args(args: Sequence[str]) -> Tuple[bool, List[str]]:
    """Split a pytest argv's arguments into (scoping-flag-seen, positionals).

    Single-sourced so the two consumers that need pytest's flag-operand
    grammar -- ``_classify_pytest`` (is this suite-shaped?) and
    ``_pytest_directory_args`` (DR-088 R9's subagent precision leg) -- walk it
    identically. A second hand-rolled walk would drift on the next flag-table
    edit and silently mis-read an operand as a positional, which on the R9 leg
    means denying a command over a ``--rootdir`` value the caller never
    intended as a scope.
    """
    scoped = False
    positionals: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        if arg == "--":
            i += 1
            continue
        if arg.startswith("-") and arg != "-":
            name = arg.split("=", 1)[0]
            if name == "-k":
                scoped = True
                i += 2 if "=" not in arg else 1
                continue
            if name in _PYTEST_SCOPING_FLAGS:
                scoped = True
                i += 1
                continue
            if "=" in arg:
                i += 1
                continue
            if name in _PYTEST_VALUE_FLAGS:
                i += 2
                continue
            # Attached short-option value (`-kfoo`, `-mfoo`, `-n4`).
            if not arg.startswith("--") and len(arg) > 2 and ("-" + arg[1]) in _PYTEST_VALUE_FLAGS:
                if arg[1] == "k":
                    scoped = True
                i += 1
                continue
            i += 1
            continue
        positionals.append(arg)
        i += 1
    return scoped, positionals


def _classify_pytest(args: Sequence[str], testpaths: Sequence[str],
                     cwd: Optional[str], label: str) -> Optional[str]:
    scoped, positionals = _walk_pytest_args(args)
    if not scoped:
        scoped = any(_is_real_scope(a, testpaths, cwd) for a in positionals)
    return None if scoped else label


def _classify_unittest(args: Sequence[str]) -> Optional[str]:
    """``python -m unittest`` with no target, or with ``discover``, walks the
    whole tree. A dotted target (``pkg.mod.TestCase.test_x``) is Tier T."""
    positional = [a for a in args if not a.startswith("-")]
    if not positional or positional[0] == "discover":
        return "python -m unittest"
    return None


def _classify_python_module(tokens: Sequence[str], testpaths: Sequence[str],
                            cwd: Optional[str]) -> Optional[str]:
    """Classify ``python[3] [pyflags] -m <module> [args]``. The FIRST ``-m``
    is the interpreter's module flag (python stops parsing its own options
    there); any later ``-m`` belongs to the module and is handled by that
    module's own classifier."""
    try:
        idx = list(tokens).index("-m")
    except ValueError:
        return None
    if idx + 1 >= len(tokens):
        return None
    module = tokens[idx + 1]
    rest = tokens[idx + 2:]
    if module in ("pytest", "py.test"):
        return _classify_pytest(rest, testpaths, cwd, "python -m pytest")
    if module in ("unittest", "nose2"):
        return _classify_unittest(rest)
    return None


def _classify_js_runner(args: Sequence[str], testpaths: Sequence[str],
                        cwd: Optional[str], label: str) -> Optional[str]:
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in _JS_SCOPING_FLAGS:
            return None
    positional = [a for a in args if not a.startswith("-") and a != "--"]
    # `vitest run` / `jest --ci` style subcommands are not scopes.
    positional = [a for a in positional if a not in ("run", "watch", "related")]
    for arg in positional:
        if _is_real_scope(arg, testpaths, cwd):
            return None
    return label


def _package_manager_test_shape(args: Sequence[str]) -> bool:
    """Is ``args`` one of the recognized package-script test-invocation
    shapes (``test``, ``t``, ``run test``, ``run-script test``) -- as
    opposed to some other package-manager subcommand (``install``, ``run
    build``, ``exec vitest``) this classifier has no opinion on at all?

    Single-sourced so ``_classify_package_manager`` (is this invocation
    unscoped -- always True once this shape matches, see that function's own
    docstring) and ``_runner_recognized`` (did a classifier actually reach a
    verdict, as opposed to bailing out on an unrecognized shape) can never
    drift apart -- same discipline as ``_cargo_test_shape``/``_go_test_shape``/
    ``_make_first_target``."""
    if not args:
        return False
    head = args[0]
    if head in ("test", "t"):
        return True
    if head in ("run", "run-script") and len(args) > 1 and args[1] == "test":
        return True
    return False


def _classify_package_manager(base: str, args: Sequence[str],
                              testpaths: Sequence[str], cwd: Optional[str]) -> Optional[str]:
    """``npm test`` / ``npm run test`` / ``yarn test`` / ``pnpm test`` /
    ``bun test`` -- and their ``run test`` / ``run-script test`` / ``t``
    spellings.

    A **package-script** invocation (``npm test``, ``npm t``, ``npm run
    test``, ``npm run-script test``, and the same four shapes for ``pnpm``
    and ``yarn``) is unscoped REGARDLESS OF TRAILING ARGS. argv scope does
    not predict execution scope once a package-manager arg-forwarding layer
    sits between the caller and the actual runner: the package manager is
    under no obligation to forward args past ``--`` to the script it runs.
    Measured by example-cockpit-repo-em on pnpm v11 with ``"test": "vitest
    run"`` (``cross-repo/inbox/2026-07-30-example-cockpit-repo-em-tier-u-guard-
    bypassed-by-pnpm-arg-forwarding.md``):

      pnpm run test -- <9 explicit .test.ts paths>              -> 642 files / 8,346 tests
      pnpm run test -- src/lib/data/leak-suite.test.ts (1 file) -> 643 files / 8,359 tests

    Both invocations ran the WHOLE suite -- pnpm v11 drops everything past
    ``--`` for this script shape rather than forwarding it to ``vitest``.
    Handing the trailing args to ``_classify_js_runner`` (the prior
    behaviour) found a real path scope in each case and returned ``None``,
    so a nine-path invocation and a one-path invocation both tiered as
    Tier T and both ran unscoped. This is the same class as DR-088's R9 --
    a run that NAMES a narrow scope but COVERS a wide one -- in its
    arg-forwarding variant; DR-088's Tier-T definition is unchanged, only
    claude-klabauter's scope DERIVATION for this shape changed. Not a new DR-088
    ruling; do not read this as "R10".

    Negative spec: resolving ``package.json``'s ``scripts.<name>`` and
    tiering on the RESOLVED runner invocation does NOT fix this -- it would
    tier ``vitest run <paths>`` as scoped, which is precisely the wrong
    verdict, because the paths never reach ``vitest`` in the first place.

    Carve-out -- ``bun test`` stays scope-aware: it invokes bun's OWN
    built-in test runner, not ``package.json``'s ``test`` script, so its
    args go straight to the runner with no forwarding layer to lose them
    across. Only bare ``bun test``/``bun t`` keeps routing through
    ``_classify_js_runner``; ``bun run test``/``bun run-script test`` (the
    script form) takes the unconditional-unscoped path like the other three
    package managers.
    """
    if not _package_manager_test_shape(args):
        return None
    head = args[0]
    label = "%s test" % base
    if head in ("test", "t"):
        if base == "bun":
            return _classify_js_runner(args[1:], testpaths, cwd, label)
        return label
    return label  # ("run"|"run-script") "test" -- the only remaining recognized shape


#: Package-manager bases whose ``test``/``run test`` script shapes
#: ``_classify_package_manager`` now returns unconditionally (2026-07-30
#: package-script arg-forwarding fix, see that function's docstring).
_PM_BASES = ("npm", "pnpm", "yarn", "bun")


def _is_package_script_label(detected: str) -> bool:
    """Is ``detected`` one of ``_classify_package_manager``'s labels for a
    package-script shape (``"npm test"``, ``"pnpm test"``, ...)? Used to
    decide whether a deny/remediation text should append the extra
    package-script-specific offer paragraph (call the runner directly --
    that IS scope-bearing, unlike the package-script form it was denied
    for)."""
    parts = detected.split()
    return len(parts) == 2 and parts[0] in _PM_BASES and parts[1] == "test"


#: The package-script-specific offer paragraph, appended to a deny or
#: remediation text only when ``_is_package_script_label`` holds. One
#: definition, two consumers (``_deny_reason_subagent`` and
#: ``_remediation_text``) -- the two copies must never drift, since the whole
#: point is that a caller denied in either path is told the same
#: scope-bearing alternative.
_PACKAGE_SCRIPT_OFFER = (
    "\n\nA package-script invocation is unscoped here no matter what "
    "follows `--`: the package manager is not obliged to forward "
    "those args to the runner, and pnpm v11 does not — `pnpm run "
    "test -- one_file.test.ts` runs the WHOLE suite. Call the "
    "runner directly, which IS scope-bearing:\n"
    "  pnpm exec vitest run src/thing.test.ts\n"
    "  npx jest src/thing.test.js"
)


def _cargo_test_shape(args: Sequence[str]) -> Optional[Sequence[str]]:
    """Is ``args`` a recognized ``cargo test``/``cargo nextest run`` shape at
    all -- as opposed to some other cargo subcommand (``build``, ``watch``,
    ``run``) this classifier has no opinion on?

    Returns the argv remainder past the recognized head (``test`` or
    ``nextest run``) when it is, else ``None``. Single-sourced so
    ``_classify_cargo`` (is this invocation SCOPED) and ``_runner_recognized``
    (did a classifier actually reach a scoped/unscoped verdict, as opposed to
    bailing out on an unrecognized shape) can never drift apart -- see
    ``_runner_recognized``'s own docstring for why the two questions must be
    asked separately. ``cargo watch -x test`` is the shape this closes: its
    ``args[0]`` is ``watch``, not ``test``/``nextest``, so this returns
    ``None`` and the caller must not treat that as "confirmed scoped."""
    if not args:
        return None
    if args[0] == "test":
        return args[1:]
    if args[0] == "nextest" and len(args) > 1 and args[1] == "run":
        return args[2:]
    return None


def _classify_cargo(args: Sequence[str]) -> Optional[str]:
    """``cargo test`` / ``cargo nextest run``. A filter positional (or an
    explicit ``--test``/``-p`` target) before the ``--`` harness separator is
    Tier T; everything else is the crate's whole test surface."""
    rest = _cargo_test_shape(args)
    if rest is None:
        return None
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg == "--":
            break
        if arg.startswith("-"):
            name = arg.split("=", 1)[0]
            if name in ("--test", "--bench", "--example", "-p", "--package", "-E", "--filter-expr"):
                return None
            if "=" not in arg and name in ("--features", "--manifest-path", "--target", "--profile", "-j", "--jobs"):
                i += 2
                continue
            i += 1
            continue
        return None  # a bare filter positional narrows the run
    return "cargo test"


def _go_test_shape(args: Sequence[str]) -> Optional[Sequence[str]]:
    """Is ``args`` a recognized ``go test`` shape -- as opposed to some other
    ``go`` subcommand (``vet``, ``build``, ``run``) this classifier has no
    opinion on? Returns the argv remainder past ``test`` when it is, else
    ``None``. Single-sourced with ``_classify_go`` and ``_runner_recognized``
    for the same reason ``_cargo_test_shape`` is -- see that function's
    docstring."""
    if not args or args[0] != "test":
        return None
    return args[1:]


def _classify_go(args: Sequence[str]) -> Optional[str]:
    """``go test``. A recursive package pattern (``./...``, ``all``) is the
    whole module; a named package is Tier T, and so is bare ``go test`` (which
    runs only the current directory's package). ``-run <regex>`` narrows."""
    rest = _go_test_shape(args)
    if rest is None:
        return None
    i = 0
    recursive = False
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("-"):
            name = arg.split("=", 1)[0]
            if name in ("-run", "-bench", "-testify.m"):
                return None
            if "=" not in arg and name in ("-timeout", "-count", "-parallel", "-tags", "-covermode", "-coverprofile", "-p"):
                i += 2
                continue
            i += 1
            continue
        if "..." in arg or arg == "all":
            recursive = True
        i += 1
    return "go test ./..." if recursive else None


def _make_first_target(args: Sequence[str]) -> Optional[str]:
    """The first non-flag, non-assignment positional in a make invocation --
    make's target, by construction -- or ``None`` if there isn't one. Single-
    sourced so ``_classify_make`` and ``_runner_recognized`` never re-derive
    (and drift on) the flag/assignment-skipping walk; see the module-level
    rationale in ``_classify_make``'s own docstring for why only the FIRST
    positional counts."""
    for arg in args:
        if arg.startswith("-"):
            continue
        if "=" in arg:
            continue
        return arg
    return None


def _classify_make(args: Sequence[str]) -> Optional[str]:
    """``make test`` / ``make -j4 test`` / ``make CC=gcc check``.

    The suite target must be the FIRST non-flag, non-assignment positional
    -- ``make`` is the only prefilter runner that is also an ordinary
    English verb ("make the exemplar useless"), so scanning every arg for a
    ``_MAKE_SUITE_TARGETS`` member (rather than stopping at the first
    positional) let arbitrary later prose ("... which branch the test
    covers.") satisfy the match. A make invocation's first positional IS
    its target by construction; anything else on the line is not make's
    concern and must not be scanned.
    """
    target = _make_first_target(args)
    if target is None:
        return None
    if target in _MAKE_SUITE_TARGETS:
        return "make %s" % target
    return None


#: ``tox``/``nox`` environment-runner bases -- see ``_classify_tox_nox``.
_TOX_NOX_BASES = frozenset({"tox", "nox"})


def _classify_tox_nox(base: str, args: Sequence[str]) -> str:
    """``tox`` / ``nox`` -- always unscoped, regardless of arguments.

    DR-088 test-suite-invocation-guard spelling-keyed-leg bypass, found
    2026-08-03 while reconstructing the relayed finding
    ``2026-08-01-example-retrieval-repo-em-test-suite-approval-gate-bypass.md`` (not
    present in this tree; relayed via
    ``cross-repo/inbox/2026-08-03-example-doctrine-repo-em-klabauter-rows-move-to-you-
    pm-ruled.md``'s sibling memo chain). Neither runner was in
    ``_RUNNER_PREFILTER_RE`` at all, so a bare ``tox`` or ``nox`` invocation
    -- each of which, by design, walks EVERY configured environment/session
    and therefore the whole test surface -- skipped this guard entirely: no
    classification, no subagent identity deny, no Tier-U grant check. Both
    the Layer-3 identity leg and the Tier-F/U grant leg are bypassed by the
    same gap, since ``check()`` never reaches either leg for a command
    ``_RUNNER_PREFILTER_RE`` does not match.

    Unlike the package-manager family (``_classify_package_manager``), no
    scoped branch is offered here even for ``-e``/``-s`` (environment/
    session selection) or a ``--`` ``posargs`` tail. Two independent
    reasons, either decisive:

    - An environment/session name is not a file, directory, or node-id --
      DR-088's Tier T definition is what the caller "authored or touched",
      and neither ``tox -e py311`` nor ``nox -s tests`` names any such
      thing. Selecting an environment narrows WHICH interpreter/deps run
      the suite, not WHAT PORTION of the suite runs.
    - ``posargs``/``session.posargs`` forwarding to the underlying runner is
      an opt-in each ``tox.ini``/``noxfile.py`` may or may not wire up --
      the same unverified-forwarding-reliability risk
      ``_classify_package_manager``'s 2026-07-30 pnpm fix closed for
      package-manager scripts. Trusting a ``--`` tail here would reopen that
      exact class for a runner this repo cannot introspect the config of at
      the guard layer.

    A repo needing a genuinely scoped tox/nox run should invoke the
    underlying test runner directly (``pytest tests/test_x.py``), which
    stays scope-aware through the ordinary pytest leg."""
    return base


#: ``Invoke-Pester`` parameters that narrow the run below the whole
#: configured test surface -- a name/tag filter (Tier T by the same logic
#: ``_JS_SCOPING_FLAGS`` uses: it narrows by NAME, not by path) or an
#: explicit ``-Path``/``-Script`` target. Matched case-insensitively
#: (PowerShell parameter binding is case-insensitive), same as the runner
#: name itself in ``_RUNNER_PREFILTER_RE``.
_PESTER_SCOPING_FLAGS = frozenset({
    "-testname", "-fullnamefilter", "-tag", "-tagfilter",
})
_PESTER_PATH_FLAGS = frozenset({"-path", "-script"})


def _pester_path_values(args: Sequence[str]) -> List[str]:
    """Values bound to ``-Path``/``-Script`` across ``args``, comma-split.

    Both the colon-bound form (``-Path:foo,bar``) and the separate-token
    form (``-Path foo,bar``) are PowerShell-legal parameter binding for a
    ``[string[]]``-typed parameter, and Pester's own ``-Path`` is exactly
    that type -- both a comma-separated list AND repeated ``-Path``
    occurrences accumulate, mirroring how PowerShell itself binds a
    string-array parameter from the command line. Every occurrence is
    collected, not just the first, so ``-Path a -Path b`` and
    ``-Path a,b`` classify identically."""
    values: List[str] = []
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        if ":" in arg:
            name, _, val = arg.partition(":")
            if name.lower() in _PESTER_PATH_FLAGS and val:
                values.extend(v for v in val.split(",") if v)
            i += 1
            continue
        if arg.lower() in _PESTER_PATH_FLAGS:
            if i + 1 < n:
                values.extend(v for v in args[i + 1].split(",") if v)
                i += 2
                continue
        i += 1
    return values


def _classify_pester(args: Sequence[str], cwd: Optional[str]) -> Optional[str]:
    """``Invoke-Pester`` with no target runs every ``*.tests.ps1`` Pester
    discovers under the current directory -- the PowerShell-native
    equivalent of a bare ``pytest``. A name/tag filter (``-TestName``,
    ``-FullNameFilter``, ``-Tag``, ``-TagFilter``) narrows the run and is
    Tier T unconditionally, same as ``_classify_js_runner``'s own scoping
    flags: presence of the flag is what matters, Pester resolves the name
    match itself.

    ``-Path``/``-Script`` is different: unlike a name filter, a bare
    PRESENCE check cannot tell a genuinely scoped file target apart from a
    directory target that reaches the same breadth as no ``-Path`` at all --
    Pester has no ``testpaths``-equivalent this classifier can read the way
    ``_is_real_scope`` reads pytest's, so there is no ancestor test available
    to credit a directory as a bounded partial scope the way pytest's own
    directory positionals are credited. Reuses the SAME directory-on-disk
    primitive DR-088 R9's pytest leg (``_pytest_directory_args``) already
    established (``_norm_path`` + ``os.path.isdir``) rather than forking a
    second directory-detection rule: a ``-Path``/``-Script`` value naming a
    directory on disk is treated as NOT a real scope and this returns
    ``"Invoke-Pester"`` (suite-shaped) exactly as the no-target case does,
    which is what lets ``check()``'s ordinary identity/grant legs deny it for
    both a subagent (identity leg) and an ungranted top-level EM (Tier U via
    ``_classify_command_core``) without a Pester-specific R9 precision leg --
    see the module-docstring R9 note on why this leg is deliberately scoped
    to the pytest family and not widened here. A value naming a FILE, or a
    value this can't resolve (no ``cwd``, or nothing on disk at that path),
    fails open and is credited as scope, mirroring
    ``_pytest_directory_args``'s own no-``cwd`` fail-open discipline and
    ``_is_real_scope``'s no-disk-check-needed treatment of file positionals."""
    i = 0
    n = len(args)
    while i < n:
        arg = args[i]
        name = arg.split(":", 1)[0].lower()
        if name in _PESTER_SCOPING_FLAGS:
            return None
        i += 1

    path_values = _pester_path_values(args)
    if not path_values:
        return "Invoke-Pester"
    if not cwd:
        return None

    for raw in path_values:
        norm = _norm_path(raw.replace("\\", "/"))
        if not norm:
            continue
        try:
            if os.path.isdir(os.path.join(cwd, norm)):
                return "Invoke-Pester"
        except OSError:
            continue
    return None


def _classify_tokens(tokens: Sequence[str], testpaths: Sequence[str],
                     cwd: Optional[str]) -> Optional[str]:
    """Classify one already-prefix-stripped argv. Returns a runner label when
    the invocation is suite-shaped, else ``None``."""
    if not tokens:
        return None
    base = _base(tokens[0])
    args = tokens[1:]

    if base in ("pytest", "py.test"):
        return _classify_pytest(args, testpaths, cwd, "pytest")
    if base in ("python", "python2", "python3", "py") or re.match(r"^python3?\.\d+$", base):
        return _classify_python_module(tokens, testpaths, cwd)
    if base in ("npm", "pnpm", "yarn", "bun"):
        return _classify_package_manager(base, args, testpaths, cwd)
    if base in ("jest", "vitest", "mocha", "jasmine", "ava"):
        return _classify_js_runner(args, testpaths, cwd, base)
    if base == "cargo":
        return _classify_cargo(args)
    if base == "go":
        return _classify_go(args)
    if base in ("make", "gmake"):
        return _classify_make(args)
    if base in _TOX_NOX_BASES:
        return _classify_tox_nox(base, args)
    if base.lower() == "invoke-pester":
        return _classify_pester(args, cwd)
    return None


# ---------------------------------------------------------------------------
# Configured fast_test_cmd / full_test_cmd segment-set containment
# ---------------------------------------------------------------------------

#: Whitespace-run collapse applied ONLY to argv tokens being compared for
#: configured-command containment matching (never to the raw argv used by
#: the shape classifiers above, which must see a flag's exact operand).
#: ``shlex`` preserves internal whitespace verbatim inside a quoted token
#: (``'not  cadence'`` survives as a two-space token, distinct from ``'not
#: cadence'``), so a configured ``fast_test_cmd`` reproduced with incidental
#: extra inner whitespace around a ``-m``/``-k`` expression would otherwise
#: fail tuple equality even though shlex already normalized away the
#: quoting-STYLE difference (single vs double quotes shlex-split
#: identically). This closes that residual gap: two runs of whitespace
#: collapse to one, and leading/trailing whitespace inside a token is
#: stripped, before the token enters a comparison tuple.
_WS_RUN_RE = re.compile(r"\s+")


def _norm_arg(tok: str) -> str:
    """Collapse whitespace runs in ``tok`` for containment-match comparison
    only -- see ``_WS_RUN_RE``'s docstring. Applied uniformly to both the
    configured command's own tokens (``_normalized_segments``) and the
    invocation's tokens (``_matches_configured_cmd``, ``_classify_command_
    core``) so both sides of every containment comparison go through the
    identical normalization -- comparing an un-normalized side against a
    normalized one would silently reintroduce the exact whitespace-
    sensitivity this exists to remove."""
    return _WS_RUN_RE.sub(" ", tok).strip()


def _normalized_segments(cmd: str) -> List[Tuple[str, ...]]:
    """Segment-argv form of a configured command, for containment matching.

    Each ``;``/``&``/``|``-separated segment of ``cmd`` is reduced to
    prefix-stripped argv with the leading interpreter/runner basename-
    reduced (``/repo/.venv/bin/python -m pytest …`` and ``python3 -m pytest
    …`` normalize identically) and each argument whitespace-collapsed
    (``_norm_arg``), mirroring the per-segment normalization ``check()``
    applies to the invocation it is matching against.

    A configured ``fast_test_cmd``/``full_test_cmd`` may itself be a chained
    invocation (``pnpm run typecheck && pnpm run test``), which can never
    equal any SINGLE invocation segment under whole-string equality -- that
    was the bug (cross-repo/inbox/2026-07-25-example-cockpit-repo-em-tier-f-
    escape-hatch-unreachable-for-chained-fast-test-cmd.md). Splitting the
    configured string the same way an invocation is split lets the caller
    test containment (every configured segment present somewhere in the
    invocation) instead of impossible whole-string equality. For a
    single-segment configured command this degrades to a one-element list
    -- verbatim-equality semantics, unchanged."""
    out: List[Tuple[str, ...]] = []
    for argv in _segment_argvs(cmd):
        tokens = _strip_command_prefix(argv)
        if not tokens:
            continue
        out.append((_norm_head(tokens[0]),) + tuple(_norm_arg(t) for t in tokens[1:]))
    return out


#: Interpreter basenames that name the SAME interpreter for containment
#: purposes. ``_base`` alone is not enough here: the resolver normalizes a
#: bare ``python`` token to the interpreter it actually found on PATH
#: (``resolve_python_interp``), so a repo that DECLARES ``python -m pytest
#: …`` resolves to ``python3 -m pytest …`` -- while the operator types the
#: declared form. Comparing those basenames as distinct strings makes the
#: configured-command leg miss the repo's own declared suite command, which
#: is the one command it exists to catch.
_PYTHON_HEAD_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)


def _norm_head(token: str) -> str:
    """Interpreter/runner head token, reduced for containment matching:
    ``_base`` (path- and ``.exe``-stripped) plus python-family collapsing.

    Used on BOTH sides of every containment comparison -- normalizing one
    side only would reintroduce the mismatch this removes.
    """
    name = _base(token)
    if _PYTHON_HEAD_RE.match(name):
        return "python"
    return name


def _segment_contains(cfg_seg: Sequence[str], inv_seg: Sequence[str]) -> bool:
    """Does one invocation segment carry everything a configured segment
    names -- same interpreter/runner head, and every configured argument
    present among the invocation's own arguments?

    Argument-level CONTAINMENT, not tuple equality. An operator types the
    declared command plus incidentals -- ``-q``, ``-x``, ``--tb=short``,
    ``2>&1`` -- and under equality every one of those spellings slipped the
    leg entirely, leaving the repo's own full-suite command ungated because
    it was quiet about it. Extra tokens widen or decorate a run; they never
    narrow it below the configured breadth -- EXCEPT when ``cfg_seg`` itself
    carries no arguments at all (see below), so a superset of a genuinely
    argument-bearing configured segment is still that tier's breadth and is
    classified as that tier.

    A genuinely narrower invocation is unaffected: ``pytest
    coordinator/tests/test_foo.py`` does not carry the configured
    ``coordinator/tests`` token at all, so it does not match and stays
    Tier T.

    Review: code-reviewer — a zero-argument configured segment (a bare
    ``fast_test_cmd: pytest`` declaration) constrains nothing, so
    ``set().issubset(x)`` was vacuously True for EVERY invocation of that
    runner, including a genuinely narrower one (``pytest
    coordinator/tests/test_foo.py::test_bar``). That is a real narrowing,
    not a widening -- the containment claim above breaks precisely when
    there is nothing left to widen. Returning ``False`` here loses no
    coverage: an unscoped invocation of the same runner is already
    classified Tier U by the generic shape classifier (``_classify_tokens``)
    regardless of this leg.
    """
    if not cfg_seg or not inv_seg:
        return False
    if cfg_seg[0] != inv_seg[0]:
        return False
    if not cfg_seg[1:]:
        return False
    return set(cfg_seg[1:]).issubset(set(inv_seg[1:]))


def _cfg_segments_satisfied(cfg_segments: Sequence[Tuple[str, ...]],
                            invocation_segments: Sequence[Tuple[str, ...]],
                            *, exact: bool = False) -> bool:
    """Does EVERY one of a configured tier's own segments (``cfg_segments``,
    one command's ``_normalized_segments``) appear somewhere in
    ``invocation_segments``?

    Review: code-reviewer — factored out of ``_matches_configured_cmd`` and
    ``_classify_command_core``, which independently built the identical
    ``all(any(_segment_contains ...))`` shape; the module's own stated
    principle is that ``check()`` and the public ``classify_command`` API
    run through the SAME implementation, and this containment step is now
    the single place both call sites use, matching how ``_segment_contains``
    itself was already extracted.

    ``exact`` selects tuple equality (``_matches_declared_fast_test_cmd``'s
    R6 authority-widening exit, which must keep exact-equality semantics)
    over the default containment (every classification leg).

    Review: code-reviewer (2026-08-04, Finding 1, tierf-s2-guards) -- the
    ``exact`` branch used to be one-directional (every CFG segment present
    somewhere in the invocation), never checking the reverse: an invocation
    carrying an EXTRA segment beyond the declared command's own segments
    (a chained ``<declared bare fast_test_cmd> && <a different, scoped
    segment>``) still satisfied this test, because nothing here rejected
    the extra segment riding along. ``_matches_declared_fast_test_cmd`` is
    the ONLY caller of ``exact=True`` (the default-containment callers are
    untouched by this addition), and it is an AUTHORITY-widening exit: the
    declaration speaks for the literal declared string and nothing else, so
    the invocation's segment SET must equal the declared command's segment
    set, not merely contain it. A legitimately CHAINED declared
    ``fast_test_cmd`` (both of ITS OWN segments declared together) still
    passes, because every invocation segment is one of ``cfg_segments``;
    only a segment absent from the declaration fails the new check.

    This SET-EQUALITY framing is deliberate, not merely one-directional-plus-
    a-length-check: the original one-directional containment here was added
    2026-07-25 to fix a structural lockout for a sibling repo whose OWN
    declared ``fast_test_cmd`` is itself chained (``pnpm run typecheck &&
    pnpm run test``) -- see
    ``cross-repo/archive/2026-07-25-example-cockpit-repo-em-tier-f-escape-hatch-
    unreachable-for-chained-fast-test-cmd.md`` (status actioned, realized_by
    8d94ebb9). A single-segment restriction on the R6 exit would rebreak
    that repo (its declared command can never satisfy a "one segment only"
    rule). Set equality satisfies BOTH constraints at once: the declared
    set is still fully present (cockpit's fix survives) AND no segment
    outside that set is tolerated (this diff's bypass closes).

    An empty ``cfg_segments`` must never satisfy -- the empty set is
    vacuously "present" in everything, which would turn an
    unresolvable/blank configured command into a match for ANY invocation.
    """
    if not cfg_segments:
        return False
    satisfied = all(
        any(
            seg == inv if exact else _segment_contains(seg, inv)
            for inv in invocation_segments
        )
        for seg in cfg_segments
    )
    if not satisfied:
        return False
    if exact and not all(inv in cfg_segments for inv in invocation_segments):
        return False
    return True


class ConfiguredCmd(NamedTuple):
    """One resolved test-tier entry -- the SOLE shape ``_configured_test_cmds``
    constructs and every downstream shape-guard (``_matches_configured_cmd``,
    ``_classify_command_core``'s ``well_formed`` filter) checks against. Both
    guards must be kept in lockstep with this shape: widening/narrowing this
    tuple without updating both guards degrades the configured-cmd leg to
    "no match" for every repo, silently, because both guards fail OPEN by
    design (see their own docstrings) rather than raising on a shape they
    don't recognize.

    ``returncode`` is the resolver's own exit code
    (``coordinator-resolve-validation-cmd.py:429-431``) -- ``0`` means
    ``cmd`` was an EXPLICIT ``fast_test_cmd``/``full_test_cmd`` declaration;
    ``3`` means ``full_test_cmd`` was never configured and the resolver fell
    back to the fast tier's own resolved string. Both origins collapse to
    the same tie-break outcome (Tier U wins) once resolved -- see the
    tie-break comment at the call site in ``_classify_command_core`` and
    DR-088 (docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-
    authority.md) -- but keeping the origin on the tuple lets callers
    (``check()``'s grant-deny leg) tell an explicit tie from a fallback tie
    apart when composing remediation text, instead of that distinction being
    unrecoverable after the resolver call returns."""

    tier: str
    cmd: str
    returncode: int


def _configured_test_cmds_native(repo_root: str) -> List[ConfiguredCmd]:
    """``_configured_test_cmds``' native leg -- the same two tiers resolved
    through ``coordinator_core.resolve_validation_cmd``, which travels with
    this guard and is therefore reachable from EVERY repo, not only the one
    that hosts ``coordinator/bin/``.

    Imported lazily so a guard on the PreToolUse hot path pays the resolver's
    import cost only on a command that already looked suite-shaped, and so an
    import failure degrades to the by-path leg instead of raising.
    """
    try:
        from coordinator_core import resolve_validation_cmd as _rvc
    except Exception:
        return []

    out: List[ConfiguredCmd] = []
    for tier, fn_name in (("fast_test_cmd", "cs_resolve_fast_test_cmd"),
                          ("full_test_cmd", "cs_resolve_full_test_cmd")):
        fn = getattr(_rvc, fn_name, None)
        if fn is None:
            continue
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                result = fn(repo_root)
        except Exception:
            continue
        rc = getattr(result, "exit_code", 1)
        cmd = (getattr(result, "cmd", None) or "").strip()
        if rc in (0, 3) and cmd:
            out.append(ConfiguredCmd(tier, cmd, rc))
    return out


def _configured_test_cmds(repo_root: Optional[str]) -> List[ConfiguredCmd]:
    """The repo's configured ``fast_test_cmd``/``full_test_cmd``, resolved by
    the canonical resolver rather than a second hand-rolled frontmatter reader.

    Resolution is NATIVE-FIRST -- ``coordinator_core.resolve_validation_cmd``,
    the importable module this guard already ships alongside -- and only then
    falls back to loading ``<repo_root>/coordinator/bin/coordinator-resolve-
    validation-cmd.py`` by path (that shim's filename is hyphenated, so a
    bareword import can never resolve it regardless of sys.path).

    Review: code-reviewer — the fallback is PER TIER, not all-or-nothing.
    ``_configured_test_cmds_native`` can resolve ``fast_test_cmd`` and still
    fail on ``full_test_cmd`` (a transient import hiccup inside its per-tier
    ``try/except Exception: continue``, or a future divergence between the
    native and by-path resolver logic); gating the by-path shim on the WHOLE
    native list being empty would silently drop the missing tier in
    claude-klabauter, the one repo where the by-path shim exists and works.
    Each tier is therefore attempted independently and the by-path leg is
    only asked for the tiers native didn't resolve; the two lists are merged
    rather than one discarding the other.

    The native leg is load-bearing, not a convenience. ``<repo_root>/
    coordinator/bin/`` holds the resolver in claude-klabauter ONLY -- the
    executable surface was consolidated here, so in every OTHER repo the
    by-path leg finds no file and returns ``[]``. With ``[]`` the
    configured-command containment leg is inert, and a whole-suite command
    that merely NAMES a path (``python -m pytest coordinator/tests``, which
    is example-doctrine-repo's declared fast AND full tier) classifies on shape alone as
    Tier T -- ungated for subagents and requiring no Tier-U grant from the
    EM. That is the guard silently not guarding, in the exact repo whose
    suite command looks scoped. Do not regress this back to by-path-only.

    Note the two resolvers differ in their return contract, hence the two
    accessor shapes below: the native module returns
    ``ResolvedCommand(cmd, exit_code)``; the bin shim returns a result
    carrying ``stdout``/``returncode``. Both use the same 0 / 3 exit codes.

    Returns ``[]`` on any failure -- this leg is belt-and-braces over the
    generic classifier, never the sole basis of a deny.

    ``sys.modules`` registration is scoped to this call: the fixed key is
    popped once the needed attributes have been read off the module, on both
    the success and failure paths, so registration and cleanup are visibly
    paired -- no stale entry survives past the call that created it.
    """
    if not repo_root:
        return []

    native = _configured_test_cmds_native(repo_root)
    native_tiers = {entry.tier for entry in native}
    missing_tiers = {"fast_test_cmd", "full_test_cmd"} - native_tiers
    if not missing_tiers:
        return native

    resolver_path = os.path.join(repo_root, "coordinator", "bin", "coordinator-resolve-validation-cmd.py")
    if not os.path.isfile(resolver_path):
        return native
    try:
        spec = importlib.util.spec_from_file_location(
            "_coordinator_resolve_validation_cmd_guard", resolver_path
        )
        if spec is None or spec.loader is None:
            return native
        module = importlib.util.module_from_spec(spec)
        # Load-bearing: the resolver module uses ``@dataclass`` at module
        # scope, and dataclasses resolves annotation types via
        # ``sys.modules.get(cls.__module__)`` *during* ``exec_module`` --
        # before ``module_from_spec``'s caller would normally register it.
        # Skipping this registration makes ``exec_module`` raise on every
        # Python whose dataclasses implementation looks the module up (seen
        # on 3.14), which this function's blanket ``except Exception``
        # then silently swallows into an empty result.
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
    except Exception:
        return native

    try:
        by_path: List[ConfiguredCmd] = []
        for tier, fn_name in (("fast_test_cmd", "resolve_fast_test_cmd"),
                              ("full_test_cmd", "resolve_full_test_cmd")):
            if tier not in missing_tiers:
                continue
            fn = getattr(module, fn_name, None)
            if fn is None:
                continue
            try:
                with contextlib.redirect_stderr(io.StringIO()):
                    result = fn(repo_root)
            except Exception:
                continue
            rc = getattr(result, "returncode", 1)
            if rc in (0, 3) and result.stdout.strip():
                by_path.append(ConfiguredCmd(tier, result.stdout.strip(), rc))
        return native + by_path
    finally:
        sys.modules.pop(spec.name, None)


def _matches_configured_cmd(segments_argv: Sequence[Sequence[str]],
                            configured: Sequence[ConfiguredCmd],
                            *, exact: bool = False) -> Optional[str]:
    """Does the invocation's segment set CONTAIN every segment of a
    configured test tier (modulo interpreter path and wrapper prefix)?

    A configured command is itself matched only when ALL of its own
    segments (``_normalized_segments``) appear somewhere in the
    invocation's segment set -- not whole-string equality against a single
    invocation segment, which is unreachable by construction for any
    configured command that is itself chained (``pnpm run typecheck &&
    pnpm run test``). For a single-segment configured command this reduces
    to exactly today's verbatim-equality behaviour, unchanged.

    Takes the already-resolved ``configured`` list rather than calling
    ``_configured_test_cmds`` itself -- ``check()`` resolves it once and
    threads it into both this function and ``_matched_tiers`` so the
    resolver (an ``importlib`` module load plus config-file reads) never
    runs twice for the same call."""
    if not configured:
        return None
    normalized = [
        (_norm_head(argv[0]),) + tuple(_norm_arg(a) for a in argv[1:])
        for argv in segments_argv if argv
    ]
    for pair in configured:
        # A belt-and-braces leg must never be the thing that crashes the
        # chain -- a malformed element (e.g. ``configured`` bound to a bare
        # string, whose chars each unpack as a 1-item non-pair) is skipped,
        # degrading this leg to "no configured-cmd match" rather than raising.
        # Shape pinned to ``ConfiguredCmd`` (tier, cmd, returncode) -- kept in
        # lockstep with the ``well_formed`` filter in
        # ``_classify_command_core`` per that NamedTuple's own docstring.
        if not isinstance(pair, (tuple, list)) or len(pair) != 3:
            continue
        tier, cmd, _returncode = pair
        cfg_segments = _normalized_segments(cmd)
        if _cfg_segments_satisfied(cfg_segments, normalized, exact=exact):
            return "the repo's configured %s" % tier
    return None


# ---------------------------------------------------------------------------
# Deny envelopes
# ---------------------------------------------------------------------------

_CTRL_WS_RE = re.compile(r"[\t\r\n\f\v]")
_C0_RE = re.compile(r"[\x00-\x1f]")


def _sanitize(cmd: str) -> str:
    """Flatten control characters before interpolating a caller-supplied
    command into a deny reason."""
    return _C0_RE.sub("", _CTRL_WS_RE.sub(" ", cmd))[:400]


_ANTI_EVASION = (
    "Do not retry the same operation reshaped so the command text parses "
    "differently — a suite run wearing a different costume is still the suite "
    "run this guard exists to stop."
)


def _deny(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _deny_reason_subagent(detected: str, cmd_safe: str) -> str:
    """Identity-leg deny text.

    Cut (2026-08-03, guard message-size discipline C8): compressed from
    ~950 to ~200 measured prose bytes. The indented block below carries the
    scoped alternatives, the override note, AND the detected/command
    diagnostic in one un-broken (no blank-line) run following the
    ``instead`` cue -- ``_message_size``'s cue-window exemption (same
    mechanism ``_alternative_liveness`` already ships) treats every
    indented line in that run as an offered alternative, not prose, so
    none of it is paid out of the 220-byte prose cap. Only the two free-
    standing sentences (why, and the anti-evasion note) are real prose.
    Switches the override pointer from a hand-written ``NAME=1`` to
    ``operator_override_note`` in the same edit (dud-offer fix -- the raw
    form never told a reader it is unreachable mid-session); safe here
    since the pointer sits inside the same exempt run, so this is not a
    net regression against ``test_deny_text_reachable_override.py``'s
    ``_KNOWN_UNFIXED_SITES`` note, it is exactly the fix that note invites
    a future dispatch to make.
    """
    package_script_note = (
        _PACKAGE_SCRIPT_OFFER if _is_package_script_label(detected) else ""
    )
    return (
        "Run the tests you actually touched: full-suite subagent runs are "
        "denied (concurrency degrades the machine) -- use these instead:\n"
        "  pytest path/to/your/test_file.py\n"
        "  pytest path/to/your/test_file.py::test_the_case_you_changed\n"
        "  pytest -k the_behaviour_you_changed\n"
        "  " + operator_override_note(_OVERRIDE_ENV_VAR) + "\n"
        "  Detected: %s -- no test file, directory, or node-id scope\n"
        "  Command:  %s\n\n"
        "A retry reshaped so the command text parses differently is still "
        "denied.%s"
    ) % (detected, cmd_safe, package_script_note)


#: Basenames (post ``_base()`` normalization) that identify the
#: ``with-suite-mutex`` acquiring wrapper -- bare, ``.cmd``, and ``.ps1``
#: shim spellings. ``_base()`` only strips ``.exe``, never ``.cmd``/``.ps1``
#: (those are Windows *script* extensions, not executable-file ones), so
#: both shim spellings are enumerated here rather than assumed handled.
_WITH_SUITE_MUTEX_BASENAMES = frozenset({
    "with-suite-mutex", "with-suite-mutex.cmd", "with-suite-mutex.ps1",
})


def _command_wrapped_in_suite_mutex(
    cmd: str,
    dialect: Optional[_Dialect],
    testpaths: Sequence[str],
    cwd: Optional[str],
    configured: Sequence["ConfiguredCmd"],
) -> bool:
    """Does the SUITE-SHAPED segment of ``cmd`` -- the one that actually
    invokes the runner -- route through ``with-suite-mutex``?

    Review: code-reviewer -- the former implementation asked "does ANY
    segment of this chained command start with ``with-suite-mutex``", which
    a decoy leg satisfies for free: ``with-suite-mutex -- true &&
    python -m pytest`` wrapped a no-op ``true`` while the real ``pytest``
    invocation two segments over ran completely bare and held no mutex --
    the exact hazard the WRAPPER leg exists to close, reopened by a
    predicate that could be satisfied by a segment that was never the suite
    invocation at all. Fixed by tying the wrapper check to the SAME
    per-segment suite-shape classification ``check()`` already runs
    (``_classify_tokens`` per segment, falling back to
    ``_matches_configured_cmd`` containment for a chained configured
    command whose tiers only match as a whole set) rather than a bare
    ``with-suite-mutex`` token-presence scan: every segment identified as
    suite-shaped must itself be wrapped, not merely coexist in a command
    that has a wrapped segment somewhere.

    A legitimately chained command (``cd x && with-suite-mutex --
    pytest ...``) still passes: ``cd x`` never classifies as suite-shaped
    so it is not required to be wrapped, and the ``pytest`` segment is both
    suite-shaped and wrapped.

    Fails CLOSED on ambiguity, never open: a chained configured command that
    only matches as a whole segment-SET (no individual segment classifies
    alone) requires every one of its segments to be wrapped.
    """
    stripped_segments: List[List[str]] = []
    wrapped_flags: List[bool] = []
    for raw_argv in _segment_argvs(cmd, dialect):
        idx = 0
        n = len(raw_argv)
        while idx < n and _ENV_ASSIGN_RE.match(raw_argv[idx]):
            idx += 1
        wrapped = idx < n and _base(raw_argv[idx]).lower() in _WITH_SUITE_MUTEX_BASENAMES
        stripped = _strip_command_prefix(raw_argv)
        if not stripped:
            continue
        stripped_segments.append(stripped)
        wrapped_flags.append(wrapped)

    suite_flags = [
        _classify_tokens(seg, testpaths, cwd) is not None
        or _matches_configured_cmd([seg], configured) is not None
        for seg in stripped_segments
    ]

    if any(suite_flags):
        return all(w for w, s in zip(wrapped_flags, suite_flags) if s)

    # No single segment classifies alone -- only the whole segment SET
    # matches a chained configured command (see ``_matches_configured_cmd``'s
    # containment docstring). Require every segment wrapped rather than
    # guessing which ones "count".
    if stripped_segments and _matches_configured_cmd(stripped_segments, configured) is not None:
        return all(wrapped_flags)

    return False


def _deny_reason_wrapper_required(detected: str, cmd_safe: str) -> str:
    """WRAPPER-leg deny text -- a granted Tier-U/F command that does not
    route through ``with-suite-mutex`` and so would hold no mutex while it
    runs. Names the wrapped form of the caller's OWN command so the fix is a
    copy-paste, matching the register/length of the sibling deny texts in
    this module."""
    return (
        "Route this through the suite mutex so no other run overlaps "
        "yours:\n"
        "  with-suite-mutex -- %s\n\n"
        "A granted Tier-U/F command must actually HOLD the machine-wide "
        "test mutex while it runs; a bare invocation holds nothing.\n\n"
        "  Detected: %s\n"
        "  Command:  %s"
    ) % (cmd_safe, detected, cmd_safe)


def _deny_reason_mutex(detected: str, cmd_safe: str, holder: Dict[str, Any]) -> str:
    return (
        "Wait for the in-flight suite run, or scope this to what you touched "
        "in the meantime:\n"
        "  pytest path/to/your/test_file.py\n\n"
        "A suite run already holds the machine-wide test mutex — one at a "
        "time, machine-wide. Concurrent runs on this fleet do not merely cost "
        "wall-clock; they produce untrustworthy output (mid-edit reads, and "
        "write-lock contention that aborts the pytest process outright).\n\n"
        "  Held by: pid %s | owner %s | started %s\n"
        "  Holding command: %s\n\n"
        "  Detected: %s\n"
        "  Command:  %s\n\n"
        "%s\n\n"
        "Override (rare-use — read the guard source before invoking):\n"
        "  %s=1" % (
            holder.get("pid", "?"), holder.get("owner", "?"),
            holder.get("started_at", "?"), holder.get("cmd", "?"),
            detected, cmd_safe, _ANTI_EVASION, _OVERRIDE_ENV_VAR,
        )
    )


#: Where the ceremony-by-ceremony grant enumeration, the "/bug-blitz,
#: /bug-sweep, ..." no-implicit-grant list, the session-scope/liveness
#: paragraph, and the authority-vs-resource-control aside live now that
#: `_deny_reason_grant` no longer inlines them on every Tier-U deny (2026-07-30
#: PM ruling: the deny ran 256 words, ~170 of it doctrine paid on every
#: firing). No wiki page exists for this guard yet, so the pointer names this
#: module's own docstring (GRANT leg, above) rather than inventing one.
_GRANT_DETAIL_POINTER = (
    "coordinator_core/bash_guards/check_test_suite_invocation.py "
    "(module docstring, GRANT leg)"
)


def _deny_reason_grant(detected: str, cmd_safe: str, *, is_tie: bool = False) -> str:
    """Deny text for the grant leg. ``is_tie`` marks a repo whose configured
    ``fast_test_cmd`` and ``full_test_cmd`` resolved to the identical command
    string -- for such a repo the ``fast_test_cmd`` remediation route was
    ALREADY unreachable by construction even before the 2026-08-04 flip (the
    same string also satisfies ``full_test_cmd``, which the tie-break in
    ``_classify_command_core`` always prefers). See module docstring negative
    spec and DR-088
    (docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md)
    for why the tie itself still classifies Tier U rather than downgrading to
    Tier F.

    Cut (2026-07-30, PM ruling): this text used to inline the ceremony-by-
    ceremony grant enumeration, the no-implicit-grant command list, the
    session-scope/liveness paragraph, and the authority-vs-resource-control
    aside -- all doctrine an agent reads but cannot act on at decision time.
    That content now lives behind `_GRANT_DETAIL_POINTER`; what stays inline
    is exactly what a caller needs to either avoid Tier U or clear it.

    Cut again (2026-07-30, word-budget ruling): the tie case used to share
    the non-tie lede ("run the fast suite (ungated), or get a grant") and
    then RETRACT the fast-suite half in a follow-on sentence once ``is_tie``
    was known -- the tie caller was offered a door and then told it was
    walled up, paying for both the offer and the retraction. The tie branch
    below is a SEPARATE lede that never offers the unreachable route in the
    first place, so there is nothing to retract: the "no Tier-F escape" fact
    is the lede's own shape, not a correction bolted on after it.

    Fixed (2026-07-30, dud-offer memo): both branches used to end with a
    hand-written ``"Override (rare-use ...):\\n  %s=1"`` clause naming
    ``_OVERRIDE_ENV_VAR`` with no indication it is PRE-LAUNCH-ONLY -- an
    agent reading the deny mid-session could and would try to set it from
    inside the session, where it structurally cannot reach this guard's
    ``os.environ`` (the PreToolUse hook is a fresh subprocess per event; see
    ``tests/test_override_unreachability_boundary.py``). Both branches now
    route through ``operator_override_note``, the one builder that carries
    that constraint.

    The tie branch briefly dropped its own ``Grant detail: %s`` line to
    absorb the helper's extra words and hold a <=45-word ceiling. That was
    reverted the same day: the ceiling was a target for cutting PROSE, and
    the pointer is not prose -- it is the only route a tie caller has to the
    grant doctrine this function deliberately stopped inlining. Removing it
    does not move that content behind a pointer, it deletes it for tie
    repos. NEGATIVE SPEC -- do not re-drop this line (or the non-tie
    branch's own ``Full grant detail (...): %s`` line, its equivalent) to
    reclaim word/byte budget.

    Fixed AGAIN (2026-08-04, PM ruling tier-f-is-grant-gated C3, the SECOND
    dud-offer this function has shipped): the non-tie branch used to name
    ``<the repo's configured fast_test_cmd> -- Tier F, ungated by this leg``
    (local ``fast_route``) as an alternative to a grant. After C1/C2 of that
    plan, the grant leg fires on a Tier-F match too, so that line went false
    for EVERY repo the moment the flip landed -- not only the Tier-F path
    this function's tie branch already special-cased, but the highest-
    traffic refusal in the fleet (the plain Tier-U, non-tie deny). Both
    branches now lead with the grant ask and never name the fast suite as an
    escape route; there is no live Tier-F (or Tier-U) route left to offer,
    per the PM's 2026-08-04 ruling that the grant ask itself is the escape
    hatch (no companion declaration-based exit was built -- see this
    module's ``check()``). Both branches also now name WHO supplies the
    grant command's quoted argument: the PM's own verbatim utterance is the
    input the caller quotes, not a phrase the calling agent invents on its
    own authority.

    Whether ``is_tie`` still earns its keyword-only parameter, decided
    explicitly rather than left implicit: YES, but only for the lede's
    opening clause. Once the dead fast-suite offer is gone from both
    branches, the two ledes converge on the same substance ("ask for a
    grant, or run only what you touched") -- ``is_tie`` no longer changes
    WHAT is offered, only whether the caller is additionally told that no
    Tier-F route ever existed for this repo (true and worth stating once,
    not restated in the non-tie case where the fast route was never claimed
    to be reachable in the first place). Collapsing the two branches
    entirely would lose that one true, tie-specific fact; keeping the
    parameter for a one-clause difference is worth the second small
    function body.

    Trimmed and then re-budgeted (2026-08-04, C3): the tie branch's new
    lede ("ask the PM for a Tier-U authorization grant (their exact words
    go in the quotes), or run only what you touched") rendered 66 words, 14
    over the then-52-word ceiling
    (``tests/test_deny_text_reachable_override.py``). Both halves of that
    overshoot were resolved the way this docstring's ordered rule requires,
    prose first and the number last.

    PROSE went first: the parenthetical naming who supplies the quoted
    argument is gone, because the ``tier-u-grant-cli grant pm "<verbatim PM
    utterance>"`` example line already shows it structurally and "get the
    PM's ... grant" already names the source -- the fact survives without
    being said twice.

    The number went second, and only after a wrong turn worth recording:
    the remaining overshoot was first bought by deleting the node-id and
    ``-k`` example lines, leaving one worked example rather than three.
    That fit 51/52 without touching the ceiling, and it was the WRONG
    trade -- the third time this function has been asked to pay a budget in
    offers. A tie caller refused mid-session needs the node-id and ``-k``
    forms precisely because the whole point of the refusal is "run
    something narrower than what you just tried," and one file-path example
    does not teach narrower-than-a-file. They are offers the reader can
    take, so the ordered rule below puts them above the number. Both were
    restored and the ceiling was raised deliberately, 52 -> 58: 56 rendered
    words with two of headroom.

    "Tier-U", "authorization grant", and "No Tier-F escape" stay as LITERAL
    substrings (several ``TestGrantLeg`` cases assert them independent of
    surrounding phrasing) even though the sentence around them was rebuilt.
    """
    if is_tie:
        return (
            "No Tier-F escape -- get the PM's Tier-U authorization grant, "
            "or run what you touched:\n"
            "  tier-u-grant-cli grant pm \"<verbatim PM utterance>\"\n"
            "  pytest path/to/your/test_file.py\n"
            "  pytest path/to/your/test_file.py::test_the_case_you_changed\n"
            "  pytest -k the_behaviour_you_changed\n\n"
            "  Detected: %s\n"
            "  Command:  %s\n\n"
            "Grant detail: %s\n\n"
            "%s"
            % (
                detected,
                cmd_safe,
                _GRANT_DETAIL_POINTER,
                operator_override_note(_OVERRIDE_ENV_VAR),
            )
        )
    return (
        "Ask the PM for a Tier-U authorization grant (their exact words go "
        "in the quotes), or run only what you touched:\n"
        "  tier-u-grant-cli grant pm \"<verbatim PM utterance>\"\n"
        "  pytest path/to/your/test_file.py\n"
        "  pytest path/to/your/test_file.py::test_the_case_you_changed\n"
        "  pytest -k the_behaviour_you_changed\n\n"
        "  Detected: %s\n"
        "  Command:  %s\n\n"
        "Full grant detail (ceremonies, session scope, authority vs. "
        "resource control): %s\n\n"
        "%s" % (detected, cmd_safe, _GRANT_DETAIL_POINTER, operator_override_note(_OVERRIDE_ENV_VAR))
    )


#: Runner heads whose positional arguments this module reads with pytest's
#: own flag-operand grammar. R9's precision leg is scoped to the pytest
#: family deliberately: "file-and-node-id precision" is a pytest concept
#: (``path::test_name``), and every other runner in ``_classify_tokens``
#: takes positionals with different semantics (``cargo test <filter>``,
#: ``go test <package>``, ``npm test -- <pattern>``) where "this token is a
#: directory on disk" is not the same claim. Widening this set requires
#: teaching this leg those grammars first -- see the R9 note in the module
#: docstring.
_PYTEST_HEADS = ("pytest", "py.test")

#: Glob metacharacters this leg treats as "the shell will expand this before
#: pytest ever sees it." Deliberately the ``fnmatch``/``glob`` special set
#: (``*``, ``?``, ``[``) and nothing wider -- brace expansion (``{a,b}``) is
#: a shell (not glob) feature with no ``glob``-module equivalent, and is out
#: of scope for this fix (see the 2026-08-03 correction note below).
_GLOB_MAGIC_CHARS = frozenset("*?[")


def _has_glob_magic(path: str) -> bool:
    """Cheap pre-check so the ``glob.iglob`` walk below is only ever paid for
    a positional that could plausibly BE a glob -- every literal path (the
    overwhelming common case) short-circuits on a single ``any()`` scan."""
    return any(c in _GLOB_MAGIC_CHARS for c in path)


def _pytest_directory_args(segments_argv: Sequence[Sequence[str]],
                           cwd: Optional[str]) -> List[str]:
    """Positional pytest arguments that name a DIRECTORY on disk.

    DR-088 R9 (example-doctrine-repo ruling, 2026-07-28): for a caller carrying a top-level
    ``agent_id``, Tier T is file-and-node-id precision, not directory
    precision. § Decision always defined Tier T as files/directories/node-ids
    *"the caller authored or touched"*; the mechanism enforced path-scoped and
    silently dropped the relevance half. A directory argument is the shape
    that dropped clause was carrying: ``pytest tests/workstate_store/`` from
    an executor that touched three files in it is a run over 87 files the
    caller never touched.

    Node ids (``path::test_name``) are NEVER returned regardless of the
    touched set -- they are bounded to one test by construction, and this is
    what keeps an executor's pre-existing-failure verification legal
    (re-running the one failing test it did not author). Files are likewise
    self-bounding and never returned.

    Deliberately NOT a touched-set intersection: the containment rule ("the
    named directory must contain >=1 file this agent touched") passes
    ``pytest tests/workstate_store/`` for an agent that touched one file in
    it, which does not close the case -- containing a touched file is not the
    same claim as being scoped to touched files. The touched set is read only
    to phrase the alternative in the deny text.

    Glob-expansion closing (2026-08-03, example-retrieval-repo Finding 2, relayed via
    ``cross-repo/inbox/2026-08-03-example-doctrine-repo-em-two-rulings-plan-orphan-
    population-and-dr088-antiscope.md``): this leg was ``os.path.isdir``-keyed
    on the LITERAL positional, so an unexpanded glob (``pytest
    coordinator_core/*/tests``, ``pytest tests/*/``) was never a directory on
    disk and slipped past leg 0 entirely, even though the shell expands it to
    exactly the breadth R9 exists to refuse before pytest ever sees it. A
    positional containing glob metacharacters (``_has_glob_magic`` -- ``*``,
    ``?``, ``[``) is now additionally expanded with ``glob.iglob(...,
    recursive=True)`` relative to ``cwd``; the positional is refused if ANY
    expansion is a directory.

    Two postures decided deliberately here, both pinned by tests:

    - A glob that expands to files ONLY (no directory among the matches) is
      NOT refused. Files are self-bounding by this function's own contract
      (a literal file argument is never returned above), and a glob that
      only ever reaches files makes the identical claim a literal file list
      would -- narrowing the runner-facing grammar of the argument does not
      change what it is scoped to. Widening the file carve-out to its glob
      form is the consistent reading, not a new exception.
    Hot-path cost, measured 2026-08-03 on claude-klabauter at ~4.2k files rather than
    reasoned about: the ``any()`` short-circuits on the first directory match,
    so the realistic patterns cost ~0ms (``coordinator_core/*/tests`` and
    ``**/tests`` both hit immediately). The worst case is a ``**`` pattern
    matching NOTHING -- nothing to short-circuit on, so the generator is drained
    and the whole tree is walked: ~62ms here. That is affordable for a
    ``PreToolUse`` guard on this repo and the walk is bounded by claude-klabauter's own
    size (the guard is inert outside it). A future reader porting this leg to a
    substantially larger tree should re-measure before assuming it still holds
    -- this note exists so that check is possible without re-deriving the cost
    model from scratch.

    - A glob that expands to NOTHING is not refused either -- fail-OPEN,
      mirroring this function's own no-``cwd`` precedent above: a pattern
      that matches zero directories cannot be denied as "the directory
      breadth R9 refuses" because there is no breadth to point at. The
      identity leg still denies any suite-shaped command regardless.

    Node ids and files are still checked with the literal-path fast path
    FIRST (a glob-shaped file argument, e.g. ``test_*.py`` that happens to
    resolve to one file, is not an ``isdir`` match and is not additionally
    globbed unless it also contains a directory-only expansion), so the two
    checks stay strictly additive rather than replacing one another.
    """
    if not cwd:
        # Without a cwd a directory cannot be distinguished from a filter
        # string, and guessing would deny on shape alone. Fail-OPEN here: the
        # identity leg below still denies every suite-shaped subagent command.
        return []
    found: List[str] = []
    for argv in segments_argv:
        if not argv:
            continue
        if _base(argv[0]) in _PYTEST_HEADS:
            args: Sequence[str] = argv[1:]
        else:
            module_args = _pytest_module_args(argv)
            if module_args is None:
                continue
            args = module_args
        _scoped, positionals = _walk_pytest_args(args)
        for arg in positionals:
            if "::" in arg:
                continue
            norm = _norm_path(arg.replace("\\", "/"))
            if not norm:
                continue
            try:
                if os.path.isdir(os.path.join(cwd, norm)):
                    found.append(arg)
                    continue
            except OSError:
                continue
            if _has_glob_magic(norm):
                try:
                    if any(
                        os.path.isdir(match)
                        for match in glob.iglob(os.path.join(cwd, norm), recursive=True)
                    ):
                        found.append(arg)
                except OSError:
                    continue
    return found


def _pytest_module_args(argv: Sequence[str]) -> Optional[Sequence[str]]:
    """Arguments past ``-m pytest`` in a ``python[3] … -m pytest …`` argv, or
    ``None`` when this argv is not a python-module pytest invocation.

    Mirrors ``_classify_python_module``'s FIRST-``-m`` rule: python stops
    parsing its own options at the first ``-m``, so a later ``-m`` belongs to
    pytest (a marker expression) and must not be mistaken for the module flag.
    """
    base = _base(argv[0])
    if not (base in ("python", "python2", "python3", "py")
            or re.match(r"^python3?\.\d+$", base)):
        return None
    try:
        idx = list(argv).index("-m")
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    if argv[idx + 1] not in ("pytest", "py.test"):
        return None
    return argv[idx + 2:]


def _agent_touched_test_files(raw_agent_id: str, session_id: str,
                              repo_root: Optional[str]) -> List[str]:
    """This agent's own touched test files, for the R9 deny text's
    better-alternative line. Best-effort: ``[]`` on any failure.

    Reads ``<git_common_dir>/coordinator-sessions/.agents/<canonical_agent_id>/
    touched.txt`` (repo-relative POSIX paths) written by
    ``coordinator_core.hooks.track_touched_files``, canonicalizing the raw
    agent id through that module's own ``_resolve_subagent_identity`` rather
    than reimplementing its regex -- same single-source reason the classifier
    is not forked.

    Deliberately NOT falling back to the session-level ``touched.txt``: that
    set belongs to the EM and to other agents, and borrowing it to phrase
    "the tests you touched" would launder exactly the relevance R9 asserts.
    """
    if not raw_agent_id or not repo_root:
        return []
    try:
        from coordinator_core.hooks.track_touched_files import _resolve_subagent_identity
        from coordinator_core.lifecycle import git_common_dir

        canonical = _resolve_subagent_identity(raw_agent_id, session_id or "")
        if not canonical:
            return []
        base = git_common_dir(repo_root)
        if base is None:
            return []
        touched = Path(base) / "coordinator-sessions" / ".agents" / canonical / "touched.txt"
        lines = touched.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: List[str] = []
    for line in lines:
        entry = line.strip()
        if not entry or not entry.endswith(".py"):
            continue
        name = entry.rsplit("/", 1)[-1]
        if name.startswith("test_") or name.endswith("_test.py"):
            out.append(entry)
    return out


def _deny_reason_subagent_directory(dir_args: Sequence[str], cmd_safe: str,
                                    touched_tests: Sequence[str]) -> str:
    """R9 deny text. Leads with the caller's own touched test files -- the
    posture this guard already takes everywhere else (offer the better
    command, don't merely refuse the worse one).

    Fixed (2026-07-30, same dud-offer memo as ``_deny_reason_grant``): the
    trailing override line used to hand-write ``"...\\n  %s=1"`` with no
    pre-launch-only qualifier; now routed through ``operator_override_note``.
    """
    if touched_tests:
        shown = list(touched_tests)[:5]
        more = len(touched_tests) - len(shown)
        alternative = (
            "Run the test files YOU touched:\n"
            + "".join("  pytest %s\n" % p for p in shown)
            + ("  ... and %d more in your touched set\n" % more if more > 0 else "")
        )
    else:
        alternative = (
            "You have touched no test files yet. Name the specific file or "
            "node id you mean:\n"
            "  pytest path/to/test_file.py\n"
            "  pytest path/to/test_file.py::test_the_case_you_changed\n"
        )
    return (
        alternative
        + "\nA node id is always permitted, touched or not -- re-running the "
        "one test you are investigating is exactly what this leg leaves "
        "open.\n\n"
        "Directory arguments are refused for dispatched agents (DR-088 R9, "
        "2026-07-28). Tier T is defined as the files, directories, or node "
        "ids the caller AUTHORED OR TOUCHED; for a dispatched agent that "
        "resolves to file-and-node-id precision. A directory names the "
        "files you touched plus every file you did not. The top-level EM is "
        "unaffected -- this narrows the subagent rung only.\n\n"
        "If you narrow to the file/node-id command above, report the "
        "substitution in your report back to your dispatcher: the breadth "
        "your brief actually asked for, the file/node-id command you ran "
        "instead, and that this override was not invoked.\n\n"
        "  Directory arg%s: %s\n"
        "  Command:  %s\n\n"
        "%s\n\n"
        "%s" % (
            "s" if len(dir_args) > 1 else "",
            ", ".join(dir_args),
            cmd_safe,
            _ANTI_EVASION,
            operator_override_note(_OVERRIDE_ENV_VAR),
        )
    )


def _mutex_holder() -> Optional[Dict[str, Any]]:
    """Current holder of the machine-wide suite-run mutex, or ``None``.

    Imported lazily and fail-OPEN by design: the mutex module is a separate
    workstream, and an absent/broken mutex must degrade this leg to "nobody is
    running the suite" rather than blocking every test command on the machine.
    The identity leg above stays fail-CLOSED and is unaffected.
    """
    try:
        from coordinator_core.testing import suite_mutex

        holder = suite_mutex.holder()
    except Exception:
        return None
    return holder if isinstance(holder, dict) else None


def _matched_tiers(cmd: str, cwd: Optional[str], testpaths: Sequence[str],
                   configured: Sequence[ConfiguredCmd]) -> frozenset:
    """Which tiers (a subset of ``{"U", "F"}``) does ANY segment of ``cmd``
    classify as?

    Routes through ``_classify_command_core`` -- the SAME tiering the public
    ``classify_command``/``classify_text`` API uses -- rather than forking a
    second tiering rule for ``check()``'s own use (see module docstring
    negative spec: there is exactly one classifier).

    PM-ruled 2026-08-04: the grant leg's Tier-F exemption (a prior version of
    this function reported Tier U only, since ``check()`` used to gate Tier U
    alone) is removed -- Tier F now requires the same authorization grant.
    This returns the full matched-tier SET, rather than a single bool, so
    ``check()`` can decide -- per tier -- which authority exit applies (the R6
    declaration exit stays reachable on the Tier-U leg only; see ``check()``'s
    own comment at its call site for why that must be an explicit branch, not
    a fall-through).

    Takes the already-resolved ``configured`` list -- see
    ``_matches_configured_cmd``'s docstring for why this isn't resolved
    here."""
    return frozenset(
        match.tier
        for match in _classify_command_core(cmd, cwd, testpaths, configured)
    )


def _tier_u_grant(cwd: Optional[str]) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Does the CALLING session hold a live Tier-U authorization grant?

    Fail-OPEN on infra, narrowly: ONLY an ``ImportError`` (the module
    genuinely doesn't exist -- a partially-landed sibling workstream, same
    shape as ``_mutex_holder``'s missing-module case) degrades to
    ``(True, None)`` (allow). This does NOT mirror ``_mutex_holder``'s
    blanket ``except Exception`` -- that comparison was wrong: a bug in the
    mutex module fails open to a RESOURCE decision (worst case, two suites
    collide, already documented elsewhere as tolerable), but a bug in
    ``check_tier_u_grant`` fails open to an AUTHORITY decision, and
    ``check_tier_u_grant``'s own module docstring states "This function
    never raises" -- so any exception OTHER than ``ImportError`` reaching
    here is a defect in the authority control itself, not infra noise, and
    must surface rather than be silently converted into "the top-level EM
    may run the full suite." This is distinct from (and does not collapse
    into) ``check_tier_u_grant``'s OWN fail-CLOSED discipline on the
    authorization question itself -- an absent/malformed/dead-session grant
    reads UNGRANTED, not "allow" -- see that function's module docstring for
    the same fail-closed-on-authorization/fail-open-on-infra distinction from
    the writer/reader side.
    """
    try:
        from coordinator_core.session import grant
    except ImportError:
        return True, None

    return grant.check_tier_u_grant(cwd)


def _fast_tier_unscoped_declaration(repo_root: Optional[str]) -> str:
    """This repo's R6 declaration prose, or ``""`` when it declares none.

    Delegates wholesale to
    ``coordinator_core.session.fast_tier_declaration.fast_tier_unscoped_
    declaration`` -- the authority layer's single owner of the R6 key, its
    spelling, and the "what counts as declared" rule. This module holds no
    copy of any of the three: the declaration is an AUTHORITY fact, and
    everything under this file's classification surface answers only "what
    shape is this command" (see the module docstring's negative spec, and
    ``tier_u_gate``'s). What stays HERE is the match test the declaration is
    paired with (``_matches_declared_fast_test_cmd`` below) -- a shape
    question this guard already owns the machinery for, and deliberately a
    different test from the CLI seam's exact-string one, because a
    ``PreToolUse`` hard-deny guard must tolerate quoting/whitespace variance
    in the INVOKED command.

    Imported lazily, matching ``_tier_u_grant``'s and ``_mutex_holder``'s own
    lazy-import discipline for this hot-path guard, and fail-CLOSED to ``""``
    (no declaration) on any import/read failure -- this reader backs an
    AUTHORITY-WIDENING exit (R6), so a failure here must fall through to the
    ordinary Tier-U grant requirement, never silently authorize.
    """
    try:
        from coordinator_core.session.fast_tier_declaration import (
            fast_tier_unscoped_declaration,
        )
    except Exception:
        return ""
    try:
        return fast_tier_unscoped_declaration(repo_root)
    except Exception:
        return ""


def _matches_declared_fast_test_cmd(segments_argv: Sequence[Sequence[str]],
                                    configured: Sequence[ConfiguredCmd]) -> bool:
    """Does the invocation's segment set CONTAIN the repo's configured
    ``fast_test_cmd`` -- token-normalized (``_matches_configured_cmd``,
    quoting-style- and internal-whitespace-insensitive), restricted to the
    ``fast_test_cmd`` tier ONLY (never ``full_test_cmd``)?

    This is the R6 declaration exit's own match test, independent of R1's
    shape-based Tier-U classification: R6 exists PRECISELY for the case
    where a repo's ``fast_test_cmd`` is legitimately unscoped by shape (a
    marker-based fast/full split, this repo's own case), so this deliberately
    does NOT consult ``generic``/``_classify_tokens`` the way
    ``_classify_command_core``'s ``cfg_tier == "fast_test_cmd"`` branch does
    -- that branch answers "what tier does this shape get by default"; this
    answers "is this literally the declared string," which is all R6's
    authority exit needs.

    Review: code-reviewer (2026-08-04, Finding 1, tierf-s2-guards) -- a
    CHAINED invocation (``;``/``&&``/``|``) whose first segment is the bare
    declared ``fast_test_cmd`` and whose second segment is a DIFFERENT,
    scoped segment (e.g. this repo's own configured ``fast_test_cmd`` with a
    test path appended, Tier F) used to discharge the WHOLE chain: R6's
    declaration only ever checked that every DECLARED segment appears
    somewhere in the invocation, never that the invocation carries nothing
    ELSE. That let an extra Tier-F (or, pre-dating this diff, Tier-U
    full-suite) segment ride along for free on the declaration, with zero
    grant on disk -- exactly the by-omission escape hatch the PM's
    2026-08-04 ruling forbade, one level down in this pre-existing
    containment gap rather than in the branch guard added for that ruling.
    Closed at the shared root, ``_cfg_segments_satisfied``'s ``exact``
    branch (see its own docstring): the declaration's segment set must now
    equal the invocation's segment set, not merely be contained by it, so
    a legitimately chained declared command (both its OWN segments
    declared together) still passes, but a chain smuggling in a segment
    the declaration never named -- Tier F or, pre-dating this diff, a
    Tier-U full-suite segment -- no longer does.
    """
    fast_only = [c for c in configured if c.tier == "fast_test_cmd"]
    # ``exact=True``, deliberately NOT the containment default the
    # classification legs use. Those legs answer "is this invocation at
    # least the configured breadth" -- a superset still runs the whole
    # suite, so it is classified as that tier. This is an AUTHORITY exit,
    # and it discharges the Tier-U gate for exactly the declared string:
    # ``<fast_test_cmd> --extra-unscoped-thing`` is a command the repo never
    # declared, so the declaration cannot speak for it and it stays denied.
    return _matches_configured_cmd(segments_argv, fast_only, exact=True) is not None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the test-suite-invocation guard against a PreToolUse payload.

    Returns ``None`` (allow) or a hard-deny envelope. Allows unless the
    command is suite-shaped AND any of: (a) the payload carries a top-level
    ``agent_id`` (a subagent caller); (b) the command is Tier U OR Tier F and
    the top-level EM's session holds no live Tier-U authorization grant
    (PM-ruled 2026-08-04: Tier F is no longer exempt from this leg); (c) a
    granted Tier-U/F command is not routed through ``with-suite-mutex``; or
    (d) the machine-wide suite mutex is held by another run. Checked in that
    order -- identity, then grant, then wrapper, then mutex.
    """
    if os.environ.get(_OVERRIDE_ENV_VAR, "0") == "1":
        return None

    # Widened alongside ``MATCHERS`` (see that constant's comment): a
    # PowerShell-tool payload is classified exactly as a Bash-tool one is --
    # both dialects carry their command text in the same ``tool_input.
    # command`` key (confirmed against the sibling multi-tool guards
    # ``block_approval_sentinel_creation.check`` and
    # ``block_reviewer_bash_outside_allowlist.check``), so no separate
    # extraction path is needed here.
    if payload.get("tool_name") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    cmd = tool_input.get("command")
    if not isinstance(cmd, str) or not cmd:
        return None

    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None

    if not _RUNNER_PREFILTER_RE.search(cmd):
        # Dynamic leg (2026-08-10): a repo whose configured test command
        # invokes a runner this static regex has never heard of (a bespoke
        # `bin/run-the-suite`) is caught here instead of falling through
        # unclassified for every leg of this guard -- see
        # `_dynamic_prefilter_hit`'s own docstring and the module docstring's
        # `_RUNNER_PREFILTER_RE` comment for the incident and cost argument.
        if not _dynamic_prefilter_hit(cmd, cwd):
            return None

    # Top-level key access ONLY -- a nested tool_response.agent_id must never
    # reach this decision (see module docstring, negative spec).
    raw_agent_id = payload.get("agent_id")
    is_subagent = isinstance(raw_agent_id, str) and bool(raw_agent_id.strip())

    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)

    detected: Optional[str] = None
    # ``configured`` is resolved AT MOST ONCE per call and threaded into both
    # legs that need it (below) -- ``_configured_test_cmds`` now does real
    # work (an importlib module load plus config-file reads), so leaving each
    # leg to resolve it independently would pay that cost twice on any
    # command that reaches both. Stays ``None`` (never resolved at all) on
    # the hot-path allow, matching the prefilter-miss/no-match short-circuits
    # above.
    configured: Optional[List[ConfiguredCmd]] = None
    segments_argv: List[List[str]] = []
    dialect = _dialect_from_tool_name(payload.get("tool_name"))
    for raw_argv in _segment_argvs(cmd, dialect):
        argv = _strip_command_prefix(raw_argv)
        if not argv:
            continue
        segments_argv.append(argv)
        if detected is None:
            detected = _classify_tokens(argv, testpaths, cwd)

    if detected is None:
        configured = _configured_test_cmds(repo_root)
        detected = _matches_configured_cmd(segments_argv, configured)

    # R9 (DR-088 amendment, 2026-07-28) -- sited above the ``detected is
    # None`` return below, because a Tier-T command is by definition not
    # suite-shaped and so never reaches the identity leg at all; that
    # ordering is correct for what the identity leg does and simply does not
    # reach this case. It runs only when the command is NOT suite-shaped, so
    # this leg is strictly ADDITIVE: it can only deny commands that were
    # previously allowed, never restate a deny another leg already owns with
    # a better diagnosis (a suite-shaped subagent command is an identity
    # problem, not a precision one, and should say so). See
    # ``_pytest_directory_args``.
    if is_subagent and detected is None:
        dir_args = _pytest_directory_args(segments_argv, cwd)
        if dir_args:
            return _deny(_deny_reason_subagent_directory(
                dir_args,
                _sanitize(cmd),
                _agent_touched_test_files(
                    raw_agent_id if isinstance(raw_agent_id, str) else "",
                    payload.get("session_id") if isinstance(payload.get("session_id"), str) else "",
                    repo_root,
                ),
            ))

    if detected is None:
        return None

    cmd_safe = _sanitize(cmd)

    if is_subagent:
        return _deny(_deny_reason_subagent(detected, cmd_safe))

    if configured is None:
        configured = _configured_test_cmds(repo_root)
    # `_matched_tiers` -> `_classify_command_core` re-tokenizes `cmd` itself
    # via its own quote-blind `_segments_with_spans`/`_tokens` walk (that
    # core is ALSO the public, payload/tool_name-free `classify_command`/
    # `classify_text` API -- see that function's own negative spec -- so it
    # cannot be widened to accept a `dialect` parameter without changing a
    # cross-repo-consumed public contract, out of this fix's scope). For a
    # PowerShell-dialect invocation this would silently re-introduce the
    # SAME `Start-Process -ArgumentList` blindness the identity leg above
    # was just fixed for, on the EM/grant leg. Feeding it the ALREADY
    # dialect-resolved `segments_argv` (computed above via `_segment_argvs`,
    # which already ran the Start-Process expansion for this dialect)
    # instead of the raw `cmd` string closes that gap without touching the
    # public API's signature: the reconstructed text carries the launched
    # command's own argv in command position, exactly as the identity leg
    # above already sees it.
    cmd_for_tiering = (
        " ; ".join(" ".join(seg) for seg in segments_argv)
        if dialect is _Dialect.POWERSHELL and segments_argv
        else cmd
    )
    matched_tiers = _matched_tiers(cmd_for_tiering, cwd, testpaths, configured)
    if matched_tiers & {"U", "F"}:
        # R6 (DR-088 amendment, 2026-07-25): a repo may DECLARE its fast
        # tier legitimately unscoped (``coordinator_core.session.
        # fast_tier_declaration`` owns that declaration and its key). This
        # discharges the AUTHORITY check for exactly the literal (token-
        # normalized) resolved ``fast_test_cmd`` -- never ``full_test_cmd``,
        # never any other Tier-U command -- and only reaches here at all
        # because the identity leg above already denied every subagent
        # outright, so this exit is EM-only by construction, matching R6's
        # "does not widen the subagent rung" requirement without a separate
        # identity check. This is ``check()``'s authority leg, not the
        # classifier: nothing under ``_classify_command_core`` reaches the
        # declaration at all -- see this module's own docstring negative spec
        # and ``tier_u_gate.py``'s for why.
        #
        # PM-ruled 2026-08-04: this leg now also fires on a Tier-F match (the
        # grant ask is Tier F's only escape hatch; no declaration-based exit
        # is to be added for it). The R6 exit above must stay reachable on
        # the Tier-U leg ONLY -- gated explicitly on ``"U" in matched_tiers``
        # below rather than left to fall through by omission, or a repo
        # carrying a stale R6 declaration (the unscoped-fast-tier exemption
        # above) would get its Tier-F command discharged by that declaration
        # for free, which is exactly the Tier-F escape hatch the PM forbade.
        declared_unscoped_fast_tier = (
            "U" in matched_tiers
            and _fast_tier_unscoped_declaration(repo_root)
            and _matches_declared_fast_test_cmd(segments_argv, configured)
        )
        granted = declared_unscoped_fast_tier
        if not granted:
            granted, _record = _tier_u_grant(cwd)
        if not granted:
            # A tie repo (fast_test_cmd == full_test_cmd, either declared
            # explicitly or arrived at via the resolver's rc=3 fallback --
            # see ``ConfiguredCmd``'s docstring) has no reachable Tier-F
            # route: the same string satisfies both tiers and the tie-break
            # always prefers full_test_cmd. Naming the Tier-F escape hatch in
            # that case would send the caller straight back into this deny.
            by_tier = {entry.tier: entry.cmd for entry in configured}
            is_tie = (
                "fast_test_cmd" in by_tier
                and "full_test_cmd" in by_tier
                and by_tier["fast_test_cmd"] == by_tier["full_test_cmd"]
            )
            # Branch left explicit on which tier fired (Tier-F-only match,
            # no Tier-U match in the same invocation, vs. a Tier-U match)
            # even though both calls render identically today: C3 (2026-08-
            # 04 PM ruling) decided the two ledes converge on "ask for a
            # grant" once the dead fast-suite offer is gone from both, so
            # there is no separate Tier-F-only deny text to write here --
            # see ``_deny_reason_grant``'s own docstring for the full
            # reasoning. The branch stays as a seam, not collapsed to one
            # `return`, so a future divergence (should one become
            # warranted) has somewhere to land without re-deriving
            # `matched_tiers` at the call site.
            if "U" in matched_tiers:
                return _deny(_deny_reason_grant(detected, cmd_safe, is_tie=is_tie))
            return _deny(_deny_reason_grant(detected, cmd_safe, is_tie=is_tie))

        # WRAPPER leg (new, sited strictly after identity and grant, and
        # strictly before the mutex leg below): a granted Tier-U/F command
        # that does not route through ``with-suite-mutex`` would hold no
        # mutex at all, reopening the exact concurrent-run hazard the grant
        # leg above authorizes but cannot itself prevent -- see
        # ``_command_wrapped_in_suite_mutex``'s docstring.
        if not _command_wrapped_in_suite_mutex(cmd, dialect, testpaths, cwd, configured):
            return _deny(_deny_reason_wrapper_required(detected, cmd_safe))

    holder = _mutex_holder()
    if holder:
        return _deny(_deny_reason_mutex(detected, cmd_safe, holder))

    return None


# ---------------------------------------------------------------------------
# Public classification API (DR-088 layer 2 seam) -- see negative spec and
# spec backlinks in the module docstring above.
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class SuiteMatch:
    """One suite-shaped command match found by ``classify_command`` or
    ``classify_text``.

    ``position`` is advisory metadata for the CALLER's own allow/deny
    policy -- see the module docstring's negative spec. This dataclass never
    decides anything; it only describes what was found and where.
    """

    #: ``"F"`` (the invocation's SHAPE is scoped -- a test file, directory,
    #: or node-id argument -- AND it verbatim-matches the repo's configured
    #: ``fast_test_cmd`` OR ``full_test_cmd``) or ``"U"`` (any other
    #: suite-shaped match, INCLUDING an unscoped runner invocation that
    #: happens to verbatim-match one of those cfg keys -- the cfg key never
    #: launders an unscoped shape into Tier F, for EITHER key -- and the
    #: undeterminable case. Tier is a property of the invocation's shape,
    #: not of the config key it was read from (R1,
    #: cross-repo/inbox/2026-07-25-example-doctrine-repo-em-validate-tier-u-shape-
    #: ruling.md). Tier F is the narrow opt-in, Tier U the default.
    tier: str
    #: Human-readable runner label, e.g. ``"pytest"``, ``"go test ./..."``,
    #: ``"the repo's configured fast_test_cmd"``.
    detected: str
    #: The exact matched command substring, control-character-sanitized.
    matched_text: str
    #: ``(start, end)`` character offsets of ``matched_text`` into the input
    #: string passed to ``classify_command``/``classify_text``.
    span: Tuple[int, int]
    #: Where the match sits in the input: ``"fenced_code"``, ``"inline_code"``,
    #: ``"negated"``, ``"reported"``, ``"descriptive"``, ``"imperative"``, or
    #: ``"unknown"``. ``"negated"`` is an instruction NOT to run something
    #: ("do not run pytest"). ``"reported"`` is a DIFFERENT linguistic shape:
    #: prose reporting someone's (in)ability to run something, or a
    #: past-tense reporting frame ("they stated they could not run pytest",
    #: "she said they failed to run pytest") -- not an instruction either
    #: way, just a narrative claim about a run that did or didn't happen.
    #: ``"descriptive"`` is a THIRD shape, distinct from both: a bare-line
    #: mention that contains an execution-intent cue word (``run``,
    #: ``running``, ``verify``, ...) but where that cue is NOT the head of
    #: its clause -- a subject noun phrase or an auxiliary/modal/copula
    #: governs it instead ("Neither consumer may run the test tier", "other
    #: sessions are running pytest against this shared worktree"). A real
    #: English imperative has the cue AS the clause head (no subject, no
    #: auxiliary precedes it: "run pytest", "Please run pytest", "re-run
    #: pytest") -- see ``_cue_is_clause_head``. Always ``"imperative"`` for
    #: ``classify_command`` (a real command line, not prose, has no
    #: code-fence, negation, reported-speech, or clause-structure context to
    #: report). A non-``"imperative"`` position is automatically
    #: non-denying at the sole downstream consumer (example-doctrine-repo's
    #: ``coordinator/hooks/scripts/block-dispatch-suite-invocation.py``,
    #: which denies a dispatch iff ANY match has ``position ==
    #: "imperative"``) -- so adding ``"reported"``/``"descriptive"`` as
    #: additional non-imperative values is safe by construction: each can
    #: only ever suppress a false "imperative", never invent one.
    position: str
    #: Actionable remediation text -- what to run/do instead, not a bare
    #: boolean the caller has to write its own copy for.
    remediation: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tier": self.tier,
            "detected": self.detected,
            "matched_text": self.matched_text,
            "span": list(self.span),
            "position": self.position,
            "remediation": self.remediation,
        }


def _remediation_text(tier: str, detected: str) -> str:
    """Actionable remediation for a public-API match. Distinct copy from
    ``_deny_reason_subagent``/``_deny_reason_mutex`` -- those are hard-deny
    envelopes keyed to ``check()``'s two legs; this is generic advice for a
    caller (e.g. Example-doctrine-repo's dispatch-brief guard) that has no payload/identity/
    mutex context of its own."""
    if tier == "F":
        return (
            "This reproduces the repo's configured fast_test_cmd verbatim. "
            "Reserve full/fast-suite runs for the top-level EM at the "
            "validation/ceremony cadence gate (coordinator:validate, "
            "/workday-complete, /workweek-complete) rather than an ad hoc "
            "dispatch or a subagent.\n\n"
            "Detected: %s" % detected
        )
    package_script_note = (
        _PACKAGE_SCRIPT_OFFER if _is_package_script_label(detected) else ""
    )
    return (
        "Scope this to what was actually touched instead of the whole "
        "suite:\n"
        "  pytest path/to/the/test_file.py\n"
        "  pytest path/to/the/test_file.py::test_the_case_that_changed\n"
        "  pytest -k the_behaviour_that_changed\n\n"
        "Unscoped/full-suite runs require a Tier-U grant and are reserved "
        "for the top-level EM -- never a dispatched subagent or a bare "
        "dispatch-brief instruction."
        "%s\n\n"
        "Detected: %s" % (package_script_note, detected)
    )


#: Label surfaced to the operator's remediation text when a scoped-shape
#: segment matches a configured cfg key -- distinct per key so the message
#: names which config field the repo declared, even though both keys share
#: one shape decision (``_tier_for_cfg_match``).
_CFG_TIER_DETECTED_LABEL: Dict[str, str] = {
    "fast_test_cmd": "the repo's configured fast_test_cmd",
    "full_test_cmd": "the repo's configured full_test_cmd",
}


#: Command heads that cannot invoke a test runner at all, however they are
#: argued -- the closed allow-list behind ``_argv_is_inert``.
#:
#: An ALLOW-list, not a deny-list of wrapper tells, and that direction is the
#: whole point. A tell-based rule ("this segment mentions a runner token, or
#: the substring ``test``") reads ``bash scripts/run-tests.sh`` correctly and
#: then hands a free pass to ``bash run-suite.sh`` -- the identical opaque
#: wrapper under a name the tell does not happen to spell. Every such rule is
#: one rename away from the fail-OPEN hole the callers below exist to close.
#: Inverting it makes the failure mode a false "could be a suite run" (the
#: caller falls back to exactly the behaviour it already had) instead of a
#: false "definitely not one".
#:
#: Membership test: could this head, under ANY argv, spawn a test run?
#: Anything that executes a script, a binary, or a subshell -- ``bash``,
#: ``sh``, ``python``, ``make``, ``xargs``, ``find`` -- fails it and is
#: deliberately absent. Extend only with heads that pass it.
_INERT_COMMAND_HEADS = frozenset({
    ":", "true", "false", "exit", "return", "echo", "printf", "pwd", "cd",
    "sleep", "test", "[",
})


def _argv_is_inert(tokens: Sequence[str]) -> bool:
    """Is this already-prefix-stripped argv PROVABLY incapable of running a
    test suite -- as opposed to merely unrecognized by ``_runner_recognized``?

    The distinction ``_runner_recognized``'s ``False`` cannot make on its own.
    That helper answers "did a per-runner classifier reach a real verdict",
    and its ``False`` covers two structurally different commands: an opaque
    wrapper whose breadth is unknowable from its shape (``bash
    scripts/run-tests.sh``, ``pnpm run tier:fast``, ``python dev.py test``),
    and a command that cannot spawn anything at all (``true``, ``exit 3``).
    Callers that fail closed on ``_runner_recognized`` being ``False`` --
    ``_tier_for_cfg_match``'s ``full_test_cmd`` leg and
    ``classify_runner_footprint`` -- must treat the second class differently,
    or a repo whose fast tier is a no-op is told it needs a Tier-U grant to
    run ``true``.
    """
    return bool(tokens) and _base(tokens[0]) in _INERT_COMMAND_HEADS


def _runner_recognized(tokens: Sequence[str]) -> bool:
    """Does a per-runner classifier in ``_classify_tokens``'s dispatch table
    actually recognize this argv's runner (and, for the python family, its
    ``-m`` module) well enough to have made a REAL scoped/unscoped
    determination -- as opposed to falling through with no opinion at all?

    Every classifier ``_classify_tokens`` dispatches to computes a genuine
    verdict once reached; the only way an argv exits with an ambiguous
    ``None`` is never reaching one of those dispatches in the first place --
    an unrecognized base, or (for the python family) no ``-m`` at all, or a
    ``-m`` module that is none of ``pytest``/``py.test``/``unittest``/
    ``nose2``. A project's own custom test wrapper (``python dev.py test``)
    is exactly this: ``_classify_python_module`` bails out on the missing
    ``-m`` before it ever asks whether ``test`` is a scope, so its ``None``
    means "I don't know", not "confirmed scoped". Used solely to gate the
    ``full_test_cmd`` shape exception in ``_tier_for_cfg_match`` against
    laundering that ambiguity into Tier F -- ``fast_test_cmd`` deliberately
    keeps trusting the operator's own declared command regardless of runner
    recognition, unchanged by this helper.

    Classifier correction, 2026-07-30 -- ``None`` was never single-meaning
    for the non-python families either: each of ``_classify_cargo``/
    ``_classify_go``/``_classify_package_manager`` returns ``None`` for two
    structurally different reasons -- (a) genuinely scoped (a filter/target
    positional was found), and (b) the argv is not a recognized test-
    invocation shape for that runner AT ALL (``cargo watch -x test``, ``go
    vet``, ``npm run build``). Granting ``True`` on the base binary name
    alone -- the prior behaviour -- could not tell those apart: a repo
    declaring ``full_test_cmd: "cargo test"`` classified ``cargo watch -x
    test`` as Tier F, because that argv merely CONTAINS the token ``test``
    (as ``-x``'s own argument) and so satisfies the cfg-match containment
    leg, while ``_classify_cargo`` returns ``None`` for it not because it is
    scoped but because ``watch`` is not a recognized head at all. That is
    exactly the fail-open R9 closed for the python family, reopened here.
    Each family below now asks its OWN shape-recognition question
    (``_cargo_test_shape``/``_go_test_shape``/``_make_first_target``/
    ``_package_manager_test_shape``) -- the identical predicate its own
    classifier uses to decide whether it has an opinion at all -- rather
    than trusting the base name. ``jest``/``vitest``/``mocha``/``jasmine``/
    ``ava`` are unchanged: ``_classify_js_runner`` has no "unrecognized
    shape" branch, so its ``None`` already always means "confirmed scoped"
    (see that function's own docstring).
    """
    if not tokens:
        return False
    base = _base(tokens[0])
    args = tokens[1:]
    if base in ("pytest", "py.test"):
        return True
    if base in ("python", "python2", "python3", "py") or re.match(r"^python3?\.\d+$", base):
        try:
            idx = list(tokens).index("-m")
        except ValueError:
            return False
        if idx + 1 >= len(tokens):
            return False
        return tokens[idx + 1] in ("pytest", "py.test", "unittest", "nose2")
    if base in ("npm", "pnpm", "yarn", "bun"):
        return _package_manager_test_shape(args)
    if base in ("jest", "vitest", "mocha", "jasmine", "ava"):
        return True
    if base == "cargo":
        return _cargo_test_shape(args) is not None
    if base == "go":
        return _go_test_shape(args) is not None
    if base in ("make", "gmake"):
        target = _make_first_target(args)
        return target is not None and target in _MAKE_SUITE_TARGETS
    if base in _TOX_NOX_BASES:
        return True
    return False


def _tier_for_cfg_match(cfg_tier: str, generic: Optional[str],
                        argv: Sequence[str]) -> Tuple[str, str]:
    """R1 (cross-repo/inbox/2026-07-25-example-doctrine-repo-em-validate-tier-u-shape-
    ruling.md): tier is a property of the invocation's SHAPE, not of the
    config key (``fast_test_cmd`` or ``full_test_cmd``) it was read from.
    The single decision point for both legs of ``_classify_command_core``'s
    cfg-match branch -- do NOT re-derive this per leg; a prior version of
    this module fixed the rule for ``fast_test_cmd`` alone and left
    ``full_test_cmd`` on the old key-determines-tier behaviour, which is
    exactly the drift this helper exists to make structurally impossible.

    ``full_test_cmd`` carries one further guard the ``fast_test_cmd`` leg
    does not need: Tier F there requires a runner classifier to have
    POSITIVELY confirmed the shape is scoped (``_runner_recognized``), not
    merely the ABSENCE of an unscoped complaint. ``fast_test_cmd`` is the
    operator's own low-friction opt-in and stays trusted verbatim even
    against a runner the classifier cannot parse at all; ``full_test_cmd``
    denotes the entire declared suite by construction, so an unrecognized
    runner's ambiguous ``None`` must default to Tier U, not be laundered
    into Tier F."""
    if cfg_tier == "full_test_cmd":
        if generic is None and _runner_recognized(argv):
            return "F", _CFG_TIER_DETECTED_LABEL["full_test_cmd"]
        # ...with one exception that is not a laundering at all: an argv
        # PROVABLY incapable of spawning a test run (``_argv_is_inert``:
        # ``true``, ``exit 3``). ``_runner_recognized`` is ``False`` for those
        # for the same reason it is ``False`` for an opaque wrapper, but the
        # ambiguity the Tier-U default protects against does not exist here --
        # there is no unknown breadth behind ``exit 3``. Measured 2026-08-02:
        # a repo whose fast tier resolved to ``exit 3`` (with no separate
        # ``full_test_cmd``, so the resolver's rc=3 fallback made the fast
        # string the full string too) had that command classified "the repo's
        # configured full_test_cmd", Tier U -- so ``enforce_tier_u_gate``
        # demanded a Tier-U grant before it would run a one-token no-op.
        if generic is None and _argv_is_inert(argv):
            return "F", _CFG_TIER_DETECTED_LABEL["full_test_cmd"]
        return "U", generic or _CFG_TIER_DETECTED_LABEL["full_test_cmd"]
    if generic is not None:
        return "U", generic
    return "F", _CFG_TIER_DETECTED_LABEL[cfg_tier]


def _segments_with_spans(cmd: str) -> List[Tuple[str, int, int]]:
    """Split a command string into independently-classifiable segments on
    ``;``/``&``/``|`` runs, preserving each segment's original offsets into
    ``cmd``. Sibling of ``_segments`` (used by ``check()``) but offset-
    preserving, since the public API must report a ``span`` -- ``check()``
    has no such requirement and is left untouched."""
    return [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[^;&|]+", cmd)]


def _classify_command_core(
    command: str,
    cwd: Optional[str],
    testpaths: Sequence[str],
    configured: Sequence[ConfiguredCmd],
) -> List[SuiteMatch]:
    """Shared classification core for ``classify_command`` and
    ``classify_text`` -- runs every segment of ONE command string through the
    same private classifier ``check()`` uses (``_classify_tokens``,
    ``_is_real_scope``, the configured-command containment leg), without
    short-circuiting on the first match. Always reports ``position``
    ``"imperative"``; callers that know the surrounding text (``classify_text``)
    override it afterward.

    Configured-tier matching needs the FULL invocation's segment set, not
    any one segment in isolation -- a chained configured command (``pnpm run
    typecheck && pnpm run test``) is matched by segment-set containment
    (every one of ITS segments present somewhere in the invocation), and
    once a configured tier is satisfied that way, EVERY invocation segment
    that is one of that tier's own segments is classified into that tier
    (both halves of the chained example above become Tier F, not just the
    half that happens to look Tier-U-shaped on its own). So this builds the
    invocation's segment-argv set FIRST, decides which configured tiers are
    satisfied against that whole set, and only then classifies each segment
    against the now-known satisfied tiers."""
    out: List[SuiteMatch] = []
    if not command:
        return out

    seg_infos: List[Tuple[str, int, int, List[str], Tuple[str, ...]]] = []
    invocation_segments: List[Tuple[str, ...]] = []
    for seg_text, start, end in _segments_with_spans(command):
        stripped = seg_text.strip()
        if not stripped:
            continue
        lead = len(seg_text) - len(seg_text.lstrip())
        trail = len(seg_text) - len(seg_text.rstrip())
        seg_start, seg_end = start + lead, end - trail

        tokens = _tokens(stripped)
        argv = _strip_command_prefix(tokens)
        if not argv:
            continue

        target = (_norm_head(argv[0]),) + tuple(_norm_arg(a) for a in argv[1:])
        seg_infos.append((stripped, seg_start, seg_end, argv, target))
        invocation_segments.append(target)

    # Shape-guard BEFORE the sort: a belt-and-braces leg must never be the
    # thing that crashes the chain, and a malformed element would raise
    # inside the sort key (``pair[0]``) before the loop body ever runs its
    # own unpack.
    # Shape pinned to ``ConfiguredCmd`` (tier, cmd, returncode) -- kept in
    # lockstep with the shape-guard in ``_matches_configured_cmd`` per that
    # NamedTuple's own docstring; a mismatch here silently drops every
    # configured-cmd match rather than raising.
    well_formed = [
        pair for pair in configured
        if isinstance(pair, (tuple, list)) and len(pair) == 3
    ]

    # Check ``full_test_cmd`` BEFORE ``fast_test_cmd`` so a tie (both tiers
    # resolved to the identical string) prefers the STRICTER tier. This is
    # the NORMAL shape for any repo with no distinct ``full_test_cmd``
    # configured: ``resolve_full_test_cmd`` falls back to the fast tier's
    # own resolved string (exit code 3) when nothing is configured, so
    # ``configured`` routinely carries two entries with the same command
    # string. Deciding by insertion order there would force the
    # whole-suite invocation to Tier F (ungated) -- exactly the coverage
    # this leg exists to provide.
    #
    # This same tie-break also fires when the repo EXPLICITLY declares
    # ``full_test_cmd`` identical to ``fast_test_cmd`` (resolver rc=0 for
    # both, not the rc=3 fallback above) -- and resolves to Tier U for that
    # case too, deliberately, not by accident of a discarded returncode.
    # DR-088 (docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-
    # authority.md:38-40) defines Tier U disjunctively: the repo's
    # ``full_test_cmd`` OR any unscoped runner invocation. An explicit tie
    # is still an unscoped runner invocation -- declaring the same string
    # under both keys does not make it a *scoped* one -- so classifying it
    # as Tier F would ungate DR-088's second disjunct. ``ConfiguredCmd``
    # carries the resolver's ``returncode`` precisely so a caller (see
    # ``check()``'s grant-deny leg) CAN tell the two tie origins apart when
    # composing remediation text, even though both origins resolve to the
    # identical Tier-U verdict here.
    satisfied: Dict[str, List[Tuple[str, ...]]] = {}
    for tier_name, cmd_str, _returncode in sorted(
        well_formed, key=lambda pair: pair[0] != "full_test_cmd"
    ):
        cfg_segments = _normalized_segments(cmd_str)
        if _cfg_segments_satisfied(cfg_segments, invocation_segments):
            satisfied.setdefault(tier_name, cfg_segments)

    # A repo may scope its fast tier by APPENDING a scope to its full tier
    # (``python dev.py test`` full, ``python dev.py test tests`` fast). The
    # configured-cmd match is token CONTAINMENT (``_segment_contains`` ->
    # ``issubset``), so such a fast invocation also "contains" the full
    # command, and the full-first tie-break above would then classify the
    # fast tier's own command as Tier U -- leaving the repo with no reachable
    # Tier F at all, so every ``/validate`` and workday-complete Step-1 gate
    # refuses rather than runs. That is a silent dead gate: it reports having
    # declined, never having failed.
    #
    # Prefer the fast tier ONLY when its configured tokens are a STRICT
    # superset of the full tier's -- fast genuinely narrows full by at least
    # one extra scope token. That is a distinct shape from the equal-string
    # tie the block above reasons about (whether an identical *scoped* string
    # under both keys is Tier U is a settled policy call, asserted by
    # ``test_classify_command_identical_scoped_string_under_both_keys_
    # still_classifies_tier_u``); this narrowing check leaves that verdict,
    # and the rc=3 fallback tie, entirely untouched.
    tier_order = ("full_test_cmd", "fast_test_cmd")
    _fast_cfg = satisfied.get("fast_test_cmd")
    _full_cfg = satisfied.get("full_test_cmd")
    if _fast_cfg and _full_cfg:
        # Compare on the same basis ``_segment_contains`` matches on: tokens
        # after the leading interpreter/binary token.
        _fast_tokens = {tok for seg in _fast_cfg for tok in seg[1:]}
        _full_tokens = {tok for seg in _full_cfg for tok in seg[1:]}
        if _full_tokens < _fast_tokens:
            tier_order = ("fast_test_cmd", "full_test_cmd")

    for stripped, seg_start, seg_end, argv, target in seg_infos:
        cfg_tier: Optional[str] = None
        for tier_name in tier_order:
            cfg_segments = satisfied.get(tier_name)
            if cfg_segments and any(
                _segment_contains(seg, target) for seg in cfg_segments
            ):
                cfg_tier = tier_name
                break

        generic = _classify_tokens(argv, testpaths, cwd)

        if cfg_tier is not None:
            # R1 (cross-repo/inbox/2026-07-25-example-doctrine-repo-em-validate-tier-u-
            # shape-ruling.md): tier is a property of the invocation's
            # SHAPE, not of the config key it was read from -- and this
            # holds for BOTH cfg keys, not just ``fast_test_cmd``. A segment
            # that verbatim-matches the repo's configured ``fast_test_cmd``
            # OR ``full_test_cmd`` is Tier F only when its own shape is
            # scoped (``generic is None`` -- ``_classify_tokens`` found no
            # unscoped runner invocation to report). When the shape IS an
            # unscoped runner invocation (``generic is not None``), the
            # cfg-key match must NOT launder it into Tier F -- classify on
            # shape as Tier U, same as any other unscoped invocation. A
            # repo whose ``fast_test_cmd`` is itself unscoped therefore has
            # no reachable Tier F (R2) -- that is a misconfiguration to
            # correct, not a tier to grant around. The two cfg keys differ
            # only in which label reaches the operator's remediation text
            # when the shape turns out scoped -- ``_tier_for_cfg_match``
            # is the single place that decision is made, on purpose, so a
            # future fix to this rule cannot land in one leg and miss its
            # twin the way this one originally did.
            tier, detected = _tier_for_cfg_match(cfg_tier, generic, argv)
        elif generic is not None:
            tier, detected = "U", generic
        else:
            continue  # Tier T (or no runner at all) -- not a match

        out.append(SuiteMatch(
            tier=tier,
            detected=detected,
            matched_text=_sanitize(stripped),
            span=(seg_start, seg_end),
            position="imperative",
            remediation=_remediation_text(tier, detected),
        ))
    return out


def classify_command(command: str, *, cwd: Optional[str] = None) -> List[SuiteMatch]:
    """Classify EVERY segment of one shell command string, zero-to-many
    matches. Unlike ``check()`` this never short-circuits on the first
    classified segment and never reads a payload dict.

    Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 1
    """
    if not isinstance(command, str) or not command:
        return []
    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)
    return _classify_command_core(command, cwd, testpaths, configured)


#: ``classify_runner_footprint``'s three verdicts. Exported as constants so a
#: caller branches on a name rather than a bare string literal it can typo
#: into a silent fail-open.
RUNNER_FOOTPRINT_NONE = "none"
RUNNER_FOOTPRINT_SCOPED = "scoped"
RUNNER_FOOTPRINT_UNPROVEN = "unproven"

def classify_runner_footprint(command: str, *, cwd: Optional[str] = None) -> str:
    """Does ``command`` carry a test-runner footprint, and if so is that
    footprint positively SCOPED?

    A shape-only question -- the third public classification API alongside
    ``classify_command`` (is any segment suite-shaped?) and
    ``classify_command_precision`` (is a Tier-T segment directory-imprecise?).
    It answers the question those two leave open: a command they BOTH report
    nothing for is either genuinely harmless (``true``, ``exit 3``, a scoped
    ``pytest path/test_x.py``) or an opaque wrapper that could be running the
    whole suite (``pnpm run tier:fast``, ``bash scripts/run-tests.sh --tier
    fast``, ``python dev.py test``). Conflating those two is the fail-OPEN
    hole ``coordinator_core.session.tier_u_gate`` exists to close, and
    distinguishing them is what lets that gate stop refusing the first group
    while still refusing the second.

    Returns one of:
      - ``RUNNER_FOOTPRINT_NONE`` -- every segment is ``_argv_is_inert``,
        i.e. provably cannot spawn a test run under any argv. ``true``,
        ``exit 3``, ``echo ok``.
      - ``RUNNER_FOOTPRINT_SCOPED`` -- at least one segment is a runner
        invocation a per-runner classifier POSITIVELY parsed
        (``_runner_recognized``) and found scoped (``_classify_tokens``
        returned no unscoped complaint), and no segment is ``unproven``.
        ``pytest path/test_x.py``, ``pytest -k expr``.
      - ``RUNNER_FOOTPRINT_UNPROVEN`` -- some segment either is a recognized
        runner invocation this module calls UNSCOPED, or is neither a
        recognized runner nor an inert head (every opaque wrapper:
        ``pnpm run tier:fast``, ``bash scripts/run-tests.sh --tier fast``,
        ``bash run-suite.sh``, ``python dev.py test``). The verdict is
        first-wins-pessimistic: one unproven segment makes the whole command
        unproven, regardless of how many scoped segments accompany it.

    Negative spec:
      - It does NOT read, import, or branch on ANY repo-level breadth
        declaration -- neither of the two keys named in this module's own
        docstring negative-spec (R7 and its twin). This function answers
        "what footprint does this command have"; what a repo DECLARED about
        its own wrapper is an AUTHORITY question answered only by the
        caller.
      - It does NOT return a permission decision. ``RUNNER_FOOTPRINT_NONE``
        is not "allow" -- it is "no test-runner footprint found"; the caller
        decides what that means for its own policy.
      - It does NOT re-implement scope detection. Both questions are asked
        with the same two private helpers ``check()`` and
        ``_classify_command_core`` already run through
        (``_runner_recognized``, ``_classify_tokens``), so a fix to either
        reaches this API for free.
    """
    if not isinstance(command, str) or not command.strip():
        return RUNNER_FOOTPRINT_NONE
    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)

    verdict = RUNNER_FOOTPRINT_NONE
    for segment in _segment_argvs(command):
        argv = _strip_command_prefix(segment)
        if not argv:
            continue
        if _runner_recognized(argv):
            if _classify_tokens(argv, testpaths, cwd) is not None:
                return RUNNER_FOOTPRINT_UNPROVEN
            verdict = RUNNER_FOOTPRINT_SCOPED
            continue
        if not _argv_is_inert(argv):
            return RUNNER_FOOTPRINT_UNPROVEN
    return verdict


#: Negation/quotation markers that flip a code-fence/inline-code/bare match
#: to ``position="negated"`` -- a brief instructing an agent NOT to do
#: something, or quoting a command in order to delete/replace it, is not an
#: imperative instruction to run that command. See module docstring negative
#: spec: this label is advisory, the caller decides what to do with it.
_NEGATION_RE = re.compile(
    r"(?:\bdo not run\b|\bdon't run\b|\bnever run\b|\bmust not\b|\bshould not\b|"
    r"\binstead of\b|\brather than\b|\bdeny[-\s]?list\b|\bremove\b|\bdelete\b|"
    r"\bstrip\b|\bforbidden\b|\bbanned\b)",
    re.IGNORECASE,
)

#: Modal-capability negation ("could not run", "was unable to run", "failed
#: to run") and past-tense reporting frames ("they stated", "reported that")
#: that mark a match as REPORTED SPEECH -- prose narrating someone's (in)
#: ability to run something, or quoting/summarizing a claim about a run,
#: never an instruction to run anything. Distinct from ``_NEGATION_RE``: a
#: negation is an instruction NOT to do something ("do not run pytest"); a
#: report is a narrative claim about a DIFFERENT actor's past action or
#: inability ("they stated they could not run pytest"). 2026-07-28 repro:
#: "They stated plainly they could not run pytest to confirm." classified
#: ``position="imperative"`` (denying) despite being pure reported speech
#: with no instruction anywhere in it -- ``_NEGATION_RE`` only recognizes
#: ``do not run``/``don't run``/``never run``, not modal-capability
#: negation shapes like ``could not run``.
#:
#: Checked in ``classify_text``'s ``_emit`` ONLY when ``_NEGATION_RE`` did
#: NOT already match (existing-negation-wins precedence) -- a "never run"
#: match already correctly reports "negated" today and must keep doing so,
#: not flip to the new "reported" value.
_REPORTED_SPEECH_RE = re.compile(
    r"(?:\b(?:could\s+not|couldn't|can\s+not|cannot|can't|"
    r"was\s+(?:not\s+)?(?:un)?able\s+to|were\s+(?:not\s+)?(?:un)?able\s+to|"
    r"failed\s+to|did\s+not|didn't|has\s+not|have\s+not|never)\s+"
    r"(?:run|ran|running|execute|invoke)\b"
    r"|\bthey\s+(?:stated|said|reported)\b|\breported\s+that\b|"
    r"\bnoted\s+that\b|\bsaid\s+they\b)",
    re.IGNORECASE,
)

#: Shared negation look-back distance (characters) applied uniformly across
#: every ``classify_text`` pass (fenced, inline-code, bare-line) so none of
#: the three gives a negation marker on a preceding line less reach than the
#: others -- a bare-line match with a shorter (or zero) look-back than its
#: fenced/inline siblings is exactly the false-"imperative" gap this constant
#: closes. 300 chars is a few lines of prose: enough to reach a negation
#: marker on the immediately preceding sentence/line without reaching back
#: across an unrelated earlier command several paragraphs up in a long brief.
_NEGATION_LOOKBACK = 300

#: Execution-intent verbs/phrases that license treating a BARE (non-fenced,
#: non-inline-code) mention of a runner token as an actual command rather
#: than a narrative/nominal mention of the runner's name. Closed set, same
#: spirit as ``_NEGATION_RE``'s marker list (closed-set-ness only -- the
#: matching MECHANISM differs, see the clause-scoping note below) -- this
#: does NOT enumerate every possible non-command sentence shape (that is
#: unbounded, and enumerating it is the denylist-of-English-words trap this
#: fix deliberately avoids); it enumerates a broad, closed vocabulary of
#: verbs/phrases that actually issue a command in English ("run pytest",
#: "verify with pytest", "please pytest the tree", "kick off pytest").
# Review: coordinator:code-reviewer (Finding 2, P0) -- the original 10-verb
# closed set missed ordinary command-issuing English ("please", "just do",
# "kick off", "start ... and monitor"). Broadened substantially. A closed
# set gating a detection gate is a recall risk (unlike ``_NEGATION_RE``'s
# closed set, which only demotes an already-caught match's label) -- this
# does not make the set exhaustive, it narrows the gap. No blanket
# "default to command when neither cue nor prose-negative matches" fallback
# was added: that would also flip the Defect-A clause ("A start ceremony
# that invokes pytest is a several-minute stall...", a genuinely
# descriptive/prose sentence) to a false positive, regressing the peer's
# fix at cross-repo/inbox/2026-07-25-example-doctrine-repo-em-dispatch-suite-
# classifier-two-live-defects.md. The middle path taken: broaden the cue
# vocabulary (this set) and add explicit prose-negative patterns (see
# ``_PROSE_NEGATIVE_RE`` below), leaving the existing lead-strip fallback
# (nothing-but-cosmetic-markers precedes the runner) as the only unmatched
# default -- narrower than the reviewer's suggested fix, but the reviewer's
# suggested fix conflicts with a hard must-preserve regression.
#
# "start"/"starting" are deliberately EXCLUDED from this clause-wide set
# (unlike every other cue here) and instead checked by ``_START_CUE_TAIL_RE``
# below: the Defect-A repro's own clause literally contains the word
# "start" as a plain noun-phrase head ("A start ceremony that invokes
# pytest ..."), so a clause-wide "start" cue reopens that exact regression.
# "start" is real signal only when it is the word immediately governing the
# runner (adjacent, at the clause tail) -- "start pytest" -- not merely
# co-occurring anywhere in the clause.
#
# ``do`` carries a negative lookahead (``do(?!\s+not\b)``) rather than a
# plain bare-word match: "do" alone is not just an imperative cue ("please
# do pytest"), it is also the ordinary English negator lead-in "do not
# ...", and a bare-word "do" cue fired on "do not weaken the guard to make
# tests pass" (2026-07-26 repro) -- a prose instruction NOT to run
# anything, whose "not" governs a different verb ("weaken") than the one
# ``_NEGATION_RE``'s ``\bdo not run\b`` marker checks for, so that marker
# never fires either. The lookahead vetoes "do" specifically when directly
# followed by "not" (the negator shape), leaving every other "do ..." use
# (including "do not run", where "run" is its own independent cue) as real
# signal.
#: ``verify``/``verifying`` carry a negative lookbehind for a ``re-`` prefix
#: (2026-07-26 repro: "Do NOT re-verify claims your prior pass already
#: confirmed clean (... the pytest result ...). Those are settled."). ``\b``
#: matches at the boundary between a hyphen and a following letter exactly
#: as it does at whitespace, so the bare word ``verify`` inside the compound
#: ``re-verify`` satisfied this cue with no lookbehind guard -- "re-verify
#: claims" means "double-check something already confirmed", not "invoke
#: pytest", and the runner token it licensed here sat deep inside an
#: unrelated parenthetical list, nowhere near being the object of that verb.
#: The lookbehind is scoped to ``verify``/``verifying`` only, not the whole
#: cue set: unlike "re-verify", "re-run pytest" (and "re-execute", "re-
#: invoke", ...) genuinely does mean "invoke the runner again" and must stay
#: real signal -- broadening the exclusion to every cue word would silently
#: reopen recall on that legitimate shape.
_IMPERATIVE_CUE_RE = re.compile(
    r"\b(?:run|running|execute|executing|invoke|invoking|launch|launching|"
    r"(?<!re-)verify|(?<!re-)verifying|call|calling|please|just|"
    r"do(?!\s+not\b)|begin|"
    r"beginning|trigger|triggering|perform|performing|kick\s+off|"
    r"fire\s+off|go\s+ahead\s+and)\b",
    re.IGNORECASE,
)

#: "start"/"starting" as a cue is licensed ONLY when it is the word
#: immediately preceding the runner token (the tail of the clause) -- see
#: the exclusion note on ``_IMPERATIVE_CUE_RE`` above.
_START_CUE_TAIL_RE = re.compile(r"\bstart(?:ing)?\s*$", re.IGNORECASE)

#: Prose-shape NEGATIVE patterns -- a runner token appearing as the OBJECT
#: of an ordinary preposition or copula, never as the head of a command.
#: Checked ahead of the lead-strip fallback so a broadened cue set (above)
#: can never override a clause that structurally reads as prose even when
#: it happens to contain a cue-adjacent word elsewhere.
# Review: coordinator:code-reviewer (Finding 2, P0) -- explicit prose-shape
# negative patterns per the suggested fix, covering the repro corpus's
# copula/preposition shapes ("is in pytest testpaths", "as a pytest
# oracle", "backed by a re-runnable pytest node id").
_PROSE_NEGATIVE_RE = re.compile(
    r"(?:\bis\s+in\b|\bas\s+an?\b|\bbacked\s+by\b|\bre-runnable\b|"
    r"\babout\b|\bmentions?\b)",
    re.IGNORECASE,
)

#: Cosmetic lead-in stripped off a bare line's pre-runner prefix before
#: testing whether nothing of substance precedes the runner token: plain
#: whitespace, shell prompt markers (``$``), and bullet/numbered-list
#: markers (``-``, ``*``, ``1.``, ``2)``).
# Review: coordinator:code-reviewer (Finding 4, P2) -- ``#`` and ``>`` were
# previously in this permissive class, treating markdown heading/blockquote
# markers as equivalent to a shell prompt lead-in; a bare (non-fenced) line
# `` # pytest configuration notes`` or ``> pytest already covers this``
# then over-stripped to empty and mis-classified as command-shaped. Both
# dropped -- they are markdown structural characters in bare-line prose,
# not command-line lead-ins.
_BARE_LINE_LEAD_RE = re.compile(r"^[\s\-*\$\d\.\)]+")

#: A single lettered ordered-list marker (``a.``, ``B)``) at the very start
#: of the prefix, stripped ahead of ``_BARE_LINE_LEAD_RE`` -- that regex's
#: permissive character class cannot safely include bare letters (doing so
#: would strip the leading letters off ordinary prose words too, e.g.
#: "Run "), so a lettered marker gets its own narrowly-anchored pattern
#: instead: exactly one letter immediately followed by ``.``/``)``.
# Review: coordinator:code-reviewer (Finding 3, P1) -- numeric list markers
# (``1.``, ``2)``) were already covered by ``_BARE_LINE_LEAD_RE``; lettered
# markers (``a.``, ``b)``) were not, so a lettered-list command line like
# ``b. pytest`` evaded detection while the numeric equivalent was caught.
_LIST_MARKER_LEAD_RE = re.compile(r"^[A-Za-z][.\)]\s*")

#: Clause boundary for scoping the imperative-cue search (see
#: ``_bare_line_is_command_shaped``) to the CURRENT clause rather than the
#: whole prefix -- a sentence/clause break a cue must not reach across.
#: Restricted to SENTENCE-ending punctuation only (``.``/``;``).
# Review: coordinator:code-reviewer (Finding 1, P0) -- ``:``/``,`` were
# previously clause boundaries too, so a colon-headed label instruction
# ("Run: pytest", "Verify: pytest" -- an extremely common dispatch-brief/
# README idiom) discarded the cue word into the segment BEFORE the split,
# evading detection even though the cue is a literal member of
# ``_IMPERATIVE_CUE_RE``. A colon/comma used as a label separator is not a
# sentence boundary; only ``.``/``;`` genuinely end a clause for this
# purpose. Verified this does not regress the Defect-A repro below (its
# clause break is a ``.``, unaffected by dropping ``:``/``,``).
# A LINE BREAK was added as a clause boundary, then REVERTED the same day
# (2026-07-28) -- do not re-add it here. This regex is SHARED with the
# reported-speech check (``_REPORTED_SPEECH_RE.search(clause)`` in
# ``classify_text``), which is deliberately designed to reach BACKWARDS
# across a line break -- a modal-negation cue on the preceding line
# ("They said they could not\nrun pytest to confirm.") is otherwise
# invisible to it, and treating a soft-wrapped sentence as two separate
# clauses turned that exact repro into a false "imperative" (the class this
# whole module exists to eliminate), because the negation marker sat on the
# far side of a boundary only ``_cue_is_clause_head`` actually needed. A
# boundary that suits one consumer of this shared regex is not automatically
# safe for the others -- same lesson as the ``:``/``,`` note above, this
# time on line breaks rather than punctuation. The governance check that
# DOES need line-scoping (``_cue_is_clause_head``'s "nothing of substance
# precedes the cue" test -- see ``test_position_imperative_bare_line_
# negation_too_far_above``, 400 chars of unrelated padding on a prior line)
# now does its own newline truncation locally, on the pre-cue text only,
# instead of this module-wide regex doing it for every caller.
_CLAUSE_BOUNDARY_RE = re.compile(r"[.;]")


def _bare_line_is_command_shaped(prefix: str) -> bool:
    """Does the text preceding a runner token on its own bare (non-fenced,
    non-inline-code) line justify treating that token as an actual command
    invocation, rather than a narrative mention of the runner's name?

    Checked in order, first match wins:

    1. False if a prose-shape NEGATIVE pattern (``_PROSE_NEGATIVE_RE``) --
       a preposition/copula that takes the runner as its object -- appears
       in the CURRENT CLAUSE. "... is in pytest `testpaths` and", "as a
       pytest oracle", "backed by a re-runnable pytest node id" -- prose
       that MENTIONS the runner without ever instructing anything be run.
    2. True if an execution-intent verb/phrase (``_IMPERATIVE_CUE_RE``)
       appears in the CURRENT CLAUSE of ``prefix`` -- the text since the
       last ``.``/``;`` -- "Run pytest ...", "then run python3 -m pytest
       ...", "verify with pytest ...", "Run: pytest", "please pytest the
       whole tree", "kick off pytest".
    3. True if nothing but a shell prompt / list marker (numeric OR
       lettered) / whitespace precedes the runner, i.e. the runner IS the
       line's command head once cosmetic lead-in is stripped (a bare
       ``$ pytest -v``, ``b. pytest``, or standalone ``pytest`` line with
       no fence).
    4. False otherwise -- substantial prose precedes the runner with
       neither a recognized cue nor a recognized negative pattern. This is
       a narrower default than "command unless proven prose": broadening
       it further is exactly what re-opens the Defect-A regression (see
       ``_IMPERATIVE_CUE_RE``'s docstring note).

    This is the fix for the 2026-07-25 false-positive class (repro:
    /private/tmp/claude-501/.../guard-false-positive-repro.md): the
    bare-line pass previously classified ANY line containing a runner
    token as a command regardless of what preceded it, so ordinary prose
    describing pytest configuration was indistinguishable from an
    instruction to run pytest. The documented ``_NEGATION_RE`` carve-out
    could never fix this -- these fragments are not negated commands, they
    are not commands at all, and negation only flips the ``position`` of
    an already-detected match, it does not gate detection itself.

    The cue search is CLAUSE-scoped, not whole-prefix -- a second,
    independent false-positive class (2026-07-25, cross-repo/inbox/
    2026-07-25-example-doctrine-repo-em-dispatch-suite-classifier-two-live-defects.md
    Defect A): "Neither consumer may run the test tier or block the
    ceremony. A start ceremony that invokes pytest is a several-minute
    stall ..." has a ``run`` in its FIRST sentence (itself part of a
    prohibition) that, under a whole-prefix search, licensed the bare
    ``pytest`` mention in the wholly separate SECOND sentence. A cue must
    govern the runner token it precedes, not merely co-occur somewhere
    earlier on the same (possibly multi-sentence) line.
    """
    clause = _CLAUSE_BOUNDARY_RE.split(prefix)[-1]
    if _PROSE_NEGATIVE_RE.search(clause):
        return False
    if _IMPERATIVE_CUE_RE.search(clause) or _START_CUE_TAIL_RE.search(clause):
        return True
    lead_source = _LIST_MARKER_LEAD_RE.sub("", prefix, count=1)
    lead = _BARE_LINE_LEAD_RE.sub("", lead_source)
    return not lead.strip()


#: A HYPHENATED ``re-`` repetition prefix directly touching an imperative
#: cue -- "re-run pytest", "re-execute the suite" -- is part of the cue's
#: OWN head word, not an auxiliary/modal governing it. Stripped from the
#: TAIL of the pre-cue text in ``_cue_is_clause_head`` before the
#: auxiliary/modal/copula check runs, so it is never mistaken for one. The
#: hyphen is REQUIRED (unlike a looser bare-``re`` match): a bare ``re``
#: with no hyphen is not a repetition prefix at all, it is the tail two
#: letters of an ordinary word -- "sessions ARE running" -- and a
#: hyphen-optional version of this pattern strips that "re" right out of
#: "are", silently deleting the very copula the check exists to detect
#: (2026-07-28 regression caught while validating this fix). Mirrors
#: ``_IMPERATIVE_CUE_RE``'s own ``(?<!re-)`` carve-out for ``verify`` in
#: spirit -- that carve-out excludes ``re-verify`` from being a cue at all;
#: this one instead keeps ``re-run`` a cue AND still recognizes it as
#: clause-initial.
_RE_PREFIX_TAIL_RE = re.compile(r"re-\s*$", re.IGNORECASE)

#: Fronted-adverbial boundary. An English imperative may carry a
#: comma-separated adverbial phrase in front of its verb without ceasing to
#: be an imperative ("Before you report back, run pytest across your
#: changes."), and that phrase routinely contains its own subject ("you").
#: The governance check below therefore measures from the LAST comma in the
#: clause rather than from the clause start, so a fronted adverbial is never
#: mistaken for the cue's own subject. A comma is deliberately NOT a
#: ``_CLAUSE_BOUNDARY_RE`` member (that would reopen the "Run: pytest"
#: label-idiom recall hole); it is a boundary for THIS check only.
_FRONTED_ADVERBIAL_BOUNDARY_RE = re.compile(r",")

#: Lead-in that may sit in front of a genuine imperative's verb without
#: making the clause declarative: a coordinating conjunction, a discourse
#: adverb, or any single ``-ly`` manner adverb ("Then run pytest", "Finally,
#: re-run pytest", "Carefully run pytest"). Stripped repeatedly from the
#: START of the pre-cue text before the leftover is tested for substance.
#: Note ``please``/``just`` are absent deliberately -- they are themselves
#: members of ``_IMPERATIVE_CUE_RE``, so the cue search finds THEM first and
#: the pre-cue text is empty before this ever runs.
_IMPERATIVE_LEAD_ADVERB_RE = re.compile(
    r"^(?:(?:and|or|but|so|then|now|next|first|finally|also|again|"
    r"afterwards|subsequently|optionally|ideally|instead)\b|\w+ly\b)\s*",
    re.IGNORECASE,
)

#: Governance test: does anything of SUBSTANCE sit between the start of the
#: cue's own clause-or-fronted-adverbial segment and the cue itself? A
#: genuine English imperative has no overt subject -- the verb IS the clause
#: head -- so any surviving leftover (a subject NP, an auxiliary, a modal, a
#: copula, a governing preposition) means the clause is DECLARATIVE prose
#: about running something rather than an instruction to run it.
#:
#: This replaced a narrower adjacency-only check (2026-07-28) that tested
#: only for an auxiliary/modal/copula sitting IMMEDIATELY before the cue.
#: Adjacency caught the three reported repros but not the class: an
#: intervening adverb ("sessions were repeatedly running pytest", "CI is
#: currently running pytest") or a bare finite verb with no auxiliary at all
#: ("Peer sessions run pytest on a shared worktree") walked straight through
#: it -- the last being the reported incident sentence merely rephrased out
#: of the progressive. A lexicon of auxiliaries is a smaller bag of words
#: than a lexicon of cue verbs, but it is still a bag of words; presence of
#: a subject is the structural property that actually discriminates.
_SUBSTANTIVE_LEFTOVER_RE = re.compile(r"\w")


def _cue_is_clause_head(clause: str) -> bool:
    """Does an imperative cue found in ``clause`` (per ``_IMPERATIVE_CUE_RE``
    / ``_START_CUE_TAIL_RE``) sit at the HEAD of its own clause -- i.e. is
    the cue verb the first thing of substance in the clause, the way a real
    English imperative's verb always is?

    This is the STRUCTURAL predicate that ``_bare_line_is_command_shaped``'s
    lexical cue search cannot express on its own: a bag-of-words cue match
    has no notion of what governs the runner token, only that the word
    appears somewhere in the clause. An English imperative has no overt
    subject -- "run pytest", "Please run pytest", "re-run pytest", "Then run
    pytest", "Before you report back, run pytest" (the fronted adverbial's
    own "you" is measured off by ``_FRONTED_ADVERBIAL_BOUNDARY_RE``). Every
    false-positive repro in the 2026-07 corpus instead has a subject sitting
    in front of the cue, with or without an auxiliary:

    - "Neither consumer **may run** the test tier" (subject + modal)
    - "they **could not run** pytest to confirm" (subject + modal + negator)
    - "other sessions **are running** pytest" (subject + copula)
    - "CI **is currently running** pytest" (subject + copula + adverb)
    - "Peer sessions **run** pytest on a shared worktree" (subject, no aux)
    - "an attitude toward **running** pytest daily" (governing preposition)

    Negative spec -- what this deliberately does NOT do. It does not decide
    whether a match is emitted at all: ``_bare_line_is_command_shaped``
    still owns that gate, and per DR-088 layer 2's negative spec (module
    docstring) a match is never suppressed once found. It decides only
    whether the caller (``classify_text``) may report the strongest
    ``"imperative"`` position for an already-included match, or must fall
    back to ``"descriptive"`` (see ``SuiteMatch``'s ``position`` docstring).
    Widening the label set is in charter under DR-230; narrowing the match
    set is not, and this function must never grow into doing so.

    Returns ``True`` when no cue is found at all: the caller only reaches
    for this after already establishing a command-shaped bare line
    elsewhere (a cue-bearing clause, or a bare command line with nothing
    but cosmetic lead-in, which by construction has no subject in front of
    it).

    Recall bias is deliberate and asymmetric. A clause whose grammar this
    cannot resolve loses only the ``"imperative"`` label, and layer 2 is
    defence-in-depth over layer 3's identity leg, which fail-CLOSES on any
    suite-shaped argv carrying a top-level ``agent_id`` and never consults
    this path. A false positive is the costlier error: each one applies live
    pressure toward the repo-root sentinel that disables the guard for every
    dispatch.

    Known-open gap (P2, review 2026-07-28, not fixed here): a cue word that
    is itself a gerund-subject head is not distinguished from the same word
    used as an imperative verb -- "Only running pytest is required to
    confirm this." strips the sole preceding word "Only" as a lead adverb
    (``_IMPERATIVE_LEAD_ADVERB_RE``'s ``\\w+ly`` alternative), leaving no
    leftover, so this returns ``True`` for a declarative gerund-subject
    sentence. The more obvious-looking counterexample -- an ordinary
    ``-ly``-ending NOUN as a bare subject ("Assembly runs the tests",
    "Italy runs pytest") -- is NOT actually realizable: English
    subject-verb agreement forces the ``-s`` verb form after a singular
    subject like that, which ``_IMPERATIVE_CUE_RE`` does not match (no
    ``\\brun\\b`` hit on "runs"), and the plural form that would take the
    bare verb ends in ``-ies``, not ``-ly`` ("families", not "familys").
    The live shape of this gap is specifically cue-as-gerund-subject, as
    above -- left open rather than fixed, consistent with this function's
    own stated recall bias (a false positive is the costlier error).

    Known-open gap (P3, review 2026-07-28, not fixed here): a fronted
    adverbial WITHOUT a comma is not measured off by
    ``_FRONTED_ADVERBIAL_BOUNDARY_RE`` at all (that boundary is comma-only
    by construction), so "After merging your change run pytest to confirm."
    leaves the whole "After merging your change " phrase as leftover and
    this returns ``False`` -- a real imperative demoted to
    ``"descriptive"``, not promoted to a false ``"imperative"``. This
    module's own hedge ("Before you report back, run pytest" still denies)
    is therefore COMMA-DEPENDENT, not comma-independent as a looser reading
    of that example might suggest. Deliberately left unfixed: stripping a
    leading subordinator-headed phrase to close this gap would also strip
    it from a genuinely declarative clause ("After the peer sessions run
    pytest nightly, the dashboard updates") and promote THAT to a false
    ``"imperative"`` -- trading a cheap false negative (caught by layer 3's
    identity leg, which fail-CLOSES on real argv and never consults this
    path) for the expensive false positive this whole module exists to
    eliminate. See ``test_position_fronted_adverbial_without_comma_stays_
    descriptive_deliberate_gap`` for the pinned behavior and the do-not-fix
    rationale restated at the call site.
    """
    m = _IMPERATIVE_CUE_RE.search(clause) or _START_CUE_TAIL_RE.search(clause)
    if not m:
        return True
    # Line-scope the pre-cue text FIRST, before the fronted-adverbial comma
    # split: ``clause`` (per ``_CLAUSE_BOUNDARY_RE``, punctuation-only) can
    # legitimately span multiple lines -- that reach is what the reported-
    # speech check needs (see ``_CLAUSE_BOUNDARY_RE``'s docstring) -- but a
    # cue's OWN governance must not look further back than its own line, or
    # an unrelated preceding line's prose (or, worse, hundreds of characters
    # of padding) reads as a subject sitting in front of this line's cue.
    # See ``test_position_imperative_bare_line_negation_too_far_above``.
    before = clause[:m.start()].rsplit("\n", 1)[-1]
    before = _FRONTED_ADVERBIAL_BOUNDARY_RE.split(before)[-1]
    before = _RE_PREFIX_TAIL_RE.sub("", before)
    before = _LIST_MARKER_LEAD_RE.sub("", before.lstrip(), count=1)
    before = _BARE_LINE_LEAD_RE.sub("", before)
    previous = None
    while previous != before:
        previous = before
        before = _IMPERATIVE_LEAD_ADVERB_RE.sub("", before)
    return not _SUBSTANTIVE_LEFTOVER_RE.search(before)


#: Sentence-punctuation character class matched at the END of a
#: whitespace-delimited token -- the ``.`` in "run pytest." -- as opposed to
#: punctuation INSIDE one, which is load-bearing argv content
#: (``tests/test_foo.py``, ``pytest.ini``, ``test_foo.py::test_bar``).
#: ``_strip_sentence_punctuation`` walks the string itself (quote-aware,
#: see its own docstring) rather than a single regex substitution, so this
#: is a plain character set, not a compiled pattern.
_SENTENCE_PUNCTUATION_CHARS = ".,;:!?"


def _strip_sentence_punctuation(candidate: str) -> str:
    """Blank out token-final sentence punctuation in a PROSE candidate so an
    ordinary written instruction still tokenizes as the command it is.

    Prose ends sentences with punctuation and argv does not. "run pytest."
    handed to the tokenizer verbatim yields ``argv[0] == "pytest."``, which
    matches no known runner, so the whole match was dropped -- the guard saw
    nothing at all. That made layer 2 fire on the sloppily-punctuated half of
    its input and miss the well-punctuated half ("run pytest.", "run npm
    test.", "run python3 -m pytest."), which is the most natural way to write
    the very instruction it exists to catch.

    Replaces rather than deletes, preserving string length: every span this
    classifier reports is an offset into the ORIGINAL text, and a
    length-changing normalization would silently slide every subsequent
    ``matched_text``/``span`` off its real position.

    Prose-path only. ``classify_command`` (layer 3, real argv from a live
    Bash call) must never be normalized this way -- a trailing ``.`` there is
    a real argument the shell actually passed, not a typographic artifact.

    Quote-aware: punctuation inside a single- or double-quoted span is left
    untouched, even when it is itself followed by whitespace ("run pytest -m
    'not slow: fast'." has a colon-then-space *inside* the quoted ``-m``
    expression). A quote-blind pass would blank that colon along with the
    genuine sentence-final period, corrupting the quoted expression's content
    before it reaches the tokenizer -- caught 2026-07-28 via repro
    ``"run pytest -m 'not slow: fast'."`` denying with ``matched_text ==
    "pytest -m 'not slow  fast'"``. Quote tracking here is a simple toggle
    (no backslash-escape handling), matching ``_tokens``'s own ``shlex``
    call's usual input shape for this guard rather than a full shell-quoting
    grammar.
    """
    chars = list(candidate)
    in_squote = False
    in_dquote = False
    n = len(chars)
    for i, ch in enumerate(chars):
        if ch == "'" and not in_dquote:
            in_squote = not in_squote
            continue
        if ch == '"' and not in_squote:
            in_dquote = not in_dquote
            continue
        if in_squote or in_dquote:
            continue
        if ch in _SENTENCE_PUNCTUATION_CHARS:
            nxt = chars[i + 1] if i + 1 < n else None
            if nxt is None or nxt.isspace():
                chars[i] = " "
    return "".join(chars)


def _mask(text: str, spans: Sequence[Tuple[int, int]]) -> str:
    """Blank out ``spans`` in ``text`` (newlines preserved) so a later regex
    pass over the result cannot re-match content already claimed by an
    earlier pass (fenced code, then inline code), while every offset into
    the ORIGINAL string stays valid."""
    chars = list(text)
    for s, e in spans:
        for i in range(s, min(e, len(chars))):
            if chars[i] != "\n":
                chars[i] = " "
    return "".join(chars)


def _fence_spans(
    text: str,
) -> Tuple[List[Tuple[int, int, int, int]], Optional[Tuple[int, int, int, int]]]:
    """Pair up consecutive ``` occurrences into fenced-code-block spans.

    Returns ``(fences, unterminated)`` where each fence/the unterminated tail
    is ``(content_start, content_end, fence_open_start, fence_full_end)``.
    An odd trailing ``` (no closing fence before end of text) is reported
    separately as ``unterminated`` rather than silently paired or dropped --
    a truncated brief's dangling fence is genuinely ambiguous, not code and
    not prose, hence ``position="unknown"`` for anything found inside it.
    """
    positions = [m.start() for m in re.finditer(r"```", text)]
    fences: List[Tuple[int, int, int, int]] = []
    i = 0
    while i + 1 < len(positions):
        open_pos, close_pos = positions[i], positions[i + 1]
        nl = text.find("\n", open_pos + 3)
        content_start = nl + 1 if nl != -1 and nl < close_pos else open_pos + 3
        fences.append((content_start, close_pos, open_pos, close_pos + 3))
        i += 2
    unterminated = None
    if i < len(positions):
        open_pos = positions[i]
        nl = text.find("\n", open_pos + 3)
        content_start = nl + 1 if nl != -1 else open_pos + 3
        unterminated = (content_start, len(text), open_pos, len(text))
    return fences, unterminated


_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _iter_prose_candidates(text: str) -> List[Tuple[int, str, str, int, int]]:
    """Enumerate every prose candidate a ``classify_text``-family API must
    inspect: each non-blank line of fenced-code content, each non-blank line
    of an unterminated trailing fence, each non-blank line of an inline-code
    span's content, and each bare (non-fenced, non-inline) line that both
    carries a runner-prefilter hit and passes
    ``_bare_line_is_command_shaped``.

    Returns ``(abs_start, line_body, base_position, window_start,
    window_end)`` tuples -- single-sourced extraction shared by
    ``classify_text`` and ``classify_text_precision`` so the fence/inline/
    bare-line walk and the negation-lookback window computation can never
    silently drift between the two consumers. ``window_start``/
    ``window_end`` are fixed per enclosing block (fence, inline span, or
    bare line), not recomputed per physical line within it -- unchanged from
    ``classify_text``'s prior single-consumer behavior.
    """
    out: List[Tuple[int, str, str, int, int]] = []

    def _add_block(sub_start: int, content: str, base_position: str,
                   window_start: int, window_end: int) -> None:
        # Enumerate line-by-line, not the whole (possibly multi-line) blob as
        # ONE command -- a fenced block quoting several independent commands
        # is several commands, not one command whose later lines shlex-split
        # into bogus positional arguments of the first.
        offset = 0
        for line in content.splitlines(keepends=True):
            line_body = line.rstrip("\n")
            if line_body.strip():
                out.append((sub_start + offset, line_body, base_position,
                           max(0, window_start), min(len(text), window_end)))
            offset += len(line)

    fences, unterminated = _fence_spans(text)
    fenced_full_spans = [(f[2], f[3]) for f in fences]
    if unterminated:
        fenced_full_spans.append((unterminated[2], unterminated[3]))

    for content_start, content_end, open_start, close_end in fences:
        _add_block(content_start, text[content_start:content_end], "fenced_code",
                  open_start - _NEGATION_LOOKBACK, close_end + 50)

    if unterminated:
        content_start, content_end, open_start, _tail = unterminated
        _add_block(content_start, text[content_start:content_end], "unknown",
                  open_start - _NEGATION_LOOKBACK, content_end)

    masked_for_inline = _mask(text, fenced_full_spans)
    inline_spans: List[Tuple[int, int, int, int]] = []
    for m in _INLINE_CODE_RE.finditer(masked_for_inline):
        inline_spans.append((m.start(1), m.end(1), m.start(), m.end()))

    for content_start, content_end, full_start, full_end in inline_spans:
        line_start = text.rfind("\n", 0, full_start) + 1
        line_end_idx = text.find("\n", full_end)
        line_end = line_end_idx if line_end_idx != -1 else len(text)
        # Same reach as the fenced/bare-line passes: a negation marker on the
        # PRECEDING line ("Do not run this:\n`pytest`") is otherwise
        # invisible to a window confined to the backtick-span's own line.
        _add_block(content_start, text[content_start:content_end], "inline_code",
                  line_start - _NEGATION_LOOKBACK, line_end)

    masked_for_bare = _mask(masked_for_inline, [(s[2], s[3]) for s in inline_spans])
    for line_match in re.finditer(r"[^\n]+", masked_for_bare):
        line = line_match.group(0)
        if not line.strip():
            continue
        prefilter_match = _RUNNER_PREFILTER_RE.search(line)
        if not prefilter_match:
            continue
        # A bare line's runner token is scanned as a command only when
        # something on the line actually signals command intent -- an
        # execution verb, or the runner being the line's own head. Absent
        # that, the runner token is just as likely a narrative/nominal
        # mention ("... is in pytest `testpaths` and", "a pytest oracle")
        # as an instruction to run it, and treating every such mention as
        # an invocation is the P1 false-positive class this guards against.
        # See ``_bare_line_is_command_shaped`` for the full rationale.
        if not _bare_line_is_command_shaped(line[:prefilter_match.start()]):
            continue
        # Anchor the candidate at the runner token itself, not the start of
        # the line -- ordinary prose text before it ("Run ", "Before you
        # report back, ") is not part of any argv and would otherwise become
        # a bogus argv[0] the classifier can't recognize as a runner at all.
        candidate_start = line_match.start() + prefilter_match.start()
        real_candidate = _strip_sentence_punctuation(
            text[candidate_start:line_match.end()])
        # Give the bare-line pass the same look-back reach as the fenced
        # pass: a negation marker on a preceding prose line ("Do not run
        # this:\npytest") is otherwise invisible here, since the matched
        # line alone never contains it.
        out.append((candidate_start, real_candidate, "imperative",
                   max(0, line_match.start() - _NEGATION_LOOKBACK), line_match.end()))

    return out


def _resolve_position(text: str, abs_start: int, base_position: str,
                      window_start: int, window_end: int) -> str:
    """Final ``position`` label for a match found at ``abs_start`` within
    ``text``, given the enclosing candidate's ``base_position`` and
    negation-lookback window. Single-sourced by ``classify_text`` and
    ``classify_text_precision`` -- see ``_iter_prose_candidates``.
    """
    w_start = max(0, window_start)
    w_end = min(len(text), window_end)
    window = text[w_start:w_end]
    if _NEGATION_RE.search(window):
        return "negated"
    # Reported-speech is checked in the BACKWARD-ONLY, CLAUSE-scoped slice of
    # the same already-computed ``window`` -- never a second, independently
    # windowed regex pass. This is deliberately NARROWER than
    # ``_NEGATION_RE``'s reach (which scans the whole window, forward and
    # back, across clause boundaries): a reported-speech cue several
    # sentences before a genuinely separate, later imperative command on the
    # same physical line must NOT suppress that later command. Repro this
    # guards: "They could not run pytest. Run `pytest -q` yourself and
    # report the result." -- the first clause's "could not run" is reported
    # speech (and is itself correctly classified "reported" when ``pytest``
    # there is picked up as its own bare-line match), but must not bleed
    # forward and demote the SECOND, genuinely imperative `pytest -q`
    # command out of "imperative".
    backward = window[:max(0, abs_start - w_start)]
    clause = _CLAUSE_BOUNDARY_RE.split(backward)[-1]
    if _REPORTED_SPEECH_RE.search(clause):
        return "reported"
    if base_position == "imperative" and not _cue_is_clause_head(clause):
        # The bare-line pass's own gate (``_bare_line_is_command_shaped``)
        # already decided this mention is command-shaped enough to include
        # -- per DR-088 layer 2's negative spec (module docstring) a found
        # match is never dropped. This is the STRUCTURAL check on TOP of
        # that inclusion decision: the cue word is present in the clause,
        # but does not GOVERN the runner token as the clause's own head verb
        # (a subject or auxiliary/modal/copula sits in that position
        # instead), so the strongest "imperative" label is withheld in
        # favor of "descriptive" -- see ``_cue_is_clause_head``.
        return "descriptive"
    return base_position


def classify_text(text: str, *, cwd: Optional[str] = None) -> List[SuiteMatch]:
    """Extract and classify candidate command strings out of arbitrary prose
    (a dispatch brief), zero-to-many matches. Makes no assumption that
    ``text`` is markdown -- fenced code blocks and inline-code spans are
    recognized where present, but a bare line with no backticks at all is
    still scanned when it contains a known runner token.

    Reports ``position`` per match (see ``SuiteMatch``) so the caller can
    apply its own policy to a quoted/negated command differently from an
    imperative one -- this function only classifies, it never decides.

    Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 1 (false-positive leg)
    """
    if not isinstance(text, str) or not text:
        return []

    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)

    matches: List[SuiteMatch] = []
    for abs_start, line_body, base_position, window_start, window_end in _iter_prose_candidates(text):
        for m in _classify_command_core(line_body, cwd, testpaths, configured):
            match_start = abs_start + m.span[0]
            match_end = abs_start + m.span[1]
            position = _resolve_position(text, match_start, base_position, window_start, window_end)
            matches.append(dataclasses.replace(m, span=(match_start, match_end), position=position))
    return matches


# ---------------------------------------------------------------------------
# Public precision-classification API (DR-088 R9 layer-2 seam) -- see
# negative spec and spec backlink in the module docstring above.
# ---------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class PrecisionMatch:
    """One R9-refusable precision match found by ``classify_command_
    precision`` or ``classify_text_precision``: a pytest-family invocation
    that is NOT suite-shaped (``classify_command``/``classify_text`` report
    nothing for it) but that names a directory positional -- exactly the
    shape DR-088 R9's precision leg (``_pytest_directory_args``, ``check()``
    leg 0) refuses for a dispatched agent.

    ``position`` is advisory metadata for the CALLER's own policy, same
    discipline as ``SuiteMatch.position`` -- this dataclass never decides
    anything; it only describes what was found and where.
    """

    #: Runner label, e.g. ``"pytest"`` or ``"python -m pytest"``.
    detected: str
    #: The exact matched command substring, control-character-sanitized.
    matched_text: str
    #: ``(start, end)`` character offsets of ``matched_text`` into the input
    #: string passed to ``classify_command_precision``/``classify_text_precision``.
    span: Tuple[int, int]
    #: Same vocabulary and semantics as ``SuiteMatch.position``.
    position: str
    #: The directory positionals that make this match R9-refusable, in the
    #: literal spelling the caller used -- see ``_pytest_directory_args``.
    directory_args: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.detected,
            "matched_text": self.matched_text,
            "span": list(self.span),
            "position": self.position,
            "directory_args": list(self.directory_args),
        }


def _classify_command_precision_core(
    command: str,
    cwd: Optional[str],
    testpaths: Sequence[str],
    configured: Sequence[ConfiguredCmd],
) -> List[PrecisionMatch]:
    """Shared precision-classification core for ``classify_command_
    precision`` and ``classify_text_precision``.

    Runs each segment of ``command`` through the same segment/argv machinery
    ``_classify_command_core`` uses, but reports a match only for a segment
    that is (1) a pytest-family invocation, (2) NOT already suite-shaped --
    that shape is ``_classify_command_core``'s own business, never restated
    here -- and (3) carries at least one directory positional per
    ``_pytest_directory_args``. Fails open (``[]``) without a ``cwd``,
    mirroring ``_pytest_directory_args``'s own fail-open discipline: a
    directory cannot be distinguished from a filter string on shape alone.
    """
    out: List[PrecisionMatch] = []
    if not command or not cwd:
        return out

    suite_spans = [m.span for m in _classify_command_core(command, cwd, testpaths, configured)]

    def _is_suite_shaped(seg_start: int, seg_end: int) -> bool:
        return any(s < seg_end and seg_start < e for s, e in suite_spans)

    for seg_text, start, end in _segments_with_spans(command):
        stripped = seg_text.strip()
        if not stripped:
            continue
        lead = len(seg_text) - len(seg_text.lstrip())
        trail = len(seg_text) - len(seg_text.rstrip())
        seg_start, seg_end = start + lead, end - trail
        if _is_suite_shaped(seg_start, seg_end):
            continue  # classify_command's/classify_text's business, not ours

        tokens = _tokens(stripped)
        argv = _strip_command_prefix(tokens)
        if not argv:
            continue

        if _base(argv[0]) in _PYTEST_HEADS:
            label = "pytest"
        elif _pytest_module_args(argv) is not None:
            label = "python -m pytest"
        else:
            continue

        dir_args = _pytest_directory_args([argv], cwd)
        if not dir_args:
            continue

        out.append(PrecisionMatch(
            detected=label,
            matched_text=_sanitize(stripped),
            span=(seg_start, seg_end),
            position="imperative",
            directory_args=list(dir_args),
        ))
    return out


def classify_command_precision(command: str, *, cwd: Optional[str] = None) -> List[PrecisionMatch]:
    """Classify EVERY segment of one shell command string for DR-088 R9's
    precision shape, zero-to-many matches. Never short-circuits on the
    first match, mirroring ``classify_command``.

    Spec backlink: cross-repo/inbox/2026-07-28-example-market-data-repo-em-dispatched-agent-scoped-test-breadth.md (example-doctrine-repo repo)
    """
    if not isinstance(command, str) or not command or not cwd:
        return []
    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)
    return _classify_command_precision_core(command, cwd, testpaths, configured)


def classify_text_precision(text: str, *, cwd: Optional[str] = None) -> List[PrecisionMatch]:
    """Extract and classify DR-088 R9 precision matches out of arbitrary
    prose (a dispatch brief), zero-to-many matches. Shares the exact fence/
    inline-code/bare-line extraction and ``position`` machinery
    ``classify_text`` runs through (see ``_iter_prose_candidates``,
    ``_resolve_position``) -- a second hand-rolled prose walk would drift on
    the next fence/negation-window edit.

    Fails open (``[]``) without a ``cwd`` -- see
    ``_classify_command_precision_core``.

    Spec backlink: cross-repo/inbox/2026-07-28-example-market-data-repo-em-dispatched-agent-scoped-test-breadth.md (example-doctrine-repo repo)
    """
    if not isinstance(text, str) or not text or not cwd:
        return []

    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)

    matches: List[PrecisionMatch] = []
    for abs_start, line_body, base_position, window_start, window_end in _iter_prose_candidates(text):
        for pm in _classify_command_precision_core(line_body, cwd, testpaths, configured):
            match_start = abs_start + pm.span[0]
            match_end = abs_start + pm.span[1]
            position = _resolve_position(text, match_start, base_position, window_start, window_end)
            matches.append(dataclasses.replace(pm, span=(match_start, match_end), position=position))
    return matches
