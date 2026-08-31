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

Divergence 9 (2026-08-01, Amendment 1 -- confine coordinator:executor too):
``coordinator:executor`` joined ``_helpers._CONFINED_FINDINGS_AGENTS``
(``docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md``, a prior C1
attempt returned BLOCKED on the substrate drift this divergence fixes). The
pre-existing ``python3 <path>`` handling in ``_first_effective_token``
(designed for ``python3 <path-to-coordinator-doc-new>``) resolves
``python3 -m pytest -q`` -- this repo's CONFIGURED fast test command -- to
the effective token ``-m``, which matches no allowlisted binary and DENIES;
an executor confined under the ORIGINAL Tier A/B logic alone could not run
its own tests at all. The REJECTED fix (an unconditional ``python3``
passthrough regardless of the second token) was explicitly rejected by the
amended plan -- it would re-admit ``python3 -c "...dispatch_message..."``,
the literal incident shape this whole guard exists to close.

The fix actually landed is a new, narrow, ADDITIVE discrimination tier
(``_evaluate_python3_interpreter``), consulted ONLY as a fallthrough after
Tier B's scaffolder check has already rejected the command (so a genuine
``python3 <path-to-coordinator-doc-new>`` invocation never reaches this tier
at all -- it is resolved by the pre-existing Tier B path exactly as before,
which is what keeps AC3 intact):
  - ``python3 -c``/``python3 -e`` (inline code) -> DENY unconditionally.
    NOT policy-overridable — there is no ruleset key that can re-admit this;
    ``_PY_INLINE_CODE_FLAGS`` is a module-level constant, not a
    ``ruleset[...]`` lookup.
  - ``python3 -m <module>`` -> allowed only if ``<module>`` is a member of
    ``ruleset["interpreter_allowed_modules"]`` — data, not code. The
    AC11-fallback default for this key is empty for every type EXCEPT
    ``coordinator:executor``'s own default override (``("pytest",)`` — see
    ``_DEFAULT_RULESET_TYPE_OVERRIDES``), so a bare python3 -m invocation is
    denied-by-omission for every OTHER confined type exactly as it would
    have been before this divergence (``coordinator:code-reviewer`` never
    reaches an ALLOW via this leg).

    **KNOWN RESIDUAL (review Finding 3, 2026-08-02, deliberately NOT fixed
    here; scope widened Amendment 2, 2026-08-03):** this leg examines ONLY
    ``tokens[2]`` (the module name) — every argument after it passes through
    unexamined. For ``pytest`` specifically this is not exec-safe the way the
    "data, not code" framing above implies: pytest auto-imports and executes
    any ``conftest.py`` in the collected directory at collection time, and
    honours a ``-p <plugin>`` flag to import an arbitrary importable module.
    A confined type that holds an unconfined ``Edit`` or ``Write`` tool can
    author a ``conftest.py`` and get arbitrary code executed by a bare
    ``python3 -m pytest -q`` — the same code-execution shape the
    ``-c``/``-e`` inline-code deny above exists to close, reachable through
    the one module this divergence allowlists. This sits under the SAME
    "eager agent taking the cheapest path, not an adversary authoring an
    evasion" threat model as the ``python3 <script-path>`` residual
    documented immediately below — accepted, not solved, for the identical
    reason: a confined type that wants to run arbitrary code already has an
    unconfined Edit/Write tool to reach for, so this leg does not newly grant
    that capability, it just offers a marginally cheaper path to it. A
    reader must not conclude ``python3 -m pytest`` is exec-safe on account
    of the "data, not code" framing above — it is exec-safe only in the
    sense that the DENYLIST cannot re-admit ``-c``/``-e``, not in the sense
    that pytest itself cannot execute arbitrary code once module-gated.
    (Amendment 2, 2026-08-03) This residual is now explicitly accepted for
    BOTH confined types, not just ``coordinator:executor``: the "already has
    unconfined write" precondition holds for ``coordinator:code-reviewer``
    too (its agent definition grants an unconfined ``Edit`` tool — see
    Amendment 2 below), so excluding it from ``interpreter_allowed_modules``
    bought no additional containment, only cost verification fidelity. Do
    not read the pre-Amendment-2 prose above as still implying
    ``coordinator:code-reviewer`` is, or should be, excluded from this leg on
    write-capability grounds — it explicitly is not, as of Amendment 2.
  - ``python3 <script-path>`` (second token does not start with ``-``) ->
    allowed only if ``ruleset["interpreter_allow_scripts"]`` is true — again
    data-gated per effective_type, defaulting to ``False`` for every type
    except ``coordinator:executor``'s override. This is the documented
    "known residual" (write-then-execute) the parent plan accepts
    deliberately: an executor that is DENIED the cheap ``python3 -c``/``-m
    coordinator_core.invoke`` path can still write a script importing
    ``dispatch_message`` and run it via ``python3 <script.py>`` — accepted,
    not solved, because the threat model here is an eager agent taking the
    cheapest path, not an adversary authoring an evasion script.
  - Any other shape (``tokens[0] != "python3"``, nothing follows ``python3``,
    or an unrecognized ``-``-prefixed second token) returns ``None`` from
    ``_evaluate_python3_interpreter`` — the caller (``check()``) then falls
    through to the ORIGINAL, UNCHANGED generic Tier B deny message ("first
    command token is not coordinator-doc-new"), which is what makes AC3's
    byte-for-byte code-reviewer parity possible: a reviewer-typed
    ``python3 evil-coordinator-doc-new --type review-findings`` (a
    pre-existing negative-control test, F1/F2) still denies with the exact
    same message text as before this divergence, because
    ``interpreter_allow_scripts`` defaults to ``False`` for
    ``coordinator:code-reviewer`` and the script-path leg above returns
    ``None`` (not an explicit deny) in that case, deferring entirely to the
    untouched generic path.

``coordinator:executor``'s Tier B scaffolder requirement is ALSO widened via
the same per-type default-ruleset-override mechanism
(``_DEFAULT_RULESET_TYPE_OVERRIDES``): its ``scaffolder_required_arg`` is
``""`` rather than ``"--type review-findings"``, so
``_has_required_type_arg``'s ``cmd.endswith("")`` is trivially satisfied —
an executor's ``coordinator-doc-new`` invocation is not pinned to any one
``--type`` value the way the reviewer's is, since an executor legitimately
scaffolds several sidecar kinds (``run-report``, not only
``review-findings``).

None of Tier A's git/readonly-fs matching paths, or Tier B's scaffolder
binary-name/suffix matching, are touched by this divergence — only the
DATA fed to them (``ruleset[...]``) is now type-conditional, and the new
interpreter tier is a wholly separate, additive fallthrough branch.

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

Divergence 12 (2026-08-02, per-type deny-message remediation text):
``coordinator:executor`` joining the confined set (Divergence 9) reused this
guard's ONE hardcoded deny message verbatim, which was written for
``coordinator:code-reviewer`` and instructs the recipient to run
``coordinator-doc-new --type review-findings`` (a pin the executor's own
ruleset does not require -- ``scaffolder_required_arg`` is ``""`` for it, per
``_DEFAULT_RULESET_TYPE_OVERRIDES``), calls the recipient a "findings agent"
throughout, and closes with "dispatch a separate non-confined executor for
that step" -- self-defeating advice when the executor IS the confined type
being denied. Reported by doe-claude-em, cross-repo memo
``cross-repo/inbox/2026-08-02-doe-claude-em-executor-confinement-deny-message-addresses-wrong-agent-class.md``.
Fix: the header line and the three agent-class-specific stanzas (scaffolder
framing, "Accepted invocation forms", closing paragraph) are now resolved
per ``effective_type`` via ``_DENY_MESSAGE_STANZA_OVERRIDES``, the same
data-driven dict-lookup shape ``_DEFAULT_RULESET_TYPE_OVERRIDES`` already
uses for ruleset content -- not an inline ``if effective_type == ...`` branch
at the emit site. ``coordinator:code-reviewer`` has no entry in that dict, so
``_deny_reason`` falls through to the ORIGINAL literal header/stanza text
unchanged -- byte-identical, pinned by the message-coherence test suite
(AC3-equivalent for this fix). The middle "Did you mean.../Denied: any other
command..." stanzas describe guard mechanics identical across every confined
type and are deliberately left shared, not made per-type.

**Negative spec, binding on any future third confined type:** remediation
text is per-confined-type, resolved through ``_DENY_MESSAGE_STANZA_OVERRIDES``
exactly as ruleset content is resolved through
``_DEFAULT_RULESET_TYPE_OVERRIDES`` -- a new confined type must add its own
entry there rather than inheriting ``coordinator:code-reviewer``'s
findings-agent framing verbatim. ``coordinator:code-reviewer``'s header and
three default stanzas must never be edited to make room for a new type's
text -- the message-coherence test suite (Review: coordinator:code-reviewer,
Finding 6) asserts that specific ENUMERATIONS embedded in this message (git
subcommands, fs binaries, metacharacters, find flags) match the
corresponding module constants, a real and valuable property, but it does
NOT assert full-string equality of the header line or the three stanzas
against a literal known-good value -- an edit to the header/stanza WORDING
that left every enumerated list untouched would currently pass that suite
silently.

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


# W3 FIX 1 (2026-07-15, security parity break): the shared _helpers.resolve_effective_types() re-exports
# subagent_sandbox.engine._canonical_agent_id, whose named-teammate leg returns the RAW
# `a<name>-<16hex>` agent_id instead of the bash-canonical `<name>@session-<short>` form.
# For NAMED-TEAMMATE dispatches that keys the wrong back-pointer dir, subagent_type
# resolves empty, and this guard FAILS OPEN where legacy bash DENIES. This module imports
# the ALREADY-CORRECT canonical resolver (write_guards.block_subagent_plan_body_write._resolve_subagent_identity)
# instead, per the same workaround that guard already uses -- see its own docstring.
CLASS = "hard-deny"
#: (2026-08-07, C6 of
#: docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md)
#: "PowerShell" joins this guard's own declared coverage now that it carries
#: a PowerShell Tier A allowlist (see Divergence 13 below) -- this is the
#: guard's own MATCHERS declaration, not an edit to dispatch.py's chain loop
#: or to the shared tool-name constant (both out of this chunk's Anti-scope).
#: (2026-08-07, C2) A direct reference to the shared universe -- never a
#: copy or re-wrap -- since this guard covers the full tool-name universe.
MATCHERS = COMMAND_TOOL_NAMES
PRIORITY = 40

#: (a) Shell-chaining metacharacter set — 9 distinct substring checks,
#: byte-for-byte per the reference hook and recipe §(b) item 2
#: (bare ``&`` and bare ``<`` are included per an already-landed finding F4,
#: not just the classic ``;&|`` trio).
_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "&")

