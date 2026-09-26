"""coordinator_core.bash_guards.check_test_suite_invocation -- PreToolUse(Bash)
hard-deny guard for **test-breadth invocation authority** (DoE's DR-088, layers
2, 3, 5 and 6 of the six-layer test-breadth ladder).

Ordered deny legs over one shared classifier -- identity, THEN authority,
THEN resource; a caller with no standing to run a suite at all is told so
before it is ever told to wait for the machine. The resource rung has two
halves, because the ladder's two tiers have two different bounds: ONE
suite-shaped run at a time machine-wide (leg 3), and K scoped runs at a time
machine-wide (leg 0.5):

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
     with a better diagnosis. Ruled by DoE 2026-07-28
     (``cross-repo/inbox/2026-07-28-doe-claude-em-dr088-tier-t-relevance-
     ruling.md``); DoE refused a breadth/test-count term in the same ruling
     (R8) -- a rule the classifier cannot evaluate without collecting is not
     a rule.

  0.5. TIER-T CONCURRENCY leg (the resource half of Tier T, fail-OPEN on
     infra) -- a SCOPED (Tier T) command issued by a SUBAGENT is ALLOWED with
     an ``updatedInput`` rewrite routing it through ``with-tier-t-slot``, the
     wrapper that takes one slot of the machine-wide K-slot semaphore in
     ``coordinator_core.testing.tier_t_slots`` and BLOCKS until one is free. K
     is per-machine, derived fresh from the same ``derive_worker_cap()``
     formula that bounds xdist workers. A CHAINED command degrades to an
     advisory rather than a rewrite (BX-12's single-segment rule -- never
     substitute a chain this guard did not parse).

     THIS LEG NEVER DENIES, and that is not a stylistic choice. It is the
     property that keeps it a resource control rather than an authority one.
     The first implementation DID deny, and turned 29 existing assertions red
     in one run -- assertions pinning "a subagent's scoped run is allowed",
     i.e. the carve-out itself. They were right and the leg was wrong: a
     control that expresses itself as a refusal IS an authority control, no
     matter what its docstring claims, and narrowing Tier T is another plane's
     ruling to change (R9), not this repo's. A guard cannot say "wait" -- but
     it can hand back a command that waits.

     So this does NOT narrow the Tier-T carve-out in the negative spec below,
     and must never be "simplified" into something that does: file- and
     node-id-scoped invocations remain permitted for everyone, always. The leg
     asks only HOW MANY may run at once -- a resource question, not an
     authority one. The distinction is the whole design: leg 0 bounds what a single
     dispatched caller may name, the mutex leg (3) bounds concurrency for
     suite-shaped commands only, and between them Tier T had an authority bound
     and no resource bound at any N.

     The incident, stated correctly because the obvious reading is wrong: on
     2026-09-20 a ``/bug-blitz`` over an 876-record backlog took the box to a
     15-minute load average of 17.83. It was NOT concurrent suites -- the mutex
     worked, killing the one suite-shaped run at its 2700s ceiling with 33
     processes live. The damage came from the TRIAGE leg, which had not yet
     reached its executors, issuing hundreds of scoped runs. Every one was
     individually cheap (0.18s, ~72MB measured) and every one was correctly
     allowed. The cost was the PRODUCT, and no artifact in the chain could see
     a product: the ceremony's evidence bar ("``already-fixed`` needs the
     failing case run against HEAD") is right, this guard's carve-out is right,
     and composed over 876 records they multiply into a spawn count neither can
     observe.

     The EM is deliberately unaffected, matching leg 0's subagent-only scope: a
     single session cannot fan out, so its scoped runs are already serial.

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
     configured commands. A live grant is either the implicit one a DoE
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
     its own if one reopens: the WRITER lives in DoE-claude's tree --
     ``coordinator/commands/<ceremony>.md`` OR
     ``coordinator/skills/<ceremony>/SKILL.md``, and both shapes are in live
     use, which is precisely what an earlier verification missed by grepping
     only the skills tree -- while the claim that a writer exists lives
     here. The two sides can silently diverge again (this list was wrong in
     both directions on 2026-08-07, within one day), so do not restate which
     ceremonies write the implicit grant here without re-verifying against
     DoE's tree first.

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
  - It does NOT deny FILE- or NODE-ID-scoped invocations ON AUTHORITY
    GROUNDS. ``pytest path/test_x.py``, ``pytest path/test_x.py::test_case``
    and the BARE form ``pytest -k expr`` (no positional at all) are Tier T and
    are allowed for everyone, always -- a node id stays permitted for a subagent regardless
    of its touched set, which is what keeps pre-existing-failure verification
    (re-running the one failing test you did not author) legal under leg 0.
    Leg 0.5 does not change this and is NOT an exception to it: a scoped run
    from a subagent is still ALLOWED -- it is merely rewritten to route
    through ``with-tier-t-slot`` so the box can bound how many execute at
    once, and the wrapper waits rather than refusing. Anyone reading this bullet as "Tier T is
    unconditionally unmediated" and deleting leg 0.5 on that basis reopens the
    2026-09-20 incident: the carve-out is about AUTHORITY, and always was.
    ``-k``/``--lf`` alongside a positional that names a ``testpaths`` root
    (``pytest tests/ -k expr`` where ``testpaths = ["tests"]``) is NOT Tier
    T -- see the 2026-08-14 classifier correction below.
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
    manifest entry (DR-088 layer 5) -- DoE owns
    ``coordinator/schemas/tier-u-grant.schema.json`` and its manifest row.
    This module OWNS the guard leg that reads it (the grant leg above) and
    the writer/reader it calls into (``coordinator_core.session.grant``),
    because the liveness primitive that leg needs
    (``coordinator_core.session.liveness``) lives in this repo, not DoE's.

This module also exposes a payload-shape-free public classification API
(``classify_command`` / ``classify_text`` / ``SuiteMatch``) for DR-088 layer
2 -- DoE's ``PreToolUse(Agent|Workflow)`` dispatch-brief-prose guard, which
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
    cross-repo/inbox/2026-07-25-doe-claude-em-dr088-marker-scope-ruling.md).
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
it. The precision API closes that gap for DoE's layer-2 hook, which needs to
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
cross-repo/inbox/2026-08-03-doe-claude-em-klabauter-rows-move-to-you-pm-
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
cross-repo/inbox/2026-08-03-doe-claude-em-two-rulings-plan-orphan-population-
and-dr088-antiscope.md (Finding 2; the relay's "spelling-keyed" framing is
imprecise -- the gap is isdir-keyed, not spelling-keyed). Fixed by expanding
any positional containing glob metacharacters with ``glob.iglob(...,
recursive=True)`` and refusing it if any expansion is a directory; see
``_pytest_directory_args``'s own docstring for the files-only and
zero-match posture calls this fix pins.

Classifier correction, 2026-08-14 -- selection-flag laundering of a
testpaths-root positional: ``_classify_pytest`` read ``_walk_pytest_args``'s
flag-derived ``scoped`` bit (True for ``-k`` and for
``_PYTEST_SCOPING_FLAGS``) and short-circuited on it BEFORE checking whether
any positional was a real scope, so ``pytest tests/ -k "<expr>"`` in a repo
with ``testpaths = ["tests"]`` classified as Tier T -- allowed for
everyone, no identity leg, no Tier-U/F grant leg, no with-suite-mutex
wrapper leg, no mutex leg -- even though pytest still COLLECTS the entire
configured suite to deselect it; ``-k`` filters SELECTION, never
COLLECTION. Confirmed live via example-retrieval-repo-em, which saturated a contested
machine running this shape with nothing firing. Fixed by making a
NON-EMPTY positional list govern outright: suite-shaped unless at least one
positional is a real scope, regardless of the flag-derived bit. An EMPTY
positional list (bare ``pytest -k expr``, bare ``pytest --lf``) is
unaffected -- see ``_classify_pytest``'s own docstring for the full rule and
for why this does not collide with DR-088 R8 (evaluated statically against
configured ``testpaths``, zero collection required). Same class as the
2026-07-30 pnpm and 2026-08-03 R9 corrections above (a run that NAMES narrow
scope but COVERS wide scope), in its selection-flag variant -- NOT a new
DR-088 ruling ("R10"); DR-088's Tier-T definition is unchanged, only
Claude-klabauter's scope DERIVATION for this shape changed. Spec backlink:
cross-repo/inbox/2026-08-14-example-retrieval-repo-em-em-level-expensive-command-guard-
question.md

Spec backlink: cross-repo/inbox/2026-07-23-claude-central-em-test-suite-invocation-guards.md
Spec backlink (public API): cross-repo/inbox/2026-07-23-claude-central-em-dr088-grant-spec-and-layer2-seam.md § Ask 1
Spec backlink (grant leg): cross-repo/inbox/2026-07-23-claude-central-em-dr088-ceremonies-write-grants-wire-the-deny.md
Spec backlink (R5-R7 amendment): cross-repo/inbox/2026-07-25-doe-claude-em-dr088-marker-scope-ruling.md
Spec backlink (R9 precision public API): cross-repo/inbox/2026-07-28-example-market-data-repo-em-dispatched-agent-scoped-test-breadth.md (DoE-claude repo)
Governing decision: DoE docs/decisions/DR-088-test-breadth-ladder-tiered-invocation-authority.md
Anti-evasion language adapted from DoE coordinator/agents/coverage-auditor.md:227.
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
#: ``COMMAND_TOOL_NAMES`` (C2 declaration-form conversion) -- never a copy
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 45

_OVERRIDE_ENV_VAR = "COORDINATOR_OVERRIDE_TEST_SUITE_INVOCATION"

#: Formerly a KNOWN LIMITATION here (removed 2026-08-10): a repo whose
#: (from ``coordinator.local.md`` and the ``COORDINATOR_{FAST,FULL}_TEST_CMD``
_RUNNER_PREFILTER_RE = re.compile(
    r"\b(pytest|py\.test|unittest|nose2|npm|pnpm|yarn|bun|npx|jest|vitest|"
    r"mocha|jasmine|ava|cargo|nextest|go|make|tox|nox|node|"
    r"(?i:invoke-pester))\b"
)


_CHEAP_ROOT_WALK_MAX_DEPTH = 64

#: other use sites further below) because ``_DYNAMIC_PREFILTER_TOKEN_STOPWORDS``
#: (``NameError: _DYNAMIC_PREFILTER_TOKEN_STOPWORDS``); moving definition
_WRAPPER_WORDS = frozenset({
    "sudo", "command", "time", "exec", "nice", "nohup", "env", "ionice",
    "stdbuf", "npx", "bunx", "pnpx", "setsid", "strace", "doas", "busybox",
})

#: (`block_subagent_commit.py`'s `_C_FLAG_SHELL_INTERPRETERS`,
#: `dispatch_checks.py`'s `_SHELL_C_WRAPPER_INTERPRETERS`) already unwrap.
_SHELL_C_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: test script, which defeats the point of a PREFILTER (it is still SAFE --
#: deny -- but it is not cheap). Reuses ``_WRAPPER_WORDS``/
#: ``_SHELL_C_INTERPRETERS`` (already-enumerated shell/wrapper names) plus
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
    module's other shlex-with-fallback call sites (``_tokens``).

    Past ``exceeds_tokenizable_ceiling`` the same whitespace fallback runs,
    matching ``_tokens``. That claim of parity was previously prose only:
    this site called ``shlex.split`` unguarded while ``_tokens`` bounded it,
    which the ``no_unguarded_direct_shlex_site`` lint flagged. It is reachable
    with unbounded input -- ``_env_head_tokens`` passes ``COORDINATOR_{FAST,
    FULL}_TEST_CMD`` straight here with no length bound (the
    ``coordinator.local.md`` leg is already capped by its own 8 KB read) --
    so the ceiling is the DoS bound it is everywhere else in this module, not
    a formality. Same never-raises contract either way."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    if _exceeds_tokenizable_ceiling(value):
        parts = value.split()
    else:
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

#: Same flag-set `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` uses for
_WRAPPER_ARG_FLAGS = {
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "--class", "-n", "--classdata", "-p", "--pid"}),
    "stdbuf": frozenset({"-i", "--input", "-o", "--output", "-e", "--error"}),
}

_NICE_BARE_NUMERIC_RE = re.compile(r"^-\d+$")

_RUN_SUBCOMMAND_WRAPPERS = frozenset({"poetry", "uv", "pdm", "hatch", "rye", "pipenv"})

#: pytest flags that consume a SEPARATE following token as their value. Needed
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-o", "-c", "-n", "-W", "-r",
    "--rootdir", "--junitxml", "--junit-xml", "--deselect", "--ignore",
    "--ignore-glob", "--maxfail", "--tb", "--log-level", "--log-cli-level",
    "--override-ini", "--import-mode", "--basetemp", "--capture", "--dist",
    "--numprocesses", "--cov", "--cov-report", "--cov-config", "--result-log",
    "--confcutdir", "--assert", "--doctest-glob", "--pdbcls", "--color",
})

_PYTEST_SCOPING_FLAGS = frozenset({
    "--lf", "--last-failed", "--ff", "--failed-first", "--sw", "--stepwise",
    "--stepwise-skip",
})

_JS_SCOPING_FLAGS = frozenset({
    "-t", "--testNamePattern", "--testPathPattern", "--testPathPatterns",
    "-g", "--grep", "--spec", "--fgrep", "-f", "--shard",
})

_MAKE_SUITE_TARGETS = frozenset({"test", "tests", "check", "test-all", "testall"})


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
            return [_strip_redirections(seg) for seg, _pipe_before in segments if seg]
    tokens = _tokenize_full_command(cmd)
    if tokens is not None:
        return [_strip_redirections(seg) for seg in _segments_from_tokens_simple(tokens) if seg]
    out: List[List[str]] = []
    for segment in _segments(cmd):
        if not segment.strip():
            continue
        out.append(_strip_redirections(_tokens(segment)))
    return out


#: duplication, ``2>&1``), and an optionally ATTACHED target (``2>/dev/null``,
#: that merely CONTAINS the character (``-k "a>b"``, a filename with an angle
_REDIRECTION_RE = re.compile(r"^(?:\d+|&)?(?:>>?|<<?)&?(?P<target>.*)$")


def _strip_redirections(argv: Sequence[str]) -> List[str]:
    """Drop shell redirection operators, and the operands they consume, from
    one segment's argv.

    A redirection is shell plumbing, never an argument the runner sees, but
    every classifier below reads argv positionally -- so an unstripped
    ``2>&1`` was walked as a pytest POSITIONAL, and the positional's breadth
    governs (``_classify_pytest``). ``pytest -k expr`` classified Tier T and
    ``pytest -k expr 2>&1`` classified suite-shaped, from the same run with
    the same scope. That is a false deny on the narrowest form this guard's
    own deny text offers as the alternative, and it fires on the most common
    way an agent captures output. The same token cut the other way one
    classifier over: ``python -m unittest 2>&1`` read ``2>&1`` as a dotted
    test target (``_classify_unittest``) and ALLOWED an unscoped suite run.

    Sited here, at the one seam every classification leg draws its argv
    from, rather than in each runner's own grammar -- a per-runner fix would
    have to be written once per grammar and would drift on the next one
    added.
    """
    out: List[str] = []
    i = 0
    n = len(argv)
    while i < n:
        tok = argv[i]
        match = _REDIRECTION_RE.match(tok) if not tok.startswith("-") else None
        if match is None:
            out.append(tok)
            i += 1
            continue
        i += 1
        if not match.group("target"):
            i += 1
    return out


def _base(token: str) -> str:
    name = token.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[: -len(".exe")]
    return name


_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: BX-13's `sh -c` unwrap set (`_SHELL_C_INTERPRETERS`) is defined near
#: `_WRAPPER_WORDS` above, not here -- see that constant's own comment.

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
        if tok == "{":
            i += 1
            continue
        # PAREN-GROUPING FIX (2026-07-29, EM-run confinement-corpus pass,
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
                            i += 1
                        continue
                    if len(t) > 2 and t[0] == "-" and t[1] != "-" and ("-" + t[1]) in taking:
                        i += 1
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
                        inner = payload.strip("'\"").split()
                    else:
                        try:
                            inner = shlex.split(payload, posix=True)
                        except ValueError:
                            break
                    return _strip_command_prefix(inner)
        break
    return list(tokens[i:])


def _norm_path(raw: str) -> str:
    p = raw.replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.rstrip("/")
    return p


def _read_testpaths(repo_root: Optional[str]) -> List[str]:
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
    if cwd:
        try:
            return os.path.exists(os.path.join(cwd, norm))
        except OSError:
            return False
    return False


def _walk_pytest_args(args: Sequence[str]) -> Tuple[bool, List[str]]:
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
    """Is this pytest invocation suite-shaped?

    ``_walk_pytest_args`` returns a flag-derived ``scoped`` bit (True for
    ``-k`` and for ``_PYTEST_SCOPING_FLAGS`` like ``--lf``) alongside the
    positionals it walked past. That flag-derived bit is a SELECTION signal,
    never a COLLECTION one: ``-k EXPR`` tells pytest which of the collected
    tests to RUN, not which files to COLLECT. When a positional names a
    ``testpaths`` root (or an ancestor of one), pytest still collects the
    entire configured suite to filter it -- ``pytest tests/ -k "foo"`` in a
    repo with ``testpaths = ["tests"]`` collects and walks all ~21k tests
    just to deselect nearly all of them. Crediting the whole invocation as
    scoped purely because a selection flag was ALSO present lets that flag
    launder a whole-suite positional past this classifier.

    So: a NON-EMPTY positional list where NONE of the positionals is a real
    scope (``_is_real_scope`` against configured testpaths) is suite-shaped
    regardless of the flag-derived bit -- the positional's breadth governs.
    An EMPTY positional list (bare ``pytest -k expr``, bare ``pytest --lf``)
    is unaffected: there is no positional to launder, so the flag-derived bit
    alone still decides, exactly as before this fix. And if any positional
    IS a real scope, the run is Tier T as it always was.

    This is evaluated statically from each positional against the repo's
    configured ``testpaths`` (``_is_real_scope``/``_read_testpaths``) -- zero
    collection required -- so it does not collide with DR-088 R8 (DoE
    refused a breadth/test-count term precisely because "a rule the
    classifier cannot evaluate without collecting is not a rule"; this rule
    needs no count, only the positional string and the configured roots).

    Same class as the 2026-07-30 pnpm arg-forwarding and 2026-08-03 R9
    unexpanded-glob corrections above (a run that NAMES narrow scope but
    COVERS wide scope) -- NOT a new DR-088 ruling ("R10"). DR-088's Tier-T
    definition is unchanged; only claude-klabauter's scope DERIVATION for this one
    shape (a name-pattern/selection flag alongside a testpaths-root
    positional) changed. See the dated docstring entry below for the
    incident and spec backlink.
    """
    if any(a.split("=", 1)[0] in _PYTEST_INFO_ONLY_FLAGS for a in args):
        return None
    scoped, positionals = _walk_pytest_args(args)
    if positionals:
        return None if any(_is_real_scope(a, testpaths, cwd) for a in positionals) else label
    return None if scoped else label


_PYTEST_INFO_ONLY_FLAGS = frozenset({"--version", "-V", "--help", "-h", "--markers"})


def _classify_unittest(args: Sequence[str]) -> Optional[str]:
    positional = [a for a in args if not a.startswith("-")]
    if not positional or positional[0] == "discover":
        return "python -m unittest"
    return None


def _classify_python_module(tokens: Sequence[str], testpaths: Sequence[str],
                            cwd: Optional[str]) -> Optional[str]:
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
    positional = [a for a in positional if a not in ("run", "watch", "related")]
    for arg in positional:
        if _is_real_scope(arg, testpaths, cwd):
            return None
    return label


def _package_manager_test_shape(args: Sequence[str]) -> bool:
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
    return label


_PM_BASES = ("npm", "pnpm", "yarn", "bun")


def _is_package_script_label(detected: str) -> bool:
    parts = detected.split()
    return len(parts) == 2 and parts[0] in _PM_BASES and parts[1] == "test"


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
    if not args:
        return None
    if args[0] == "test":
        return args[1:]
    if args[0] == "nextest" and len(args) > 1 and args[1] == "run":
        return args[2:]
    return None


def _classify_cargo(args: Sequence[str]) -> Optional[str]:
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
        return None
    return "cargo test"


def _go_test_shape(args: Sequence[str]) -> Optional[Sequence[str]]:
    if not args or args[0] != "test":
        return None
    return args[1:]


def _classify_go(args: Sequence[str]) -> Optional[str]:
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


_TOX_NOX_BASES = frozenset({"tox", "nox"})


def _classify_tox_nox(base: str, args: Sequence[str]) -> str:
    """``tox`` / ``nox`` -- always unscoped, regardless of arguments.

    DR-088 test-suite-invocation-guard spelling-keyed-leg bypass, found
    2026-08-03 while reconstructing the relayed finding
    ``2026-08-01-example-retrieval-repo-em-test-suite-approval-gate-bypass.md`` (not
    present in this tree; relayed via
    ``cross-repo/inbox/2026-08-03-doe-claude-em-klabauter-rows-move-to-you-
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


#: ``_JS_SCOPING_FLAGS`` uses: it narrows by NAME, not by path) or an
#: name itself in ``_RUNNER_PREFILTER_RE``.
_PESTER_SCOPING_FLAGS = frozenset({
    "-testname", "-fullnamefilter", "-tag", "-tagfilter",
})
_PESTER_PATH_FLAGS = frozenset({"-path", "-script"})


def _pester_path_values(args: Sequence[str]) -> List[str]:
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


_NODE_TEST_FILE_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".jsx", ".tsx")


