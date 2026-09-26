"""coordinator_core.bash_guards.block_reviewer_bash_outside_allowlist — Python
engine-ification of DoE's retired
``coordinator/hooks/scripts/block-reviewer-bash-outside-allowlist.sh``
PreToolUse(Bash) hook (deleted 2026-07-16, DoE ``2f8b8450``).

Purpose: this module confines a SET of subagent types, not only reviewers
(as of Amendment 1, 2026-08-01, below) — see ``_helpers.
is_confined_findings_agent`` / ``_helpers._CONFINED_FINDINGS_AGENTS`` for the
current membership. Originally a single-member set
(``{"coordinator:code-reviewer"}``), granted Bash access ONLY so a confined
findings-agent could scaffold its own findings sidecar via
``coordinator-doc-new --type review-findings``. Without this guard, nothing
prevents a findings-agent under context pressure from running arbitrary
shell commands — violating read-only discipline. ``coordinator:executor`` was
added as a second confined member (Amendment 1) with a DIFFERENT, wider
per-type ruleset (it needs to run its own test suite) — see that section for
why the two types' rulesets diverge and how AC3 (code-reviewer unchanged) is
preserved despite the shared module.

This was originally a faithful byte-for-byte engine-ification of the
reference hook. It now carries two deliberate post-migration divergences,
plus a third added 2026-07-25 (below).

Divergence 1 (2026-07-22): coordinator-doc-new migrated to a naked-Python
CLI (commit b644d5a9, ``#!/usr/bin/env python3``, executable) per the
2026-07-21 de-polyglot mandate, so the ``bash `` interpreter-prefix
acceptance the reference hook offered is swapped for ``python3 `` -- a
``bash <path>`` invocation of a Python file fails at runtime, so continuing
to advertise it in the deny message would be a trap-offer. The
deny-message's "Accepted invocation forms" block is modernized to offer the
dispatch-time EM-resolved absolute-path form instead of the stale
bareword/``bin/``/``bash <path>`` examples.

Divergence 2 (2026-07-24): a quote-strip was added to first-token
extraction so a shell-quoted dispatch-time absolute path is recognised (see
``_extract_first_token`` for the exact, narrowly-scoped rule).

Divergence 3 (2026-07-25, initial): a second, READ-ONLY allowlist tier
was added -- ``git`` inspection subcommands (``show``, ``diff``, ``log``,
``status``, ``blame``, ``ls-files``, ``rev-parse``, ``describe``) and
filesystem read/enumeration binaries (``ls``, ``cat``, ``head``, ``tail``,
``wc``, ``find``, ``file``, ``stat``). Reason: the confined findings-agent
(``coordinator:code-reviewer``) was previously unable to see the diff it
was dispatched to review -- with Bash access confined to exactly one
scaffolder command, it could not run ``git show``/``git diff``/``git log``
and had to infer the change from current file state alone, silently losing
visibility into what a commit *removed* and any adjacent-behavior
regression a fix introduced. Source: example-cockpit-repo-em cross-repo memo
``cross-repo/inbox/2026-07-25-example-cockpit-repo-em-claude-klabauter-reviewer-readonly-git.md``.
The no-write guarantee this guard exists to enforce is UNCHANGED: every
newly-allowed command is read-only by construction (git subcommands are
matched via a strict per-subcommand allowlist, never a bare ``git *``
prefix -- ``commit``, ``push``, ``add``, ``checkout``, ``stash``, ``reset``,
``config``, etc. all fall through to deny; ``find`` is additionally denied
if it carries a write/execute flag such as ``-delete`` or ``-exec``), and
the 9-member shell-chaining-metacharacter deny and the strict
first-token boundary-anchoring apply to this tier exactly as they do to the
scaffolder tier -- checked FIRST, before either allowlist tier is
evaluated. Everything else -- the exact 9-member shell-metacharacter set,
the word-boundary ``--type review-findings`` check (scoped ONLY to the
coordinator-doc-new tier), and every pre-existing deny/allow CONDITION --
remains unchanged from the original byte-for-byte port.

Divergence 4 (2026-07-25, THIS change): ``grep`` was added to
``_READONLY_FS_BINARIES``. This closes the last read-only gap: the
confined findings-agent has no native content-search tool (no Grep/Glob
in its harness tool surface — see the DoE-side correction memo below), so
without ``grep`` it could enumerate and read files but not search their
contents. Requested by DoE-claude via cross-repo memo
``cross-repo/inbox/2026-07-25-doe-claude-em-reviewer-bash-search-fallback-correction.md``,
which retracts a larger earlier ask (that memo's predecessor claimed the
reviewer had "zero search capability" and asked for both ``find`` and
``grep``; the correction, sent after empirically probing this guard,
confirms ``find`` was already present and working, and narrows the ask to
``grep`` alone). No new write-vector guard analogous to ``_FIND_WRITE_FLAGS``
is warranted for ``grep`` -- see the negative-spec note on
``_is_readonly_fs_command`` for why.

Divergence 5 (2026-07-25, THIS change, P0 security fix): Tier A's initial
landing (Divergence 3) validated the git ``subcommand`` and then returned
ALLOW for the entire command string WITHOUT inspecting any further tokens --
an admitted subcommand could carry its own write-capable and execution-
capable options. Confirmed empirically against real git on this machine:
``git show --output=<path> <sha>`` and ``git log --output=<path>`` BOTH
create/overwrite an arbitrary file at ``<path>`` -- a total defeat of this
guard's no-write purpose, since this guard is the last structural barrier
preventing a findings agent from writing to disk after DR-058 removed the
Edit write-sandbox. Also confirmed allowed pre-fix (no metacharacter
involved, so the existing gate never saw them): ``git -c core.pager=evil
log`` / ``git -c diff.x.command=evil show HEAD`` (``-c`` config injection
into ``core.pager`` or a ``diff.<driver>.command``/``textconv`` driver --
either is exec-capable), ``git --exec-path=/tmp/evil show HEAD``
(redirects where git resolves its own subcommand binaries), and
``git show --ext-diff HEAD`` (enables an external diff driver). Fix: Tier A
validation is now genuinely two-level -- binary -> subcommand -> OPTIONS --
split at the subcommand boundary because option meaning is
position-dependent (``git -p log`` is the disallowed global ``--paginate``
short form; ``git log -p`` is the allowed, common ``--patch`` subcommand
flag; the walk below treats the same token differently depending on which
side of the subcommand it falls). See ``_evaluate_git_tier_a``,
``_locate_git_subcommand_and_bad_global``, and
``_find_git_subcommand_write_flag`` for the implementation, and the updated
Design/Allow/Deny sections below for the full two-level contract.

Divergence 6 (2026-07-25, THIS change, quote-aware metacharacter gate): the
metacharacter gate (a) was previously a raw substring scan over the WHOLE
raw command string, denying if any of the 9 banned tokens appeared
ANYWHERE -- including inside a quoted argument. Three independent
review-findings dispatches in one wave hit this as a false positive:
``grep -n "A|B" file`` (regex alternation), ``git log --format='%h|%s'``,
and ``grep -c ";" file`` were all wrongly denied, because the pipe/
semicolon were quoted DATA, not shell operators. The gate is now
token-aware: it walks the command character by character tracking POSIX
quote/escape state (single quotes: fully literal, no escaping; double
quotes: backslash escapes only ``" \\ $ ` <newline>``, command substitution
(backtick / ``$(``) STILL fires inside double quotes exactly as real bash
parses it, everything else is literal; unquoted: backslash escapes the
following character, so an escaped metacharacter such as ``grep foo \\> bar``
is literal data, not an operator). A metacharacter found OUTSIDE any quote
(or a backtick/``$(`` found inside a double-quoted region, where
substitution is still live) denies exactly as before; the same
metacharacter found inside a single-quoted argument, or inside a
double-quoted argument for the non-substitution operators
(``; && || | > < &``), is literal data and now allows. An
unbalanced/unterminated quote fails CLOSED (denies) rather than guessing at
the intended parse. Newline is unconditionally denied regardless of quoting
(unchanged from the pre-fix behavior -- not made quote-aware, per the
narrow scope of this fix). See ``_scan_for_unquoted_metacharacter`` for the
implementation and why it is a direct character-level state machine rather
than ``shlex`` -- both ``shlex.split(..., posix=True)`` and ``posix=False``
were evaluated and rejected (``posix=True`` collapses a quoted
``";"`` to a bare ``';'`` token indistinguishable from an unquoted one;
``posix=False`` both mis-splits ``--format='%h|%s'`` and fails to flag
``"$(...)"`` as still-dangerous inside double quotes) -- see that
function's docstring for the concrete counter-examples each mode gets
wrong.

Divergence 7 (2026-07-27, THIS change, policy-table refactor): the Tier A/B
allowlist surface (git read-only subcommands, git global options, git
subcommand write-flag denylist, read-only filesystem binaries, find's
write-flag denylist, the scaffolder binary name and its required argument)
is now resolved from DoE's declared ``bash_policy:`` table
(``coordinator/subagent-sandbox-policy.yaml``, loaded via
``coordinator_core.subagent_sandbox.engine.load_policy``) keyed by the
resolved ``effective_type``, instead of being exclusively hardcoded module
constants. The confined-agent-set membership check gains the same
policy-driven leg: a ``subagent_type`` present as a ``bash_policy:`` key is
confined even if it is not (yet) a member of ``_helpers._CONFINED_FINDINGS_
AGENTS`` -- this is what lets AC10 land a second confined agent class as a
pure-data YAML addition (a second ``bash_policy:`` row), with no code
change here.

**Fail-open inversion is deliberate and load-bearing (AC11).** Every other
``bash_policy``/``report_sidecar`` consumer in this codebase fail-opens a
lookup-miss to "not eligible" -- correct there, because a miss provisions
nothing (harmless). This guard is a DENY-GUARD, not an eligibility check: a
naive port of that same fail-open posture would mean "policy file missing/
unreadable/malformed" silently degrades to "everything this subagent type
runs is now unconfined", which is a silent total loss of the one structural
barrier a findings-agent has against arbitrary Bash after DR-058 removed
the Edit/Write write-sandbox (precedent:
cross-repo/archive/2026-07-25-claude-klabauter-em-code-reviewer-sidecar-provisioning-fails-most-spawns.md,
same "every miss fail-opens silently" failure shape, in production, 4 of 5
spawns). So EVERY leg of the policy resolution -- an absent/unreadable/
unparseable ``bash_policy:`` file, a missing key for this ``effective_
type``, or a per-key value that fails ``_validate_ruleset`` shape-checking
-- falls back to the ORIGINAL hardcoded module constants (``_GIT_READONLY_
SUBCOMMANDS``, ``_GIT_VALUE_TAKING_OPTIONS``, ``_GIT_NO_VALUE_OPTIONS``,
``_READONLY_FS_BINARIES``, ``_FIND_WRITE_FLAGS``, ``_ALLOWED_BINARY_
SUFFIX``, ``_REQUIRED_TYPE_ARG_END`` -- see ``_default_ruleset``), which
are KEPT in this file unchanged rather than deleted, and the confined-set
check falls back to ``is_confined_findings_agent`` (the ``_helpers``
hardcoded single-member set) rather than to "not confined". A lookup-miss
here degrades to the PRIOR enforcement, never to ALLOW. See
``_resolve_ruleset``/``_is_confined_type``/``_validate_ruleset`` for the
implementation and
``coordinator_core/bash_guards/tests/test_block_reviewer_bash_outside_allowlist_bash_policy_fallback.py``
for the dedicated fallback tests.

The shell-chaining-metacharacter gate (``_METACHARACTERS``/
``_scan_for_unquoted_metacharacter``) is deliberately NOT made policy-
driven -- it is a structural invariant applied identically before either
tier is evaluated, not a per-agent-type variable surface, and its
quote-aware state machine is exactly the kind of security-boundary logic
this divergence log (Divergence 6) already warns against complicating for a
construct no allowlisted command needs. ``bash_policy:``'s
``denied_metacharacters:`` row documents this set for a human reader; it is
not consulted at runtime.

**KNOWN LIMITATION (review finding 1, 2026-07-27, deliberately NOT fixed
here):** ``_evaluate_git_tier_a``'s two deny-reason strings (the bad-global-
option message and the bad-subcommand-option message) hardcode prose
describing the DEFAULT ruleset -- the allowed-global-option list
("-C, --git-dir, --work-tree, --no-pager, --literal-pathspecs") and the
denied-subcommand-option rationale ("--output/-o writes to an arbitrary
caller-chosen file... --ext-diff enables an external diff driver") -- rather
than being derived from the ``ruleset`` actually resolved for the calling
``effective_type``. Under the AC11 fallback this is correct (the ruleset IS
the default), but a well-formed CUSTOM ``bash_policy`` entry whose global- or
denied-option sets differ from the defaults would still see this same
DEFAULT-describing prose in its deny reason -- the offending token named is
still correct, only the explanatory parenthetical can go stale relative to
the real ruleset. This is a message-accuracy gap, not a security regression
(the underlying ALLOW/DENY decision is still ruleset-derived and correctly
fail-closed) -- deliberately left as-is rather than made ruleset-derived,
because AC5 pins these two deny-reason strings byte-identical across the
pinned matrix and no second ``bash_policy`` row exists in production today
to exercise the gap. Revisit -- make the message generation ruleset-derived
-- when a real second ``bash_policy`` row with a non-default global/denied-
option set actually lands.

Divergence 8 (2026-07-28, false-positive fix): two independent
``coordinator:code-reviewer`` dispatches in the same wave reported the same
two false positives against the ALREADY-quote-aware Divergence 6 gate:
(a) a top-level UNQUOTED ``|`` pipe was an unconditional deny even when
EVERY segment of the pipeline was independently Tier-A-allowlisted (e.g.
``git show <rev>:<path> | wc -c``), and (b) a plain ``2>/dev/null`` (or
``>/dev/null``) redirect was an unconditional deny. Root cause confirmed by
reading the code (not guessed): Divergence 6's quote-awareness was already
correct for DATA occurrences of a metacharacter inside quotes (a quoted
``|`` in a grep alternation already allowed before this change); the
reported false positives were both genuinely UNQUOTED, real shell operators
that the pre-Divergence-8 gate denied unconditionally regardless of
context. Two narrow, structurally write/exec-incapable carve-outs were
added:
  - a bare unquoted ``|`` (single pipe, never ``||``) is no longer an
    automatic deny -- ``_scan_for_unquoted_metacharacter`` now records its
    index as a pipeline split point instead of denying, and ``check()``
    verifies (``_evaluate_pipeline_segments``) that EVERY resulting segment
    is independently Tier A allowlisted (a read-only git subcommand or a
    read-only filesystem binary) before allowing the whole pipeline. A
    single non-allowlisted segment anywhere in the pipeline still denies
    the WHOLE command (see ``_pipeline_segment_deny_reason``). Tier B (the
    ``coordinator-doc-new`` scaffolder) is deliberately excluded from
    pipeline-segment eligibility -- see ``_segment_is_tier_a_allowlisted``.
  - a bare unquoted ``>``/``>>`` is allowed ONLY when it is a plain redirect
    to exactly ``/dev/null`` (``_match_devnull_redirect``) -- redirection to
    any other path (``git diff > /tmp/d``) still denies exactly as before.
Everything else this divergence log already documents is unchanged:
command substitution, ``;``/``&&``/``||`` chaining into anything, real
write-redirection, and backgrounding all still deny unconditionally, and
the deny message (``_METACHARACTER_REASON``) was updated to describe the
two new carve-outs precisely rather than continuing to claim ``|``/``>``
always deny.

Divergence 9 (2026-08-01, Amendment 1 -- confine coordinator:executor too;
REVERSED 2026-08-02 by PM ruling; DELETED entirely 2026-09-23, this plan's C1):

Amendment 1 added ``coordinator:executor`` to
``_helpers._CONFINED_FINDINGS_AGENTS`` and layered a wider, executor-specific
ruleset (an ``interpreter_allowed_modules``/``interpreter_allow_scripts``
pytest/script allowance, and a relaxed ``scaffolder_required_arg``) onto the
shared ``coordinator-doc-new`` scaffolder logic. ``docs/plans/2026-08-03-
narrow-subagent-commit-confinement-two-classes.md`` reversed the membership
addition the next day, so ``coordinator:executor`` was never confined once
that plan landed -- the override dict entries it left behind were dead code
from that point on (unreachable by ``_default_ruleset``, which is consulted
only for a confined type), and their own tests passed vacuously. Reported by
doe-claude-em (cross-repo/inbox/2026-09-06-doe-claude-em-executor-is-not-
confined-so-its-ruleset-override-is-dead.md) after the surviving prose here
misled claude-klabauter's own EM into asserting the opposite to two sibling repos.

**State (binding, current): ``coordinator:executor`` is unconfined outright
and carries no Bash ruleset of its own.** All three legs of
``_is_confined_type`` return ``False`` for it (the roster does list it, so
leg 3 does not fire either); the reviewer's own ruleset
(``_DEFAULT_RULESET_TYPE_OVERRIDES[_REVIEWER_TYPE]``) and the shared
``_default_ruleset()`` base are the only entries this module resolves. The
dead ``_EXECUTOR_TYPE`` override dict entries, the executor-specific deny-
message stanzas, and the tests that forced the executor into confinement to
exercise them were deleted by this plan's C1
(``state/bug-backlog/2026-09-06-divergence-9-s-executor-ruleset-override-
84156761aab8.yaml``), which also added a structural pin asserting every
``_DEFAULT_RULESET_TYPE_OVERRIDES`` key names a type ``_is_confined_type``
actually confines -- the pin that would have caught this divergence going
stale.

The python3-interpreter discrimination tier this divergence introduced
(``_evaluate_python3_interpreter``: unconditional deny for ``-c``/``-e``
inline code; a module-allowlist gate for ``python3 -m <module>``; a
per-type ``interpreter_allow_scripts`` gate for ``python3 <script-path>``)
is live code, unaffected by this deletion -- Amendment 2 (2026-08-03, below)
extended its module allowance to ``coordinator:code-reviewer`` and remains
the current behaviour for that type.

Divergence 10 (2026-08-01, stderr/stdout fd-duplication carve-out):
confirmed live, a confined ``coordinator:executor`` running its own ordinary
test command (a ``python3 -m pytest ...`` invocation ending in the reflexive
``2>&1`` idiom) was denied solely for that trailing redirect, once the
metacharacter gate's unconditional ``>`` deny (Divergence 6) reached it —
the pipe/devnull carve-outs (Divergence 8) did not cover it. PM ruling: this
allowlist enforces standards and coherence for a Bash-shaped tool, not an
adversarial security boundary — the test for a carve-out is "is this
coherent to a reader", not "could a determined attacker abuse it". The exact
token ``2>&1`` (digit ``2``, ``>``, ``&``, digit ``1``) is a SIBLING of the
already-landed ``/dev/null`` exemption on identical reasoning: it is
write-incapable (a file-descriptor duplication opens no file and truncates
nothing — it re-points an already-connected stream onto another
already-connected stream) and exec-incapable (it starts no process and
chains no command), so a reader who accepts the ``/dev/null`` carve-out and
then hits an unconditional deny on this equally-harmless, far more common
idiom has no coherent reason to be given for the difference. Modeled
directly on ``_match_devnull_redirect`` -- same place in the scan (the ``ch
== ">"`` branch of ``_scan_for_unquoted_metacharacter``), same shape of
lookahead-and-consume, same boundary rule (end-of-string, space, or a
top-level ``|``) -- see the new ``_match_fd_dup_redirect``. The mirror-image
token ``1>&2`` (stdout duplicated onto stderr) falls out of the SAME check
for free, via ``_FD_DUP_COMPLEMENT``'s symmetric ``{"1": "2", "2": "1"}``
mapping, and is included for the identical reason (equally write/exec-
incapable) rather than sought out separately. This carve-out is
deliberately EXACT, not generalized to "any ``&`` followed by a digit" --
e.g. a bare ``&1`` with no preceding fd digit, or a redirect target other
than the complementary standard-stream digit, still falls through to the
unconditional ``>``/``&`` deny exactly as before. Nothing else changes: the
pipe rule (Divergence 8) is untouched, the git/readonly-fs Tier A matching
and the Tier B scaffolder matching are untouched, and every other
metacharacter in the 9-member set still denies unconditionally.

Spec backlink: pln-findings-agents-self-persist-4c98b3 § D2 (bash-guard)
Ported from the retired DoE bash guard ``block-reviewer-bash-outside-allowlist.sh``
  (deleted 2026-07-16, DoE ``2f8b8450``).
Recipe: scratch/subagent-sandbox/bash-to-python-migration/W3a-preuse-bash-recipe.md
  § (a) (shared identity resolver + confined-findings-agent SSOT),
  § (b) item 2 (exact deny conditions to preserve byte-for-byte)

Design: effective type is resolved via an OR over two resolvers (both legs
always computed via the shared ``resolve_effective_types`` — see
``_helpers``/``subagent_sandbox.engine``):
  Primary:   agent_type (top-level payload field, present at Bash-call time for
             UNNAMED/foreground dispatch — the back-pointer is absent for those)
  Secondary: subagent_type via the dispatched-agents back-pointer chain
             (covers NAMED/teammate dispatch where agent_type is the teammate
             name)
The confined set is the SSOT helper ``is_confined_findings_agent`` in
``_helpers.py`` (ported from bash's ``_cs_is_confined_findings_agent``).
When the effective type is in the confined set, the command is checked
against a strict allowlist with TWO tiers:
  - Tier A (read-only inspection/discovery, added 2026-07-25, option-surface
    hardened 2026-07-25 same day per Divergence 5 above): first token is
    ``git`` (boundary-anchored) -- validation is then genuinely TWO-LEVEL,
    split at the subcommand boundary because option meaning is
    position-dependent:
      (i)  every ``-``-prefixed token BEFORE the subcommand is checked
           against a global-option ALLOWLIST -- only ``-C``/``--git-dir``/
           ``--work-tree`` (value-taking, either ``--opt=value`` or
           space-form) and ``--no-pager``/``--literal-pathspecs``
           (no-value) pass; anything else (``-c``, ``--exec-path``,
           ``--paginate``/``-p`` in GLOBAL position, ``--namespace``,
           ``--config-env``, ...) denies explicitly, by omission, with a
           reason naming the specific rejected option;
      (ii) the subcommand itself (first non-dash token reached) must be one
           of ``show``/``diff``/``log``/``status``/``blame``/``ls-files``/
           ``rev-parse``/``describe`` -- deny-by-omission, same as before;
      (iii) every token AFTER the subcommand (up to a bare ``--`` pathspec
           separator, which ends option parsing) is checked against a
           subcommand write/exec-flag DENYLIST -- ``--output`` and
           ``--ext-diff`` are matched by a hyphen-boundary PREFIX rule (bare,
           ``=``-form, and attached-no-``=`` all deny; a ``-`` immediately
           after the matched prefix is exempted, so
           ``--output-indicator-{new,old,context}=X`` stay permitted), and
           ``-o``/``-o<path>`` (attached form) deny explicitly; everything
           else (``--stat``, ``--oneline``, ``-p``, ``-5``, ``--format=...``,
           ``--name-only``, pathspecs, ...) stays allowed.
    OR first token is one of the read-only filesystem binaries
    ``ls``/``cat``/``head``/``tail``/``wc``/``find``/``file``/``stat``/
    ``grep`` (with ``find`` additionally denied if it carries a
    write/execute flag; ``grep`` added 2026-07-25 for content search) --
    these binaries have no analogous option-injection surface (no ``-c``,
    no ``--exec-path`` equivalent), so they keep the single-level
    binary-name check.
    OR first token matches ``machine-local`` (Divergence 11, 2026-08-02) AND
    the following subcommand is one of the read-only
    ``get``/``has``/``keys``/``path``/``dir`` -- a subcommand ALLOWLIST
    sibling of the git-subcommand check above, single-level (no
    option-injection surface analogous to git's), deny-by-omission for a
    write subcommand (``set``/``array-append``/``array-set``/
    ``migrate-publish-mirrors``) or a bare ``machine-local`` with no
    subcommand.
  - Tier B (scaffolder, original): must invoke coordinator-doc-new
    (optionally path-prefixed or via python3) AND include
    --type review-findings (word-boundary, not substring) -- the
    ``--type review-findings`` requirement is scoped to THIS tier only.
  - Both tiers: must contain no UNQUOTED shell-chaining metacharacter
    (; && || ` $( < & newline), checked FIRST before either tier and
    quote-aware (2026-07-25, Divergence 6) -- a metacharacter appearing
    inside a single- or double-quoted argument is literal data and does not
    deny (except backtick/``$(`` command substitution, which still fires
    inside double quotes per real shell semantics, and newline, which
    denies unconditionally regardless of quoting). An unbalanced/
    unterminated quote fails closed (denies). ``|`` and ``>``/``>>`` are
    narrower carve-outs as of 2026-07-28 (Divergence 8), not unconditional
    denies -- see immediately below.
  - Pipeline carve-out (Divergence 8, 2026-07-28): an UNQUOTED single ``|``
    (never ``||``, which still denies unconditionally) splits the command
    into pipeline segments instead of denying outright; the whole command
    is allowed only if EVERY segment independently satisfies Tier A (a
    read-only git subcommand or a read-only filesystem binary) --
    ``_evaluate_pipeline_segments``/``_segment_is_tier_a_allowlisted``. Tier
    B (the scaffolder) is not eligible as a pipeline segment. A single
    non-Tier-A segment anywhere in the pipeline denies the whole command.
  - Redirect carve-out (Divergence 8, 2026-07-28): an UNQUOTED ``>``/``>>``
    is allowed ONLY when it is a plain redirect to exactly ``/dev/null``
    (``_match_devnull_redirect``, e.g. ``grep foo bar 2>/dev/null``) --
    redirection to any other path still denies exactly as before this
    divergence.
  - fd-duplication carve-out (Divergence 10, 2026-08-01): an UNQUOTED ``>``
    is ALSO allowed when it is the exact stderr-to-stdout fd-duplication
    idiom ``2>&1``, or its stdout-to-stderr mirror ``1>&2``
    (``_match_fd_dup_redirect``, e.g. ``python3 -m pytest -q 2>&1``) --
    sibling of the ``/dev/null`` carve-out on identical write-incapable/
    exec-incapable reasoning; redirection using any other fd pairing still
    denies exactly as before this divergence.
Everything else is denied, fail-closed, with a design-as-offers reason.

Allow conditions (pass through):
  (1) tool_name != Bash -> allow (defense-in-depth; MATCHERS already filters).
  (2) No agent_id in payload -> top-level EM Bash call -> allow. Cheap
      pre-filter BEFORE any identity-resolution cost — the majority-case
      optimization the reference hook calls out repeatedly.
  (3) agent_id present, canonical resolution fails (unrecognised shape) ->
      fail-open allow.
  (4) effective type (OR of both legs) not in the confined set -> allow
      (executors, enrichers, integrators etc. keep their Bash freedom).
  (5) confined findings-agent + command is a read-only Tier A command
      (git invocation with clean global options, a read-only subcommand,
      and clean subcommand options -- see the two-level walk above -- a
      read-only filesystem enumeration/inspection/search binary, or a
      ``machine-local`` invocation with a read-only subcommand -- ``get``/
      ``has``/``keys``/``path``/``dir``, Divergence 11) -> allow.
  (6) confined findings-agent + command is clean
      ``coordinator-doc-new --type review-findings`` (Tier B) -> allow.
  (7) confined findings-agent + command is a top-level pipeline (Divergence
      8, 2026-07-28) whose EVERY segment independently satisfies (5) above
      -> allow the whole pipeline (e.g. ``git show <rev> | wc -c``).
  (8) confined findings-agent + command's only redirect is the exact
      ``2>&1``/``1>&2`` fd-duplication idiom (Divergence 10, 2026-08-01)
      -> allow (sibling of the ``/dev/null`` carve-out in (5)/Divergence 8).

Deny conditions:
  confined findings-agent + command is anything else -> deny, fail-closed.
  This is the ONE fold-candidate that is genuinely fail-CLOSED on ambiguity
  for the confined subset (contrast plan-body-write's fail-OPEN
  target-detection) — an empty/unparseable command, an unbalanced/
  unterminated quote (2026-07-25, Divergence 6 -- denies rather than
  guessing at the intended parse), a lone unquoted trailing backslash with
  nothing following (review Finding 5, 2026-07-25 -- same fail-closed
  treatment as an unterminated quote), an UNQUOTED occurrence of any of the
  7 unconditionally-banned shell-chaining metacharacters (``; && || ` $( <
  &``/newline -- quoted occurrences are literal data and allowed, except
  command substitution which still fires inside double quotes, and newline
  which always denies), an UNQUOTED ``|`` where at least one pipeline
  segment fails Tier A (Divergence 8, 2026-07-28 -- named in the reason), an
  UNQUOTED ``>``/``>>`` that is not a plain redirect to exactly
  ``/dev/null`` and not the exact ``2>&1``/``1>&2`` fd-duplication idiom
  (Divergence 8, 2026-07-28 / Divergence 10, 2026-08-01), a ``git`` invocation carrying a
  disallowed GLOBAL option (``-c``, ``--exec-path``, ``--paginate``/``-p``
  pre-subcommand, ``--namespace``, ``--config-env``, or any other option not
  on the narrow global allowlist -- deny-by-omission, named specifically in
  the reason), a ``git`` invocation whose subcommand isn't in the read-only
  set (``commit``, ``push``, ``add``, ``checkout``, ``stash``, ``reset``,
  ``config``, etc. all deny by NOT being on the allowlist -- deny-by-omission,
  never a denylist), a ``git`` invocation on an otherwise-read-only
  subcommand carrying a disallowed SUBCOMMAND option (``--output`` or
  ``--ext-diff`` in bare, ``=``-form, or attached-no-``=`` shape --
  hyphen-boundary prefix match, so ``--output-indicator-*`` stays exempted --
  or ``-o``/``-o<path>`` -- confirmed arbitrary-write / external-command
  vectors, named specifically in the reason), a ``find`` invocation carrying
  a write/execute flag
  (``-delete``/``-exec``/``-execdir``/``-ok``/``-okdir``/``-fprint``/
  ``-fprintf``/``-fls``), a first token that isn't Tier A or (optionally
  ``python3``-prefixed / path-prefixed) ``coordinator-doc-new``, or a
  missing/malformed ``--type review-findings`` argument on a Tier B command
  all deny.

Divergence 11 (2026-08-02, machine-local Tier A): ``machine-local`` is the
sanctioned path-resolution tool in this fleet -- the concrete-path-citation
guard's own remediation text instructs a caller to run
``machine-local get repos.<key>`` instead of hardcoding an absolute path,
and dispatch briefs instruct it too. Confirmed live twice in one day: a
confined ``coordinator:executor`` running exactly that instructed command
was denied by this guard, because ``machine-local`` was on no allowlist at
all -- an incoherence, not a security gap (PM ruling, same standard applied
to the ``2>&1`` carve-out, Divergence 10: this allowlist enforces standards
and coherence for a Bash-shaped tool, not an adversarial security boundary
-- an instruction a confined agent cannot follow teaches every reader the
rules are approximate). Fix: ``machine-local``'s READ subcommands --
``get``, ``has``, ``keys``, ``path``, ``dir`` -- are admitted as a new Tier
A leg (``_MACHINE_LOCAL_READONLY_SUBCOMMANDS`` /
``_evaluate_machine_local_tier_a``), modeled directly on
``_GIT_READONLY_SUBCOMMANDS`` / ``_evaluate_git_tier_a`` -- same subcommand-
ALLOWLIST shape (deny-by-omission, never a bare ``machine-local *`` prefix
match), same place in ``check()``'s evaluation order (a sibling ``elif``
immediately after the git branch, before the read-only-filesystem-binary
branch), and policy-driven the same way (Divergence 7) via two OPTIONAL
``bash_policy:`` keys (``machine_local_binary`` /
``machine_local_readonly_subcommands``) that fall back to these hardcoded
constants under the AC11 posture exactly like the interpreter keys do.
``machine-local``'s WRITE subcommands (``set``, ``array-append``,
``array-set``, ``migrate-publish-mirrors``) stay denied by the SAME
deny-by-omission mechanism the git tier already uses for ``commit``/
``push``/etc -- they are never enumerated as a denylist, because a future
write subcommand not yet named must deny too. A bare ``machine-local`` with
no subcommand also denies (it only prints usage; admitting it costs
nothing to refuse) -- ``_evaluate_machine_local_tier_a`` returns
``(False, None)`` when no subcommand token follows, identical treatment to
a bare ``git`` with no subcommand. Unlike git, ``machine-local`` has no
write/exec-capable GLOBAL or subcommand OPTION surface analogous to
``-c``/``--exec-path``/``--output`` -- it is a thin registry-lookup CLI,
not a general-purpose VCS with a config-injection surface -- so this new
leg is deliberately a single-level subcommand check, not the two-level
global-option/subcommand-option walk ``_evaluate_git_tier_a`` needs; adding
that machinery here would be unrequested generalization for an option
surface that does not exist.

No escape-hatch env var — the reference hook has none (unlike its
Write/Edit sibling guards); confirmed by direct read, not an omission here.
The new Tier A allowlist is a READ-ONLY escape hatch, not a general one --
it does not add an env-var bypass and it does not touch the metacharacter
gate.

Divergence 12 (2026-08-02, per-type deny-message remediation text; the
per-type override mechanism it introduced DELETED 2026-09-23 by this plan's
C1, since ``coordinator:executor`` was its sole caller): ``coordinator:
executor`` joining the confined set (Divergence 9) reused this guard's ONE
hardcoded deny message verbatim, which was written for ``coordinator:code-
reviewer`` and instructed the recipient to run ``coordinator-doc-new --type
review-findings`` (a pin the executor's own ruleset did not require), called
the recipient a "findings agent" throughout, and closed with "dispatch a
separate non-confined executor for that step" -- self-defeating advice when
the executor was the confined type being denied. Fixed at the time via a
``_DENY_MESSAGE_STANZA_OVERRIDES`` per-``effective_type`` dict, the same
data-driven shape ``_DEFAULT_RULESET_TYPE_OVERRIDES`` uses for ruleset
content. Once Divergence 9's confinement addition was reversed
(2026-08-02), that dict had exactly one entry (``coordinator:executor``) and
no live caller could ever reach it, so this plan's C1 deleted the dict, the
executor-specific header/stanza constants, and the lookup in
``_deny_reason`` entirely rather than keep an unreachable mechanism "for a
future confined type" -- zero cost is not a reason to keep code.
``_deny_reason`` now always renders the single default header and stanzas
(below); a future third confined type with distinct remediation text
reintroduces the per-type mechanism rather than resurrecting this one from
history.

Divergence 13 (2026-08-07, C6 of
`docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md`
-- total-lockout fix): measured, with a valid confined identity: every
PowerShell cmdlet denied -- Get-ChildItem, Select-String, Get-Content, gci,
the Where-Object pipeline -- while `git status --porcelain` allowed (see the
verdict record cited below, table 2). A `coordinator:code-reviewer` granted
PowerShell could run NOTHING. Root cause: this guard's Tier A allowlist was
expressed exclusively as POSIX binary names; a PowerShell cmdlet name is
never a match for a POSIX binary name, so every PowerShell command fell
through to the generic Tier B/deny path regardless of whether it was
genuinely read-only.

Fix: the allowlist is now expressed PER DIALECT, carried from
`payload["tool_name"]` via `_dialect.dialect_from_tool_name` (this guard's
own `MATCHERS` above now references `COMMAND_TOOL_NAMES` directly) -- never
inferred from the command string (Anti-scope: "do not build a dialect
detector"). `Dialect.BASH` keeps the pre-existing git/machine-local/
readonly-fs-binary/scaffolder Tier A+B logic byte-for-byte unchanged (AC4).
`Dialect.POWERSHELL` gains its OWN narrow Tier A allowlist
(`_READONLY_POWERSHELL_CMDLETS` / `_POWERSHELL_PIPELINE_FILTER_CMDLETS`,
see those constants' own docstrings for the exact three cmdlets admitted and
why) -- Tier B (the coordinator-doc-new scaffolder) and the python3-
interpreter tier are NOT extended to PowerShell by this divergence; neither
was reported as a concrete defect and widening either would be an
unrequested surface enlargement this fix does not ask for. A dialect this
module's `Dialect` enum does not (yet) recognize records SILENT
(`_verdict.record_silent`) and declines to rule rather than falling open --
see the dialect-gap leg in `check()`, currently unreachable given the
top-gate accepts only "Bash"/"PowerShell", kept explicit for a future third
dialect.

**Cite ratified `docs/decisions/DR-277-guards-are-advisory-by-default-two-
named.md` (carve-out 2):** the census flipped this guard to advisory on a
"not a security boundary" reading; DR-277 promotes it back to keep-hard as
the backstop for `block_subagent_commit`'s own miss (`183176e7`/`edd72e36`
-- an executor wrapping the commit API in `python3 -c` to evade three prose
instructions), reasoning that an advisory backstop is no backstop against a
model that already ignored prose.

**Negative spec, binding and non-optional:** SILENT for a dialect this guard
has no allowlist for is NOT a class flip and must NEVER be read as fail-open
drift away from DR-277. This guard stays fail-closed (hard-deny) for EVERY
dialect it DOES recognize -- `Dialect.BASH` and, as of this divergence,
`Dialect.POWERSHELL` both still deny-by-omission anything outside their own
Tier A/B allowlist, exactly as the pre-C6 Bash-only guard always did. SILENT
covers ONLY the dialect gap (a `Dialect` member with no allowlist expressed
here at all) -- it never covers, and must never be extended to cover, a
recognized-dialect MISS (a PowerShell or Bash command this guard understands
and denies today). A reader must not conclude from this divergence that
"PowerShell support" means PowerShell got any less strict than Bash already
was -- it did not; it got its OWN equally strict, equally narrow allowlist.

Divergence 14 (2026-08-10, confinement-policy self-edit close): a confined
type's ENFORCED Bash ruleset is now resolved exclusively from code
(`_default_ruleset()` / `_DEFAULT_RULESET_TYPE_OVERRIDES`), never from a
YAML-supplied `bash_policy:` per-type entry -- see `_resolve_ruleset`'s own
comment for the full mechanism, the two rejected alternatives (per-process
cache -- moot under this codebase's documented spawn-per-call architecture,
DR-215; relocating the policy file -- out of this module's repo/scope), and
the exact threat closed vs. left open. `_is_confined_type`'s YAML-driven
SET-MEMBERSHIP leg (Divergence 7/AC10) is UNCHANGED and deliberately not
touched by this divergence -- it is a widen-only lever, not an escape
lever. This narrows AC10's original "second confined type is a pure-data
YAML addition" promise: a new confined type now needs a
`_DEFAULT_RULESET_TYPE_OVERRIDES` code entry for anything beyond the shared
conservative default, since the ruleset itself is code-pinned now.
Reported: `state/bug-backlog/2026-08-10-a-reviewer-s-confinement-policy-is-
edita-459e2790ebb7.yaml`.

Divergence 15 (2026-08-10, C2 of
`docs/plans/2026-08-10-deny-unenumerated-agent-types-at-dispatch.md`, AC5 --
unenumerated-type-is-confined-not-exempt): `_is_confined_type` gains a THIRD
leg, `_helpers.is_confined_by_roster_absence`, OR'd onto the existing
`bash_policy:`-key and `is_confined_findings_agent` legs. Before this
divergence, a `subagent_type`/`agent_type` absent from BOTH existing legs
fell through to "not confined" -- unrestricted Bash -- which meant an
INVENTED type had a WIDER Bash surface than `coordinator:code-reviewer`,
this project's own findings agent. The new leg answers a different question
than the first two ("is this a type we chose to confine" vs. "do we even
know this type at all"): it confines only when `effective_type` is absent
from C1's dispatch-seam roster (`coordinator_core.hooks.
block_unenumerated_agent_type.resolve_roster` -- the same union-of-three
roster C1 denies dispatch against), never for a type this project simply
left unconfined on purpose (`coordinator:enricher` and siblings, on the
roster but in neither of the first two sets, are unaffected). Defense in
depth, not the primary fix -- C1's `PreToolUse(Agent)` deny already refuses
to let an unenumerated type reach dispatch at all; this leg only matters for
a caller that bypassed or predates that gate. See `is_confined_by_roster_
absence`'s own docstring for the fail-closed-on-roster-load-failure
contract and why it is checked last (real disk I/O, unlike the two cheaper
legs it supplements).

Divergence 16 (2026-08-11, named-teammate effective_type resolution fix):
confirmed live, same session, twice -- a `coordinator:executor` dispatched
WITH a `name` (an Agent-teams teammate) was denied `python3 -m pytest ...`
under the reviewer-shaped default ruleset, while the identical type
dispatched WITHOUT a `name` ran the same command fine. Root cause (pinned by
reading the payload-identity resolution, not guessed):
`payload["agent_type"]` for a NAMED dispatch is the teammate's `name` string,
not the real `coordinator:*` type (see the Design section's "Secondary:
subagent_type ... covers NAMED/teammate dispatch where agent_type is the
teammate name", already documented above this divergence). The old
`effective_type` selection (`agent_type if _is_confined_type(agent_type,
policy) else subagent_type`) asked only "is this leg confined at all" --
Divergence 15's leg 3 (unenumerated-roster catch-all) correctly confines the
raw name string (it genuinely is not a known type), which then WON priority
over the correctly back-pointer-resolved `subagent_type`, so
`effective_type` became the garbage name and `_resolve_ruleset` fell through
to the conservative default (no `_DEFAULT_RULESET_TYPE_OVERRIDES` entry
matches a random name) regardless of the dispatched agent's real type.
See `_is_type_known`/`_resolve_effective_type` for the fix: prefer a leg
resolving to a KNOWN identity (confined via legs 1/2, or simply enumerated
on the roster) over a leg confined only via leg 3's defensive catch-all.
Leg 3's fail-closed confinement verdict for a genuinely unknown type on
BOTH legs is unchanged -- this divergence only changes WHICH already-
confined identity's ruleset applies, never whether confinement fires at all
(negative spec, restated in `_resolve_effective_type`'s own docstring).

Divergence 17 (2026-08-11, THIS change -- close the accepted type-smuggling
residual): Divergence 16's fix above was accepted with a known residual --
`_resolve_effective_type` preferred a KNOWN `agent_type` outright without
ever comparing it against a KNOWN `subagent_type`, on the reasoning that the
dispatcher already chooses the agent type so this crosses no trust boundary.
That reasoning is overruled: `agent_type` on a NAMED dispatch is caller-
chosen free text, while `subagent_type` is derived exclusively from actual
dispatch records via the back-pointer chain -- a caller-chosen string must
never outrank a back-pointer-derived identity. Concretely, a caller could
previously dispatch a `coordinator:code-reviewer` with
`name: "coordinator:executor"` and have it run under the executor's wider
ruleset (`interpreter_allowed_modules: ("pytest",)`, empty
`scaffolder_required_arg`), because `agent_type == "coordinator:executor"`
(known) won over the correctly-resolved `subagent_type ==
"coordinator:code-reviewer"` (also known, but never consulted once
`agent_type` was known). Fix: `_resolve_effective_type` now checks
`subagent_type` FIRST -- a known `subagent_type` always wins over `agent_type`,
known or not, agreeing or not. `agent_type` is consulted only when
`subagent_type` is not known, which is exactly the unnamed-dispatch case
(`subagent_type` empty/absent) and Divergence 16's original named-dispatch
fix case (`agent_type` free text, unknown). See `_resolve_effective_type`'s
own docstring for the full ordering and why each of the four required
resolution shapes still holds. `is_confined` (the OR of `_is_confined_type`
over both legs, computed in `check()`) is untouched by this divergence --
only WHICH already-confined identity's ruleset applies can change, never
whether confinement fires at all.

Divergence 18 (2026-08-14, close the named-dispatch confinement-
manufacturing residual): confirmed live -- a NAMED (Agent-teams teammate)
dispatch whose back-pointer-resolved `subagent_type` is a type the policy
does NOT confine (e.g. `coordinator:git-commit-agent`) was denied a command
(`scoped-git-commit ...`) it is entitled to run, because `check()`'s
`is_confined` computation was an unconditional
`_is_confined_type(agent_type, policy) or _is_confined_type(subagent_type,
policy)`. For a named dispatch, `agent_type` is the caller-chosen teammate
NAME, never a real `coordinator:*` type (Design section, above); an unknown
name is confined by leg 3 of `_is_confined_type`
(`is_confined_by_roster_absence`), so the OR made EVERY named dispatch
confined regardless of its real type. `_resolve_effective_type`
(Divergence 16/17) then had no known identity to prefer over the
confining-via-leg-3 `agent_type`, so `effective_type` became the garbage
name and `_resolve_ruleset` fell through to `_default_ruleset()` -- the
narrow findings-agent allowlist -- for a dispatch that should not have been
confined at all. The same type dispatched UNNAMED was never confined
(`agent_type` then carries the real type, `subagent_type` empty), so
confinement depended on whether the EM typed a `name:` -- the actual bug;
Divergence 16/17 only ever fixed WHICH ruleset a confined identity gets,
never WHETHER confinement fires.

Fix: `check()` now prefers a KNOWN back-pointer-derived `subagent_type` for
the confinement verdict, but not unconditionally -- a KNOWN `subagent_type`
confines when it is itself confined, OR when `agent_type` is ALSO known and
confined (`_is_confined_type(subagent_type, policy) or
(_is_confined_type(agent_type, policy) and _is_type_known(agent_type,
policy))`). The original OR is consulted only when `subagent_type` is not
known (an unnamed dispatch, where it is empty; or a named dispatch whose
back-pointer chain itself failed to resolve) -- fail-closed leg-3
confinement for a type unknown on both legs is UNCHANGED.

**Staff-eng review (2026-08-14, finding 0/major) corrected the first cut of
this fix**, which let a KNOWN, non-confined `subagent_type` (e.g. a stale or
attacker-written `dispatched-agents.txt` row resolving to
`coordinator:enricher`) CLEAR confinement a KNOWN, genuinely-confined
`agent_type` (`coordinator:code-reviewer`) would otherwise impose -- a new
de-confinement primitive, not merely a parity fix. The corrected rule: a
caller-chosen NAME (an unknown `agent_type`) can neither manufacture
confinement for a real, non-confined type, nor -- the corrected direction --
can a KNOWN, non-confined `subagent_type` launder a KNOWN, genuinely-confined
`agent_type` into freedom. Both directions are pinned by the added test
suite. See `check()`'s own Divergence 18 comment for the exact code, and
`_resolve_effective_type`'s docstring for the scoping note this divergence
adds there (its own "never whether confinement fires" claim describes only
that function, not `check()`).

Divergence 20 (2026-08-21, generic-deny message honesty fix): filed as
`state/bug-backlog/2026-08-21-bash-guard-applies-code-reviewer-allowlist-
to-other-agents-intermittently.yaml` (defect 2 of 3; defects 1 and 3 of
that entry are a separate ruleset-selection question and an
unreproducible flap, both explicitly out of scope here). A `python3
"<script-path>"` invocation this ruleset does not admit (`interpreter_
allow_scripts` false) falls through `_evaluate_python3_interpreter` to
the generic Tier B deny message, whose `first_token` is `_extract_first_
token`'s EFFECTIVE token -- tokens[1], not tokens[0], for an exact
`python3 <script>` invocation (`_first_effective_token`'s own documented
behavior, unchanged by this divergence). The message then called that
effective token "first command token", which mis-describes argv[0]: a
denied `python3 "<path>/opt34.py"` read "first command token is not
coordinator-doc-new (got: <path>/opt34.py)", asserting the path WAS
argv[0] when it was argv[1]. Fix: the generic-deny branch now also reads
the raw, untouched `tokens_for_interpreter[0]` (already tokenized one
line above for the misspelling-alias check) and, only when it differs
from the effective token, names both ("command token is not
coordinator-doc-new (got: <effective>, invoked via <raw>)") instead of
mislabeling the effective token as "first". When the two coincide (every
non-python3-prefixed case, including the AC3/AC5-pinned `curl`/`rm`
messages) the original byte-identical string is untouched. The
effective-token MATCHING logic itself (`_first_effective_token`,
`_evaluate_python3_interpreter`, the alias-remedy tier below) is not
touched -- this is a message-accuracy fix, not a reclassification.

REFUTED prior hypothesis (recorded so it is not re-investigated): the
`dispatched-agents.txt` back-pointer row write is NOT broken. A live named
dispatch this session (`bp-probe@session-0b5c80ee`) produced a correct row
(`bp-probe@session-0b5c80ee<TAB>sonnet<TAB>coordinator:executor`) plus a
correct `em-session-id.txt`, at spawn time. The 187 `.agents/` directories
with no matching row are archived-session survivorship (session directories
are removed at SessionEnd, `.agents/` directories persist) -- not a write
failure.

Test surface: `coordinator_core/bash_guards/tests/test_block_reviewer_bash_outside_allowlist.py`
-- the eight probe commands from the verdict record
(`docs/research/spike-verdicts/2026-08-07-powershell-guard-detection-and-
tokenizer-mechanism.md`, table 2), both dialects (AC8), plus a control
proving each PowerShell-allow assertion is not vacuous (fails when this
divergence's PowerShell Tier A branch is reverted/stubbed out) -- see that
note's own docstring, `_resolve_subagent_identity` fail-closes on an
`agent_id` matching neither the bare-hex nor named-teammate shape, so every
test payload here uses a valid bare-hex `agent_id` (>= 12 chars) and
`agent_type: "coordinator:code-reviewer"` (the sole member of
`_CONFINED_FINDINGS_AGENTS`), or the test passes vacuously without ever
reaching detection.

Amendment 2 (2026-08-03, PM ruling -- re-anchor the discriminator, close the
pytest-for-code-reviewer gap): PM ruling, given verbatim: "bash confinement
should only be for destructive actions that would degrade a machine." This
re-anchors WHY a command is denied on destructive-vs-non-destructive, not on
the read-only-vs-executing framing the ``coordinator:executor``-only pytest
allowance (Divergence 9) was originally argued from. Running a test suite is
non-destructive -- it degrades nothing on the machine it runs on -- so an
allowlist built on the corrected discriminator has no principled reason to
grant it to one confined type and not the other.

Concretely: ``coordinator:code-reviewer`` could not run ``python3 -m pytest``
at all (Divergence 9's ``interpreter_allowed_modules`` default of ``()`` for
every type except ``coordinator:executor``'s override, and this module's own
prior KNOWN RESIDUAL prose explicitly said so -- a reviewer never reaches an
ALLOW via this leg). This cost real review fidelity: a dispatched
``coordinator:code-reviewer`` reviewing new regression tests could not run
them against a pre-fix tree, so a verdict on "would these tests have caught
the old bug" was hand-traced static analysis rather than execution, self-
flagged by that reviewer as a limitation its dispatcher needed to know about.

The KNOWN RESIDUAL block's own accepted rationale for ``coordinator:executor``
-- pytest's ``conftest.py``-auto-import residual is accepted because that
type "already has an unconfined Write tool," so this leg does not newly
grant that capability -- applies identically to ``coordinator:code-reviewer``,
confirmed from its own agent definition
(``coordinator/agents/code-reviewer.md``, DoE-claude repo):
``tools: ["Bash", "Read", "Edit", "ToolSearch"]`` -- an unconfined ``Edit``,
not a sandboxed one (the agent's own doc says so explicitly: "``Edit`` is
**not** structurally confined -- nothing blocks a source edit but the
contract"). An agent that can Edit can already author or modify a
``conftest.py``; denying it ``pytest`` bought no containment, only cost
verification. Fix: ``coordinator:code-reviewer`` gains its own
``_DEFAULT_RULESET_TYPE_OVERRIDES`` entry, ``interpreter_allowed_modules:
("pytest",)`` ONLY -- mirroring exactly the module-allowlist half of the
executor's override and nothing else. ``interpreter_allow_scripts`` (bare
``python3 <script-path>``) and ``scaffolder_required_arg`` (the ``--type
review-findings`` pin) are deliberately NOT extended to
``coordinator:code-reviewer`` by this amendment -- neither was reported as a
concrete defect, and widening either is a separate, unrequested surface
enlargement this ruling does not ask for. The ``-c``/``-e`` inline-code deny
(``_PY_INLINE_CODE_FLAGS``) is untouched and remains unconditional for both
confined types -- it is not a ``ruleset[...]`` lookup at all, so no per-type
override can re-admit it; the actual containment this guard exists to enforce
survives unweakened.

``rm`` was separately assessed, at the same PM ruling, for a scratchpad-scoped
carve-out (a confined agent wanting to clean up its own throwaway scratch
files under the session scratchpad) -- deliberately NOT implemented by this
amendment, assessment only: see the accompanying dispatch report for why a
path-scoped write/delete allowance is a materially different, higher-risk
shape than a module-allowlist read addition, and what it would take to
express safely in this guard's existing structure.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    normalize_executable_basename as _normalize_executable_basename,
    token_matches_binary as _token_matches_binary,
    tokenize_full_command as _tokenize_full_command,
)
from coordinator_core.bash_guards import _helpers
from coordinator_core.bash_guards._helpers import (
    is_confined_findings_agent,
    is_confined_by_roster_absence,
    resolve_git_root,
    _read_backpointer_subagent_type,
    prefix_denies,
    scan_tokens_until_separator,
)
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)
from coordinator_core.subagent_sandbox.engine import load_policy
from coordinator_core.bash_guards._dialect import Dialect, dialect_from_tool_name
from coordinator_core.bash_guards._verdict import record_silent
from coordinator_core.bash_guards._tool_names import COMMAND_TOOL_NAMES


# For NAMED-TEAMMATE dispatches that keys the wrong back-pointer dir, subagent_type
# the ALREADY-CORRECT canonical resolver (write_guards.block_subagent_plan_body_write._resolve_subagent_identity)
CLASS = "hard-deny"
#: guard's own MATCHERS declaration, not an edit to dispatch.py's chain loop
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&")

#: Deny-reason text for a banned, UNQUOTED metacharacter (2026-07-25,
#: unconditional deny by itself -- it is allowed to split a pipeline PROVIDED
_METACHARACTER_REASON = (
    "shell-chaining metacharacter detected outside any quoted argument "
    "(; && || ` $( < & or newline, or a non-allowlisted pipe/redirect -- "
    "see below). A metacharacter INSIDE a single- or double-quoted argument "
    "is literal data and is allowed -- quote the literal (e.g. "
    "grep -n \"A|B\" file, git log --format='%h|%s') or use grep -e A -e B "
    "instead of an unquoted regex alternation. Command substitution "
    "(backtick / $(...)) still denies even inside double quotes, since "
    "double quotes do not suppress it. An unquoted `|` pipe is allowed only "
    "when EVERY segment of the pipeline is independently on the Tier A "
    "read-only allowlist (e.g. `git show <rev> | wc -c`); an unquoted `>` "
    "or `>>` redirect is allowed only when it targets exactly /dev/null "
    "(e.g. `grep foo bar 2>/dev/null`) or is the exact stderr/stdout "
    "fd-duplication idiom `2>&1` (or its mirror `1>&2`, e.g. "
    "`python3 -m pytest -q 2>&1`) -- any other pipe segment or redirect "
    "target still denies"
)

#: to the generic ``_METACHARACTER_REASON`` text, which does not name the
def _pipeline_segment_deny_reason(segment: str) -> str:
    return (
        f"pipeline segment {segment!r} is not on the read-only Tier A "
        "allowlist -- an unquoted `|` shell-chaining metacharacter pipe is "
        "only allowed when EVERY segment is independently allowlisted (a "
        "read-only git subcommand -- show/diff/log/status/blame/ls-files/"
        "rev-parse/describe/check-ignore/check-attr/ls-tree/cat-file -- or a "
        "read-only filesystem binary -- ls/cat/"
        "head/tail/wc/find/file/stat/grep); this segment is not"
    )

_UNTERMINATED_QUOTE_REASON = (
    "command has an unbalanced/unterminated quote (unmatched ' or \") -- "
    "denied fail-closed rather than guessed at"
)

_TRAILING_BACKSLASH_REASON = (
    "command ends in a lone unquoted backslash with nothing following -- "
    "an incomplete/ambiguous shell fragment, denied fail-closed rather than "
    "guessed at"
)

_DOUBLE_QUOTE_ESCAPABLE = ('"', "\\", "$", "`", "\n")

_ALLOWED_BINARY_SUFFIX = "coordinator-doc-new"

#: switch, sparse-checkout, ...) denies. This is a subcommand ALLOWLIST,
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {
        "show",
        "diff",
        "log",
        "status",
        "blame",
        "ls-files",
        "rev-parse",
        "describe",
        "check-ignore",
        "check-attr",
        "ls-tree",
        "cat-file",
    }
)

_READONLY_FS_BINARIES = ("ls", "cat", "head", "tail", "wc", "find", "file", "stat", "grep")

_FIND_WRITE_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
)

#: ordering) -- an UNQUOTED ``>``/``|`` still denies exactly as before

#: directly on ``_GIT_READONLY_SUBCOMMANDS`` -- a subcommand ALLOWLIST, never
_MACHINE_LOCAL_BINARY = "machine-local"
_MACHINE_LOCAL_READONLY_SUBCOMMANDS = frozenset({"get", "has", "keys", "path", "dir"})

#: ``_READONLY_FS_BINARIES``'s bash-side rationale (enumeration/inspection/
#: Matched case-INSENSITIVELY (unlike the bash-side binary names) because
#: case). Deliberately EXCLUDES the common ``ls``/``dir``/``cat``/``type``
_READONLY_POWERSHELL_CMDLETS = frozenset(
    {"get-childitem", "gci", "select-string", "sls", "get-content", "gc"}
)

_POWERSHELL_PIPELINE_FILTER_CMDLETS = frozenset({"where-object", "?", "where"})


def _is_readonly_powershell_command(cmd: str) -> bool:
    """(C6, PowerShell dialect) ``True`` iff ``cmd``'s first token is a
    read-only PowerShell cmdlet/alias on ``_READONLY_POWERSHELL_CMDLETS``.
    Case-insensitive match (see that constant's own docstring for why).
    Sibling of ``_is_readonly_fs_command`` for the PowerShell dialect.
    """
    first_token = _extract_first_token(cmd)
    return first_token.lower() in _READONLY_POWERSHELL_CMDLETS


def _segment_is_powershell_tier_a_allowlisted(segment: str, *, is_first_segment: bool) -> bool:
    """(C6, PowerShell dialect) Sibling of ``_segment_is_tier_a_allowlisted``
    for a PowerShell pipeline segment: the FIRST segment must be a read-only
    data-source cmdlet (``_READONLY_POWERSHELL_CMDLETS``); any LATER segment
    may additionally be a filter cmdlet (``_POWERSHELL_PIPELINE_FILTER_CMDLETS``,
    e.g. ``Where-Object``), since a filter has no data source of its own and
    is only meaningful downstream of one.
    """
    first_token = _extract_first_token(segment).lower()
    if first_token in _READONLY_POWERSHELL_CMDLETS:
        return True
    if not is_first_segment and first_token in _POWERSHELL_PIPELINE_FILTER_CMDLETS:
        return True
    return False


def _evaluate_powershell_pipeline_segments(cmd: str, split_indices: list) -> Optional[str]:
    bounds = [-1] + split_indices + [len(cmd)]
    for idx, (start, end) in enumerate(zip(bounds, bounds[1:])):
        segment = cmd[start + 1 : end].strip()
        if not _segment_is_powershell_tier_a_allowlisted(segment, is_first_segment=(idx == 0)):
            return segment
    return None

#: hardening) that take a SEPARATE value argument (space-form, e.g.
_GIT_VALUE_TAKING_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree"})

#: ``CONFINEMENT_DENY`` band, strictly BEFORE ``git-no-optional-locks``
#: (``guard_no_optional_locks.py``, ``ADVISORY_REWRITE`` band) ever executes
_GIT_NO_VALUE_OPTIONS = frozenset({"--no-pager", "--literal-pathspecs", "--no-optional-locks"})


_REQUIRED_TYPE_ARG_END = "--type review-findings"
_REQUIRED_TYPE_ARG_MID = "--type review-findings "

_CMD_SAFE_MAX_LEN = 200

_GIT_SHORT_FORM_OUTPUT_FLAG = "-o"


#: ``_DEFAULT_RULESET_TYPE_OVERRIDES`` -- see the module docstring's
_REVIEWER_TYPE = "coordinator:code-reviewer"

#: verification fidelity (see the KNOWN RESIDUAL note on
#: STRUCTURAL PIN (this plan's C1): every key in this dict must be a type
_DEFAULT_RULESET_TYPE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    _REVIEWER_TYPE: {
        "interpreter_allowed_modules": ("pytest",),
    },
}


def _default_ruleset(effective_type: str = "") -> Dict[str, Any]:
    """(Divergence 7) The prior hardcoded enforcement, expressed in the same
    shape ``_validate_ruleset`` returns for a policy-supplied entry. This is
    the AC11 fallback -- returned whenever no ``bash_policy:`` entry for the
    resolved ``effective_type`` is available, readable, or well-formed. Every
    value here is one of the module's own pre-existing hardcoded constants --
    none are deleted or altered, per the AC11 requirement to KEEP the prior
    literals as the fallback.

    (Amendment 1, 2026-08-01; widened Amendment 2, 2026-08-03) ``effective_
    type`` selects an optional overlay from ``_DEFAULT_RULESET_TYPE_
    OVERRIDES`` -- ``coordinator:code-reviewer`` is the sole member (a
    pytest module allowance); any other type gets the base dict completely
    unchanged, byte-for-byte identical to the pre-Amendment-1 return value
    (AC3).
    """
    base: Dict[str, Any] = {
        "git_readonly_subcommands": _GIT_READONLY_SUBCOMMANDS,
        "git_global_value_options": _GIT_VALUE_TAKING_OPTIONS,
        "git_global_no_value_options": _GIT_NO_VALUE_OPTIONS,
        "git_subcommand_denied_options": ("--output", _GIT_SHORT_FORM_OUTPUT_FLAG, "--ext-diff"),
        "readonly_fs_binaries": _READONLY_FS_BINARIES,
        "find_denied_options": _FIND_WRITE_FLAGS,
        "machine_local_binary": _MACHINE_LOCAL_BINARY,
        "machine_local_readonly_subcommands": _MACHINE_LOCAL_READONLY_SUBCOMMANDS,
        "scaffolder_binary": _ALLOWED_BINARY_SUFFIX,
        "scaffolder_required_arg": _REQUIRED_TYPE_ARG_END,
        "interpreter_allowed_modules": (),
        "interpreter_allow_scripts": False,
    }
    base.update(_DEFAULT_RULESET_TYPE_OVERRIDES.get(effective_type, {}))
    return base


def _validate_ruleset(raw: Any) -> Optional[Dict[str, Any]]:
    """(Divergence 7) Validate one ``bash_policy:`` per-``effective_type``
    entry (already known to be a ``dict`` -- ``engine.load_policy`` drops any
    non-dict value at the top level) against the shape
    ``coordinator/subagent-sandbox-policy.yaml``'s ``bash_policy:`` header
    comment documents. Returns a normalized ruleset dict (see
    ``_default_ruleset`` for the shape) on success, or ``None`` on ANY
    structural defect -- a missing key, a wrong-typed value, an empty
    scaffolder binary/arg string, or a non-string list member. ``None`` here
    is the AC11 malformed-value signal: the caller (``_resolve_ruleset``)
    falls back to ``_default_ruleset()`` in full, never merges a partially
    valid entry with hardcoded defaults (a partial merge would let a
    malformed policy silently widen or narrow the surface in an
    unreviewable, field-by-field way).

    Negative spec (Divergence 14, 2026-08-10): this function's return value
    is no longer consumed by ``_resolve_ruleset`` for enforcement -- see
    that function's own comment. Kept, not deleted, purely as living
    documentation of the shape a future policy-driven ruleset would need to
    satisfy if a non-self-editable delivery path for one is ever built (e.g.
    a signed/read-only-mounted policy file outside any confined agent's
    write surface); do not read its continued presence as evidence the
    guard still honours a YAML-supplied ruleset today -- it does not.

    (Amendment 1, 2026-08-01) Two additional keys --
    ``interpreter_allowed_modules`` (a string list) and
    ``interpreter_allow_scripts`` (a bool) -- are OPTIONAL, unlike every key
    above: a policy entry authored before this divergence existed (e.g. an
    already-deployed ``coordinator:code-reviewer`` row) has neither, and
    must keep validating exactly as it did before. Absent, each defaults to
    the conservative (deny-more) value -- ``[]``/``False`` -- rather than
    invalidating the whole entry; PRESENT-but-wrong-typed still invalidates
    the whole entry, consistent with every other field's all-or-nothing
    treatment above.
    """
    if not isinstance(raw, dict):
        return None
    try:
        git_readonly = raw["git_readonly_subcommands"]
        git_global = raw["git_global_options"]
        git_denied_options = raw["git_subcommand_denied_options"]
        fs_binaries = raw["readonly_fs_binaries"]
        find_denied = raw["find_denied_options"]
        scaffolder_binary = raw["scaffolder_binary"]
        scaffolder_arg = raw["scaffolder_required_arg"]
    except (KeyError, TypeError):
        return None
    if not isinstance(git_global, dict):
        return None
    value_taking = git_global.get("value_taking")
    no_value = git_global.get("no_value")
    interpreter_modules = raw.get("interpreter_allowed_modules", [])
    interpreter_allow_scripts = raw.get("interpreter_allow_scripts", False)
    # (2026-08-02) machine-local Tier A keys are OPTIONAL, same treatment as
    # constants (a READ-ONLY allowlist), never widened by omission.
    machine_local_binary = raw.get("machine_local_binary", _MACHINE_LOCAL_BINARY)
    machine_local_readonly = raw.get(
        "machine_local_readonly_subcommands", list(_MACHINE_LOCAL_READONLY_SUBCOMMANDS)
    )

    def _is_str_list(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    if not (
        _is_str_list(git_readonly)
        and _is_str_list(value_taking)
        and _is_str_list(no_value)
        and _is_str_list(git_denied_options)
        and _is_str_list(fs_binaries)
        and _is_str_list(find_denied)
        and isinstance(scaffolder_binary, str)
        and scaffolder_binary
        and isinstance(scaffolder_arg, str)
        and scaffolder_arg
        and _is_str_list(interpreter_modules)
        and isinstance(interpreter_allow_scripts, bool)
        and isinstance(machine_local_binary, str)
        and machine_local_binary
        and _is_str_list(machine_local_readonly)
    ):
        return None
    return {
        "git_readonly_subcommands": frozenset(git_readonly),
        "git_global_value_options": frozenset(value_taking),
        "git_global_no_value_options": frozenset(no_value),
        "git_subcommand_denied_options": tuple(git_denied_options),
        "readonly_fs_binaries": tuple(fs_binaries),
        "find_denied_options": frozenset(find_denied),
        "machine_local_binary": machine_local_binary,
        "machine_local_readonly_subcommands": frozenset(machine_local_readonly),
        "scaffolder_binary": scaffolder_binary,
        "scaffolder_required_arg": scaffolder_arg,
        "interpreter_allowed_modules": frozenset(interpreter_modules),
        "interpreter_allow_scripts": interpreter_allow_scripts,
    }


def _resolve_ruleset(effective_type: str, policy: Any) -> Dict[str, Any]:
    """(Divergence 7, AC11) Resolve the Tier A/B allowlist ruleset for
    ``effective_type``.

    ``policy`` is VESTIGIAL as of Divergence 14
    below: this function no longer reads it at all (it terminates
    unconditionally in ``_default_ruleset(effective_type)``), kept ONLY for
    call-site parity with ``_is_confined_type`` (which still does consult
    ``policy`` for set-membership, see that function's docstring). Do not
    read the summary line above as "still policy-driven" -- see Divergence
    14 for why the YAML-entry path was deliberately removed.

    ``effective_type`` is a validated ``bash_policy:`` entry for that exact
    type if one is present and well-formed, else ``_default_ruleset()`` --
    the prior hardcoded enforcement. ``policy`` is whatever
    ``engine.load_policy`` returned (already fail-open to an empty
    ``Policy`` -- ``bash_policy == {}`` -- for an absent/unreadable/
    unparseable file, per that function's own contract), so a lookup-miss at
    EITHER layer (no policy loaded, or no entry for this type, or an
    entry that fails ``_validate_ruleset``) lands here identically: the
    hardcoded fallback, never an unconfined/empty ruleset.

    (Green-tests-inert-production fix, 2026-08-03) A validated ``bash_policy:``
    entry used to be returned VERBATIM, bypassing ``_DEFAULT_RULESET_TYPE_
    OVERRIDES`` entirely -- every test in this module's own suites calls
    ``check()``/``_resolve_ruleset()`` with ``policy_path=None`` (or a
    from-scratch tmp_path fixture that never happens to declare THIS type),
    so ``load_policy`` fails open to an empty ``Policy`` and every test only
    ever exercised the ``_default_ruleset()`` branch below -- the ONE branch
    that applies ``_DEFAULT_RULESET_TYPE_OVERRIDES``. Production's real,
    long-lived ``subagent-sandbox-policy.yaml`` (threaded through by
    ``dispatch.py``'s ``policy_file`` kwarg, per that module's own docstring)
    already carries a well-formed ``coordinator:code-reviewer`` entry
    authored BEFORE Amendment 2 (2026-08-03) added the ``pytest``
    ``interpreter_allowed_modules`` grant to ``_DEFAULT_RULESET_TYPE_
    OVERRIDES`` -- ``interpreter_allowed_modules``/``interpreter_allow_
    scripts`` are optional keys (``_validate_ruleset`` defaults each to the
    conservative ``[]``/``False`` when absent, precisely so an
    already-deployed policy row does not fail validation), so that entry
    validates successfully and used to WIN outright, silently shadowing the
    Python-side grant for every type with a live policy row -- the grant was
    genuinely wired, genuinely present in the imported module, and genuinely
    inert in production. Layering ``_DEFAULT_RULESET_TYPE_OVERRIDES`` onto
    a validated policy entry too (not only inside ``_default_ruleset()``)
    closes this: a YAML row authored before a given Amendment cannot shadow
    it. ``_DEFAULT_RULESET_TYPE_OVERRIDES.get(effective_type, {})`` is empty
    for any type absent from that dict, so an unresolved/unknown
    ``effective_type`` is untouched by this change -- it still resolves to
    whatever the validated entry (or ``_default_ruleset``) already produced,
    never a more-permissive ruleset by virtue of being unrecognized.
    """
    # `_DEFAULT_RULESET_TYPE_OVERRIDES`), which is what made the enforced
    # at a trust boundary" fix) was considered and REJECTED: this guard's
    # `_default_ruleset()`/`_DEFAULT_RULESET_TYPE_OVERRIDES` ONLY, never a
    # `_DEFAULT_RULESET_TYPE_OVERRIDES` code entry to get anything other
    # decide SET MEMBERSHIP -- this is intentionally unclosed because it is
    # `_CONFINED_FINDINGS_AGENTS` set or loosen its ruleset), so a confined
    return _default_ruleset(effective_type)


def _is_confined_type(effective_type: str, policy: Any) -> bool:
    """(Divergence 7, AC11; Divergence 15/AC5, 2026-08-10) A
    ``subagent_type``/``agent_type`` leg is confined if:

      1. it is an exact key in the loaded ``bash_policy:`` table (the
         data-driven SSOT -- what lets a second confined type land as a
         pure-data addition, AC10); OR
      2. it is a member of the ORIGINAL hardcoded ``_helpers.
         _CONFINED_FINDINGS_AGENTS`` set (the AC11 fallback -- consulted
         whenever the policy is absent, unreadable, malformed, or simply has
         no key for this type); OR
      3. (Divergence 15, AC5) it is absent from C1's dispatch-seam roster
         entirely (``_helpers.is_confined_by_roster_absence`` --
         ``coordinator_core.hooks.block_unenumerated_agent_type.
         resolve_roster``). Legs 1-2 above answer "is this type one we
         deliberately chose to confine"; leg 3 answers a DIFFERENT question
         this function never used to ask -- "do we even know this type at
         all". Before this leg, a ``subagent_type`` absent from BOTH of the
         first two checks fell through to "not confined", granting an
         INVENTED type a wider Bash surface than ``coordinator:
         code-reviewer`` -- the "less governed than any agent in the stable"
         defect docs/plans/2026-08-10-deny-unenumerated-agent-types-at-
         dispatch.md's Problem section names. An enumerated-but-
         not-explicitly-confined type (e.g. ``coordinator:enricher``, on the
         roster but in neither of the first two sets) still returns
         ``False`` overall -- leg 3 only fires for a type this function
         cannot find on ANY of the three legitimate-dispatch sources, never
         for a type this project simply chose to leave unconfined.

    This is an OR across all three legs: none of them is ever suppressed by
    another failing or being empty, only ever supplemented.

    Leg 3 is checked LAST, deliberately -- it is the one leg that performs
    real disk I/O (``resolve_roster()`` reads DoE's policy YAML, walks
    ``coordinator/agents/*.md``, and walks the plugin discovery tree), so
    the common case (an already-known confined OR already-known-and-exempt
    enumerated type) never reaches it. Defense in depth, not the primary
    fix: C1's ``PreToolUse(Agent)`` deny already refuses to let an
    unenumerated type reach dispatch at all, so leg 3 only fires for a
    caller that bypassed or predates that gate.
    """
    raw = getattr(policy, "bash_policy", None)
    if effective_type and isinstance(raw, dict) and effective_type in raw:
        return True
    if is_confined_findings_agent(effective_type):
        return True
    return is_confined_by_roster_absence(effective_type)


def _is_type_known(effective_type: str, policy: Any) -> bool:
    """(Divergence 16, 2026-08-11) ``True`` when ``effective_type`` resolves
    to a genuine dispatch-seam identity -- either a type this project
    deliberately confined (legs 1/2 of ``_is_confined_type``: a
    ``bash_policy:`` key, or membership in ``_helpers.
    _CONFINED_FINDINGS_AGENTS``), or a type C1's dispatch-seam roster
    enumerates at all (``not is_confined_by_roster_absence(...)``).

    This is a NARROWER question than ``_is_confined_type`` answers: leg 3 of
    that function (``is_confined_by_roster_absence``) confines an
    unenumerated string defensively (AC5/Divergence 15) -- correct for the
    confinement VERDICT, but it does not mean the string is a real type
    identity worth trusting for ruleset RESOLUTION. See
    ``_resolve_effective_type`` immediately below for why that distinction
    is the fix.
    """
    if not effective_type:
        return False
    raw = getattr(policy, "bash_policy", None)
    if isinstance(raw, dict) and effective_type in raw:
        return True
    if is_confined_findings_agent(effective_type):
        return True
    return not is_confined_by_roster_absence(effective_type)


def _resolve_effective_type(agent_type: str, subagent_type: str, policy: Any) -> str:
    """(Divergence 16, 2026-08-11, fix for the confirmed defect recorded at
    ``docs/problems/2026-08-11-a-dispatched-coordinator-executor-is-den.md``)

    Root cause, pinned by reading the payload-identity resolution above
    (``check()``, step 3) rather than guessed: for a NAMED (Agent-teams
    teammate) dispatch, ``payload["agent_type"]`` is NOT the dispatched
    agent's real ``coordinator:*`` type -- it is the teammate's own ``name``
    string (an arbitrary caller-chosen identifier, e.g. ``"archive-guard"``),
    per the module docstring's Design section ("Secondary: subagent_type via
    the dispatched-agents back-pointer chain (covers NAMED/teammate dispatch
    where agent_type is the teammate name)"). That string is, correctly,
    absent from C1's dispatch-seam roster, so ``_is_confined_type`` at
    Divergence 15's leg 3 (``is_confined_by_roster_absence``) confines it --
    the fail-closed-on-unknown-type verdict is right. The bug was the OLD
    ``effective_type`` selection (``agent_type if _is_confined_type(agent_type,
    policy) else subagent_type``): it asked ONLY "is this leg confined at
    all", so a raw teammate name confined solely via the defensive leg-3
    catch-all outranked the correctly back-pointer-resolved ``subagent_type``
    (the real ``coordinator:executor``/``coordinator:code-reviewer`` etc.)
    every time -- ``effective_type`` became the garbage name string, which
    matches no ``_DEFAULT_RULESET_TYPE_OVERRIDES`` entry, so
    ``_resolve_ruleset`` fell through to the conservative base ruleset (the
    reviewer-shaped scaffolder requirement) regardless of the dispatched
    agent's real, correctly-resolved type.

    Fix: prefer a leg that is KNOWN (``_is_type_known`` above -- confined via
    legs 1/2, or simply enumerated on the roster) over a leg that is confined
    ONLY via leg 3's defensive unknown-type catch-all. ``agent_type`` still
    wins when it IS a known identity (the unnamed/foreground-dispatch case,
    where ``agent_type`` already carries the real type and
    ``subagent_type`` is empty -- unaffected by this fix). When neither leg
    is known (a genuinely fabricated type on both legs, or a named dispatch
    of a type absent from the roster on both legs), this degrades to the
    ORIGINAL selection (whichever leg ``_is_confined_type`` accepts, agent_type
    first) -- fail-closed leg 3 confinement, and the resulting ruleset
    resolution, are UNCHANGED for that case; only a KNOWN-vs-garbage
    resolution priority was added.

    Negative spec: this does not, and must not, let a known-but-NOT-confined
    type (e.g. a real ``coordinator:enricher`` on either leg) smuggle a wider
    surface into an actually-confined dispatch -- ``is_confined`` (computed by
    the caller, see ``check()``) still governs whether this guard evaluates
    the command at all; this function only ever changes WHICH already-
    confined identity's ruleset applies, never whether confinement fires.

    (Divergence 18, 2026-08-14) The claim immediately above -- "never
    whether confinement fires" -- describes THIS function's own contract
    only, not ``check()``'s. As of Divergence 18, ``check()`` no longer
    computes ``is_confined`` as an unconditional OR over both legs: a KNOWN
    ``subagent_type`` now governs the confinement verdict outright, and the
    OR is consulted only as a fallback when ``subagent_type`` is not known.
    See ``check()``'s own Divergence 18 comment for the full mechanism and
    why an unconditional OR there was itself a defect (it MANUFACTURED
    confinement for a named dispatch of a genuinely non-confined type), not
    a restatement of the history above -- this note is additive, appended,
    not a revision of the Divergence 16/17 record.

    Divergence 17 (2026-08-11, close the type-smuggling residual accepted
    above): the ordering above was itself incomplete -- it preferred a KNOWN
    ``agent_type`` unconditionally, without ever comparing it against a KNOWN
    ``subagent_type``. ``subagent_type`` is derived exclusively from the
    dispatch-record back-pointer chain (``_read_backpointer_subagent_type``);
    ``agent_type`` on a NAMED dispatch is free text the caller chose. A
    caller who names a teammate with a literal known type STRING (e.g.
    ``name: "coordinator:executor"`` while actually dispatching a
    ``coordinator:code-reviewer``) previously won outright, because both legs
    being "known" was never distinguished from only ``agent_type`` being
    known -- the reviewer ran under the executor's wider ruleset. Principle:
    a caller-chosen string must never outrank a back-pointer-derived
    identity. Fix: check ``subagent_type`` FIRST -- a known ``subagent_type``
    always wins, whether or not ``agent_type`` is also known and whether or
    not the two agree. ``agent_type`` is consulted only when ``subagent_type``
    is NOT known (absent/empty for an unnamed dispatch, or itself
    unresolvable) -- this is what preserves the unnamed-dispatch path (bullet
    3: ``subagent_type`` empty -> not known -> falls through to
    ``agent_type``) and Divergence 16's original named-dispatch fix (bullet
    1: ``agent_type`` free text, unknown -> falls through to the correctly
    back-pointer-resolved ``subagent_type``) while closing the disagreement
    hole (bullet 2: both known, ``subagent_type`` now wins instead of
    ``agent_type``). ``is_confined`` is unaffected -- see the negative spec
    directly above; this function still only ever changes WHICH already-
    confined identity's ruleset applies.
    """
    if _is_type_known(subagent_type, policy):
        return subagent_type
    if _is_type_known(agent_type, policy):
        return agent_type
    if _is_confined_type(agent_type, policy):
        return agent_type
    return subagent_type


def _strip_crlf(cmd: str) -> str:
    return cmd.replace("\r", "")


_DEVNULL_TARGET = "/dev/null"

_FD_DUP_COMPLEMENT = {"1": "2", "2": "1"}


def _match_devnull_redirect(cmd: str, i: int) -> Optional[int]:
    n = len(cmd)
    j = i
    if j >= n or cmd[j] != ">":
        return None
    j += 1
    if j < n and cmd[j] == ">":
        j += 1
    if j < n and cmd[j] == " ":
        j += 1
    if cmd[j : j + len(_DEVNULL_TARGET)] != _DEVNULL_TARGET:
        return None
    j += len(_DEVNULL_TARGET)
    if j < n and cmd[j] not in (" ", "|"):
        return None
    return j - i


def _match_fd_dup_redirect(cmd: str, i: int) -> Optional[int]:
    """(Divergence 10, 2026-08-01) Given ``cmd[i] == ">"``, return the number
    of characters (starting at ``i``) consumed by the exact fd-duplication
    idiom ``2>&1`` (stderr onto stdout) -- or its mirror image ``1>&2``
    (stdout onto stderr, admitted for free by this same symmetric check, see
    ``_FD_DUP_COMPLEMENT``) -- or ``None`` if what surrounds ``i`` is not
    exactly one of those two tokens.

    Modeled directly on ``_match_devnull_redirect`` (same sibling carve-out,
    same shape of check): the leading fd digit (``2`` or ``1``) is an
    ordinary character already consumed by the caller's main scan loop
    BEFORE it ever reaches this function, so this function reads it back via
    ``cmd[i - 1]`` rather than re-consuming it, then requires exactly ``&``
    and the complementary digit, then a boundary (end-of-string, a space, or
    a top-level ``|``, matching ``_match_devnull_redirect``'s boundary
    exactly, so ``cmd 2>&1 | wc -l`` still allows the redirect AND still
    finds the pipe split immediately after).

    Neither this function nor its caller ever opens a file or starts a
    process: ``2>&1``/``1>&2`` duplicate one already-open standard stream
    onto another already-open standard stream -- write-incapable (no path is
    named, nothing is truncated or created) and exec-incapable (no command
    is invoked) -- the same two properties that justify the ``/dev/null``
    exemption immediately above. See the module docstring's Divergence 10
    entry for the full rationale and why this is a narrow, exact-token
    carve-out rather than a general ``&<digit>`` allowance.
    """
    n = len(cmd)
    if i >= n or cmd[i] != ">":
        return None
    if i == 0:
        return None
    complement = _FD_DUP_COMPLEMENT.get(cmd[i - 1])
    if complement is None:
        return None
    j = i + 1
    if j >= n or cmd[j] != "&":
        return None
    j += 1
    if j >= n or cmd[j] != complement:
        return None
    j += 1
    if j < n and cmd[j] not in (" ", "|"):
        return None
    return j - i


def _scan_for_unquoted_metacharacter(cmd: str) -> tuple:
    """(a) Token-aware shell-chaining-metacharacter scan (2026-07-25,
    Divergence 6 -- replaces the pre-fix raw substring scan over the whole
    raw command string, which denied a metacharacter appearing ANYWHERE,
    including inside a quoted argument).

    Walks ``cmd`` one character at a time tracking POSIX shell quote/escape
    state:
      - single-quoted: fully literal, no escaping recognised at all (a
        single quote can only be closed by another single quote);
      - double-quoted: a backslash escapes only ``" \\ $ ` <newline>``
        (``_DOUBLE_QUOTE_ESCAPABLE``) -- anything else after a backslash is
        two literal characters, matching real double-quote escaping rules.
        Command substitution (backtick / ``$(``) is STILL live inside
        double quotes, exactly as a real shell parses it (double quotes
        suppress word-splitting and globbing, NOT substitution), so those
        two are flagged even mid-double-quote;
      - unquoted: a backslash escapes the following character literally
        (so an escaped metacharacter, e.g. ``grep foo \\> bar``, is data,
        not an operator -- this matches real shell behavior, where the
        backslash strips the character's special meaning).

    (Divergence 8, 2026-07-28) Two additional carve-outs, both narrowly
    scoped and both structurally incapable of granting write/exec:
      - an unquoted ``>``/``>>`` is skipped over (not flagged) when it is a
        plain redirect to exactly ``/dev/null`` (``_match_devnull_redirect``)
        -- redirecting to any other path still flags ``>`` exactly as
        before;
      - a bare unquoted ``|`` (single pipe, NOT ``||`` -- that is checked
        first and still denies unconditionally) is not flagged as a deny by
        this function at all; instead its index is recorded as a pipeline
        split point. This function's caller (``check()``) is responsible for
        verifying every resulting segment is independently allowlisted
        (``_evaluate_pipeline_segments``) -- this scanner only proves the
        command is free of every OTHER banned metacharacter and locates
        where the pipe segments split.

    Returns a 3-tuple ``(deny_marker, split_indices, fixup_index)``:
      - ``deny_marker`` is the specific metacharacter found (``"\\n"`` for a
        newline, checked first and unconditionally regardless of quoting --
        narrow unchanged carry-over from the pre-fix behavior, not made
        quote-aware by this fix), the sentinel ``"<unterminated-quote>"``
        for an unbalanced quote (fails CLOSED rather than guessing at the
        intended parse), the sentinel ``"<trailing-backslash>"`` for a lone
        unquoted trailing backslash with nothing following (review Finding
        5, 2026-07-25 -- same fail-closed treatment as an unterminated
        quote, since it is equally an incomplete/ambiguous shell fragment),
        or ``None`` if the command has no OTHER banned metacharacter (a
        clean single command, or a pipeline whose only unquoted
        metacharacters are pipe splits / devnull redirects).
      - ``split_indices`` is the list of string indices (character offset of
        each unquoted, single ``|``) at which the caller should split ``cmd``
        into pipeline segments. Empty when there is no top-level pipe.
        Meaningless (always ``[]``) when ``deny_marker`` is not ``None`` --
        the caller must check ``deny_marker`` first.
      - ``fixup_index`` (2026-07-29, duty-of-care promotion) is the
        character offset the caller needs to build a copy-pasteable
        correction, valid only when ``deny_marker`` is
        ``"<unterminated-quote>"`` (offset of the QUOTE CHARACTER that never
        closed -- the same character this function already had to remember
        to enter the ``quote`` state) or ``"<trailing-backslash>"`` (offset
        of the trailing backslash itself, always ``len(cmd) - 1``).
        ``-1`` for every other ``deny_marker`` value, including ``None``.

    Deliberately NOT implemented via ``shlex``: both
    ``shlex.split(cmd, posix=True)`` and ``shlex.shlex(cmd, posix=False,
    punctuation_chars=...)`` were evaluated and rejected as unsound for this
    exact check --
      - ``posix=True`` strips quotes before returning tokens, so
        ``grep -c ";" file`` tokenizes to a bare ``';'`` token
        INDISTINGUISHABLE from an unquoted ``;`` -- the very information
        this check needs (quoted vs. unquoted) is thrown away by the
        library before we ever see it;
      - ``posix=False`` (quote characters retained in the token) both
        mis-splits a real, legitimate command --
        ``git log --format='%h|%s'`` splits ``|`` out as its OWN token,
        because non-posix mode does not support a quote opening mid-word
        the way posix mode does -- and fails to recognise that
        ``"$(whoami)"`` is still dangerous inside double quotes (it returns
        the whole quoted region as one inert-looking token).
    A direct character-level quote/escape walk -- an honest state machine
    mirroring the shell's own quoting grammar, not a regex -- is the
    correct tool for a check that hinges on exactly the quoted/unquoted
    distinction shlex's two modes each lose in a different way.

    Negative spec (review Finding 2, 2026-07-25): ``$'...'`` ANSI-C quoting
    is DELIBERATELY not recognized as its own quote form -- a ``'`` is
    always treated as a plain POSIX single-quote (no ``\\`` escaping at
    all), even when preceded by ``$``. Real bash's ANSI-C quoting form
    recognizes ``\\'`` inside a ``$'...'`` string as an escaped literal
    apostrophe (the quoted region does not close there), so this scanner's
    inferred quote span can close one character SOONER than bash's real
    ``$'...'`` span whenever an escaped ``'`` appears inside one. This is
    proven safe-direction, not a bypass: the scanner's span is always a
    SUBSET of (or equal to) bash's real span for this construct -- bash can
    only extend the quoted string further than this scanner believes,
    never end it sooner -- so the only possible failure mode is an extra,
    confusing DENIAL of an otherwise-safe command (e.g.
    ``$'a\\'; rm -rf /'`` is one inert ANSI-C-quoted argument to real bash,
    but this scanner sees the quote close at the escaped ``'`` and flags
    the trailing ``;`` as live). It can never produce a false ALLOW.
    Extending the state machine to special-case ``$'`` would add real
    parser complexity at a security boundary for a construct no command in
    the allowlisted tiers (grep/git/ls/cat/head/tail/wc/find/file/stat/
    coordinator-doc-new) plausibly needs -- that is not a good trade. If an
    allowlisted command ever needs ``$'...'`` in practice, THAT is the
    trigger to revisit this decision, not a hypothetical.
    """
    if "\n" in cmd:
        return "\n", [], -1
    i, n = 0, len(cmd)
    quote: Optional[str] = None
    quote_open_index = -1
    splits: list = []
    while i < n:
        ch = cmd[i]
        if quote == "'":
            if ch == "'":
                quote = None
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and cmd[i + 1] in _DOUBLE_QUOTE_ESCAPABLE:
                i += 2
                continue
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch == "`":
                return "`", [], -1
            if cmd[i : i + 2] == "$(":
                return "$(", [], -1
            i += 1
            continue
        if ch == "\\":
            if i + 1 >= n:
                return "<trailing-backslash>", [], i
            i += 2
            continue
        if ch == "'":
            quote = "'"
            quote_open_index = i
            i += 1
            continue
        if ch == '"':
            quote = '"'
            quote_open_index = i
            i += 1
            continue
        if cmd[i : i + 2] == "&&":
            return "&&", [], -1
        if cmd[i : i + 2] == "||":
            return "||", [], -1
        if cmd[i : i + 2] == "$(":
            return "$(", [], -1
        if ch == ">":
            consumed = _match_devnull_redirect(cmd, i)
            if consumed is not None:
                i += consumed
                continue
            consumed = _match_fd_dup_redirect(cmd, i)
            if consumed is not None:
                i += consumed
                continue
            return ">", [], -1
        if ch == "|":
            splits.append(i)
            i += 1
            continue
        if ch in (";", "`", "<", "&"):
            return ch, [], -1
        i += 1
    if quote is not None:
        return "<unterminated-quote>", [], quote_open_index
    return None, splits, -1


def _quote_context_window(cmd: str, index: int, radius: int = 20) -> str:
    start = max(0, index - radius)
    end = min(len(cmd), index + radius + 1)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(cmd) else ""
    return prefix + cmd[start:end] + suffix


def _metacharacter_deny_reason(
    found: str, cmd_for_check: str = "", fixup_index: int = -1
) -> str:
    """Map a ``_scan_for_unquoted_metacharacter`` result to deny-reason
    text (2026-07-25, Divergence 6).

    2026-07-29 (duty-of-care promotion, C/D -> B): the unterminated-quote
    and trailing-backslash legs used to name the failure class and stop
    there, leaving the caller to re-derive both WHICH quote/backslash is at
    fault and what a fixed command would look like. Both are now
    mechanically derivable from ``fixup_index`` (see
    ``_scan_for_unquoted_metacharacter``'s docstring) without a second
    parser: an unterminated quote is fixed, minimally, by closing it at the
    end of the string (this changes nothing about how any character BEFORE
    the open quote is interpreted); a trailing lone backslash is fixed by
    dropping it, since -- by construction of this scanner -- it has nothing
    after it to escape. Both corrected forms are built from
    ``cmd_for_check`` (the same string ``fixup_index`` was computed
    against) and are only ever offered as a syntactically-valid STARTING
    POINT, never asserted to be what the caller actually meant -- a real
    shell can extend a quote further than a single trailing close would
    imply if the caller genuinely wanted more text inside it.
    """
    if found == "<unterminated-quote>" and 0 <= fixup_index < len(cmd_for_check):
        quote_char = cmd_for_check[fixup_index]
        # `_sanitize_cmd_for_reason` truncates at `_CMD_SAFE_MAX_LEN` chars.
        corrected = _sanitize_cmd_for_reason(cmd_for_check) + quote_char
        context = _quote_context_window(cmd_for_check, fixup_index)
        return (
            f"{_UNTERMINATED_QUOTE_REASON}. The unmatched {quote_char} opens at "
            f"character {fixup_index} (near: {context!r}). Minimal fix that "
            f"becomes syntactically valid -- close it at the end: "
            f"{corrected!r}. Verify that's actually the string boundary you "
            "meant before resending; this only proves the syntax parses, not "
            "that the closing position is where you intended it."
        )
    if found == "<trailing-backslash>" and 0 <= fixup_index < len(cmd_for_check):
        corrected = _sanitize_cmd_for_reason(cmd_for_check[:fixup_index])
        return (
            f"{_TRAILING_BACKSLASH_REASON}. Dropping the dangling backslash "
            f"(nothing follows it to escape) gives a syntactically valid "
            f"command: {corrected!r}. If you meant a multi-line continuation, "
            "resend it as one single-line command instead -- this guard "
            "denies newlines unconditionally regardless of quoting."
        )
    if found == "<unterminated-quote>":
        return _UNTERMINATED_QUOTE_REASON
    if found == "<trailing-backslash>":
        return _TRAILING_BACKSLASH_REASON
    return _METACHARACTER_REASON


def _tokenize_segment(cmd: str) -> list:
    tokens = _tokenize_full_command(cmd)
    return tokens if tokens is not None else []


#: `/tmp/evil/python3`, and `PYTHONPATH=`/`PYTHONSTARTUP=`/`BASH_ENV=` reach
_EXEC_INFLUENCING_ENV_NAME_RE = re.compile(
    r"^(?:PATH|SHELL|IFS|ENV|BASH_ENV|LD_[A-Z0-9_]*|DYLD_[A-Z0-9_]*|PYTHON[A-Z0-9_]*)$"
)

_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

_ENV_IGNORE_ENVIRONMENT_FLAGS = ("-i", "--ignore-environment")


def peel_env_assignment_prefix(tokens: list) -> list:
    """Return ``tokens`` with a leading run of ``VAR=value`` assignments --
    and a leading bare ``env`` carrying its own assignments/``-i`` -- removed,
    so the effective token is resolved from the command actually being run.

    Why (memo, doe-claude-em, 2026-09-06): a ``coordinator:code-reviewer`` may
    run ``python3 -m pytest <file>`` (Amendment 2's ruling), but could not set
    an environment variable on that same sanctioned run, because tokens[0] was
    then the assignment (or ``env``) and matched no allowlisted binary. Any
    test whose fixture is selected by the environment was therefore unreachable
    to the agent that found the finding -- the guard did not prevent the run,
    it moved it to a human. Allowlisting ``env`` as a BINARY was rejected: it
    takes an arbitrary command and would admit everything the allowlist exists
    to exclude. This peel keeps the decision anchored on the invoked binary.

    Negative-spec -- what this deliberately does NOT peel:
      - An assignment whose name matches ``_EXEC_INFLUENCING_ENV_NAME_RE``.
        Peeling it would resolve the effective token from a name the
        assignment itself redirects (see that constant's own note). Returns
        ``tokens`` unchanged, so the caller denies as it did before.
      - ``env`` carrying any OTHER flag (``-S``/``--split-string``, ``-u``,
        ``-C``, ``--``): ``env -S`` re-parses its argument as a whole command
        line, so the token following it is not the invoked binary. Only the
        bare ``env`` + assignments/``-i`` form is a pure passthrough.
      - A ``python3`` unwrap. That stays in ``_first_effective_token`` where
        it already lives; this runs BEFORE it, so ``FOO=1 python3 -m pytest``
        reaches the same interpreter tier ``python3 -m pytest`` does.

    Assignment COUNT is uncapped: the name predicate above is the whole
    discriminator, and a second harmless assignment carries no risk a first
    one does not.
    """
    if not tokens:
        return []
    peeled = list(tokens)
    while True:
        before = len(peeled)
        while peeled:
            match = _ENV_ASSIGNMENT_RE.match(peeled[0])
            if match is None:
                break
            if _EXEC_INFLUENCING_ENV_NAME_RE.match(match.group(1)):
                return list(tokens)
            peeled = peeled[1:]
        if peeled and _normalize_executable_basename(peeled[0]) == "env":
            rest = peeled[1:]
            while rest:
                if rest[0] in _ENV_IGNORE_ENVIRONMENT_FLAGS:
                    rest = rest[1:]
                    continue
                match = _ENV_ASSIGNMENT_RE.match(rest[0])
                if match is None:
                    break
                if _EXEC_INFLUENCING_ENV_NAME_RE.match(match.group(1)):
                    return list(tokens)
                rest = rest[1:]
            if rest and rest[0].startswith("-"):
                return list(tokens)
            if not rest:
                return list(tokens)
            peeled = rest
        if len(peeled) == before:
            break
    return peeled


def _unpeeled_exec_influencing_env_name(cmd: str) -> Optional[str]:
    """Name of the leading environment assignment that stopped ``cmd`` from
    being peeled, or ``None`` when no leading assignment/``env`` prefix was
    the reason it went unpeeled.

    Message-accuracy only (2026-09-06): the allow/deny outcome is already
    settled by ``peel_env_assignment_prefix`` before this is consulted. It
    exists so a denial caused by ``PATH=``/``PYTHONPATH=``/``LD_PRELOAD=``
    names that cause instead of reporting the assignment token as an
    unrecognised binary -- see ``_EXEC_INFLUENCING_ENV_NAME_RE``.
    """
    for token in _tokenize_segment(cmd):
        match = _ENV_ASSIGNMENT_RE.match(token)
        if match is None:
            if _normalize_executable_basename(token) == "env" or token in _ENV_IGNORE_ENVIRONMENT_FLAGS:
                continue
            return None
        if _EXEC_INFLUENCING_ENV_NAME_RE.match(match.group(1)):
            return match.group(1)
    return None


def _first_effective_token(tokens: list) -> str:
    tokens = peel_env_assignment_prefix(tokens)
    if not tokens:
        return ""
    if tokens[0] == "python3" and len(tokens) >= 2:
        return tokens[1]
    return tokens[0]


def _extract_first_token(cmd: str) -> str:
    return _first_effective_token(_tokenize_segment(cmd))


#: ``_WINDOWS_ARGV0_HEAD_PATH_RE`` -- see that module's docstring for the
#: recognize are resolved from the ALREADY-RESOLVED ``ruleset`` for this
_ARGV0_HEAD_BOUNDARY_PRE = r"(?:\A|[;&|\n])\s*(?:['\"`(])?"
_RAW_HEAD_TOKEN_RE = re.compile(r"(" + _ARGV0_HEAD_BOUNDARY_PRE + r")([^\s;&|]+)")


def _windows_argv0_identity_names(ruleset: Dict[str, Any]) -> frozenset:
    names = {"git", ruleset["scaffolder_binary"], ruleset["machine_local_binary"]}
    names.add(ruleset["scaffolder_binary"] + ".py")
    names.update(ruleset["readonly_fs_binaries"])
    return frozenset(names)


def _build_windows_argv0_head_path_re(names: frozenset) -> "re.Pattern[str]":
    alternation = "|".join(re.escape(n) for n in sorted(names))
    return re.compile(
        r"(?P<sep>\A|[;&|\n])(?P<ws>\s*)(?P<q>[\"']?)"
        r"(?P<path>(?:[A-Za-z]:)?[\\/]{1,2}(?:[^\\/\r\n]+?[\\/])*"
        r"(?:(?:" + alternation + r")(?:\.(?:exe|cmd))?))"
        r"(?P=q)(?=[\s;&|]|\Z)",
        re.IGNORECASE,
    )


def _normalize_windows_argv0_head_path_with_spaces(cmd: str, ruleset: Dict[str, Any]) -> str:
    pattern = _build_windows_argv0_head_path_re(_windows_argv0_identity_names(ruleset))

    def _rewrite(m: "re.Match[str]") -> str:
        sep, ws, q, path = m.group("sep"), m.group("ws"), m.group("q"), m.group("path")
        normalized = path.replace("\\", "/")
        if not q and re.search(r"\s", normalized):
            quoted = normalized.replace("'", "'\\''")
            return sep + ws + "'" + quoted + "'"
        return sep + ws + q + normalized + q

    return pattern.sub(_rewrite, cmd)


def _normalize_windows_git_argv0(cmd: str, ruleset: Dict[str, Any]) -> str:
    """Rewrite a ``\\``-separated Windows path token, AT ARGV0 POSITION ONLY
    (the head of the command or of a ``;``/``&``/``|``/newline-separated
    segment), whose basename normalizes to one of
    ``_windows_argv0_identity_names(ruleset)`` (``C:\\Git\\bin\\git``,
    ``.\\git.exe``, ``C:\\tools\\coordinator-doc-new.cmd``, ...) into its
    forward-slash equivalent, BEFORE ``_tokenize_segment`` runs. Scoped to a
    raw, pre-tokenization ARGV0 token whose basename normalizes to EXACTLY
    one of the recognized names: an unrelated backslash elsewhere in the
    command is untouched, and a token like ``evil-coordinator-doc-new`` is
    untouched too (its normalized basename is the whole hyphenated string,
    not a member of the set).
    """
    names = _windows_argv0_identity_names(ruleset)

    def _rewrite(m: "re.Match[str]") -> str:
        boundary, token = m.group(1), m.group(2)
        if "\\" in token and _normalize_executable_basename(token) in names:
            token = token.replace("\\", "/")
        return boundary + token

    return _RAW_HEAD_TOKEN_RE.sub(_rewrite, cmd)


# DIFFERENT-direction fix for THIS module specifically: ``coordinator-doc-


def _git_command_tokens(cmd: str) -> list:
    tokens = peel_env_assignment_prefix(_tokenize_segment(cmd))
    if not tokens:
        return []
    start = 1
    if tokens[0] == "python3":
        start = 2
    return tokens[start:]


def _locate_git_subcommand_and_bad_global(tokens: list, ruleset: Dict[str, Any]) -> tuple:
    """(Tier A option-surface hardening, 2026-07-25) Walk ``tokens`` (the
    git-binary-stripped token list) validating each ``-``-prefixed token
    AHEAD of the subcommand against the read-only global-option allowlist
    (``_GIT_VALUE_TAKING_OPTIONS`` / ``_GIT_NO_VALUE_OPTIONS``) instead of
    unconditionally skipping any dash-prefixed token to hunt for the
    subcommand -- the pre-hardening walk this replaced skipped straight past
    write/exec-capable global options (``-c core.pager=evil``,
    ``--exec-path=...``, ``--paginate``) because it only ever looked for
    where the subcommand was, never validated what it stepped over.

    (Divergence 7, 2026-07-27) ``ruleset`` supplies the global-option
    allowlists -- ``ruleset["git_global_no_value_options"]`` /
    ``ruleset["git_global_value_options"]`` -- via ``_resolve_ruleset``,
    defaulting to ``_GIT_NO_VALUE_OPTIONS``/``_GIT_VALUE_TAKING_OPTIONS``
    (AC11 fallback) when no well-formed policy entry exists for the calling
    ``effective_type``.

    Returns ``(index, subcommand, bad_global_token)`` where exactly one of
    ``subcommand``/``bad_global_token`` is non-``None`` (or both ``None`` if
    no subcommand token is present at all, e.g. bare ``git`` or an
    options-only command): a disallowed global option returns
    ``(i, None, <offending token>)`` -- the literal token, not a bool, so the
    caller's deny reason can name exactly what was rejected; a subcommand
    found before any disallowed option returns
    ``(i, <subcommand>, None)`` with ``i`` the subcommand's own index (used
    by the caller to slice the remaining subcommand-option tokens).
    """
    no_value_options = ruleset["git_global_no_value_options"]
    value_taking_options = ruleset["git_global_value_options"]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("-"):
            return i, token, None
        base = token.split("=", 1)[0] if token.startswith("--") else token[:2]
        if base in no_value_options:
            i += 1
            continue
        if base in value_taking_options:
            if "=" in token or token != base:
                i += 1
            else:
                i += 2
            continue
        return i, None, token
    return len(tokens), None, None


def _find_git_subcommand_write_flag(tokens: list, ruleset: Dict[str, Any]) -> Optional[str]:
    """(Tier A option-surface hardening, 2026-07-25; policy-driven since
    Divergence 7, 2026-07-27) Scan ``tokens`` (the argv slice AFTER the git
    subcommand) for a write/exec-capable flag drawn from
    ``ruleset["git_subcommand_denied_options"]`` -- ``_default_ruleset()``
    populates this with the ORIGINAL hardcoded three
    (``"--output"``/``"-o"``/``"--ext-diff"``), preserving this function's
    pre-refactor behaviour byte-for-byte when no well-formed policy entry
    exists for the calling ``effective_type`` (AC11 fallback; AC5 relies on
    this identity). Each configured option is matched via the shared
    ``_helpers.prefix_denies`` hyphen-boundary rule EXCEPT the short-form
    ``-o`` flag, which needs the same special-cased attached-form check the
    pre-refactor ``_helpers.find_git_diff_family_write_flag`` used
    (``prefix_denies`` alone does not model a bare single-dash short option).
    """
    denied_options = ruleset["git_subcommand_denied_options"]
    for token in scan_tokens_until_separator(tokens):
        for option in denied_options:
            if option == _GIT_SHORT_FORM_OUTPUT_FLAG:
                if token == "-o" or (token.startswith("-o") and not token.startswith("--")):
                    return token
            elif prefix_denies(token, option):
                return token
    return None


def _segment_is_tier_a_allowlisted(segment: str, ruleset: Dict[str, Any]) -> bool:
    first_token = _extract_first_token(segment)
    if _token_matches_binary(first_token, "git"):
        allowed, _reason = _evaluate_git_tier_a(segment, ruleset)
        return allowed
    if _token_matches_binary(first_token, ruleset["machine_local_binary"]):
        allowed, _reason = _evaluate_machine_local_tier_a(segment, ruleset)
        return allowed
    return _is_readonly_fs_command(segment, ruleset)


def _evaluate_pipeline_segments(
    cmd: str, split_indices: list, ruleset: Dict[str, Any]
) -> Optional[str]:
    bounds = [-1] + split_indices + [len(cmd)]
    for start, end in zip(bounds, bounds[1:]):
        segment = cmd[start + 1 : end].strip()
        if not _segment_is_tier_a_allowlisted(segment, ruleset):
            return segment
    return None


def _evaluate_git_tier_a(cmd: str, ruleset: Dict[str, Any]) -> tuple:
    """(Tier A option-surface hardening, 2026-07-25) Given a command whose
    first token is already known to be ``git`` (boundary-anchored, checked
    by the caller via ``_token_matches_binary``), perform the full two-level
    validation: global options (pre-subcommand) -> subcommand
    (``ruleset["git_readonly_subcommands"]`` deny-by-omission allowlist) ->
    subcommand options (post-subcommand write-flag deny set).

    (Divergence 7, 2026-07-27) ``ruleset`` is resolved once per ``check()``
    call by ``_resolve_ruleset`` and threaded through unchanged -- see that
    function and ``_default_ruleset`` for the AC11 fallback shape.

    Returns ``(allowed, deny_reason)``:
      - ``(True, None)`` -- clean read-only git invocation, Tier A allows.
      - ``(False, "<reason>")`` -- a SPECIFIC option-level rejection (bad
        global option, or a write-flag on an otherwise-read-only
        subcommand); the caller uses this reason directly rather than
        falling through to the generic coordinator-doc-new deny message.
      - ``(False, None)`` -- the subcommand itself isn't on the read-only
        allowlist (or no subcommand was found at all) -- deny-by-omission,
        no option was rejected per se, so the caller falls through to the
        existing generic Tier B deny path (which names ``git`` as an
        unrecognised first token).
    """
    tokens = _git_command_tokens(cmd)
    idx, subcommand, bad_global = _locate_git_subcommand_and_bad_global(tokens, ruleset)
    if bad_global is not None:
        return False, (
            f"git global option {bad_global!r} is not on the read-only "
            "allowlist (-C, --git-dir, --work-tree, --no-pager, "
            "--literal-pathspecs). git global options can inject write/exec "
            "behavior before the subcommand even runs (-c core.pager=..., "
            "-c diff.<driver>.command=..., --exec-path, --paginate), so only "
            "this narrow read-only set is permitted -- drop the option, or "
            "if you need the subcommand's own flags, put them AFTER the "
            "subcommand instead (e.g. `git log -p`, not `git -p log`)"
        )
    if subcommand is None or subcommand not in ruleset["git_readonly_subcommands"]:
        return False, None
    bad_option = _find_git_subcommand_write_flag(tokens[idx + 1 :], ruleset)
    if bad_option is not None:
        return False, (
            f"git {subcommand} option {bad_option!r} is a write/exec vector "
            "-- --output/-o writes to an arbitrary caller-chosen file "
            "(confirmed: `git show --output=<path>` and `git log "
            "--output=<path>` both create the target file) and --ext-diff "
            "enables an external diff driver. Drop the option and read the "
            "output from stdout instead"
        )
    return True, None


def _evaluate_machine_local_tier_a(cmd: str, ruleset: Dict[str, Any]) -> tuple:
    tokens = _tokenize_segment(cmd)
    start = 2 if tokens and tokens[0] == "python3" else 1
    if len(tokens) <= start:
        return False, None
    subcommand = tokens[start]
    if subcommand in ruleset["machine_local_readonly_subcommands"]:
        return True, None
    return False, None


def _has_find_write_flag(cmd: str, ruleset: Dict[str, Any]) -> bool:
    denied = ruleset["find_denied_options"]
    return any(token in denied for token in _tokenize_segment(cmd))


def _is_readonly_fs_command(cmd: str, ruleset: Dict[str, Any]) -> bool:
    """(Tier A, 2026-07-25; policy-driven since Divergence 7, 2026-07-27)
    True iff the command's first token (boundary-anchored) is one of the
    read-only filesystem enumeration/inspection/search binaries in
    ``ruleset["readonly_fs_binaries"]``, with ``find`` additionally denied
    if it carries a write/execute flag (see ``_has_find_write_flag``).
    ``grep`` needs no analogous flag check -- see the negative-spec comment
    above ``_FIND_WRITE_FLAGS``.
    """
    first_token = _extract_first_token(cmd)
    binaries = ruleset["readonly_fs_binaries"]
    matched = next(
        (b for b in binaries if _token_matches_binary(first_token, b)),
        None,
    )
    if matched is None:
        return False
    if matched == "find" and _has_find_write_flag(cmd, ruleset):
        return False
    return True


def _first_token_is_allowlisted_binary(cmd: str, ruleset: Dict[str, Any]) -> bool:
    """(b) First significant token (after optionally stripping a leading
    ``python3 `` prefix) is exactly ``coordinator-doc-new``, or ends with a
    path-separator-anchored ``coordinator-doc-new`` suffix (reference hook
    247-268, since diverged post-migration). Accepts bare, ``bin/``-prefixed,
    absolute-path, and ``python3 <path>`` forms.

    2026-07-22 post-migration divergence: coordinator-doc-new is now a naked
    Python CLI (``#!/usr/bin/env python3``, executable) per the 2026-07-21
    de-polyglot mandate. A ``bash <path>`` invocation would fail at runtime
    (bash cannot execute a Python file) -- accepting it here would be a
    trap-offer, not a real allowed form. ``python3 <path>`` is the sanctioned
    interpreter-explicit fallback for platforms where direct shebang exec is
    unavailable, so the stripped prefix swaps from ``bash `` to ``python3 ``.

    F1 fix (2026-07-22 review): the suffix check is boundary-anchored --
    either the token equals ``coordinator-doc-new`` exactly, or the
    character immediately preceding the suffix is a path separator (``/`` or
    ``\\``, Windows-first-class since a dispatch prompt may inject a
    backslash path). This closes the free-text-suffix bypass where a token
    like ``evil-coordinator-doc-new`` or ``yoloscoordinator-doc-new``
    mechanically matched a bare ``endswith()`` check while preserving
    bareword, ``bin/``-prefixed, and absolute-path (POSIX or Windows)
    acceptance.

    Divergence 19 (2026-08-17, false-deny fix): also accepts the SAME token
    with a literal ``.py`` suffix appended to ``ruleset["scaffolder_binary"]``
    (e.g. ``coordinator-doc-new.py``), tried as a second, independent
    ``_token_matches_binary`` call -- not a change to ``normalize_executable_
    basename`` itself, which stays scoped to the ``.exe``/``.cmd`` Windows-
    launcher axis it was built for and is shared by the git/readonly-fs/
    machine-local matchers too (stripping ``.py`` THERE would let a
    confined-agent-authored script literally named e.g. ``git.py`` pass as
    real ``git`` for Tier A's git-subcommand walk, since that walk trusts the
    matched identity rather than the file's actual contents -- a genuine
    widening this fix must not cause). ``coordinator-doc-new`` is the ONE
    binary this guard's own module docstring already documents as a naked
    Python script (Divergence 1: ``#!/usr/bin/env python3``), and its real
    on-disk name IS ``coordinator-doc-new.py`` (confirmed on disk; the
    ``.cmd`` Windows-launcher twin is a SEPARATE generated file that keeps
    the bare ``coordinator-doc-new.cmd`` name, no ``.py`` in it -- see this
    file's own ``token_matches_binary`` migration comment above) -- so
    every absolute-path or ``python3``-prefixed dispatch of the REAL script
    carries that suffix and previously matched neither ``_ALLOWED_BINARY_
    SUFFIX`` nor its normalized basename. The legacy bareword/``bin/``-
    relative forms (no ``.py``) are UNCHANGED and still match via the first,
    unmodified ``_token_matches_binary`` call -- this is additive, not a
    replacement of the existing match. Root cause confirmed live: six
    ``test_confined_*_allows`` cases all used the same ``.py``-suffixed
    absolute path and all failed for this one reason, not six unrelated
    bugs -- see ``test_block_reviewer_bash_outside_allowlist.py``'s own
    ``_CLAUDE_KLABAUTER_ABS_PATH`` constant.
    """
    first_token = _extract_first_token(cmd)
    binary = ruleset["scaffolder_binary"]
    return _token_matches_binary(first_token, binary) or _token_matches_binary(
        first_token, binary + ".py"
    )


def _has_required_type_arg(cmd: str, ruleset: Dict[str, Any]) -> bool:
    """(c) ``ruleset["scaffolder_required_arg"]`` present as a complete word
    — not a prefix of a longer type value (reference hook 270-279).
    (Divergence 7, 2026-07-27) policy-driven; ``_default_ruleset()``
    populates this with ``_REQUIRED_TYPE_ARG_END`` (``"--type
    review-findings"``), preserving the original word-boundary match exactly
    when no well-formed policy entry exists for the calling
    ``effective_type``.
    """
    required = ruleset["scaffolder_required_arg"]
    return cmd.endswith(required) or (required + " ") in cmd


_PY_INLINE_CODE_FLAGS = frozenset({"-c", "-e"})


def _python3_inline_code_deny_reason(flag: str) -> str:
    return (
        f"python3 {flag!r} runs inline code and is denied unconditionally -- "
        "this is the exact bypass shape (python3 -c \"...dispatch_message...\") "
        "the confinement this guard enforces exists to close, and no ruleset "
        "can re-enable it. Run a script file instead (python3 <script.py>) "
        "or, for module invocation, python3 -m <allowlisted-module>"
    )


def _python3_module_not_allowlisted_deny_reason(module: str, ruleset: Dict[str, Any]) -> str:
    allowed = ", ".join(sorted(ruleset.get("interpreter_allowed_modules") or ())) or "(none)"
    return (
        f"python3 -m {module!r} is not on this agent type's interpreter "
        f"module allowlist ({allowed}) -- -m module invocation is itself "
        "allowlisted, not blanket-permitted, so an unlisted module (e.g. "
        "coordinator_core.invoke, which can reach a committing op) denies "
        "even though the python3 -m shape is otherwise permitted"
    )


def _evaluate_python3_interpreter(tokens: list, ruleset: Dict[str, Any]) -> Optional[tuple]:
    """(Amendment 1, 2026-08-01) Discriminate a ``python3 <...>`` invocation
    that Tier B's scaffolder check has ALREADY rejected -- this is only
    called from that fallthrough (see ``check()``), so a genuine
    ``python3 <path-to-coordinator-doc-new>`` invocation never reaches here
    at all; it is resolved entirely by the pre-existing, untouched Tier B
    path, which is what keeps AC3 (code-reviewer unchanged) intact.

    Returns ``None`` when this tier does not classify ``tokens`` at all
    (empty, first token isn't exactly ``python3``, nothing follows it, or an
    unrecognized ``-``-prefixed second token) -- the caller falls through to
    the ORIGINAL, unchanged generic Tier B deny message for those cases.
    Also returns ``None`` for a script-path second token when this
    ``ruleset`` does not opt into ``interpreter_allow_scripts`` (the
    ``coordinator:code-reviewer`` default) -- again deferring to the
    original generic message rather than emitting a new one, since AC3
    requires that message's TEXT stay byte-for-byte unchanged for that type.

    Otherwise returns ``(allowed, deny_reason)``:
      - ``tokens[1]`` is ``-c``/``-e`` -> ``(False, ...)`` UNCONDITIONALLY,
        not gated by ``ruleset`` at all (AC2a).
      - ``tokens[1]`` is ``-m`` -> ``(True, None)`` only if ``tokens[2]`` is
        a member of ``ruleset["interpreter_allowed_modules"]``; otherwise
        ``(False, ...)`` naming the rejected module (AC2b).
      - ``tokens[1]`` is a script path (does not start with ``-``) and
        ``ruleset["interpreter_allow_scripts"]`` is true -> ``(True, None)``.
    """
    if len(tokens) < 2:
        return None
    raw_leading = tokens[0]
    basename = _normalize_executable_basename(raw_leading)
    if basename != "python3":
        # (C1b, 2026-08-11) A PATH-PREFIXED python-family basename (e.g.
        # `.venv/Scripts/python.exe`, `/repo/.venv/bin/python`) is a LOCATION
        raw_leading_stripped = raw_leading.rstrip("/\\")
        has_path_separator = "/" in raw_leading_stripped or "\\" in raw_leading_stripped
        if not (has_path_separator and _PYTHON_FAMILY_ALIAS_RE.match(basename)):
            return None
    second = tokens[1]
    if second in _PY_INLINE_CODE_FLAGS:
        return False, _python3_inline_code_deny_reason(second)
    if second == "-m":
        if len(tokens) < 3:
            return False, "python3 -m requires a module argument"
        module = tokens[2]
        if module in (ruleset.get("interpreter_allowed_modules") or ()):
            return True, None
        return False, _python3_module_not_allowlisted_deny_reason(module, ruleset)
    if second.startswith("-"):
        return None
    if ruleset.get("interpreter_allow_scripts"):
        return True, None
    return None


_PYTHON_FAMILY_ALIAS_RE = re.compile(r"^(python2(\.\d+)?|python3\.\d+|python|py)$")


def _python_family_alias_token(token: str) -> Optional[str]:
    basename = _normalize_executable_basename(token)
    if basename == "python3":
        return None
    if _PYTHON_FAMILY_ALIAS_RE.match(basename):
        return basename
    return None


def _remedy_command_with_python3(cmd: str, leading_token: str) -> str:
    stripped = cmd.lstrip()
    lead_ws = cmd[: len(cmd) - len(stripped)]
    if stripped[: len(leading_token)] != leading_token:
        return cmd
    rest = stripped[len(leading_token):]
    return f"{lead_ws}python3{rest}"


def _python_family_misspelling_deny_reason(remedy_cmd: str) -> str:
    return (
        "first command token is a python interpreter spelling other than "
        "the accepted `python3` -- this command IS in scope, just "
        f"misspelled. Retry with: {remedy_cmd!r}"
    )


def _sanitize_cmd_for_reason(cmd: str) -> str:
    """Port of CMD_SAFE (reference hook 288-294): strip tab/CR/LF/FF/VT to a
    single space, strip remaining C0 control chars, truncate to 200 chars
    with a trailing ellipsis, and fall back to a placeholder when empty.
    """
    safe = cmd
    for ch in ("\t", "\r", "\n", "\f", "\v"):
        safe = safe.replace(ch, " ")
    safe = "".join(ch for ch in safe if ord(ch) > 0x1F or ch == " ")
    if len(safe) > _CMD_SAFE_MAX_LEN:
        safe = safe[:_CMD_SAFE_MAX_LEN] + "..."
    return safe or "(empty/unparseable)"


_DEFAULT_HEADER_LINE = "BLOCKED: Bash outside allowlist."

#: above then tells them they are one, and offers them the REVIEWER's
#: executor in `_helpers._CONFINED_FINDINGS_AGENTS` -- the one set that
_ROSTER_UNREADABLE_HEADER_LINE = (
    "BLOCKED: agent roster unreadable, so every type is confined until it resolves."
)
_TYPE_UNENUMERATED_HEADER_LINE = (
    "BLOCKED: this dispatch identity is on no roster, so Bash is confined."
)

#: the CAUSE ("on no roster") without naming the IDENTITY still leaves the
#: Same discipline as the block above: this REPLACES the header line rather
_UNENUMERATED_IDENTITY_MAX_LEN = 60


def _unenumerated_header_line(effective_type: str) -> str:
    """Header for a leg-3 unenumerated confinement, naming the identity that
    was confined.

    Falls back to the bare `_TYPE_UNENUMERATED_HEADER_LINE` when
    `effective_type` is empty. That fallback is currently unreachable by
    construction -- `is_confined_by_roster_absence` returns False for an
    empty string, so leg 3 cannot fire without a non-empty type -- and is
    kept as a defensive branch rather than an assertion because a deny
    message is the wrong place to raise.
    """
    if not effective_type:
        return _TYPE_UNENUMERATED_HEADER_LINE
    shown = _sanitize_cmd_for_reason(effective_type)
    if len(shown) > _UNENUMERATED_IDENTITY_MAX_LEN:
        shown = shown[:_UNENUMERATED_IDENTITY_MAX_LEN] + "..."
    return f"BLOCKED: dispatch identity {shown!r} is on no roster, so Bash is confined."

_DEFAULT_SCAFFOLDER_STANZA = (
    "  coordinator-doc-new --type review-findings [--plan <path>] [--chunk <id>] ...",
)

_DEFAULT_ACCEPTED_FORMS_STANZA = (
    "  <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ...",
    "  python3 <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ...",
)

_DEFAULT_CLOSING_STANZA: tuple = ()


#: enumeration, moved onto INDENTED lines shared by every confined type
#: ``_message_size._exempt_span_bytes`` / ``_INDENTED_CMD_RE``) -- this is
#: The first line is a single BACKTICK-wrapped, genuinely runnable example
_TIER_A_ENUM_BLOCK = (
    "  `git show`",
    "  git show / diff / log / status / blame / ls-files / rev-parse / describe / check-ignore / check-attr / ls-tree / cat-file",
    "  ls / cat / head / tail / wc / find / file / stat / grep",
    "  Denied: find with a write/execute flag such as -delete or -exec",
    "  Denied: unquoted shell-chaining metacharacter (; && || ` $( < & or newline)",
)


def _confinement_cause(effective_type: str, policy: Any) -> str:
    """Which leg of ``_is_confined_type`` confined ``effective_type``:
    ``"policy"``, ``"findings"``, ``"roster-unreadable"``,
    ``"unenumerated"``, or ``""`` when none of them did.

    Mirrors ``_is_confined_type``'s own leg ORDER exactly, so the cause this
    reports is the one a reader re-deriving the verdict by hand would find
    first. Kept as a SEPARATE function rather than widening
    ``_is_confined_type``'s return type: that predicate is called four times
    per dispatch across two identity legs, and only one call in the whole
    guard -- a denial that is already being composed -- needs the cause.

    COST IS PAID ONLY ON THE DENY PATH. The ``roster-unreadable`` vs
    ``unenumerated`` split needs ``resolve_roster()``, which is real disk
    I/O (DoE's policy YAML, ``coordinator/agents/*.md``, the plugin
    discovery tree). It is reached only after ``check()`` has decided to
    deny -- never on an allow, and never on a command this guard has
    nothing to say about.

    NEGATIVE SPEC -- do NOT collapse this into ``_is_confined_type`` as
    ``bool(_confinement_cause(...))``, however plainly the two ladders read
    as one. Reviewed and rejected (Kira, 2026-08-31) on a premise that does
    not hold: the leg-3 arm below resolves the roster a SECOND time, after
    ``is_confined_by_roster_absence`` already resolved it to return True.
    ``_helpers._resolve_roster_accessor`` caches the IMPORT, not the
    result, and ``resolve_roster`` itself is uncached -- so the second call
    is a second full three-tree walk. ``_is_confined_type`` runs four times
    per dispatch across two identity legs; routing it through here would
    make every roster-absence-confined dispatch pay eight walks where it
    now pays four, on a surface whose per-process budget is 500ms. The
    duplication is deliberate, and the desync it risks is pinned by
    ``test_confinement_cause_ladder_matches_predicate`` rather than by a
    shared body.

    Never raises: a resolver that throws yields ``"unenumerated"``, the
    weaker of the two leg-3 claims, so a failure here can only make the
    message less specific, never wrong."""
    raw = getattr(policy, "bash_policy", None)
    if effective_type and isinstance(raw, dict) and effective_type in raw:
        return "policy"
    if is_confined_findings_agent(effective_type):
        return "findings"
    if not is_confined_by_roster_absence(effective_type):
        return ""
    try:
        roster, _reason = _helpers._resolve_roster_accessor()()
    except Exception:  # noqa: BLE001 -- see docstring: degrade, never raise
        return "unenumerated"
    return "unenumerated" if roster is not None else "roster-unreadable"


def _deny_reason(
    effective_type: str,
    cmd: str,
    deny_reason: str,
    suppress_retry_advice: bool = False,
    confinement_cause: str = "",
) -> str:
    """The REASON block: header line and the three deny-message stanzas
    (scaffolder framing, accepted-invocation-forms, closing paragraph)
    always resolve to their ``_DEFAULT_*`` value -- the per-``effective_type``
    override mechanism this function once consulted
    (``_DENY_MESSAGE_STANZA_OVERRIDES``) was deleted 2026-09-23 (this plan's
    C1) once ``coordinator:executor``, its sole entry, was confirmed dead
    (module docstring Divergence 9/12).

    (Message-size discipline, 2026-08-03) Restructured to fit the 220-byte
    guard-message prose cap (``docs/plans/2026-08-02-guard-message-size-
    discipline.md``, chunk C8) while preserving the duty-of-care contract:
    what was denied (header + Command), why (Reason, when the caller
    supplies one), and a ready-to-run alternative (the "Use instead:" cue
    window below, whose indented commands are exempt from the byte count --
    see ``_TIER_A_ENUM_BLOCK`` above). ``agent_id`` is no longer echoed --
    the dispatching EM already knows which subagent it dispatched; the prior
    "Subagent:" line spent bytes restating information already known to the
    reader, not new information a denied agent needs to self-correct.

    ``suppress_retry_advice`` (Divergence 18, 2026-08-11): forces the
    closing stanza empty regardless of ``effective_type`` -- correct for a
    genuinely out-of-scope command, actively wrong for a python-family
    misspelling whose ``python3``-corrected form would allow. The caller
    sets this ``True`` for exactly that case. (``_DEFAULT_CLOSING_STANZA``
    is empty today, so this currently only guards against a future
    non-empty default closing stanza.)
    """
    cmd_safe = _sanitize_cmd_for_reason(cmd)
    header_line = _DEFAULT_HEADER_LINE
    # `_ROSTER_UNREADABLE_HEADER_LINE`'s own comment.
    if confinement_cause == "roster-unreadable":
        header_line = _ROSTER_UNREADABLE_HEADER_LINE
    elif confinement_cause == "unenumerated":
        header_line = _unenumerated_header_line(effective_type)
    scaffolder_stanza = _DEFAULT_SCAFFOLDER_STANZA
    accepted_forms_stanza = _DEFAULT_ACCEPTED_FORMS_STANZA
    closing_stanza = () if suppress_retry_advice else _DEFAULT_CLOSING_STANZA
    lines = [
        header_line,
        "",
        f"Command: {cmd_safe}",
    ]
    if deny_reason:
        lines.append(f"Reason: {deny_reason}")
    lines.extend(["", "Use instead:"])
    lines.extend(_TIER_A_ENUM_BLOCK)
    lines.extend(scaffolder_stanza)
    lines.extend(accepted_forms_stanza)
    if closing_stanza:
        lines.append("")
        lines.extend(closing_stanza)
    return "\n".join(lines)


def check(payload: Dict[str, Any], policy_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    # absent one -- is still not this guard's business at all (MATCHERS
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    cwd = payload.get("cwd")
    git_root = resolve_git_root(cwd)
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    agent_type = payload.get("agent_type") or ""
    if not git_root:
        git_root = resolve_git_root(None)

    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(
            git_root, agent_id, expected_em_session_id=session_id
        )

    # Empty canonical AGENT_ID -> no subagent or unrecognised shape -> allow
    if not agent_id:
        return None

    policy = load_policy(policy_path)

    # This is a DIFFERENT question from Divergence 16/17's fix immediately
    # MANUFACTURE confinement for a type the policy does not confine, on the
    if _is_type_known(subagent_type, policy):
        is_confined = _is_confined_type(subagent_type, policy) or (
            _is_confined_type(agent_type, policy) and _is_type_known(agent_type, policy)
        )
    else:
        is_confined = _is_confined_type(agent_type, policy) or _is_confined_type(
            subagent_type, policy
        )
    if not is_confined:
        return None

    effective_type = _resolve_effective_type(agent_type, subagent_type, policy)

    ruleset = _resolve_ruleset(effective_type, policy)

    if dialect not in (Dialect.BASH, Dialect.POWERSHELL):
        record_silent(
            "block_reviewer_bash_outside_allowlist",
            f"no allowlist for dialect {dialect!r} -- declined to rule, not a class flip",
        )
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    cmd = _strip_crlf(cmd)

    deny = False
    deny_reason = ""
    suppress_retry_advice = False

    if not cmd:
        deny = True
        deny_reason = (
            "command could not be parsed from the PreToolUse payload -- "
            "tool_input.command was missing or an empty string. Resend the "
            "Bash call with a non-empty command; there is nothing else to "
            "correct here, since an empty payload carries no command text "
            "to diagnose or fix."
        )

    # against this normalized ``cmd_for_check``; the ORIGINAL, unrewritten
    cmd_for_check = cmd
    if not deny:
        cmd_for_check = _normalize_windows_argv0_head_path_with_spaces(cmd_for_check, ruleset)
        cmd_for_check = _normalize_windows_git_argv0(cmd_for_check, ruleset)

    pipeline_splits: list = []
    if not deny:
        found_metacharacter, pipeline_splits, fixup_index = _scan_for_unquoted_metacharacter(
            cmd_for_check
        )
        if found_metacharacter is not None:
            deny = True
            deny_reason = _metacharacter_deny_reason(
                found_metacharacter, cmd_for_check, fixup_index
            )

    if not deny and pipeline_splits:
        if dialect is Dialect.POWERSHELL:
            bad_segment = _evaluate_powershell_pipeline_segments(cmd_for_check, pipeline_splits)
        else:
            bad_segment = _evaluate_pipeline_segments(cmd_for_check, pipeline_splits, ruleset)
        if bad_segment is not None:
            deny = True
            deny_reason = _pipeline_segment_deny_reason(bad_segment)
        else:
            return None

    if not deny and not pipeline_splits:
        first_token = _extract_first_token(cmd_for_check)
        if _token_matches_binary(first_token, "git"):
            git_allowed, git_deny_reason = _evaluate_git_tier_a(cmd_for_check, ruleset)
            if git_allowed:
                return None
            if git_deny_reason:
                # Option-surface hardening (2026-07-25): a SPECIFIC
                deny = True
                deny_reason = git_deny_reason
        elif _token_matches_binary(first_token, ruleset["machine_local_binary"]):
            ml_allowed, ml_deny_reason = _evaluate_machine_local_tier_a(
                cmd_for_check, ruleset
            )
            if ml_allowed:
                return None
            if ml_deny_reason:
                deny = True
                deny_reason = ml_deny_reason
        elif _is_readonly_fs_command(cmd_for_check, ruleset):
            return None
        elif dialect is Dialect.POWERSHELL and _is_readonly_powershell_command(cmd_for_check):
            return None

    if not deny and not _first_token_is_allowlisted_binary(cmd_for_check, ruleset):
        # case that must preserve the ORIGINAL deny message text (AC3).
        tokens_for_interpreter = peel_env_assignment_prefix(_tokenize_segment(cmd_for_check))
        interpreter_result = _evaluate_python3_interpreter(tokens_for_interpreter, ruleset)
        if interpreter_result is not None:
            interpreter_allowed, interpreter_deny_reason = interpreter_result
            if interpreter_allowed:
                return None
            deny = True
            deny_reason = interpreter_deny_reason
        else:
            deny = True
            # `first_token` is the EFFECTIVE token this guard actually
            # UNMODIFIED tokens[0] already tokenized above for the alias
            # touching the effective-token MATCHING logic, which stays
            first_token = _extract_first_token(cmd_for_check)
            raw_first_token = tokens_for_interpreter[0] if tokens_for_interpreter else ""
            unsafe_env_name = _unpeeled_exec_influencing_env_name(cmd_for_check)
            if unsafe_env_name is not None:
                deny_reason = (
                    f"{unsafe_env_name}= redirects which binary or code runs, so it is not "
                    f"peeled to find the command; run without it"
                )
                suppress_retry_advice = True
            if raw_first_token and raw_first_token != first_token:
                deny_reason = (
                    f"not coordinator-doc-new ({first_token or 'empty'}, invoked via {raw_first_token})"
                )
            else:
                deny_reason = f"not coordinator-doc-new (got: {first_token or 'empty'})"
            # is a python-family MISSPELLING whose `python3`-corrected form
            if tokens_for_interpreter:
                alias_basename = _python_family_alias_token(tokens_for_interpreter[0])
                if alias_basename is not None:
                    remedy_tokens = ["python3"] + tokens_for_interpreter[1:]
                    remedy_result = _evaluate_python3_interpreter(remedy_tokens, ruleset)
                    if remedy_result is not None and remedy_result[0]:
                        remedy_cmd = _remedy_command_with_python3(
                            cmd_for_check, tokens_for_interpreter[0]
                        )
                        deny_reason = _python_family_misspelling_deny_reason(remedy_cmd)
                        suppress_retry_advice = True

    if not deny and not _has_required_type_arg(cmd_for_check, ruleset):
        deny = True
        deny_reason = "missing required argument: --type review-findings (exact type value required)"

    if not deny:
        return None

    reason = _deny_reason(
        effective_type,
        cmd,
        deny_reason,
        suppress_retry_advice,
        confinement_cause=_confinement_cause(effective_type, policy),
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