#: Deny-reason text for a banned, UNQUOTED metacharacter (2026-07-25,
#: Divergence 6 -- reworded from the pre-fix unconditional-substring-scan
#: text to reflect that quoted occurrences no longer deny).
#:
#: (Divergence 8, 2026-07-28) Reworded again to describe the two carve-outs
#: added by this divergence: a bare unquoted ``|`` is no longer an
#: unconditional deny by itself -- it is allowed to split a pipeline PROVIDED
#: every resulting segment is independently Tier-A-allowlisted (see
#: ``_evaluate_pipeline_segments``); a bare unquoted ``>``/``>>`` is allowed
#: ONLY when it is a plain redirect to ``/dev/null`` (see
#: ``_match_devnull_redirect``). Redirecting to any other path, and every
#: other metacharacter in the 9-member set, are unchanged -- still an
#: unconditional deny.
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

#: Deny-reason template for a pipeline segment that isn't independently
#: Tier-A-allowlisted (Divergence 8, 2026-07-28). Deliberately still
#: contains the phrase "shell-chaining metacharacter" -- the unquoted `|`
#: that formed this pipeline IS the metacharacter that triggered this check;
#: this reason explains the pipeline-specific rule rather than falling back
#: to the generic ``_METACHARACTER_REASON`` text, which does not name the
#: offending segment.
def _pipeline_segment_deny_reason(segment: str) -> str:
    return (
        f"pipeline segment {segment!r} is not on the read-only Tier A "
        "allowlist -- an unquoted `|` shell-chaining metacharacter pipe is "
        "only allowed when EVERY segment is independently allowlisted (a "
        "read-only git subcommand -- show/diff/log/status/blame/ls-files/"
        "rev-parse/describe -- or a read-only filesystem binary -- ls/cat/"
        "head/tail/wc/find/file/stat/grep); this segment is not"
    )

#: Deny-reason text for a command with an unbalanced/unterminated quote --
#: fails closed rather than guessing at the intended shell parse
#: (2026-07-25, Divergence 6).
_UNTERMINATED_QUOTE_REASON = (
    "command has an unbalanced/unterminated quote (unmatched ' or \") -- "
    "denied fail-closed rather than guessed at"
)

#: Deny-reason text for a command ending in a lone, unquoted, unescaped
#: trailing backslash -- an incomplete/ambiguous shell fragment (review
#: Finding 5, 2026-07-25), fails closed the same way an unterminated quote
#: does.
_TRAILING_BACKSLASH_REASON = (
    "command ends in a lone unquoted backslash with nothing following -- "
    "an incomplete/ambiguous shell fragment, denied fail-closed rather than "
    "guessed at"
)

#: Escape targets recognised inside a double-quoted argument -- POSIX shell
#: double-quote escaping only treats a backslash as an escape when it is
#: immediately followed by one of these characters; a backslash before
#: anything else inside double quotes is itself literal (2026-07-25,
#: Divergence 6).
#:
#: Negative spec (review Finding 3, 2026-07-25): the ``"\n"`` entry here is
#: unreachable dead code in practice -- ``_scan_for_unquoted_metacharacter``
#: unconditionally denies on ANY newline in ``cmd`` (the ``if "\n" in cmd:
#: return "\n"`` check at the top of that function, BEFORE the character
#: loop that consults this constant ever runs), so the character loop never
#: sees a literal newline to evaluate against this set. Kept rather than
#: removed: it is harmless, documents the real POSIX double-quote escape
#: rule for a reader reasoning about the grammar in isolation, and removing
#: it invites a future reader to re-add it as a "fix" for behavior that was
#: never actually missing. Do not assume newline-escape handling lives in
#: the double-quote branch -- it doesn't; the unconditional top-level check
#: is the only newline handling that ever executes.
_DOUBLE_QUOTE_ESCAPABLE = ('"', "\\", "$", "`", "\n")

#: The one allowlisted scaffolder command.
_ALLOWED_BINARY_SUFFIX = "coordinator-doc-new"

#: Tier A (2026-07-25): read-only git subcommands. Deny-by-omission — any
#: subcommand NOT in this set (commit, push, add, checkout, stash, reset,
#: config, apply, am, cherry-pick, merge, rebase, tag, remote, fetch, pull,
#: worktree, notes, update-ref, gc, filter-branch, submodule, bisect,
#: switch, sparse-checkout, ...) denies. This is a subcommand ALLOWLIST,
#: never a bare ``git *`` prefix match.
_GIT_READONLY_SUBCOMMANDS = frozenset(
    {"show", "diff", "log", "status", "blame", "ls-files", "rev-parse", "describe"}
)

#: Tier A (2026-07-25): read-only filesystem enumeration/inspection/search
#: binaries. ``grep`` was added same-day, after the initial Tier A landing,
#: per the DoE-claude correction memo (see module docstring Divergence 4):
#: content search was the only read-only gap left once ``find``/``cat``/
#: ``git show`` were already in place -- the confined findings-agent has no
#: native Grep/Glob tool in its harness surface, so ``grep`` is its only
#: path to search file contents rather than just enumerate/read them.
_READONLY_FS_BINARIES = ("ls", "cat", "head", "tail", "wc", "find", "file", "stat", "grep")