def _is_node_scope(arg: str, testpaths: Sequence[str],
                   cwd: Optional[str]) -> bool:
    if _is_real_scope(arg, testpaths, cwd):
        return True
    token = arg.replace("\\", "/")
    norm = _norm_path(token.split("::", 1)[0])
    if not norm or norm in (".", ".."):
        return False
    for tp in testpaths:
        if norm == tp or tp == norm or tp.startswith(norm + "/"):
            return False
    return norm.lower().endswith(_NODE_TEST_FILE_SUFFIXES)


def _classify_node_test(args: Sequence[str], testpaths: Sequence[str],
                        cwd: Optional[str]) -> Optional[str]:
    """``node --test`` -- Node's own built-in test runner.

    Same bypass class as ``_classify_tox_nox``'s: ``node`` was not in
    ``_RUNNER_PREFILTER_RE`` at all, so NO ``node --test`` shape reached
    this guard -- not the classification leg, not the Layer-3 subagent
    identity deny, not the Tier-F/U grant check. ``check()`` never reaches
    any of them for a command the prefilter does not match. Reported as
    ``cross-repo/archive/2026-08-11-doe-claude-em-tier-u-node-runner-
    unclassified.md``, confirmed live by dominant-shard3 group 4.

    THE RULE IS THE ONE EVERY OTHER RUNNER ALREADY GETS, not a new one:
    a positional operand naming a file or directory IS scope (DR-088's
    Tier T is "the files, directories, or node ids the caller authored or
    touched"), and its absence is an unscoped runner invocation, which is
    Tier U by DR-088's disjunct and by DoE's own R1 ruling that tier is a
    property of the invocation's SHAPE. So a bare ``node --test`` -- which
    discovers and runs every test file under the cwd -- is Tier U, and
    ``node --test tests/`` or ``node --test x.test.js`` is scoped, exactly
    as ``pytest`` bare vs ``pytest tests/`` already classify.

    ONLY ``--test`` COUNTS. Plain ``node script.js`` is running a program,
    not invoking a test runner, and classifying it would make this guard
    fire on every node invocation in the fleet. ``--test`` is the flag that
    makes node a runner, so it is the flag that makes this leg apply.

    NEGATIVE SPEC -- this deliberately does NOT deny ``node --test
    <path>/run.js``, the specific invocation the reporting memo asked
    about, and that is a RULING rather than an oversight. That operand
    names a file, so it is scoped by the same test that permits ``pytest
    tests/test_one.py``. The unboundedness the memo was reaching for lives
    INSIDE ``run.js``, which fans out to a whole plugin ecosystem -- a fact
    no text classifier can see, and one that no filename heuristic can
    infer without also denying a genuinely single-file ``node --test
    foo.test.js``. The reporting repo's two ceremony surfaces claim this
    guard denies that invocation; they overclaim, and the correction is
    theirs. See ``docs/decisions/DR-395-...`` for the split: the classifier
    was wrong about the shapes it could not see, the prose is wrong about
    the one it can."""
    if "--test" not in args:
        return None
    positionals = [tok for tok in args if not tok.startswith("-")]
    if positionals:
        return (
            None
            if any(_is_node_scope(a, testpaths, cwd) for a in positionals)
            else "node --test"
        )
    return "node --test"


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
    if base == "node":
        return _classify_node_test(args, testpaths, cwd)
    if base.lower() == "invoke-pester":
        return _classify_pester(args, cwd)
    return None


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
    out: List[Tuple[str, ...]] = []
    for argv in _segment_argvs(cmd):
        tokens = _strip_command_prefix(argv)
        if not tokens:
            continue
        out.append((_norm_head(tokens[0]),) + tuple(_norm_arg(t) for t in tokens[1:]))
    return out


#: (``resolve_python_interp``), so a repo that DECLARES ``python -m pytest
_PYTHON_HEAD_RE = re.compile(r"^python(?:\d+(?:\.\d+)*)?$", re.IGNORECASE)


def _norm_head(token: str) -> str:
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

    A zero-argument configured segment (a bare
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

    Factored out of ``_matches_configured_cmd`` and
    ``_classify_command_core``, which independently built the identical
    ``all(any(_segment_contains ...))`` shape; the module's own stated
    principle is that ``check()`` and the public ``classify_command`` API
    run through the SAME implementation, and this containment step is now
    the single place both call sites use, matching how ``_segment_contains``
    itself was already extracted.

    ``exact`` selects tuple equality (``_matches_declared_fast_test_cmd``'s
    R6 authority-widening exit, which must keep exact-equality semantics)
    over the default containment (every classification leg).

    The
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

    Note (C1, docs/plans/2026-07-30-diff-scoped-ceremony-gates-elegant.md
    Design decision 1): the by-path shim is now a thin re-export trampoline
    over this same coordinator_core module, so the two legs can no longer
    resolve a tier differently -- the fallback is redundant, not a distinct
    source of truth, and could be collapsed to the native leg alone. Left
    in place here rather than collapsed: `coordinator_core/bash_guards/
    tests/test_check_test_suite_invocation.py` (out of this chunk's writes
    scope) pins the by-path fallback's own behaviour directly, and collapsing
    this function would break that pinned suite without authorization to
    edit it. Tracked as a follow-up, not silently dropped.

    The fallback is PER TIER, not all-or-nothing.
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
    is DoE-claude's declared fast AND full tier) classifies on shape alone as
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
    if not configured:
        return None
    normalized = [
        (_norm_head(argv[0]),) + tuple(_norm_arg(a) for a in argv[1:])
        for argv in segments_argv if argv
    ]
    for pair in configured:
        if not isinstance(pair, (tuple, list)) or len(pair) != 3:
            continue
        tier, cmd, _returncode = pair
        cfg_segments = _normalized_segments(cmd)
        if _cfg_segments_satisfied(cfg_segments, normalized, exact=exact):
            return "the repo's configured %s" % tier
    return None


_CTRL_WS_RE = re.compile(r"[\t\r\n\f\v]")
_C0_RE = re.compile(r"[\x00-\x1f]")


def _sanitize(cmd: str) -> str:
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


def _deny_reason_subagent(
    detected: str,
    cmd_safe: str,
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
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
    _override_note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    _override_line = "  " + _override_note + "\n" if _override_note else ""
    return (
        "Full-suite subagent runs are denied (concurrency). Use instead:\n"
        "  python3 -m pytest path/to/test_file.py\n"
        "  python3 -m pytest path/to/test_file.py::test_case\n"
        "  python3 -m pytest -k behaviour_changed\n"
        + _override_line
        + "  Detected: %s\n"
        "  Command:  %s\n\n"
        "Reshaping the command text does not bypass this.%s"
    ) % (detected, cmd_safe, package_script_note)


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

    The former implementation asked "does ANY
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


_WITH_TIER_T_SLOT_BASENAMES = frozenset({
    "with-tier-t-slot", "with-tier-t-slot.cmd", "with-tier-t-slot.ps1",
})


def _command_wrapped_in_tier_t_slot(
    cmd: str,
    dialect: Optional[_Dialect],
) -> bool:
    """Does every RUNNER-INVOKING segment of ``cmd`` route through
    ``with-tier-t-slot``?

    Per-segment by construction, for the same reason
    ``_command_wrapped_in_suite_mutex`` is: a decoy leg
    (``with-tier-t-slot -- true && python -m pytest x.py``) would otherwise
    satisfy a bare token-presence scan while the real run took no slot. The
    predicate that matters is "the segment that spawns the runner is the
    segment that is wrapped", never "the wrapper appears somewhere".

    Keyed on ``_runner_recognized`` rather than on suite-shape: this leg's
    subject is precisely the command that is NOT suite-shaped (a scoped
    Tier-T run), so the suite classifiers all return nothing for it and only
    the runner-recognition predicate can see it at all.

    Returns True when no segment invokes a runner — a command with nothing to
    bound is trivially compliant, and this leg must never deny something it
    is not the control for.
    """
    runner_segments = 0
    wrapped_runner_segments = 0
    for raw_argv in _segment_argvs(cmd, dialect):
        idx = 0
        n = len(raw_argv)
        while idx < n and _ENV_ASSIGN_RE.match(raw_argv[idx]):
            idx += 1
        wrapped = idx < n and _base(raw_argv[idx]).lower() in _WITH_TIER_T_SLOT_BASENAMES
        stripped = _strip_command_prefix(raw_argv)
        if not stripped or not _runner_recognized(stripped):
            continue
        runner_segments += 1
        if wrapped:
            wrapped_runner_segments += 1
    return runner_segments == wrapped_runner_segments


def _tier_t_slot_verdict(
    cmd: str,
    dialect: Optional[_Dialect],
    segments_argv: Sequence[Sequence[str]],
) -> Optional[Dict[str, Any]]:
    if not any(_runner_recognized(list(argv)) for argv in segments_argv):
        return None

    cmd_safe = _sanitize(cmd)
    taken, cap = _tier_t_occupancy()
    wrapped = "with-tier-t-slot -- %s" % cmd.strip()

    note = (
        "Routed through the Tier-T slot semaphore (%d/%d slots taken). Your "
        "command is unchanged and still runs; it now takes one of %d slots on "
        "this box and waits if none is free.\n\n"
        "Nothing is being refused and there is nothing to ask for. A single "
        "scoped run is cheap -- it is hundreds at once that took this box down "
        "on 2026-09-20, without any one of them being wrong."
    ) % (taken, cap, cap)

    if len(segments_argv) == 1:
        return _allow_rewrite_tier_t(wrapped, note)

    return _advisory_tier_t(
        "Run this through the Tier-T slot semaphore so the box can bound how "
        "many scoped runs execute at once:\n"
        "  with-tier-t-slot -- <the test segment of your command>\n\n"
        "Not applied automatically here: your command chains several segments, "
        "and rewriting it whole would mean reconstructing a chain this guard "
        "did not parse. Allowed as-is.\n\n"
        "  Slots:   %d/%d taken\n"
        "  Command: %s" % (taken, cap, cmd_safe)
    )


def _allow_rewrite_tier_t(new_cmd: str, ctx: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {"command": new_cmd},
            "additionalContext": ctx,
        }
    }


def _advisory_tier_t(msg: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "additionalContext": msg,
        }
    }