#: ``find`` flags that are write/execute vectors — denied even though
#: ``find`` itself is in the read-only tier.
_FIND_WRITE_FLAGS = frozenset(
    {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
)

#: Negative spec (2026-07-25, grep addition): ``grep`` has NO analogous
#: write/execute flag denylist, and this is deliberate, not an oversight.
#: Neither GNU nor BSD grep has a flag that writes, deletes, or executes
#: anything -- unlike ``find -exec``/``-delete``, there is no
#: ``grep --do-a-write-thing`` shape to deny. The one way a shell command
#: containing ``grep`` could still cause a write is via output redirection
#: (``grep foo bar > out.txt``) or a pipe into a writing command
#: (``grep foo | tee out.txt``), and BOTH are still unconditionally denied
#: by the shell-chaining-metacharacter gate (``_scan_for_unquoted_metacharacter``),
#: checked BEFORE either allowlist tier is evaluated (see ``check()``
#: ordering) -- an UNQUOTED ``>``/``|`` still denies exactly as before
#: (2026-07-25 quote-awareness, Divergence 6, only stops denying a
#: metacharacter that is DATA inside a quoted argument; a real redirect or
#: pipe operator has to be unquoted to function as one in any real shell,
#: so this argument is unaffected by that fix) -- so a flag-level denylist
#: for grep would be redundant dead code, not missing coverage. Do not add
#: one.

#: Tier A (2026-08-02): read-only ``machine-local`` subcommands. Modeled
#: directly on ``_GIT_READONLY_SUBCOMMANDS`` -- a subcommand ALLOWLIST, never
#: a bare ``machine-local *`` prefix match, so it reads as a sibling of the
#: git tier rather than a bolt-on. Deny-by-omission: any subcommand NOT in
#: this set (``set``, ``array-append``, ``array-set``,
#: ``migrate-publish-mirrors``, and any future write subcommand) denies.
#: See the module docstring's negative-spec entry for why this is a READ
#: allowlist and not a write denylist.
_MACHINE_LOCAL_BINARY = "machine-local"
_MACHINE_LOCAL_READONLY_SUBCOMMANDS = frozenset({"get", "has", "keys", "path", "dir"})

#: Tier A (2026-08-07, C6 -- PowerShell dialect): read-only cmdlets a
#: confined findings-agent legitimately needs to see its own diff/dispatched
#: files under a PowerShell-routed shell, modeled directly on
#: ``_READONLY_FS_BINARIES``'s bash-side rationale (enumeration/inspection/
#: content-search, nothing that creates, deletes, or executes). Deliberately
#: NARROW -- exactly the three cmdlets measured in the verdict record's
#: eight-probe table (``docs/research/spike-verdicts/2026-08-07-powershell-
#: guard-detection-and-tokenizer-mechanism.md``), plus their standard
#: built-in aliases, and nothing wider:
#:   - ``Get-ChildItem``/``gci`` -- directory enumeration, the ``ls``/``find``
#:     sibling. Read-only: it lists, it does not write.
#:   - ``Select-String``/``sls`` -- content search, the ``grep`` sibling.
#:   - ``Get-Content``/``gc`` -- file-content read, the ``cat`` sibling.
#: Matched case-INSENSITIVELY (unlike the bash-side binary names) because
#: PowerShell cmdlet/alias resolution is itself case-insensitive -- a
#: case-sensitive match here would silently under-admit a spelling PowerShell
#: itself treats as identical, which is a usability gap in the exact same
#: "incoherent to a reader" sense Divergence 10/11's PM ruling already named,
#: not a security concern (nothing in this set is write-capable regardless of
#: case). Deliberately EXCLUDES the common ``ls``/``dir``/``cat``/``type``
#: aliases some readers might expect: those are PowerShell aliases for the
#: SAME cmdlets already covered by ``Get-ChildItem``/``Get-Content`` above
#: (``ls``/``dir`` -> ``Get-ChildItem``; ``cat``/``type`` -> ``Get-Content``),
#: but neither was in the measured eight-probe table, and admitting an
#: unmeasured alias name is exactly the "over-broad allowlist" failure mode
#: this chunk's own dispatch brief warns against -- widen only against a
#: future measured need, not speculatively.
_READONLY_POWERSHELL_CMDLETS = frozenset(
    {"get-childitem", "gci", "select-string", "sls", "get-content", "gc"}
)

#: Tier A (2026-08-07, C6 -- PowerShell dialect) pipeline-filter cmdlets:
#: valid ONLY as a non-first segment of a pipeline whose first segment is
#: already Tier-A-allowlisted (mirrors the bash-side pipeline carve-out,
#: Divergence 8) -- never as a standalone command, since a bare
#: ``Where-Object`` has no data source of its own and admitting it as a
#: first-segment entry would buy nothing. ``Where-Object`` is the one
#: measured in the verdict record's eighth probe
#: (a ``Get-ChildItem`` piped into a ``Where-Object`` length filter, which
#: that probe recorded as a deny verdict and this entry now intends to
#: allow) -- filtering an already-read-only object stream is
#: itself read-only (it drops or keeps objects already produced upstream; it
#: creates, deletes, and executes nothing). Its built-in alias ``?`` is
#: included for the same reason ``gci``/``sls``/``gc`` are; ``where`` (the
#: other built-in alias) is included alongside it, both unmeasured but
#: structurally identical low-risk aliases of the one measured cmdlet, not a
#: new capability class.
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
    """(C6, PowerShell dialect) Sibling of ``_evaluate_pipeline_segments``:
    split ``cmd`` at each unquoted ``|`` in ``split_indices`` and verify
    every resulting segment independently satisfies
    ``_segment_is_powershell_tier_a_allowlisted`` (first segment: read-only
    data source; later segments: read-only data source OR filter cmdlet).
    Returns the first non-allowlisted segment's stripped text (deny), or
    ``None`` if every segment is allowlisted. Splitting mechanics mirror
    ``_evaluate_pipeline_segments`` exactly -- the scanner already proved
    every index in ``split_indices`` is a top-level, unquoted, single ``|``.
    """
    bounds = [-1] + split_indices + [len(cmd)]
    for idx, (start, end) in enumerate(zip(bounds, bounds[1:])):
        segment = cmd[start + 1 : end].strip()
        if not _segment_is_powershell_tier_a_allowlisted(segment, is_first_segment=(idx == 0)):
            return segment
    return None

#: git global options ALLOWED before the subcommand (2026-07-25, option-surface
#: hardening) that take a SEPARATE value argument (space-form, e.g.
#: ``-C /path``) as well as an inline ``--opt=value``/attached form. Used both
#: to validate the option itself and to skip past its value when locating the
#: subcommand token for shapes like ``git -C <path> <subcommand>`` /
#: ``git --git-dir=... <subcommand>``. Deliberately excludes ``-c`` (arbitrary
#: config injection -- ``-c core.pager=evil``, ``-c diff.x.command=evil`` are
#: write/exec vectors) which the pre-hardening version wrongly allowed.
_GIT_VALUE_TAKING_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree"})

#: git global options ALLOWED before the subcommand that take NO value
#: argument.
#:
#: Divergence N (2026-08-11, cross-guard conflict audit): added
#: ``--no-optional-locks``. This module's confinement check runs in the
#: ``CONFINEMENT_DENY`` band, strictly BEFORE ``git-no-optional-locks``
#: (``guard_no_optional_locks.py``, ``ADVISORY_REWRITE`` band) ever executes
#: -- ``dispatch.py``'s band ordering and single-pass "first non-None wins"
#: loop mean a confined agent's OWN pre-rewrite command is what this module
#: sees, so the auto-rewrite's inserted flag was never actually reachable by
#: this allowlist and the two guards were never in conflict via that path.
#: The real gap: a confined agent typing ``--no-optional-locks`` itself --
#: which `docs/wiki/machine-load-norm.md` and the fleet-wide index-lock
#: campaign explicitly brief every agent to do on read-only git invocations
#: -- hit this allowlist directly and was denied for a flag its own briefing
#: told it to use. The flag is strictly read-only-safe: per
#: ``guard_no_optional_locks.py``'s own module docstring, it only suppresses
#: write-back of refreshed index stat data (never the refresh itself, and
#: never any content the command would not otherwise read), granting a
#: confined agent no capability beyond what bare ``git`` already has.
_GIT_NO_VALUE_OPTIONS = frozenset({"--no-pager", "--literal-pathspecs", "--no-optional-locks"})

#: git subcommand-level options that are write/exec vectors even though the
#: subcommand itself is on the read-only allowlist. ``--output``/``-o``
#: write to an arbitrary caller-chosen file -- confirmed empirically against
#: real git (``git show --output=<path>`` and ``git log --output=<path>``
#: both created the target file). ``--ext-diff`` enables an external diff
#: driver, i.e. arbitrary command execution via ``diff.<driver>.command``
#: config, achievable without going through the denied ``-c`` global option.
#: Both are matched via the shared ``_helpers.prefix_denies`` (2026-07-25 gap
#: fix, extracted to ``_helpers`` same day per the P0 fix so
#: ``block_subagent_destructive_action`` reuses the identical implementation
#: -- see ``_helpers`` module docstring item 3) -- a
#: hyphen-boundary prefix match that denies the bare flag, the ``=``-form,
#: AND the attached-no-``=`` form (``--output/tmp/x``) uniformly, while
#: exempting ``--output-indicator-{new,old,context}=X`` (real,
#: non-write git formatting flags whose next char after the ``--output``
#: substring is ``-``). No analogous ``--ext-diff*``-prefixed legitimate git
#: flag exists, so ``--ext-diff`` is widened to the same prefix rule rather
#: than kept as an exact-match set.

#: The one allowlisted --type value, checked as a word-boundary match
#: (recipe §(b) item 2(c) — NOT a substring
#: match; ``--type review-findingsXYZ`` must NOT match).
_REQUIRED_TYPE_ARG_END = "--type review-findings"
_REQUIRED_TYPE_ARG_MID = "--type review-findings "

#: Max length of the sanitized command echoed back in the deny reason
#: (reference hook line 291).
_CMD_SAFE_MAX_LEN = 200

#: Hardcoded short-form write flag treated specially by
#: ``_find_git_subcommand_write_flag`` (matches ``-o``/``-o<path>`` attached
#: form, which ``prefix_denies`` alone does not model since it is a
#: single-dash short option, not a hyphen-boundary long-option prefix).
_GIT_SHORT_FORM_OUTPUT_FLAG = "-o"


#: (Amendment 1, 2026-08-01) The confined type whose default ruleset
#: diverges most widely from the shared base -- see
#: ``_DEFAULT_RULESET_TYPE_OVERRIDES``.
_EXECUTOR_TYPE = "coordinator:executor"

#: (Amendment 2, 2026-08-03) The other confined type -- see the module
#: docstring's Amendment 2 entry for why it now shares the pytest allowance
#: with ``_EXECUTOR_TYPE`` above.
_REVIEWER_TYPE = "coordinator:code-reviewer"

#: (Amendment 1, 2026-08-01; widened Amendment 2, 2026-08-03) Per-
#: ``effective_type`` overrides layered onto the shared base
#: ``_default_ruleset()`` returns. This is the "policy row" for a confined
#: type that has no external ``bash_policy:`` YAML entry -- expressing the
#: divergence as DATA here (rather than an ``if effective_type == ...``
#: branch inside a matching function) is what keeps the git/readonly-fs/
#: scaffolder matching paths themselves untouched (see module docstring
#: Divergence 9).
#:
#: (Amendment 2, 2026-08-03) ``coordinator:code-reviewer`` now ALSO gets an
#: entry -- ``interpreter_allowed_modules: ("pytest",)`` only, mirroring
#: exactly the executor's module allowance and nothing else (no
#: ``interpreter_allow_scripts``, no ``scaffolder_required_arg`` relaxation
#: -- those remain executor-only divergences). See the module docstring's
#: Amendment 2 entry for the PM-ruling discriminator (destructive-vs-
#: non-destructive, not read-only-vs-executing) that motivates this, and
#: why it does not weaken containment: ``coordinator:code-reviewer`` already
#: holds an unconfined ``Edit`` tool (confirmed against its own agent
#: definition, ``coordinator/agents/code-reviewer.md``,
#: ``tools: ["Bash", "Read", "Edit", "ToolSearch"]``), so it can already
#: author or modify a ``conftest.py`` regardless of whether ``pytest`` is on
#: this allowlist -- denying it here bought no containment, only cost
#: verification fidelity (see the KNOWN RESIDUAL note on
#: ``_evaluate_python3_interpreter`` below, updated the same day).
#: Reviewer-type-unaffected structural coverage
#: (``test_reviewer_type_unaffected_by_executor_interpreter_allowance`` and
#: its ``_default_ruleset``-fallback siblings) still holds for every OTHER
#: divergence: ``dict.get(type, {})`` merges only the keys present in a
#: type's own entry, so the reviewer picks up ``interpreter_allowed_modules``
#: alone and stays on the base (``()``-empty/``False``) value for every other
#: interpreter/scaffolder key -- AC3's narrower claim (byte-identical DENY
#: MESSAGE TEXT for a non-pytest command) is untouched, since no message
#: stanza is edited by this amendment.
_DEFAULT_RULESET_TYPE_OVERRIDES: Dict[str, Dict[str, Any]] = {
    _EXECUTOR_TYPE: {
        "interpreter_allowed_modules": ("pytest",),
        "interpreter_allow_scripts": True,
        "scaffolder_required_arg": "",
    },
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

    (Amendment 1, 2026-08-01) ``effective_type`` selects an optional overlay
    from ``_DEFAULT_RULESET_TYPE_OVERRIDES`` -- ``coordinator:code-reviewer``
    (and any other type with no entry there) gets the base dict completely
    unchanged, byte-for-byte identical to the pre-Amendment-1 return value
    (AC3). ``coordinator:executor`` layers in its interpreter-tier allowances
    and its relaxed Tier B scaffolder requirement (see module docstring
    Divergence 9 for why each differs).
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
    # the interpreter keys immediately above -- a policy entry authored
    # before this change has neither, and must keep validating exactly as it
    # did before. Absent, each defaults to the conservative hardcoded
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

    Review: code-reviewer -- ``policy`` is VESTIGIAL as of Divergence 14
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
    # Divergence 14 (2026-08-10, confinement-editable-by-its-own-subject
    # close, this change): a validated `bash_policy:` YAML entry for
    # `effective_type` USED to be returned here (optionally topped up by
    # `_DEFAULT_RULESET_TYPE_OVERRIDES`), which is what made the enforced
    # ALLOW/DENY surface for a confined type editable by anyone who could
    # write that YAML file. `coordinator:code-reviewer` and
    # `coordinator:executor` both hold an unconfined `Edit` tool (see
    # Amendment 2 above) that reaches
    # `coordinator/subagent-sandbox-policy.yaml` -- so the confined subject
    # of this guard could rewrite its own `bash_policy:` row, and because
    # `load_policy()` re-reads the file fresh on every `check()` call (by
    # design, for `report_sidecar`'s legitimate hot-reload need -- see that
    # function's own docstring), the very next Bash call honoured the
    # rewrite. Confirmed live in a throwaway fixture (never the fleet
    # policy): denied `python3 /tmp/evil.py` before, allowed after flipping
    # `interpreter_allow_scripts` to `true` in the YAML mid-session, no
    # process restart. Reported by coordinatorstaff-eng
    # (`state/bug-backlog/2026-08-10-a-reviewer-s-confinement-policy-is-
    # edita-459e2790ebb7.yaml`).
    #
    # A per-process cache keyed on `policy_path` (the obvious "resolve once
    # at a trust boundary" fix) was considered and REJECTED: this guard's
    # own host module documents itself as stateless/spawn-per-call (DR-215,
    # see `subagent_sandbox.engine`'s module docstring) -- each PreToolUse
    # invocation is a fresh interpreter process, so a process-lifetime cache
    # would be re-populated, and thus re-defeated, on literally the next
    # Bash call. It buys nothing here; it only would in a resident-daemon
    # architecture this codebase deliberately does not have.
    #
    # Moving the YAML to a location a confined agent's tools cannot write
    # was also considered and rejected as OUT OF SCOPE for this module: the
    # policy file lives in DoE-claude's tree
    # (`coordinator/subagent-sandbox-policy.yaml`), not this repo, and this
    # guard does not own that repo's write-surface or its Edit-tool sandbox
    # definition.
    #
    # Fix landed: the enforced RULESET is now hard-pinned in code --
    # `_default_ruleset()`/`_DEFAULT_RULESET_TYPE_OVERRIDES` ONLY, never a
    # YAML-supplied `bash_policy:` entry for `effective_type`, for ANY
    # confined type. `_validate_ruleset` is kept (not deleted) as dead code
    # documenting the shape a future policy-driven ruleset would need to
    # satisfy IF a non-editable delivery path for it is built later -- see
    # its own docstring negative-spec note. `entry`/`raw` above are computed
    # for `_is_confined_type` only now (see that function): a `bash_policy:`
    # key can still WIDEN which types are confined (safe -- it only adds
    # confinement, never removes or loosens the ruleset a confined type is
    # held to), but it can no longer narrow or widen what a confined type is
    # ALLOWED to run. This is deliberately narrower than AC10's original
    # promise ("a second confined type lands as a pure-data YAML addition,
    # no code change") -- a genuinely new confined type now needs its own
    # `_DEFAULT_RULESET_TYPE_OVERRIDES` code entry to get anything other
    # than the shared conservative default. That regression is the
    # necessary cost of closing this hole under the option-3 shape (hard-pin
    # confined-type entries in code) -- see the module docstring's
    # Divergence 14 section for the full writeup.
    #
    # THREAT CLOSED: a confined agent's own Edit-tool rewrite of
    # `bash_policy:`'s per-type RULESET content no longer has any effect on
    # what that guard enforces for it.
    # THREAT LEFT OPEN: `_is_confined_type` below still consults the YAML to
    # decide SET MEMBERSHIP -- this is intentionally unclosed because it is
    # a widen-only lever (adding a type to `bash_policy:` can only ADD
    # confinement, it cannot remove a type from the hardcoded
    # `_CONFINED_FINDINGS_AGENTS` set or loosen its ruleset), so a confined
    # subject rewriting it cannot use it to escape its own confinement.
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
    """Strip CRLF artifacts — Windows/Git-Bash jq emits CR+LF in text mode
    (reference hook line 217)."""
    return cmd.replace("\r", "")


#: Target of the one redirect this guard allows (Divergence 8, 2026-07-28).
_DEVNULL_TARGET = "/dev/null"

#: Complementary standard-stream file descriptors for the fd-duplication
#: carve-out (Divergence 10, 2026-08-01) -- ``"2"`` maps to ``"1"`` (stderr
#: duplicated onto stdout, the exact idiom this carve-out exists for) and
#: ``"1"`` maps to ``"2"`` (the stdout-onto-stderr mirror image, admitted for
#: free by the same symmetric check -- see ``_match_fd_dup_redirect``).
_FD_DUP_COMPLEMENT = {"1": "2", "2": "1"}


def _match_devnull_redirect(cmd: str, i: int) -> Optional[int]:
    """(Divergence 8, 2026-07-28) Given ``cmd[i] == ">"``, return the number
    of characters (starting at ``i``) consumed by a plain redirect to
    ``/dev/null``, or ``None`` if what follows is not exactly that.

    Matches ``>``/``>>`` (append), optional single space before the target
    (``> /dev/null`` and ``>/dev/null`` both match), then the literal
    ``/dev/null``, then a boundary: end-of-string, a space, or a top-level
    ``|`` (so ``grep foo bar 2>/dev/null | wc -l`` still allows the redirect
    AND still finds the pipe split immediately after). Anything else after
    the target -- e.g. ``/dev/nullx``, or a redirect to any OTHER path such
    as ``/tmp/d`` -- is NOT a match, so ``git diff > /tmp/d`` (a real write
    vector) is unaffected by this carve-out and still denies via the
    unmatched-``>`` fallthrough in the caller.

    A leading file-descriptor digit (``2>``, ``1>``) needs no special
    handling here -- the digit is an ordinary character already consumed by
    the caller's main scan loop before it ever reaches this function; this
    function only ever sees the ``>`` itself and what follows it.
    """
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
        # Unquoted.
        if ch == "\\":
            if i + 1 >= n:
                # Trailing unquoted backslash with nothing following
                # (review Finding 5, 2026-07-25): not valid, self-contained
                # shell input -- it awaits a continuation. Fail closed,
                # consistent with the unterminated-quote handling and this
                # function's own stated philosophy of denying a parse
                # anomaly rather than guessing at the intended command.
                # ``i`` is already the offset of this trailing backslash --
                # it is by definition the last character (2026-07-29,
                # duty-of-care promotion: this is what lets the caller build
                # a copy-pasteable "drop the dangling backslash" fix).
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
            # (Divergence 8, 2026-07-28) Skip a plain redirect to
            # /dev/null; anything else is still a deny exactly as before.
            consumed = _match_devnull_redirect(cmd, i)
            if consumed is not None:
                i += consumed
                continue
            # (Divergence 10, 2026-08-01) Skip the exact stderr-to-stdout
            # fd-duplication idiom (``2>&1``) or its stdout-to-stderr mirror
            # (``1>&2``) -- both write-incapable and exec-incapable, sibling
            # carve-out to the /dev/null redirect immediately above.
            consumed = _match_fd_dup_redirect(cmd, i)
            if consumed is not None:
                i += consumed
                continue
            return ">", [], -1
        if ch == "|":
            # (Divergence 8, 2026-07-28) A bare single pipe is no longer an
            # unconditional deny -- record the split point and keep
            # scanning; the caller verifies every resulting segment is
            # independently allowlisted.
            splits.append(i)
            i += 1
            continue
        if ch in (";", "`", "<", "&"):
            return ch, [], -1
        i += 1
    if quote is not None:
        # quote_open_index is the offset of the quote character that never
        # found its match (2026-07-29, duty-of-care promotion) -- the
        # caller uses it to name exactly which quote is unbalanced and to
        # build a minimal syntactically-valid completion (close it at the
        # end of the command).
        return "<unterminated-quote>", [], quote_open_index
    return None, splits, -1


def _quote_context_window(cmd: str, index: int, radius: int = 20) -> str:
    """Short excerpt of ``cmd`` centered on ``index``, for pointing a caller
    at WHICH quote/backslash is the offending one in a long command rather
    than making them re-scan the whole (possibly 200-char-truncated)
    ``Command:`` line by eye."""
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
        # Truncate-THEN-append (2026-07-30, H-medium M13/M19 review fix):
        # `_sanitize_cmd_for_reason` truncates at `_CMD_SAFE_MAX_LEN` chars.
        # Appending `quote_char` before sanitizing meant that for any
        # `cmd_for_check` AT OR OVER the cap, the 200-char slice discarded
        # the very character this branch exists to add -- the message then
        # asserted "close it at the end: <corrected>" while `<corrected>`
        # was just the truncated, still-unterminated original with an
        # ellipsis, silently presenting the unfixed command as fixed.
        # Sanitizing (and truncating) FIRST, then appending the closing
        # quote to the already-bounded result, guarantees the offered fix
        # actually ends with the quote character regardless of length.
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
    """Quote-aware tokenization for a single command/pipeline-segment
    string, via the canonical shlex-based ``_command_tokenizer.
    tokenize_full_command`` -- the SAME machinery ``block_subagent_commit.py``
    uses, not a private copy. Returns ``[]`` on an unparseable ``cmd``
    (unterminated quote / trailing backslash), which every caller below
    treats as "no token matched anything" -- fail-closed, matching this
    guard's own stated deny-by-default posture, and consistent with the
    metacharacter gate (``_scan_for_unquoted_metacharacter``) already
    denying an unbalanced quote before Tier A/B evaluation ever runs; this
    is a defensive second check, not the primary one.
    """
    tokens = _tokenize_full_command(cmd)
    return tokens if tokens is not None else []


def _first_effective_token(tokens: list) -> str:
    """Return the token identifying the invoked binary at the head of
    ``tokens`` -- the first token, or the SECOND token when the first is
    exactly ``python3`` (the ``python3 <path>`` invocation form). Returns
    ``""`` for an empty list.
    """
    if not tokens:
        return ""
    if tokens[0] == "python3" and len(tokens) >= 2:
        return tokens[1]
    return tokens[0]


def _extract_first_token(cmd: str) -> str:
    """Shared first-token extraction: the ONE place ``first_token`` is
    derived from a command/segment string -- both
    ``_first_token_is_allowlisted_binary``, ``_is_readonly_fs_command``,
    ``_segment_is_tier_a_allowlisted``, and ``check()``'s own Tier A entry
    call this instead of hand-duplicating tokenization.

    2026-07-29 (THIS change, P0 security fix): rebuilt on the shlex-based
    ``_tokenize_segment``/``_first_effective_token`` -- the previous
    implementation (own-module ``_strip_prefix_and_split`` + a
    single-quote-pair-only unquote) split ``cmd`` on the FIRST RAW SPACE
    regardless of quoting. That bug's most visible symptom was a spaced
    dispatch-time path (e.g. a Windows profile with a space in the
    username) mis-splitting into a path fragment that matched no allowlisted
    binary -- a false DENY (fails safe). But the SAME naive splitter also
    fed ``_git_command_tokens``' whitespace-only ``rest.split()``, which is
    the actual P0: a QUOTED write-flag argument
    (``git show "--output=/tmp/evil" HEAD``, ``git show
    '--output=/tmp/evil' HEAD``) was tokenized with its surrounding quote
    characters still attached (``'"--output=/tmp/evil"'``), so
    ``_helpers.prefix_denies``'s ``startswith("--output")`` check never
    matched -- confirmed empirically through the real ``check()``
    entrypoint: both forms returned ALLOW where the identical unquoted
    command correctly DENIED, even though a real shell strips the quotes
    and git genuinely writes to the arbitrary target path either way. This
    is a total defeat of the exact P0 this guard's Tier A option-hardening
    (Divergence 5 above) was written to close, reopened by a different,
    quote-blind tokenizer feeding the same option scan. Rebuilding
    tokenization on shlex closes it: a quoted ``--output=...`` now
    de-quotes to the bare flag before ``prefix_denies`` ever sees it, same
    as a real shell's argv.
    """
    return _first_effective_token(_tokenize_segment(cmd))


#: Windows-first-class argv0-head normalization (2026-07-29, THIS change,
#: ported from ``block_subagent_commit.py``'s ``_normalize_windows_git_argv0``
#: / ``_normalize_windows_argv0_head_path_with_spaces`` / (their shared)
#: ``_WINDOWS_ARGV0_HEAD_PATH_RE`` -- see that module's docstring for the
#: full rationale). Needed as a companion to the shlex rebuild above: POSIX
#: ``shlex`` treats a bare backslash as an escape character and silently
#: drops it, which would otherwise mangle an ordinary Windows path
#: (``C:\Users\John Doe\...\coordinator-doc-new``, a real shape on this
#: project's primary platform -- a spaced Windows username is the DEFAULT
#: profile shape, not an exotic one) into unrecognisable garbage before
#: ``_tokenize_segment`` ever sees it -- reopening, one layer up, the exact
#: false-DENY failure mode the module docstring above names as this
#: guard's OWN prior symptom of the retired naive tokenizer.
#:
#: Unlike the ``block_subagent_commit.py``/``coordinator-safe-commit``
#: pair (a fixed two-name set), this guard's Tier A/B identity set is
#: policy-driven (Divergence 7) -- so the identity names these passes
#: recognize are resolved from the ALREADY-RESOLVED ``ruleset`` for this
#: call (``git`` plus the ruleset's scaffolder binary and read-only fs
#: binaries), not hardcoded, so a well-formed custom ``bash_policy`` entry's
#: own binary names are covered too.
_ARGV0_HEAD_BOUNDARY_PRE = r"(?:\A|[;&|\n])\s*(?:['\"`(])?"
_RAW_HEAD_TOKEN_RE = re.compile(r"(" + _ARGV0_HEAD_BOUNDARY_PRE + r")([^\s;&|]+)")


def _windows_argv0_identity_names(ruleset: Dict[str, Any]) -> frozenset:
    """The executable basenames the Windows argv0-head normalization passes
    below treat as ARGV0-position identities worth un-mangling: ``git``
    plus this call's resolved Tier B scaffolder binary (bare and
    ``.py``-suffixed), Tier A read-only fs binaries, and the Tier A
    ``machine-local`` binary (see the module-level comment above these
    functions for why this is resolved per-``ruleset`` rather than a
    hardcoded pair like the sibling guards use).

    # Review: coordinator:code-reviewer, Finding 2 -- ``machine_local_binary``
    # was omitted here, so a Windows absolute-path invocation of
    # machine-local with an embedded-space username fell through the P0
    # normalization pass and denied where it should allow.

    # Review: coordinator:code-reviewer, Finding 2 follow-up (2026-08-17,
    # false-deny) -- ``scaffolder_binary``'s real on-disk name carries a
    # ``.py`` suffix (see ``_matches_scaffolder_dispatch``'s own Divergence
    # 19 docstring above for why: ``coordinator-doc-new`` is a naked Python
    # script, while its Windows-launcher twin keeps the bare
    # ``coordinator-doc-new.cmd`` name, no ``.py`` in it). The
    # embedded-space Windows-path rewrite passes below only recognized the
    # bare name plus ``.exe``/``.cmd``, so a ``Program Files``-style
    # absolute path ending in ``coordinator-doc-new.py`` still shlex-split
    # on its embedded space and false-denied. Adding the ``.py``-suffixed
    # literal HERE, not to ``normalize_executable_basename`` -- same
    # reasoning as Divergence 19: stripping ``.py`` in the shared basename
    # normalizer would let a confined-agent-authored ``git.py`` resolve to
    # real ``git`` for every other caller (Tier A's git-subcommand walk
    # trusts the matched identity, not the file's contents). This is an
    # additive literal scoped to the scaffolder name only.
    """
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
    """Rewrite a Windows absolute (drive-letter- or root-rooted) argv0-head
    path -- possibly containing embedded-space components
    (``C:\\Users\\John Doe\\...\\coordinator-doc-new``) -- whose basename
    identifies one of ``_windows_argv0_identity_names(ruleset)``, into its
    forward-slash equivalent, additionally single-quoting it when unquoted
    AND whitespace-containing, so the whole path lands as ONE shlex token
    at argv0 position instead of splitting on the embedded space. Runs
    BEFORE ``_normalize_windows_git_argv0`` -- once a matched path is
    quoted here, it no longer contains a bare backslash outside the
    quotes, so that pass's simpler ``if "\\\\" in token`` gate finds
    nothing left to do for it.
    """
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


# ``_token_matches_binary`` (2026-07-29, guard-brick incident response,
# part 2): no longer an own-module copy. It is now
# ``_command_tokenizer.token_matches_binary``, imported above -- the F1 fix
# (boundary-anchored: token equals ``binary`` exactly, or the character
# immediately preceding a trailing ``binary`` suffix is a path separator,
# closing the free-text-suffix bypass where e.g. ``evil-git`` or ``notls``
# would mechanically match a bare ``endswith()`` check) is preserved
# unchanged; what's NEW is that the canonical matcher also strips a
# trailing ``.exe`` OR ``.cmd`` (case-insensitively) before comparing,
# which this module's own prior copy never did -- so ``git.exe``/``GIT.EXE``
# at argv0 position previously did NOT match ``git`` here, silently
# admitting a Tier-A git invocation through the SAME first-token check
# this module uses for BOTH the read-only-git allowlist gate
# (``_segment_is_tier_a_allowlisted``, ``_evaluate_git_tier_a``) and the
# read-only-filesystem-binary gate (``_is_readonly_fs_command``) as well as
# the Tier B ``coordinator-doc-new`` scaffolder gate
# (``_first_token_is_allowlisted_binary``) -- confirmed empirically
# (2026-07-29): this module's own prior copy returned ``False`` for
# ``git.exe`` against binary ``git`` while correctly returning ``True`` for
# bare ``git``, ``/usr/bin/git``, and ``bin/git``. The ``.cmd`` half is a
# DIFFERENT-direction fix for THIS module specifically: ``coordinator-doc-
# new.cmd`` is this project's own generated Windows launcher twin
# (confirmed on disk, ``coordinator/bin/coordinator-doc-new.cmd`` -- no
# ``.py`` in the launcher's own name, unlike the scaffolder script itself)
# for the Tier B scaffolder, and before this fix it was wrongly DENIED by
# ``_first_token_is_allowlisted_binary`` -- a Windows-usability defect
# (blocking a legitimately-allowed tool's ordinary invocation), not a
# security bypass, since this gate's default is deny. See
# ``_command_tokenizer.token_matches_binary``'s own docstring for the full
# bypass/gap set this consolidation closes, including the negative-control
# preservation (``evil-coordinator-safe-commit``/``evil-coordinator-doc-
# new.cmd`` still do not match).


def _git_command_tokens(cmd: str) -> list:
    """(Tier A option-surface hardening, 2026-07-25) Tokenize everything
    AFTER the ``git`` binary token itself (and after a leading ``python3``
    prefix token, if present). Shared by both the global-option walk
    (``_locate_git_subcommand_and_bad_global``) and the subcommand-option
    write-flag scan (``_find_git_subcommand_write_flag``) so neither
    hand-duplicates tokenization.

    2026-07-29 (THIS change, P0 security fix): rebuilt on the shlex-based
    ``_tokenize_segment`` -- the previous implementation split ``rest``
    with a quote-BLIND ``rest.split()``, so a QUOTED write-flag argument
    (``git show "--output=/tmp/evil" HEAD``) tokenized with its quote
    characters still attached, which ``_find_git_subcommand_write_flag``'s
    ``prefix_denies`` check never matched -- confirmed empirically as a
    live ALLOW-where-should-DENY bypass through ``check()``. See
    ``_extract_first_token``'s docstring for the full incident writeup;
    both functions share the same root cause and the same fix.
    """
    tokens = _tokenize_segment(cmd)
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
    """(Divergence 8, 2026-07-28) True iff ``segment`` -- one command of a
    pipeline, already stripped of leading/trailing whitespace -- is on its
    own an allowed Tier A command (a clean read-only ``git`` invocation, or
    a read-only filesystem binary, or a read-only ``machine-local``
    invocation -- Divergence 11 -- Review: coordinator:code-reviewer, Finding
    1, this leg was landed for the bare-command Tier A path but never wired
    into pipeline-segment eligibility, an over-tightness gap the module
    docstring's own allow-condition (7)/(5) cross-reference already claimed
    was covered). Deliberately does NOT check Tier B
    (the ``coordinator-doc-new`` scaffolder) -- the scaffolder command is
    never a sensible pipeline member, and admitting it here would widen the
    one-shot-scaffold contract Tier B exists to enforce.
    """
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
    """(Divergence 8, 2026-07-28) Given ``cmd`` and the top-level unquoted
    pipe positions ``_scan_for_unquoted_metacharacter`` found, split ``cmd``
    into pipeline segments and verify EVERY segment is independently
    Tier-A-allowlisted (``_segment_is_tier_a_allowlisted``). Returns the
    first non-allowlisted segment's stripped text (deny), or ``None`` if
    every segment passes (allow the whole pipeline). Splitting only ever
    happens at the exact indices the scanner already proved are top-level,
    unquoted, single ``|`` characters -- this function does no quote
    handling of its own, it only slices at pre-validated boundaries.
    """
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
    """(2026-08-02) Given a command whose first token is already known to
    match ``ruleset["machine_local_binary"]`` (boundary-anchored, checked by
    the caller via ``_token_matches_binary``), validate the subcommand
    against ``ruleset["machine_local_readonly_subcommands"]`` --
    deny-by-omission, modeled directly on ``_evaluate_git_tier_a``'s
    subcommand-allowlist step (the two are siblings in the evaluation
    order, see ``check()``).

    ``machine-local``'s write subcommands (``set``, ``array-append``,
    ``array-set``, ``migrate-publish-mirrors``) are NOT enumerated here --
    like the git subcommand allowlist, this is a positive allowlist of read
    subcommands, never a denylist of write ones, so an as-yet-unnamed
    future write subcommand denies by omission too.

    Unlike git, ``machine-local`` has no analogous write/exec-capable
    GLOBAL or subcommand OPTION surface to scrutinize (no ``-c`` config
    injection, no ``--exec-path``, no ``--output``-shaped flag) -- see the
    module docstring's negative-spec entry -- so this is a single-level
    subcommand check, not the two-level walk ``_evaluate_git_tier_a`` needs.

    Returns ``(True, None)`` for a clean read-only invocation, or
    ``(False, None)`` for anything else (a bare ``machine-local`` with no
    subcommand, or a subcommand not on the read-only allowlist) --
    deny-by-omission, no option-level reason to report, so the caller falls
    through to the existing generic Tier B deny message naming
    ``machine-local`` as an unrecognised first token (exactly the same
    fallthrough shape ``_evaluate_git_tier_a`` uses when the git subcommand
    itself isn't on the read-only allowlist).
    """
    tokens = _tokenize_segment(cmd)
    start = 2 if tokens and tokens[0] == "python3" else 1
    if len(tokens) <= start:
        return False, None
    subcommand = tokens[start]
    if subcommand in ruleset["machine_local_readonly_subcommands"]:
        return True, None
    return False, None


def _has_find_write_flag(cmd: str, ruleset: Dict[str, Any]) -> bool:
    """(Tier A, 2026-07-25; policy-driven since Divergence 7, 2026-07-27)
    True iff ``cmd`` contains a ``find`` flag in
    ``ruleset["find_denied_options"]`` -- a write/execute vector
    (``-delete``, ``-exec``, ``-execdir``, ``-ok``, ``-okdir``, ``-fprint``,
    ``-fprintf``, ``-fls`` in the AC11 hardcoded-fallback shape) that denies
    even though ``find`` itself is in the read-only filesystem tier.

    2026-07-29 (THIS change): rebuilt on the shlex-based
    ``_tokenize_segment`` -- the previous ``cmd.split()`` was quote-blind,
    so a quoted ``"-exec"`` tokenized with its quote characters still
    attached and never matched the bare-string membership test, same root
    cause and same fix as ``_extract_first_token``/``_git_command_tokens``.
    """
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


#: (Amendment 1, 2026-08-01) python3 inline-code flags -- unconditionally
#: denied, per AC2a. Deliberately a bare module constant, NOT a
#: ``ruleset[...]`` lookup: no policy content can widen this set (see module
#: docstring Divergence 9 -- an unconditional ``-c``/``-e`` passthrough is
#: the exact incident bypass shape this whole guard exists to close).
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
        # the caller chose deliberately -- on Windows there is often no
        # `python3.exe` sibling to retype to (see the plan's "Amended
        # 2026-08-11 (C1 execution)" section), so the "retype as python3"
        # remedy below would be a trap-offer for this spelling. Admit it into
        # this SAME decision the exact `python3` spelling already enters --
        # every check below (inline-code deny, -m module allowlist) still
        # applies unconditionally. A BARE python-family token (no path
        # separator) is a NAME the caller got wrong, not a location -- it
        # stays out of this tier and keeps the existing "retype as python3"
        # remedy path (`_python_family_alias_token` /
        # `_python_family_misspelling_deny_reason`) byte-identical.
        # Decided off the RAW token, not the normalized basename, since
        # normalization is exactly what discards the path-vs-bare distinction.
        # (P3 fix, 2026-08-11) A bare trailing separator with no real
        # directory component (e.g. `python/`) is NOT a chosen location --
        # strip trailing separators first so this predicate means what it
        # says ("the caller named a directory before the basename"), not
        # merely "a separator character appears anywhere in the token".
        # `_normalize_executable_basename` already collapses `python/` and
        # `python` to the identical basename, so this tightening changes no
        # downstream ALLOW/DENY outcome -- it only stops a degenerate
        # spelling from being misclassified as path-prefixed.
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


#: Divergence 18 (2026-08-11, THIS change -- python-family misspelling
#: remedy): a confined agent typing bare `python -m pytest ...` (no `3`)
#: fell through to the generic Tier B deny message ("first command token is
#: not coordinator-doc-new"), which both misnames the tier (this command IS
#: in scope, merely misspelled) and closes with "report the blocker to the
#: dispatching EM rather than retrying it" -- wrong advice when retrying
#: with the one-character fix would allow. Measured live: three dispatched
#: executors gave up and handed pytest back unrun rather than retry with
#: `python3`. This tier fires ONLY as a fallthrough after Tier B's
#: scaffolder check AND the exact-`python3` interpreter tier have both
#: already declined to classify the command -- see `check()`'s call site.
#: It never widens what executes: the command is still denied; only the
#: reason text changes, and only when the substituted `python3 <same rest>`
#: invocation would ITSELF be allowed by `_evaluate_python3_interpreter` for
#: this `ruleset` -- a spelling whose corrected form would ALSO deny (e.g.
#: `python -c "..."`) falls through to the untouched generic message, never
#: advertising a retry that denies too (the exact trap-offer failure mode
#: Divergence 1 above already warns this module against).
_PYTHON_FAMILY_ALIAS_RE = re.compile(r"^(python2(\.\d+)?|python3\.\d+|python|py)$")


def _python_family_alias_token(token: str) -> Optional[str]:
    """Return the case/suffix-normalized basename of ``token`` when it is a
    python-family interpreter spelling OTHER than the exact accepted
    ``python3`` -- e.g. ``python``, ``py``, ``python2``, ``python3.11``, or
    any of those with a path prefix and/or a ``.exe``/``.cmd`` suffix
    (Windows), via the same ``_normalize_executable_basename`` every other
    identity check in this module routes through. Returns ``None`` for
    ``python3`` itself (already handled by the exact-tier above) and for
    anything that isn't a recognized python spelling at all.
    """
    basename = _normalize_executable_basename(token)
    if basename == "python3":
        return None
    if _PYTHON_FAMILY_ALIAS_RE.match(basename):
        return basename
    return None


def _remedy_command_with_python3(cmd: str, leading_token: str) -> str:
    """Rewrite ``cmd``'s leading python-family token to ``python3``,
    preserving the rest of the command verbatim -- used only to render a
    human-readable remedy suggestion in the deny message, never to actually
    execute anything.
    """
    stripped = cmd.lstrip()
    lead_ws = cmd[: len(cmd) - len(stripped)]
    if stripped[: len(leading_token)] != leading_token:
        return cmd  # defensive -- unexpected shape, leave cmd untouched
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


#: (Message-parity fix, 2026-08-02) Per-``effective_type`` override of the
#: header line and the three deny-message stanzas that are genuinely
#: agent-class-specific -- the "BLOCKED:" header, the scaffolder-framing
#: stanza, the "Accepted invocation forms" stanza, and the closing framing
#: paragraph -- selected the same data-driven way
#: ``_DEFAULT_RULESET_TYPE_OVERRIDES`` selects ruleset content, per
#: cross-repo/inbox/2026-08-02-doe-claude-em-executor-confinement-deny-message-addresses-wrong-agent-class.md.
#: ``coordinator:code-reviewer`` (and any other type with no entry here) has
#: no key in this dict, so ``_deny_reason`` falls through to the
#: ``_DEFAULT_HEADER_LINE``/``_DEFAULT_*_STANZA`` values below unchanged --
#: byte-identical to the pre-fix message (AC3). The middle "Did you
#: mean.../Denied: any other command..." stanzas describe guard mechanics
#: that hold for every confined type identically and are deliberately NOT
#: made per-type here -- only the header and the three stanzas that name a
#: specific agent class or a specific required argument vary.
#:
#: Negative spec (binding for a future third confined type): remediation
#: text is per-confined-type, resolved through this dict exactly as
#: ``_DEFAULT_RULESET_TYPE_OVERRIDES`` resolves ruleset content -- a new
#: confined type MUST add its own entry here rather than either (a) reusing
#: the code-reviewer's findings-agent framing verbatim, or (b) forking the
#: deny path with an inline ``if effective_type == ...`` at the emit site.
#: ``coordinator:code-reviewer``'s default header and three stanzas MUST NOT
#: be edited to accommodate a new type -- the message-coherence test suite
#: enforces ENUMERATION coherence (git subcommands, fs binaries,
#: metacharacters, find flags named in the message match the module
#: constants), not full-string equality against a literal known-good value.
_EXECUTOR_SCAFFOLDER_STANZA = (
    "Confined coordinator:executor subagents may also run read-only git,",
    "python3 -m pytest, python3 <script>, the read-only machine-local",
    "subcommands (get/has/keys/path/dir), and coordinator-doc-new -- no --type",
    "pin required, since an executor legitimately scaffolds several sidecar kinds:",
    "",
    "  coordinator-doc-new [--plan <path>] [--chunk <id>] ...",
)

_EXECUTOR_ACCEPTED_FORMS_STANZA = (
    "Accepted invocation forms (the dispatching EM resolves <claude-klabauter-live-root> from the",
    "machine-local registry key repos.claude_klabauter and injects the literal",
    "absolute path into the dispatch prompt -- a confined agent cannot resolve it):",
    "  <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py [--plan <path>] [--chunk <id>] ...",
    "  python3 <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py [--plan <path>] [--chunk <id>] ...",
)

_EXECUTOR_CLOSING_STANZA = (
    "This guard confines coordinator:executor Bash to the commands named above.",
    "A command outside this list is genuinely out of scope for this dispatch --",
    "report the blocker to the dispatching EM rather than retrying it.",
)

_EXECUTOR_HEADER_LINE = "BLOCKED: confined coordinator:executor Bash invocation outside the allowlist."

_DEFAULT_HEADER_LINE = "BLOCKED: confined findings-agent Bash outside allowlist."

#: Leg-3 headers. `_is_confined_type`'s third leg
#: (``is_confined_by_roster_absence``) confines by ABSENCE, so it fires for
#: agents that are not findings agents at all -- and the default header
#: above then tells them they are one, and offers them the REVIEWER's
#: `coordinator-doc-new --type review-findings` allowlist as their remedy.
#:
#: That is not a cosmetic inaccuracy; it is a wrong diagnosis that routes the
#: reader away from the cause. Measured 2026-08-31: a `coordinator:executor`
#: confined solely because the roster could not be read received the default
#: header, and TWO sessions across nineteen days went looking for the
#: executor in `_helpers._CONFINED_FINDINGS_AGENTS` -- the one set that
#: cannot contain it, because leg 3 never names a type anywhere. The report
#: (`2026-08-20-example-retrieval-repo-em-executor-confined-under-the-reviewer-
#: allowlist.md`) was accurate the whole time and read as unreproducible.
#:
#: These REPLACE the header rather than adding a line, so the prose byte
#: count (`_message_size`) is unaffected -- the fix is that the one sentence
#: already being spent says something true.
#:
#: Deliberately NOT a verdict change. Both leg-3 confinements are argued
#: fail-closed behaviour (an unreadable roster degrades to "cannot confirm
#: this type is legitimate", never to "assume it is fine"), and whether an
#: unclassifiable input should pass or refuse is a separate, open,
#: direction-class ruling. A refusal can be honest about its cause under
#: either answer.
_ROSTER_UNREADABLE_HEADER_LINE = (
    "BLOCKED: agent roster unreadable, so every type is confined until it resolves."
)
_TYPE_UNENUMERATED_HEADER_LINE = (
    "BLOCKED: this dispatch identity is on no roster, so Bash is confined."
)

#: (Message-size discipline, 2026-08-03) Trimmed to prose-cap width. Moved
#: onto ONE indented line so it lands inside the "Use instead:" cue window
#: (see ``_deny_reason``) and is exempted from the prose byte count --
#: content unchanged in substance (still names the binary and the required
#: arg), only the surrounding sentence is gone.
_DEFAULT_SCAFFOLDER_STANZA = (
    "  coordinator-doc-new --type review-findings [--plan <path>] [--chunk <id>] ...",
)

#: (Message-size discipline, 2026-08-03) The explanatory sentence is gone;
#: the two indented forms are self-explanatory ready-to-run alternatives and
#: sit in the same exempted cue window as the scaffolder/Tier-A lines above.
_DEFAULT_ACCEPTED_FORMS_STANZA = (
    "  <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ...",
    "  python3 <claude-klabauter-live-root>/coordinator/bin/coordinator-doc-new.py --type review-findings ...",
)

#: (Message-size discipline, 2026-08-03) No closing stanza for the default
#: (findings-agent) type -- the indented alternatives above already are the
#: route forward; nothing left to restate.
_DEFAULT_CLOSING_STANZA: tuple = ()

_DENY_MESSAGE_STANZA_OVERRIDES: Dict[str, Dict[str, Any]] = {
    _EXECUTOR_TYPE: {
        "header": _EXECUTOR_HEADER_LINE,
        "scaffolder": _EXECUTOR_SCAFFOLDER_STANZA,
        "accepted_forms": _EXECUTOR_ACCEPTED_FORMS_STANZA,
        "closing": _EXECUTOR_CLOSING_STANZA,
    },
}


#: (Message-size discipline, 2026-08-03) The Tier A enumerations (git
#: subcommands, read-only fs binaries) and the metacharacter-set
#: enumeration, moved onto INDENTED lines shared by every confined type
#: (they described guard mechanics identically for both types even before
#: this change -- see the pre-existing module docstring note on the
#: "Did you mean.../Denied: any other command..." stanzas). Indenting them
#: lands them inside the "Use instead:" cue window ``_deny_reason`` opens,
#: which exempts them from the measured prose-byte cap (see
#: ``_message_size._exempt_span_bytes`` / ``_INDENTED_CMD_RE``) -- this is
#: what makes a duty-of-care-complete message (names the read-only escape
#: hatch AND the denied-metacharacter set) fit under the 220-byte prose cap.
#: Content is unchanged from the pre-existing text in substance; only the
#: connecting sentences around each enumeration are gone, and each
#: enumeration test in
#: ``test_block_reviewer_bash_outside_allowlist_message_coherence.py``
#: still locates its anchor by regex, not by full-string match, so this
#: reformat does not touch that suite's assertions.
#: The first line is a single BACKTICK-wrapped, genuinely runnable example
#: (``git show``, no placeholder). This is a deliberate liveness-gate
#: anchor, not decoration: ``extract_alternatives`` classifies every
#: backtick span in a cue window FIRST, and only falls through to its raw
#: indented-line scan when that first pass finds nothing at all. The two
#: slash-separated enumeration lines below (needed verbatim, byte-for-byte,
#: by ``test_offer_block_git_subcommands_match_the_enforced_set`` /
#: ``test_offer_block_fs_binaries_match_the_enforced_set``) are NOT
#: themselves single runnable commands -- ``ls / cat / head / ...`` parses,
#: under that raw fallback, as one `ls` invocation with every other name as
#: a literal path argument, which genuinely fails ("No such file or
#: directory") and would register as a false DEAD alternative. Anchoring a
#: real, classifiable command in this same window is what keeps the
#: fallback from ever reaching those enumeration lines at all -- it is not
#: itself required as an "offer" (the two lines below already are), so it
#: is intentionally terse.
#:
#: The last two lines are prefixed ``Denied:`` (not offered commands) --
#: belt-and-suspenders should the anchor above ever be removed -- so the
#: raw indented-line fallback's own skip-list (``candidate.startswith(
#: ("Subagent:", "Command:", "Denied:", "Reason:"))``) would still exclude
#: them: ``unquoted shell-chaining metacharacter (...)`` has no argv[0]
#: that resolves on PATH and would otherwise register as a false DEAD
#: alternative. Every line here still lands inside the indented span the
#: prose-byte exemption matches (``_message_size`` does not consult label
#: prefixes or backticks -- see ``_exempt_span_bytes``), so none of this
#: costs cap budget.
_TIER_A_ENUM_BLOCK = (
    "  `git show`",
    "  git show / diff / log / status / blame / ls-files / rev-parse / describe",
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
    discovery tree). That is the same call leg 3 already made to reach this
    verdict, and it is reached only after ``check()`` has decided to deny --
    never on an allow, and never on a command this guard has nothing to say
    about.

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
    """The REASON block, with the header line and the three
    agent-class-specific stanzas resolved per ``effective_type`` via
    ``_DENY_MESSAGE_STANZA_OVERRIDES`` (see that dict's docstring).
    ``coordinator:code-reviewer`` has no entry there, so the header and
    every stanza below resolve to their ``_DEFAULT_*`` value.

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

    ``suppress_retry_advice`` (Divergence 18, 2026-08-11): the closing
    stanza (only non-empty for ``coordinator:executor`` today) ends with
    "report the blocker to the dispatching EM rather than retrying it" --
    correct for a genuinely out-of-scope command, actively wrong for a
    python-family misspelling whose ``python3``-corrected form would allow.
    The caller sets this ``True`` for exactly that case; it forces the
    closing stanza empty regardless of ``effective_type``, never adds text.
    """
    cmd_safe = _sanitize_cmd_for_reason(cmd)
    overrides = _DENY_MESSAGE_STANZA_OVERRIDES.get(effective_type, {})
    header_line = overrides.get("header", _DEFAULT_HEADER_LINE)
    # A leg-3 confinement is not a findings-agent confinement, and saying so
    # is what stopped two sessions finding the cause -- see
    # `_ROSTER_UNREADABLE_HEADER_LINE`'s own comment. An explicit per-type
    # override still wins: a type with its own header (today only
    # `coordinator:executor`) has an identity that DID resolve, so leg 3 is
    # not why it is here.
    if "header" not in overrides:
        if confinement_cause == "roster-unreadable":
            header_line = _ROSTER_UNREADABLE_HEADER_LINE
        elif confinement_cause == "unenumerated":
            header_line = _TYPE_UNENUMERATED_HEADER_LINE
    scaffolder_stanza = overrides.get("scaffolder", _DEFAULT_SCAFFOLDER_STANZA)
    accepted_forms_stanza = overrides.get("accepted_forms", _DEFAULT_ACCEPTED_FORMS_STANZA)
    closing_stanza = (
        () if suppress_retry_advice else overrides.get("closing", _DEFAULT_CLOSING_STANZA)
    )
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
    """Evaluate the reviewer-bash-allowlist guard against a PreToolUse payload.

    ``policy_path`` (Divergence 7, 2026-07-27) is the explicit path to DoE's
    ``subagent-sandbox-policy.yaml``, injected by the dispatch caller (C5a)
    -- ``None`` (the default, and what every pre-2026-07-27 caller and this
    module's own 99-test oracle suite still pass) makes ``load_policy``
    fall through to its own weaker env-var/best-effort resolution legs,
    which resolve to an empty ``Policy`` in a bare test/dev environment --
    exercising exactly the AC11 hardcoded-fallback path. Never consumed
    against the weak in-process fallback chain in production (C5a wires the
    explicit path); see the module docstring's Divergence 7 section.

    Returns ``None`` (allow) or the nested hard-deny envelope.
    """
    # 1. Tool-name guard — defense-in-depth (reference hook 62-67).
    # (2026-08-07, C6) Widened from a bare "!= Bash" literal to accept
    # PowerShell too, now that this guard carries a PowerShell Tier A
    # allowlist (Divergence 13 below). Any OTHER tool_name -- including an
    # absent one -- is still not this guard's business at all (MATCHERS
    # already filters in production; this remains a defense-in-depth
    # pre-filter, not the dialect-gap SILENT case) and returns plain None,
    # exactly as the pre-C6 gate did for every non-Bash tool_name.
    tool_name = payload.get("tool_name") or ""
    dialect = dialect_from_tool_name(tool_name)
    if dialect is None:
        return None

    # 2. No agent_id -> top-level EM Bash call -> allow BEFORE any
    #    identity-resolution cost (reference hook 69-73).
    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    # 3. Identity resolution via the shared resolver (reference hook 75-117).
    cwd = payload.get("cwd")
    git_root = resolve_git_root(cwd)
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)
    agent_type = payload.get("agent_type") or ""
    # Divergence 19 (2026-08-23, root cause for the P1 filed as "intermittent"
    # at state/bug-backlog/2026-08-21-bash-guard-applies-code-reviewer-allowlist-
    # to-other-agents-intermittently.yaml): `git_root` was derived SOLELY from
    # the agent's `payload["cwd"]`, which is wherever that agent happened to be
    # working at the moment of the call -- routinely its own scratchpad, which is
    # outside the repo. `resolve_git_root()` then returns None, the back-pointer
    # leg below never runs, and `subagent_type` stays "". For a NAMED dispatch
    # `agent_type` is the caller-chosen teammate NAME rather than a type, so
    # `_resolve_effective_type` has no known identity to prefer, degrades to that
    # name, and `_is_confined_type`'s leg 3 confines it for absence from the
    # roster -- handing an arbitrary agent `coordinator:code-reviewer`'s
    # allowlist.
    #
    # That is what made this look intermittent for three sessions: the verdict
    # tracks the cwd of each individual Bash call, so the SAME agent running the
    # SAME command is allowed from the repo and denied from its scratchpad. Not a
    # race -- measured, the back-pointer was on disk 79s before the denial.
    #
    # The back-pointer store does not move when an agent changes directory: it
    # lives in the repo the EM session runs in, which is the HOOK process's own
    # cwd. So fall back to that (`resolve_git_root(None)`, the documented
    # resolve-against-process-cwd contract) when the payload cwd cannot resolve.
    #
    # This does NOT relax confinement, which is why it is preferred over the two
    # alternatives (fail open on unresolvable git_root, or stop selecting the
    # code-reviewer ruleset). `_read_backpointer_subagent_type` is already
    # cross-checked against THIS payload's `session_id` via
    # `expected_em_session_id`, so widening WHERE the store is looked for cannot
    # manufacture a false identity -- a wrong root simply finds no matching row
    # and returns "", landing on exactly today's fail-closed behaviour. It can
    # only recover a TRUE identity that a transient cwd was hiding. Divergence
    # 18's deliberate fail-closed-on-unresolved posture is untouched: it still
    # governs every case where the chain genuinely does not resolve.
    if not git_root:
        git_root = resolve_git_root(None)

    subagent_type = ""
    if agent_id and git_root:
        # Review: coordinator:code-reviewer (2026-08-14, Divergence 18
        # deferred finding) -- the back-pointer chain never checked that the
        # em_session_id it read from em-session-id.txt matched THIS payload's
        # own session_id, so a stale/cross-session/fabricated back-pointer
        # could resolve a caller-chosen agent_type to any real, non-confined
        # subagent_type and clear confinement via Divergence 18's new formula.
        # Passing expected_em_session_id here cross-checks the resolved
        # em_sid against the live calling session and fails the lookup
        # (returns "") on any mismatch.
        subagent_type = _read_backpointer_subagent_type(
            git_root, agent_id, expected_em_session_id=session_id
        )

    # Empty canonical AGENT_ID -> no subagent or unrecognised shape -> allow
    # (fail-open, reference hook line 117).
    if not agent_id:
        return None

    # Divergence 7 (2026-07-27): load the declared bash_policy table once.
    # engine.load_policy() is itself fail-open (absent/unreadable/malformed
    # file -> an empty Policy, never a raised exception) -- every downstream
    # consumer below (_is_confined_type / _resolve_ruleset) degrades that
    # emptiness to the PRIOR hardcoded enforcement (AC11), never to ALLOW.
    policy = load_policy(policy_path)

    # 6. Confined-set membership (reference hook 163-192), policy-driven with
    #    a hardcoded-set fallback (AC10/AC11).
    #
    # Divergence 18 (2026-08-14, close the named-dispatch confinement-
    # manufacturing residual): a bare OR here made EVERY named (Agent-teams
    # teammate) dispatch confined, regardless of the dispatched agent's real
    # type. Root cause: for a NAMED dispatch, `agent_type` is the caller-
    # chosen teammate NAME, never a real `coordinator:*` type -- an unknown
    # name is confined by leg 3 of `_is_confined_type`
    # (`is_confined_by_roster_absence`), so the OR manufactured confinement
    # for a dispatch whose back-pointer-resolved `subagent_type` is a type
    # the policy does NOT confine (e.g. `coordinator:git-commit-agent`,
    # `coordinator:enricher`). `_resolve_effective_type` then had no known
    # identity to prefer (the confining leg, `agent_type`, is the only KNOWN
    # one via leg-3's catch-all), so `effective_type` became the garbage
    # name and `_resolve_ruleset` fell through to `_default_ruleset()` -- the
    # narrow findings-agent allowlist -- denying commands (e.g.
    # `scoped-git-commit`) the real, non-confined type is entitled to run.
    # This is a DIFFERENT question from Divergence 16/17's fix immediately
    # below: those changed WHICH already-confined identity's ruleset
    # applies; this changes WHETHER confinement fires at all for a named
    # dispatch. Fix: a KNOWN back-pointer-derived `subagent_type` governs the
    # confinement verdict outright -- a caller-chosen name must never
    # MANUFACTURE confinement for a type the policy does not confine, on the
    # same "back-pointer identity outranks caller-chosen free text"
    # principle Divergence 17 already established for ruleset selection.
    # Only when `subagent_type` is not known (unnamed dispatch: empty; or a
    # named dispatch whose back-pointer chain itself failed to resolve) does
    # the original OR apply -- fail-closed leg-3 confinement for a type
    # unknown on BOTH legs is unchanged.
    #
    # Staff-eng review (2026-08-14, finding 0/major): the first cut of this
    # fix let a KNOWN, non-confined `subagent_type` CLEAR confinement a
    # KNOWN, genuinely-confined `agent_type` (e.g. `coordinator:code-
    # reviewer`) would otherwise impose -- a stale or attacker-written
    # `dispatched-agents.txt` row could no-op this guard entirely for a
    # findings agent. Corrected: when `subagent_type` is known, it confines
    # the dispatch on its own leg OR when `agent_type` is ALSO known and
    # confined -- a known `subagent_type` can free a caller-chosen NAME
    # (unknown `agent_type`) from manufactured confinement, but it can never
    # launder a known-and-confined `agent_type` into freedom. A name cannot
    # manufacture confinement for a type the policy does not confine, and a
    # back-pointer value cannot clear confinement a known-confined
    # `agent_type` imposed.
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

    # Resolve the most informative type label for the deny message
    # (reference hook 194-196). (Divergence 16, 2026-08-11) A NAMED-teammate
    # dispatch's agent_type is a raw name string, not a real type -- see
    # _resolve_effective_type's own docstring for why a plain
    # "whichever leg is confined at all" selection picks that garbage string
    # over the correctly back-pointer-resolved subagent_type.
    effective_type = _resolve_effective_type(agent_type, subagent_type, policy)

    # Resolve this effective_type's allowed Bash surface -- a validated
    # bash_policy entry if one exists, else _default_ruleset() (AC11).
    ruleset = _resolve_ruleset(effective_type, policy)

    # (2026-08-07, C6) Dialect-gap SILENT leg -- see Divergence 13's negative
    # spec below. Unreachable today given the top-gate above accepts ONLY
    # "Bash"/"PowerShell" (both of which this guard now has an allowlist
    # for), but kept explicit rather than assumed: a THIRD dialect entering
    # `Dialect` in the future, with no allowlist added here yet, must record
    # SILENT and decline to rule -- never fall open to allow, and never
    # silently reuse the Bash or PowerShell allowlist for a dialect neither
    # was written for.
    if dialect not in (Dialect.BASH, Dialect.POWERSHELL):
        record_silent(
            "block_reviewer_bash_outside_allowlist",
            f"no allowlist for dialect {dialect!r} -- declined to rule, not a class flip",
        )
        return None

    # 7. Confined findings-agent confirmed — extract and validate the command
    #    (reference hook 198-282).
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

    # Windows argv0-head normalization (2026-07-29, THIS change): rewrite an
    # unquoted Windows backslash argv0-head path -- with or without an
    # embedded-space component (e.g. a spaced Windows username) -- whose
    # basename identifies git, the Tier B scaffolder, or a Tier A read-only
    # fs binary, into a shlex-safe forward-slash (and, for the
    # embedded-space case, single-quoted) form. Every check below runs
    # against this normalized ``cmd_for_check``; the ORIGINAL, unrewritten
    # ``cmd`` is still what the human-facing deny message echoes (see
    # ``_deny_reason`` call below).
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
        # (Divergence 8, 2026-07-28) A clean top-level pipeline -- every
        # segment must be independently Tier-A-allowlisted. This is checked
        # BEFORE the single-command Tier A/B logic below, which does not
        # know how to evaluate a multi-segment command.
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
        # Tier A (2026-07-25): read-only inspection/discovery escape hatch.
        # Checked AFTER the metacharacter gate (which applies to both
        # tiers) but BEFORE the Tier B coordinator-doc-new checks below --
        # a Tier A command never needs --type review-findings.
        first_token = _extract_first_token(cmd_for_check)
        if _token_matches_binary(first_token, "git"):
            git_allowed, git_deny_reason = _evaluate_git_tier_a(cmd_for_check, ruleset)
            if git_allowed:
                return None
            if git_deny_reason:
                # Option-surface hardening (2026-07-25): a SPECIFIC
                # global-option or subcommand-write-flag rejection --
                # skip the generic Tier B fallthrough below and deny with
                # this reason directly.
                deny = True
                deny_reason = git_deny_reason
            # else: subcommand not on the read-only allowlist (or no
            # subcommand at all) -- deny-by-omission, falls through to the
            # generic Tier B checks below (which will name "git" as an
            # unrecognised first token).
        elif _token_matches_binary(first_token, ruleset["machine_local_binary"]):
            # Tier A (2026-08-02): read-only machine-local subcommand
            # escape hatch -- sibling of the git branch immediately above,
            # same place in the evaluation order. A bare `machine-local`
            # with no subcommand, or a write subcommand (set,
            # array-append, array-set, migrate-publish-mirrors), returns
            # (False, None) here and falls through to the generic Tier B
            # deny message below (which will name "machine-local" as an
            # unrecognised first token) -- exactly the git branch's own
            # deny-by-omission fallthrough shape.
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
            # Tier A (2026-08-07, C6): read-only PowerShell cmdlet escape
            # hatch -- sibling of the bash readonly-fs-binary branch
            # immediately above, gated on dialect since these cmdlet names
            # (Get-ChildItem, Select-String, Get-Content) are meaningless as
            # Bash-dialect binary names and must not be admitted there.
            return None

    if not deny and not _first_token_is_allowlisted_binary(cmd_for_check, ruleset):
        # (Amendment 1, 2026-08-01) Not the scaffolder -- try the narrow
        # python3-interpreter discrimination tier before falling to the
        # original generic deny. See _evaluate_python3_interpreter's
        # docstring: it returns None (not applicable/not granted) for every
        # case that must preserve the ORIGINAL deny message text (AC3).
        tokens_for_interpreter = _tokenize_segment(cmd_for_check)
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
            # matched against the allowlist -- `_extract_first_token` (via
            # `_first_effective_token`) deliberately returns tokens[1], not
            # tokens[0], for an exact `python3 <script>` invocation (see
            # that function's own docstring). `raw_first_token` is the
            # UNMODIFIED tokens[0] already tokenized above for the alias
            # check below. When the two differ, a message that calls
            # `first_token` "first command token" mis-describes argv[0] --
            # filed as state/bug-backlog/2026-08-21-bash-guard-applies-
            # code-reviewer-allowlist-to-other-agents-intermittently.yaml
            # (defect 2). Naming both keeps the message honest without
            # touching the effective-token MATCHING logic, which stays
            # exactly as before (AC3/AC5 pin this string byte-identical for
            # every case where the two tokens already coincide, e.g.
            # `curl`/`rm` -- unaffected by this branch).
            first_token = _extract_first_token(cmd_for_check)
            raw_first_token = tokens_for_interpreter[0] if tokens_for_interpreter else ""
            if raw_first_token and raw_first_token != first_token:
                deny_reason = (
                    f"command token is not coordinator-doc-new (got: {first_token or 'empty'}, "
                    f"invoked via {raw_first_token})"
                )
            else:
                deny_reason = f"first command token is not coordinator-doc-new (got: {first_token or 'empty'})"
            # (Divergence 18, 2026-08-11) The exact-`python3` tier above
            # declined (tokens[0] != "python3") -- check whether tokens[0]
            # is a python-family MISSPELLING whose `python3`-corrected form
            # would itself be allowed by this ruleset. Only ever narrows the
            # MESSAGE; the command above is already denied either way.
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