def _tier_t_occupancy() -> Tuple[int, int]:
    try:
        from coordinator_core.testing import tier_t_slots

        taken, cap, _ = tier_t_slots.occupancy()
        return taken, cap
    except Exception:
        return 0, 0


def _deny_reason_mutex(detected: str, cmd_safe: str, holder: Dict[str, Any]) -> str:
    return (
        "Wait for the in-flight suite run, or scope this to what you touched "
        "in the meantime:\n"
        "  python3 -m pytest path/to/your/test_file.py\n\n"
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


_GRANT_DETAIL_POINTER = "this guard's own GRANT-leg doctrine (no wiki page yet)"


def _resolved_sid_for_diagnosis() -> Optional[str]:
    try:
        from coordinator_core.session import core as _session_core

        return _session_core.resolve_session_id(None)
    except Exception:
        return None


def _ungranted_record_failing_gate(
    record: Dict[str, Any], resolved_sid: Optional[str], cwd: Optional[str]
) -> str:
    try:
        from coordinator_core.session import grant as _grant

        granted_by = record.get("granted_by")
        if granted_by not in _grant._VALID_GRANTED_BY:
            return (
                "granted_by is %r, which is not a granter this engine "
                "recognises" % (granted_by,)
            )
        ceremony = record.get("ceremony")
        if granted_by == "ceremony" and not ceremony:
            return "granted_by is 'ceremony' but the record names no ceremony"
        if granted_by == "pm" and ceremony:
            return "granted_by is 'pm' but the record also names a ceremony"
        stored = record.get("session_id")
        if stored != resolved_sid:
            return (
                "the grant was written for a different session than this "
                "guard process resolved -- writer/reader disagreement, an "
                "engine defect"
            )
        from coordinator_core.session import liveness as _liveness

        if not _liveness.session_live(resolved_sid or "", cwd):
            return (
                "the sids match, but the session that minted this grant no "
                "longer reads live -- mint a new grant"
            )
        return (
            "no gate this guard mirrors rejected it, so the authorization "
            "path has a gate this text does not know about -- report this line"
        )
    except Exception:
        return "undetermined (the diagnostic itself failed; the deny stands)"


def _deny_reason_grant(
    detected: str,
    cmd_safe: str,
    *,
    is_tie: bool = False,
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
    ungranted_record: Optional[Dict[str, Any]] = None,
    ungranted_cwd: Optional[str] = None,
) -> str:
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
    _override_note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
    if ungranted_record is not None:
        _stored_sid = ungranted_record.get("session_id")
        _granted_by = ungranted_record.get("granted_by")
        _diag_sid = _resolved_sid_for_diagnosis()
        return (
            (
                "A Tier-U grant record exists here but did not authorize this "
                "run -- asking for another will not change that.\n"
                "  grant session_id: %s\n"
                "  granted_by:       %s\n"
                "  this process sid: %s\n\n"
                "  why:              %s\n\n"
                "  Detected: %s\n"
                "  Command:  %s\n\n"
                "Full grant detail: %s"
                % (
                    _stored_sid or "<absent>",
                    _granted_by or "<absent>",
                    _diag_sid or "<unresolvable>",
                    _ungranted_record_failing_gate(
                        ungranted_record, _diag_sid, ungranted_cwd
                    ),
                    detected,
                    cmd_safe,
                    _GRANT_DETAIL_POINTER,
                )
            )
            + ("\n\n%s" % _override_note if _override_note else "")
        )
    if is_tie:
        return (
            (
                "No Tier-F escape -- get the PM's Tier-U authorization grant, "
                "or run what you touched:\n"
                "  tier-u-grant-cli grant pm \"<verbatim PM utterance>\"\n"
                "  python3 -m pytest path/to/your/test_file.py\n"
                "  python3 -m pytest path/to/your/test_file.py::test_the_case_you_changed\n"
                "  python3 -m pytest -k the_behaviour_you_changed\n\n"
                "  Detected: %s\n"
                "  Command:  %s\n\n"
                "Grant detail: %s"
                % (detected, cmd_safe, _GRANT_DETAIL_POINTER)
            )
            + ("\n\n%s" % _override_note if _override_note else "")
        )
    return (
        (
            "Ask the PM for a Tier-U authorization grant (their exact words go "
            "in the quotes), or run only what you touched:\n"
            "  tier-u-grant-cli grant pm \"<verbatim PM utterance>\"\n"
            "  python3 -m pytest path/to/your/test_file.py\n"
            "  python3 -m pytest path/to/your/test_file.py::test_the_case_you_changed\n"
            "  python3 -m pytest -k the_behaviour_you_changed\n\n"
            "  Detected: %s\n"
            "  Command:  %s\n\n"
            "Full grant detail (ceremonies, session scope, authority vs. "
            "resource control): %s"
            % (detected, cmd_safe, _GRANT_DETAIL_POINTER)
        )
        + ("\n\n%s" % _override_note if _override_note else "")
    )


_PYTEST_HEADS = ("pytest", "py.test")

_GLOB_MAGIC_CHARS = frozenset("*?[")


def _has_glob_magic(path: str) -> bool:
    return any(c in _GLOB_MAGIC_CHARS for c in path)


def _pytest_directory_args(segments_argv: Sequence[Sequence[str]],
                           cwd: Optional[str]) -> List[str]:
    """Positional pytest arguments that name a DIRECTORY on disk.

    DR-088 R9 (DoE ruling, 2026-07-28): for a caller carrying a top-level
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
    ``cross-repo/inbox/2026-08-03-doe-claude-em-two-rulings-plan-orphan-
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


#: ``--collect-only, --co``). Unlike ``_PYTEST_SCOPING_FLAGS`` (a SELECTION
_PYTEST_COLLECT_ONLY_FLAGS = frozenset({"--collect-only", "--co"})


def _is_pytest_collect_only_segment(argv: Sequence[str]) -> bool:
    if not argv:
        return False
    if _base(argv[0]) in _PYTEST_HEADS:
        args: Sequence[str] = argv[1:]
    else:
        module_args = _pytest_module_args(argv)
        if module_args is None:
            return False
        args = module_args
    return any(a.split("=", 1)[0] in _PYTEST_COLLECT_ONLY_FLAGS for a in args)


def _agent_touched_test_files(raw_agent_id: str, session_id: str,
                              repo_root: Optional[str]) -> List[str]:
    """This agent's own touched test files, for the R9 deny text's
    better-alternative line. Best-effort on identity/path resolution
    failures (``[]``); the touched-set read itself never raises (C3's
    ``project_live_claims`` contract) and never collapses a degraded
    stream to a hard failure -- see below.

    Reads the agent-keyed sink, ``<git_common_dir>/coordinator-sessions/
    .agents/<canonical_agent_id>/touch-record.jsonl``, through C3's single
    read seam, ``coordinator_core.session.touch_record.project_live_claims``
    -- never a second, ad hoc parse of that file. The raw agent id is
    canonicalized through the CANONICAL resolver,
    ``coordinator_core.write_guards._subagent_identity._resolve_subagent_identity``
    (imported by ten other guards) -- rather than
    ``track_touched_files``'s own now-retired copy (C7 deletes that module;
    C9 folded its one extra branch, the already-canonical
    ``<name>@session-<short>`` rebuild, into this canonical resolver first so
    this repoint carries zero behavior change).

    This call site is advisory only (it phrases the R9 deny's
    better-alternative line; the deny itself already fired on the directory
    argument alone) so C3's typed failure signal is honored, not
    re-adjudicated, by taking ``TouchProjection.claims`` exactly as the seam
    returns it -- whatever it could decode, including a partial result
    behind a ``degraded=True`` read -- rather than this function inventing
    its own collapse-to-``[]``-on-any-error behavior the way the retired
    direct-file-read implementation did (one bad line used to blank out an
    otherwise-healthy file; the seam isolates the fault per-line/per-file
    instead, so more real signal survives here than before, never less).

    Deliberately NOT falling back to the session-level sink: that set
    belongs to the EM and to other agents, and borrowing it to phrase "the
    tests you touched" would launder exactly the relevance R9 asserts.
    """
    if not raw_agent_id or not repo_root:
        return []
    try:
        from coordinator_core.write_guards._subagent_identity import _resolve_subagent_identity
        from coordinator_core.lifecycle import git_common_dir
        from coordinator_core.session import touch_record

        canonical = _resolve_subagent_identity(raw_agent_id, session_id or "")
        if not canonical:
            return []
        base = git_common_dir(repo_root)
        if base is None:
            return []
        sink = touch_record.sink_path(
            Path(base) / "coordinator-sessions" / ".agents" / canonical
        )
        projection = touch_record.project_live_claims(sink, cwd=repo_root)
    except Exception:
        return []
    out: List[str] = []
    for path in projection.claims:
        name = path.rsplit("/", 1)[-1]
        if name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py")):
            out.append(path)
    return out


def _deny_reason_subagent_directory(
    dir_args: Sequence[str],
    cmd_safe: str,
    touched_tests: Sequence[str],
    payload: Optional[Dict[str, Any]] = None,
    git_root: Optional[str] = None,
) -> str:
    if touched_tests:
        shown = list(touched_tests)[:5]
        more = len(touched_tests) - len(shown)
        alternative = (
            "Run the test files YOU touched:\n"
            + "".join("  python3 -m pytest %s\n" % p for p in shown)
            + ("  ... and %d more in your touched set\n" % more if more > 0 else "")
        )
    else:
        alternative = (
            "You have touched no test files yet. Name the specific file or "
            "node id you mean:\n"
            "  python3 -m pytest path/to/test_file.py\n"
            "  python3 -m pytest path/to/test_file.py::test_the_case_you_changed\n"
        )
    _override_note = operator_override_note(_OVERRIDE_ENV_VAR, payload=payload, git_root=git_root)
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
        "%s" % (
            "s" if len(dir_args) > 1 else "",
            ", ".join(dir_args),
            cmd_safe,
            _ANTI_EVASION,
        )
        + ("\n\n%s" % _override_note if _override_note else "")
    )


def _mutex_holder() -> Optional[Dict[str, Any]]:
    try:
        from coordinator_core.testing import suite_mutex

        holder = suite_mutex.holder()
    except Exception:
        return None
    return holder if isinstance(holder, dict) else None


def _matched_tiers(cmd: str, cwd: Optional[str], testpaths: Sequence[str],
                   configured: Sequence[ConfiguredCmd]) -> frozenset:
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

    A
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
    # suite, so it is classified as that tier. This is an AUTHORITY exit,
    return _matches_configured_cmd(segments_argv, fast_only, exact=True) is not None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    # Read the override off the PER-CALL payload, not this process's environ.
    from coordinator_core.bash_guards.dispatch_checks import _override

    if _override(_OVERRIDE_ENV_VAR, payload=payload):
        return None

    # Widened alongside ``MATCHERS`` (see that constant's comment): a
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
        # `_RUNNER_PREFILTER_RE` comment for the incident and cost argument.
        if not _dynamic_prefilter_hit(cmd, cwd):
            return None

    raw_agent_id = payload.get("agent_id")
    is_subagent = isinstance(raw_agent_id, str) and bool(raw_agent_id.strip())

    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)

    detected: Optional[str] = None
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

    # this leg is strictly ADDITIVE: it can only deny commands that were
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
                payload=payload,
                git_root=repo_root,
            ))

        # TIER-T CONCURRENCY leg (layer 6's missing half, fail-OPEN on infra)
        # WHY THIS IS NOT A NARROWING OF THE TIER-T CARVE-OUT, which would be
        if _command_wrapped_in_tier_t_slot(cmd, dialect) is False:
            verdict = _tier_t_slot_verdict(cmd, dialect, segments_argv)
            if verdict is not None:
                return verdict

    if detected is None:
        return None

    cmd_safe = _sanitize(cmd)

    if is_subagent:
        return _deny(_deny_reason_subagent(detected, cmd_safe, payload=payload, git_root=repo_root))

    if configured is None:
        configured = _configured_test_cmds(repo_root)
    cmd_for_tiering = (
        " ; ".join(" ".join(seg) for seg in segments_argv)
        if dialect is _Dialect.POWERSHELL and segments_argv
        else cmd
    )
    matched_tiers = _matched_tiers(cmd_for_tiering, cwd, testpaths, configured)
    collect_only = any(_is_pytest_collect_only_segment(argv) for argv in segments_argv)
    if matched_tiers & {"U", "F"} and not collect_only:
        # discharges the AUTHORITY check for exactly the literal (token-
        declared_unscoped_fast_tier = (
            "U" in matched_tiers
            and _fast_tier_unscoped_declaration(repo_root)
            and _matches_declared_fast_test_cmd(segments_argv, configured)
        )
        granted = declared_unscoped_fast_tier
        grant_record: Optional[Dict[str, Any]] = None
        if not granted:
            granted, grant_record = _tier_u_grant(cwd)
        if not granted:
            by_tier = {entry.tier: entry.cmd for entry in configured}
            is_tie = (
                "fast_test_cmd" in by_tier
                and "full_test_cmd" in by_tier
                and by_tier["fast_test_cmd"] == by_tier["full_test_cmd"]
            )
            if "U" in matched_tiers:
                return _deny(_deny_reason_grant(detected, cmd_safe, is_tie=is_tie, payload=payload, git_root=repo_root, ungranted_record=grant_record, ungranted_cwd=cwd))
            return _deny(_deny_reason_grant(detected, cmd_safe, is_tie=is_tie, payload=payload, git_root=repo_root, ungranted_record=grant_record, ungranted_cwd=cwd))

        if not _command_wrapped_in_suite_mutex(cmd, dialect, testpaths, cwd, configured):
            return _deny(_deny_reason_wrapper_required(detected, cmd_safe))

    holder = _mutex_holder()
    if holder:
        return _deny(_deny_reason_mutex(detected, cmd_safe, holder))

    return None


@dataclasses.dataclass(frozen=True)
class SuiteMatch:

    #: suite-shaped match, INCLUDING an unscoped runner invocation that
    tier: str
    detected: str
    matched_text: str
    span: Tuple[int, int]
    #: ("do not run pytest"). ``"reported"`` is a DIFFERENT linguistic shape:
    position: str
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
        "  python3 -m pytest path/to/the/test_file.py\n"
        "  python3 -m pytest path/to/the/test_file.py::test_the_case_that_changed\n"
        "  python3 -m pytest -k the_behaviour_that_changed\n\n"
        "Unscoped/full-suite runs require a Tier-U grant and are reserved "
        "for the top-level EM -- never a dispatched subagent or a bare "
        "dispatch-brief instruction."
        "%s\n\n"
        "Detected: %s" % (package_script_note, detected)
    )


_CFG_TIER_DETECTED_LABEL: Dict[str, str] = {
    "fast_test_cmd": "the repo's configured fast_test_cmd",
    "full_test_cmd": "the repo's configured full_test_cmd",
}


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
    """R1 (cross-repo/inbox/2026-07-25-doe-claude-em-validate-tier-u-shape-
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
        # PROVABLY incapable of spawning a test run (``_argv_is_inert``:
        if generic is None and _argv_is_inert(argv):
            return "F", _CFG_TIER_DETECTED_LABEL["full_test_cmd"]
        return "U", generic or _CFG_TIER_DETECTED_LABEL["full_test_cmd"]
    if generic is not None:
        return "U", generic
    return "F", _CFG_TIER_DETECTED_LABEL[cfg_tier]


def _segments_with_spans(cmd: str) -> List[Tuple[str, int, int]]:
    segments: List[Tuple[str, int, int]] = []
    n = len(cmd)
    seg_start = 0
    i = 0
    in_single = False
    in_double = False
    while i < n:
        c = cmd[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c in ";&|":
            if i > seg_start:
                segments.append((cmd[seg_start:i], seg_start, i))
            j = i + 1
            while j < n and cmd[j] in ";&|":
                j += 1
            seg_start = j
            i = j
            continue
        i += 1
    if n > seg_start:
        segments.append((cmd[seg_start:n], seg_start, n))
    return segments


def _classify_command_core(
    command: str,
    cwd: Optional[str],
    testpaths: Sequence[str],
    configured: Sequence[ConfiguredCmd],
) -> List[SuiteMatch]:
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

    well_formed = [
        pair for pair in configured
        if isinstance(pair, (tuple, list)) and len(pair) == 3
    ]

    # resolved to the identical string) prefers the STRICTER tier. This is
    # This same tie-break also fires when the repo EXPLICITLY declares
    satisfied: Dict[str, List[Tuple[str, ...]]] = {}
    for tier_name, cmd_str, _returncode in sorted(
        well_formed, key=lambda pair: pair[0] != "full_test_cmd"
    ):
        cfg_segments = _normalized_segments(cmd_str)
        if _cfg_segments_satisfied(cfg_segments, invocation_segments):
            satisfied.setdefault(tier_name, cfg_segments)

    # A repo may scope its fast tier by APPENDING a scope to its full tier
    # configured-cmd match is token CONTAINMENT (``_segment_contains`` ->
    tier_order = ("full_test_cmd", "fast_test_cmd")
    _fast_cfg = satisfied.get("fast_test_cmd")
    _full_cfg = satisfied.get("full_test_cmd")
    if _fast_cfg and _full_cfg:
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
            tier, detected = _tier_for_cfg_match(cfg_tier, generic, argv)
        elif generic is not None:
            tier, detected = "U", generic
        else:
            continue

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
    if not isinstance(command, str) or not command:
        return []
    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)
    return _classify_command_core(command, cwd, testpaths, configured)


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


_NEGATION_RE = re.compile(
    r"(?:\bdo not run\b|\bdon't run\b|\bnever run\b|\bmust not\b|\bshould not\b|"
    r"\binstead of\b|\brather than\b|\bdeny[-\s]?list\b|\bremove\b|\bdelete\b|"
    r"\bstrip\b|\bforbidden\b|\bbanned\b)",
    re.IGNORECASE,
)

#: that mark a match as REPORTED SPEECH -- prose narrating someone's (in)
#: never an instruction to run anything. Distinct from ``_NEGATION_RE``: a
#: report is a narrative claim about a DIFFERENT actor's past action or
#: with no instruction anywhere in it -- ``_NEGATION_RE`` only recognizes
#: Checked in ``classify_text``'s ``_emit`` ONLY when ``_NEGATION_RE`` did
_REPORTED_SPEECH_RE = re.compile(
    r"(?:\b(?:could\s+not|couldn't|can\s+not|cannot|can't|"
    r"was\s+(?:not\s+)?(?:un)?able\s+to|were\s+(?:not\s+)?(?:un)?able\s+to|"
    r"failed\s+to|did\s+not|didn't|has\s+not|have\s+not|never)\s+"
    r"(?:run|ran|running|execute|invoke)\b"
    r"|\bthey\s+(?:stated|said|reported)\b|\breported\s+that\b|"
    r"\bnoted\s+that\b|\bsaid\s+they\b)",
    re.IGNORECASE,
)

_NEGATION_LOOKBACK = 300

#: Execution-intent verbs/phrases that license treating a BARE (non-fenced,
#: spirit as ``_NEGATION_RE``'s marker list (closed-set-ness only -- the
#: matching MECHANISM differs, see the clause-scoping note below) -- this
# set gating a detection gate is a recall risk (unlike ``_NEGATION_RE``'s
# ``_PROSE_NEGATIVE_RE`` below), leaving the existing lead-strip fallback
# "start"/"starting" are deliberately EXCLUDED from this clause-wide set
# (unlike every other cue here) and instead checked by ``_START_CUE_TAIL_RE``
# ``_NEGATION_RE``'s ``\bdo not run\b`` marker checks for, so that marker
_IMPERATIVE_CUE_RE = re.compile(
    r"\b(?:run|running|execute|executing|invoke|invoking|launch|launching|"
    r"(?<!re-)verify|(?<!re-)verifying|call|calling|please|just|"
    r"do(?!\s+not\b)|begin|"
    r"beginning|trigger|triggering|perform|performing|kick\s+off|"
    r"fire\s+off|go\s+ahead\s+and)\b",
    re.IGNORECASE,
)

#: the exclusion note on ``_IMPERATIVE_CUE_RE`` above.
_START_CUE_TAIL_RE = re.compile(r"\bstart(?:ing)?\s*$", re.IGNORECASE)

#: Prose-shape NEGATIVE patterns -- a runner token appearing as the OBJECT
_PROSE_NEGATIVE_RE = re.compile(
    r"(?:\bis\s+in\b|\bas\s+an?\b|\bbacked\s+by\b|\bre-runnable\b|"
    r"\babout\b|\bmentions?\b)",
    re.IGNORECASE,
)

_BARE_LINE_LEAD_RE = re.compile(r"^[\s\-*\$\d\.\)]+")

#: of the prefix, stripped ahead of ``_BARE_LINE_LEAD_RE`` -- that regex's
# (``1.``, ``2)``) were already covered by ``_BARE_LINE_LEAD_RE``; lettered
_LIST_MARKER_LEAD_RE = re.compile(r"^[A-Za-z][.\)]\s*")

#: Restricted to SENTENCE-ending punctuation only (``.``/``;``).
# ``_IMPERATIVE_CUE_RE``. A colon/comma used as a label separator is not a
# A LINE BREAK was added as a clause boundary, then REVERTED the same day
# reported-speech check (``_REPORTED_SPEECH_RE.search(clause)`` in
# ``classify_text``), which is deliberately designed to reach BACKWARDS
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
    2026-07-25-doe-claude-em-dispatch-suite-classifier-two-live-defects.md
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
#: hyphen is REQUIRED (unlike a looser bare-``re`` match): a bare ``re``
#: ``_IMPERATIVE_CUE_RE``'s own ``(?<!re-)`` carve-out for ``verify`` in
_RE_PREFIX_TAIL_RE = re.compile(r"re-\s*$", re.IGNORECASE)

#: ``_CLAUSE_BOUNDARY_RE`` member (that would reopen the "Run: pytest"
_FRONTED_ADVERBIAL_BOUNDARY_RE = re.compile(r",")

#: members of ``_IMPERATIVE_CUE_RE``, so the cue search finds THEM first and
_IMPERATIVE_LEAD_ADVERB_RE = re.compile(
    r"^(?:(?:and|or|but|so|then|now|next|first|finally|also|again|"
    r"afterwards|subsequently|optionally|ideally|instead)\b|\w+ly\b)\s*",
    re.IGNORECASE,
)

#: Governance test: does anything of SUBSTANCE sit between the start of the
#: copula, a governing preposition) means the clause is DECLARATIVE prose
#: only for an auxiliary/modal/copula sitting IMMEDIATELY before the cue.
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
    # split: ``clause`` (per ``_CLAUSE_BOUNDARY_RE``, punctuation-only) can
    # speech check needs (see ``_CLAUSE_BOUNDARY_RE``'s docstring) -- but a
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
    out: List[Tuple[int, str, str, int, int]] = []

    def _add_block(sub_start: int, content: str, base_position: str,
                   window_start: int, window_end: int) -> None:
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
        # PRECEDING line ("Do not run this:\n`pytest`") is otherwise
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
        if not _bare_line_is_command_shaped(line[:prefilter_match.start()]):
            continue
        candidate_start = line_match.start() + prefilter_match.start()
        real_candidate = _strip_sentence_punctuation(
            text[candidate_start:line_match.end()])
        out.append((candidate_start, real_candidate, "imperative",
                   max(0, line_match.start() - _NEGATION_LOOKBACK), line_match.end()))

    return out


def _resolve_position(text: str, abs_start: int, base_position: str,
                      window_start: int, window_end: int) -> str:
    w_start = max(0, window_start)
    w_end = min(len(text), window_end)
    window = text[w_start:w_end]
    if _NEGATION_RE.search(window):
        return "negated"
    # Reported-speech is checked in the BACKWARD-ONLY, CLAUSE-scoped slice of
    # windowed regex pass. This is deliberately NARROWER than
    # ``_NEGATION_RE``'s reach (which scans the whole window, forward and
    backward = window[:max(0, abs_start - w_start)]
    clause = _CLAUSE_BOUNDARY_RE.split(backward)[-1]
    if _REPORTED_SPEECH_RE.search(clause):
        return "reported"
    if base_position == "imperative" and not _cue_is_clause_head(clause):
        # match is never dropped. This is the STRUCTURAL check on TOP of
        return "descriptive"
    return base_position


def classify_text(text: str, *, cwd: Optional[str] = None) -> List[SuiteMatch]:
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


@dataclasses.dataclass(frozen=True)
class PrecisionMatch:

    detected: str
    matched_text: str
    span: Tuple[int, int]
    position: str
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
            continue

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
    if not isinstance(command, str) or not command or not cwd:
        return []
    repo_root = resolve_git_root(cwd) or cwd
    testpaths = _read_testpaths(repo_root)
    configured = _configured_test_cmds(repo_root)
    return _classify_command_precision_core(command, cwd, testpaths, configured)


def classify_text_precision(text: str, *, cwd: Optional[str] = None) -> List[PrecisionMatch]:
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
