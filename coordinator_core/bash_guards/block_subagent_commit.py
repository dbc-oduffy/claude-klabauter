"""coordinator_core.bash_guards.block_subagent_commit -- subagent-commit
enforcement gate (structural teeth for the AC6/AC13 no-self-commit rule).

Own-module pattern, mirroring ``block_reviewer_bash_outside_allowlist.py``
(NOT a ``dispatch_checks.py`` fold) -- this keeps the file disjoint from
M1's ``check_validate_commit`` (Check 5, scoped-staging) edit in the same
module, a deliberate parallel-safety choice, not an accident.

Caller-identity signal (the Director of Engineering Finding D1, delta review -- pinned, not
deferred): ``COORDINATOR_AGENT_CONTEXT`` is REJECTED as the gate signal --
``coordinator-safe-commit`` used to read
``os.environ.get("COORDINATOR_AGENT_CONTEXT", "em") or "em"``, so an unset
var resolved to ``"em"`` and was ALLOWED: cooperative and fail-open by
construction, the exact flaw this gate exists to close.
``enforce-agent-dispatch-mode.py`` is registered on the Agent matcher, not
Bash -- it never runs on the PreToolUse Bash path a ``git commit`` traverses.
The correct signal is the HARNESS-SUPPLIED ``agent_id`` in the PreToolUse
payload (resolved via ``coordinator_core.subagent_sandbox.engine``,
re-exported through ``bash_guards._helpers``) -- non-cooperative, a subagent
cannot unset it.

This module is that detection: ``coordinator_core/bash_guards/
nudge_subagent_scoped_commit.py``'s identity gate + git-commit detection,
with the verdict FLIPPED (allow -> deny) and the scoped-pathspec exemption
REMOVED -- M4 denies ANY subagent ``git commit``, scoped or not, not just an
unscoped one. ``nudge_subagent_scoped_commit`` itself is RETIRED (its
guard_chain registration removed) in the same change that registers this
module, so there is exactly one gate on subagent commits, not two.

Allow-set enumeration (the Director of Engineering Finding D6, verified against
``coordinator/agents/*.md`` frontmatter 2026-07-24): the Opus reviewer/
synthesizer roster (eng-director, staff-eng, staff-data-sci, senior-front-end,
staff-ux, vp-product, research-sweep, research-synthesizer,
structured-synthesizer) never authors source edits that need a commit, and
``code-reviewer`` is deliberately Bash-confined precisely so it cannot
commit at all. Allow-set = {EM} only; ``_ALLOWED_SUBAGENT_TYPES`` is an
empty forward-compatibility hook for a future named-Opus member, not a
live allowance today. Known, accepted boundary: model tier is not
observable on this PreToolUse path (only ``agent_id``/``agent_type``/
``subagent_type`` are) -- a persona dispatched with a ``model:`` override
would be allowed by ``subagent_type`` alone; that residual is mitigated by
the existing model-guard hook on dispatch, not by this gate.

Deny-by-default: any command that resolves to a git-commit invocation
(plain ``git commit`` or via ``coordinator-safe-commit``, since the helper
itself shells out to ``git commit`` under the hood) AND resolves to a
caller identity that is NOT the EM main-loop (no ``agent_id`` in payload)
is DENIED -- a subagent that fails to set cooperative state cannot evade
this gate, because the signal it is keyed on is not cooperative.

Bypass fix (2026-07-25, confirmed-against-live-guard report): three shapes
previously ALLOWED despite matching the paragraph above --
``git -c user.name=x commit -m "msg"`` (arbitrary global option before the
subcommand; the option's separate-arg VALUE token is neither a flag nor
``commit``, which a fixed two-word-sequence pre-filter cannot express),
``git  commit -m "msg"`` (extra internal whitespace), and
``coordinator-safe-commit -m "msg"`` (the pre-filter never checked for the
helper name AT ALL, despite this docstring's own claim above that the
helper is in scope). Root cause: ``_prefilter_mentions_commit`` was a
cheap substring check narrower than the full matchers it gated -- it
required the literal sequence ``"git commit"`` or ``"git -C"``, so
``check()`` returned ALLOW before the full matcher ever ran. Fixed by
replacing the pre-filter with a genuine over-approximation (see
``_prefilter_mentions_commit``'s docstring for the invariant and the
generalizable lesson) and replacing the fixed-shape git-commit regex with
a token walk that tolerates arbitrary global options
(``_has_git_commit`` / ``_tokens_reach_commit_after_git``), plus a
dedicated ``_has_coordinator_safe_commit`` matcher that was previously
entirely absent.

Bypass fix, part 2 (2026-07-25, same-day follow-up, confirmed-against-
live-guard report): a FOURTH shape ALLOWED despite the fix above --
``/usr/bin/git commit -m x`` (git invoked via an absolute path). Root
cause: the part-1 fix boundary-anchored the ``coordinator-safe-commit``
HELPER matcher (``_token_matches_binary`` / ``_extract_first_token``)
across bare / ``bin/``-prefixed / absolute / Windows / ``python3 <path>``
forms, but ``_tokens_reach_commit_after_git``'s own ``git``-token
recognition was left as a literal ``tokens[start] != "git"`` equality
check -- it never received the same treatment. That is the specific
asymmetry to watch for the next time this file changes: **hardening one
matcher in a file and leaving its structurally-identical sibling literal
is the recurring defect shape here, not a one-off.** Fixed by routing
``_tokens_reach_commit_after_git``'s ``git``-token test through the SAME
``_token_matches_binary`` helper the coordinator-safe-commit matcher
already used (no third derivation) -- so ``git`` now matches bare
``git``, ``bin/git``, an absolute POSIX path ending ``/git``, or an
absolute Windows path ending ``\\git``, on the identical path-separator
boundary rule (``evil-git``, ``mygit``, ``gitlab`` still correctly
ALLOW -- none end on a path-separator boundary before ``git``).
2026-07-29 follow-up (guard-brick incident response, part 2): the
``.exe``-suffix gap flagged below as out-of-scope was closed, and a SECOND
gap of the same class was found and closed alongside it: ``_token_
matches_binary`` now delegates to ``_command_tokenizer.token_matches_
binary``, which strips a trailing ``.exe`` OR ``.cmd`` (case-insensitively)
before comparing basenames -- so ``git.exe commit -m x``, ``GIT.EXE commit
-m x``, ``C:\\Git\\bin\\git.exe commit -m x``, AND
``coordinator-safe-commit.cmd -m x`` are now all detected, the first three
via the ``git``-token check (``_tokens_reach_commit_after_git``) and the
last via the ``coordinator-safe-commit`` helper check (``_has_coordinator_
safe_commit``), since both route through the same shared matcher.
``coordinator-safe-commit.cmd`` is not a hypothetical spelling -- it is
this project's OWN generated Windows launcher twin for that helper
(``coordinator/bin/gen-launcher-shim.py``), confirmed present on disk at
``coordinator/bin/coordinator-safe-commit.cmd``, and is the load-bearing
Windows invocation form (PATHEXT resolves it; ``CreateProcess`` cannot exec
the extensionless POSIX shebang directly). Confirmed empirically against
the live guard before this fix: both ``git.exe commit -m x`` and
``coordinator-safe-commit.cmd -m x`` returned ALLOW (not denied) despite
being subagent commits in every way indistinguishable from the plain
``git commit -m x`` this gate already denied -- the ordinary Windows
spelling defeated a guard whose whole purpose is to deny exactly this.
``.bat`` and ``.ps1`` were deliberately NOT added to the stripped-suffix
set -- see ``_command_tokenizer._WINDOWS_LAUNCHER_SUFFIX_RE``'s own
comment for the evidence (neither is a real carrier this project's
generator ever emits for these two binaries).

2026-07-29 update, part 3 (reconciled against ``block_subagent_
destructive_action.py``'s contradictory same-day verdict on the identical
shape -- code-reviewer Finding 1): the embedded-space Windows path case
(``C:\\Program Files\\Git\\bin\\git.exe``) was previously documented here as
"confirmed NOT independently exploitable" and left unfixed as a cosmetic
gap. That verdict was reached by reasoning about this module's OWN
``shlex``-based tokenizer in isolation, not by re-deriving the actual
execution model. Re-derived: this project's Bash-tool execution path on
Windows is Git Bash / MSYS bash (see ``docs/wiki/bash-on-windows-
gotchas.md``, example-doctrine-repo) -- a genuine POSIX shell that performs real
word-splitting and backslash-escape processing on the command string
BEFORE any executable is resolved, not a raw ``CreateProcess``
successive-space-delimited-prefix resolution (that Win32 hazard requires
``lpApplicationName=NULL`` handed a raw, un-split command line directly to
the loader -- a real shell's own ``exec`` never does this; it resolves one
path first and hands CreateProcess an already-quoted application name).
So an unquoted ``C:\\Program Files\\Git\\bin\\git.exe`` DOES mangle into
non-existent argv0 garbage under the harness's actual invoking shell
exactly as it does under this module's ``shlex`` model -- the underlying
``git`` invocation itself fails to execute, independent of whether this
guard's classifier recognizes the shape. The "non-exploitable" conclusion
for THIS harness therefore holds, and it is the sibling guard's "P0
security fix, not an exotic edge case" framing for the identical shape
that overstates the threat: its own module comment names the alternative
mechanism it worries about ("some invocation paths resolve raw
CreateProcess semantics") as speculative, and that mechanism does not
match this project's documented Windows Bash-tool execution model. See
``block_subagent_destructive_action.py``'s corresponding comment (search
"SPACED-WINDOWS-PATH ARGV0 HOLE") for the reconciled framing -- it is
retained there as defense-in-depth classifier hardening, not as a fix for
a demonstrated live bypass.

Ported here anyway, for two reasons neither of which is "the bypass is
real": (1) consistency -- two guards processing the identical shape should
reach the identical classification, not merely the identical *safety*
verdict, since a future reader diffing them should not have to re-derive
this whole analysis; (2) the asymmetry the reviewer named is real
independent of exploitability -- THIS module gates a *plain* ``git
commit``, which ``block_subagent_destructive_action.py``'s sub-verb
classifier does not catch, so if the execution-model analysis above is
ever wrong (a future harness change, a different OS shell substrate), this
module would otherwise be the only guard on that path with a known,
undetected gap. ``_normalize_windows_argv0_head_path_with_spaces`` below
mirrors (does not import -- see this module's established own-copy
pattern) the sibling's fix, scoped to ``git`` only (not the sibling's full
interpreter set, and deliberately NOT ``coordinator-safe-commit`` either --
that helper's own matcher, ``_has_coordinator_safe_commit``, is a separate
frag-based extractor that is not quote/space-aware and would mis-split an
already-quoted spaced path; extending this fix there surfaces a second,
independent, out-of-scope latent bug rather than closing this one, so it
is flagged here rather than folded into this change).

Fail-open boundary (the Director of Engineering Finding D8, inherited, not introduced here): this
guard fails open (allow) on every unevaluable input -- no ``agent_id`` in
the payload (EM main-loop), an ``agent_id`` present but unresolvable to a
canonical id (unrecognised shape), or a resolved id with no populated
effective type via either resolver leg. Three layers above this guard
(``preuse-bash-dispatch.py``'s claude-klabauter-unresolvable/unimportable-engine
path, ``evaluate_payload_json``'s unparseable-JSON path, and the ``Bash``-
only ``MATCHERS`` filter) also fail open, unchanged by this module -- this
gate is defence-in-depth over the M2' prose commit-discipline sections, not
a replacement for them.

Known pre-existing false-positive NOT inherited here (2026-07-24 sibling
report, ``state/lessons/2026-07-24-universal-subagent-commit-lock-false-
fla-fbcb8157d368.yaml``): ``block_subagent_destructive_action.py``'s
per-segment git-verb classifier (``_evaluate_git_segment``,
block_subagent_destructive_action.py:798-883) independently denies a
subagent ``git commit`` whenever the ``-m`` message text merely CONTAINS
the substring ``restore`` as a whole word (``_RESTORE_KEYWORD_RE = re.compile
(r"\\brestore\\b")`` at block_subagent_destructive_action.py:445, applied to
the FULL segment text -- including quoted commit-message content -- at
block_subagent_destructive_action.py:814-816), and denies any ``git rm
<file>`` outright because ``rm`` is not a member of that module's
``_SAFE_VERB_RE`` allowlist (block_subagent_destructive_action.py:478-481).
This module does NOT reuse that classifier -- it is built from
``nudge_subagent_scoped_commit``'s simpler "is this text a git-commit
invocation" detection, which has no restore/chmod/rm sub-verb awareness at
all -- so it does not inherit those two false positives. They remain live
in ``block_subagent_destructive_action.py`` (a different, already-registered
guard, out of this module's scope) and continue to independently deny the
same commits this module also denies for identity, ahead of this module in
``dispatch.py``'s guard_chain order. Not fixed here per this chunk's explicit
scope fence (single-file, single-purpose new-module addition); flagged for
a follow-up against that file.

Prose false-positive fix (2026-07-26, live-guard report): a subagent
persisting a review sidecar via a Bash heredoc (``cat <<'EOF' > review.md
... EOF``) whose PROSE discussed this guard's own "git commit" enforcement
was denied, even though the executed command ran no git at all -- the
heredoc BODY text supplied the guard's ``_has_git_commit`` scan with
literal adjacent ``git``/``commit`` tokens. Root cause: both
``_has_git_commit``'s segmentation (``re.split(r"[;&|]", cmd)``) and its
per-fragment tokenization (``frag_norm.split(" ")``) were quote/heredoc-
blind -- the same defect CLASS ``8fb0c481`` (indirection guard splitting on
operators inside quotes) and ``e88bc98b`` (dispatch guard false-positiving
on its own prose) fixed in sibling guards the same day. Fixed by porting
``block_subagent_destructive_action.py``'s already-battle-tested
heredoc-body-strip (``_strip_heredoc_bodies``, still an own-module copy
here) and quote-aware ``shlex``-based tokenizer (plus the Windows-argv0
backslash-mangling guard ``_normalize_windows_git_argv0``) into this
module. ``_has_coordinator_safe_commit`` is untouched (no reported bug, no
regression test exercises it through this shape).

2026-07-29 update: the tokenizer trio (``_tokenize_full_command``,
``_segments_from_tokens``, and ``_normalize_executable_basename`` used by
``_normalize_windows_git_argv0``) is no longer an own-module copy -- it now
imports from ``coordinator_core.bash_guards._command_tokenizer``, the
single canonical home four other guard modules already depended on
indirectly via ``block_subagent_destructive_action.py``. Keeping this
module's copy hand-maintained had already caused real drift: it never
received that sibling copy's 2026-07-28 case-folding fix, so a
``GIT.EXE``-spelled argv0 silently bypassed THIS guard's git-commit
detection (while every other bash-guard already caught it). The
``_extract_first_token``/``_token_matches_binary`` own-module-copy pattern
below was, AT THE TIME OF THIS PARAGRAPH, left unchanged and still
deliberate (that concern -- avoiding cross-module coupling during
concurrent chunked development of a sibling guard -- does not apply to a
small, stable, no-guard-policy tokenizer module maintained specifically to
prevent this class of drift). Superseded by the part 2 and part 4 entries
below: ``_token_matches_binary`` was unified onto the canonical
``_command_tokenizer`` copy in part 2, and ``_extract_first_token`` itself
was retired entirely in part 4 once it was confirmed to carry its own
independent quote-blindness bug, not merely a cross-module-coupling
concern.

2026-07-29 update, part 4 (integrator report during the Windows argv0-head
port above -- the ``_has_coordinator_safe_commit`` quote-blindness the
part-3 entry flagged and deliberately left unfixed): confirmed live and
closed. ``_extract_first_token`` split each fragment on the first raw
space and quote-stripped only when a SINGLE token happened to be wrapped
start-to-end in matching quotes -- an already-quoted argv0-head path with
an embedded space (``'/opt/coordinator tools/coordinator-safe-commit' -m
"x"``, or the ordinary unquoted Windows form
``C:\\Users\\John Doe\\.coordinator-claude-settings\\bin\\coordinator-safe-
commit.cmd -m "x"``, "John Doe" being an ordinary Windows username on this
project's primary platform) mis-split at the internal space regardless of
quoting, so the extracted "first token" was a path FRAGMENT that matched
neither ``git`` nor ``coordinator-safe-commit`` -- confirmed ALLOW (not
denied) through the real ``check()`` entrypoint for all four spaced-path
spellings (quoted-POSIX, quoted-Windows-backslash, unquoted-Windows-
backslash, quoted-Windows-backslash-with-``.cmd``), while the identical
command with a space-free argv0 correctly DENIED. Fixed by retiring
``_extract_first_token`` and rebuilding ``_has_coordinator_safe_commit`` on
the SAME canonical machinery ``_has_git_commit`` already used
(``_tokenize_full_command`` + ``_segments_from_tokens`` + shared
``token_matches_binary``), plus generalizing the two argv0-head
normalization passes (``_normalize_windows_git_argv0`` and
``_WINDOWS_ARGV0_HEAD_PATH_RE`` / ``_normalize_windows_argv0_head_path_with_
spaces``) that previously only recognized a ``git``-basename argv0 to also
recognize a ``coordinator-safe-commit``-basename one -- the part-3 entry's
"extending this fix to that binary surfaces a second, independent,
out-of-scope latent bug" framing was the diagnosis, not a reason to leave
it open once this chunk WAS the fix for that exact bug. The path-separator
boundary rule is unchanged and re-verified: ``evil-coordinator-safe-commit``
still does not match (the generalized normalizer only rewrites a token
whose normalized basename is EXACTLY ``git`` or ``coordinator-safe-commit``,
never a substring/suffix of a longer identifier), and ``python3
coordinator-safe-commit`` and its ``bin/``/absolute-path spellings are
unaffected (still detected, same as before this change).

2026-07-29 update, part 5 (ceremony-op commit-gate hole, PM-relayed
incident): this module detected a subagent ``git commit`` and a subagent
``coordinator-safe-commit`` invocation, but had ZERO awareness that
``python3 -m coordinator_core.invoke ceremony.scoped_git_commit '<json>'``
is a THIRD way to reach the exact same ``git commit`` outcome --
``coordinator_core.ops.ceremony.scoped_git_commit`` (the op this CLI
dispatches to) is the very op ``coordinator-safe-commit`` itself shells out
to. Verified structurally before this fix: zero occurrences of
``scoped_git_commit``, ``coordinator_core.invoke``, ``python3 -m``, or
``-m coordinator_core.invoke`` anywhere in this file. A subagent invoking
this op against the shared worktree committed, unguarded, in a confirmed
peer report -- and a deny message elsewhere in this package had just begun
handing callers this exact invocation string as its own recommended
route, sharply raising the odds an eager (not adversarial) agent would
find it next. Fixed by adding ``_has_ceremony_scoped_commit_invoke``, a
third sibling matcher alongside ``_has_git_commit`` and
``_has_coordinator_safe_commit``, wired into ``check()`` the same way.
Unlike the other two matchers, this one does NOT need position-0/wrapper-
peeling logic (``_first_effective_token`` et al.): it walks the WHOLE
segment token list for a token that boundary-matches ``python3`` or
``python`` (any position, via the same canonical ``token_matches_binary``
every other identity check in this file already uses -- so ``.exe``-
suffixed, case-folded, and absolute-path interpreter spellings are
recognized for free), then, from that position, skips any of Python's own
leading global flags looking for a literal ``-m``, then requires the next
token to be exactly ``coordinator_core.invoke`` and the token after THAT to
be exactly ``ceremony.scoped_git_commit`` -- mirroring
``_tokens_reach_commit_after_git``'s own full-segment-scan shape (which,
for the identical reason, needs no separate wrapper-skip step either: a
preceding ``env``/``sudo``/``nice``/``timeout``/assignment prefix simply
sits at an earlier token index the scan already walks past on its way to
finding the interpreter token). The existing ``_wrapped_shell_c_payloads``
unwrap (BX-13) and heredoc-body strip both already run ahead of this
matcher in ``check()``, so ``sh -c 'python3 -m coordinator_core.invoke
ceremony.scoped_git_commit ...'`` and a heredoc merely discussing the op in
prose are both handled the same as they are for the other two matchers,
with no new code. Deliberately NOT covered (named, not silently dropped):
a Windows argv0-head path with an embedded-space component
(``C:\\Program Files\\Python311\\python.exe -m ...``) -- the same class of
gap this module's own docstring already reasons is not independently
exploitable under this project's actual Git-Bash/MSYS execution model (see
the 2026-07-29 part-3 entry above), so extending the spaced-path
normalization regex to a THIRD binary identity was judged not worth the
added regex complexity for a shape already argued non-exploitable twice in
this file; flagged here for a follow-up if that execution-model reasoning
is ever revisited.

2026-08-01 update, part 6 (C2 of the allowlist-confinement plan -- two
no-obfuscation holes, reachable with NO obfuscation at all, closed as
defense-in-depth for any agent not yet confined by C1's Bash allowlist):

Hole (a) -- the prefilter short-circuited most committing ops. ``_prefilter_
mentions_commit`` was a bare ``"commit" in cmd``, so it returned False --
short-circuiting ``check()`` to ALLOW before identity resolution ever ran --
for four committing ops whose names contain no ``commit`` substring at all:
``session.boot_sweep`` (three ``git commit`` sites in
``coordinator_core/ops/session/boot_sweep.py``), ``distill.apply_disposal``
(``coordinator_core/ops/distill_apply_disposal.py``), ``memo.send``
(``coordinator_core/ops/fleet/memo_send.py``), and ``ceremony.wsc_tail``
(``coordinator_core/ops/ceremony/wsc_tail.py``, which goes straight to
``run_commit_pipeline``). Even a fixed prefilter alone would not have
sufficed -- the full invoke-matcher (part 5's ``_has_ceremony_scoped_
commit_invoke``) only recognized the single literal op name ``ceremony.
scoped_git_commit``, so it would still have returned False for these four
even past a widened prefilter. Fixed by introducing ``_COMMITTING_OP_NAMES``,
a single named module-level set (``ceremony.scoped_git_commit`` plus the
four above) that BOTH ``_prefilter_mentions_commit`` and the renamed,
generalized invoke-matcher (``_has_committing_op_invoke`` /
``_tokens_reach_committing_op_after_python``, formerly ``_has_ceremony_
scoped_commit_invoke`` / ``_tokens_reach_ceremony_scoped_commit_after_
python``) now consult -- so the two cannot independently re-drift the way
the pre-filter/full-matcher pair already drifted once before (2026-07-25
"LESSON" entry above).

Hole (b) -- flag-before-positional defeated the op matcher. The part-5
matcher required the op token to sit IMMEDIATELY after ``coordinator_core.
invoke``, but ``--repo`` is effectively required by that CLI (AC-5's
no-implicit-``os.getcwd()``-fallback rule -- see ``coordinator_core/invoke/
__main__.py``'s own docstring), so the DOCUMENTED spelling ``python3 -m
coordinator_core.invoke --repo /path ceremony.scoped_git_commit '{}'``
walked straight past a positional-adjacency check -- no obfuscation
required, just the CLI's own natural usage. In-repo callers happen to put
flags AFTER the op (``... ceremony.scoped_git_commit '{}' --repo /x``,
already pinned by the part-5 test suite), which is why this was never
noticed. Fixed by making op resolution flag-tolerant
(``_first_positional_after_invoke_module``): it skips ``coordinator_core.
invoke``'s own leading optional flags -- including value-taking ones
(``--repo <path>``, ``--params-file <path>``, both derived from
``_build_arg_parser``'s real flag surface, not guessed) and ``--flag=value``
attached forms -- before resolving the first remaining positional as the
op name, instead of requiring positional adjacency.

NEGATIVE SPEC: do not widen ``_C_FLAG_SHELL_INTERPRETERS`` or add
``python``/``perl``/``node`` unwrapping to "fix" either hole above -- that
is the denylist reflex the sibling allowlist-confinement plan (C1) exists
to retire, explicitly ruled out by that plan's own Anti-scope section, and
would be a SIXTH patch of the exact defect class this file's whole history
is closing one instance at a time. Both holes above are fixed by widening
what THIS gate's own matchers recognize, never by widening the interpreter
set BX-13's ``sh -c`` unwrap reaches into.

AMENDED 2026-08-03 (2026-08-03-narrow-subagent-commit-confinement-two-
classes.md chunk C1) -- this NEGATIVE SPEC's precondition was holes (a) and
(b) above: for THOSE two holes, the interpreter set was never the gap, so
widening it would have been the denylist reflex this paragraph correctly
rejects. C1 is a DIFFERENT hole, unrelated to (a)/(b): a Python
interpreter's ``-c`` payload was never treated as EXECUTED TEXT at all
(``python3 -c "...scoped-git-commit..."`` tokenizes its payload as one
inert-looking shlex word, exactly the shape BX-13 already fixed for a
shell's ``-c`` -- see ``_C_FLAG_SHELL_INTERPRETERS``'s own docstring). That
is a missing-unwrap gap, not a missing-matcher gap, so it supersedes this
paragraph's precondition rather than violating its rule: C1 does NOT widen
``_C_FLAG_SHELL_INTERPRETERS`` (that set stays shell-only, exactly as this
paragraph requires) and does NOT add a fourth matcher (AC2) -- it extends
``_wrapped_shell_c_payloads``, the SAME shared unwrap BX-13 added, with a
second, separately-reasoned interpreter set (``_PYTHON_INTERPRETER_NAMES``,
already used elsewhere in this file for a different purpose) so a Python
``-c`` payload is unwrapped and re-scanned the same way a shell's already
is. This paragraph's rule -- do not reach for the interpreter set to patch
a matcher-shape gap -- still binds for any FUTURE hole in holes (a)/(b)'s
family; it does not forbid closing a genuinely new "payload never unwrapped
at all" gap by adding a same-purpose parallel set to the one function that
already does that unwrapping.

2026-08-01 update, part 7 (C5 of the allowlist-confinement plan --
``coordinator/bin/scoped-git-commit`` trampoline): a fourth committing
shape, unrelated to holes (a)/(b) above, was found unmatched by ANY of this
module's three matchers. ``scoped-git-commit`` (and its ``.cmd`` Windows
launcher twin) imports ``cc_invoke.route_mutation`` and spawns
``python3 -m coordinator_core.invoke ceremony.scoped_git_commit '<json>'``
as a RAW SUBPROCESS from inside an already-permitted Bash-tool process --
so the PreToolUse chain never re-inspects the real commit, and
``_has_git_commit`` (needs basename exactly ``git``),
``_has_coordinator_safe_commit`` (needed basename exactly
``coordinator-safe-commit``), and ``_has_committing_op_invoke`` (needs the
``python3 -m coordinator_core.invoke <op>`` shape at the CALLING shell
layer, which ``scoped-git-commit`` hides one process down) all missed it.
The prefilter (``_prefilter_mentions_commit``) did NOT need widening: its
strict-superset invariant already covers this shape for free, since the
literal string ``"scoped-git-commit"`` contains the substring ``"commit"``,
the same property that already covered plain ``coordinator-safe-commit``.
Fixed by generalizing ``_has_coordinator_safe_commit`` (not adding a fourth
near-duplicate matcher) into a basename-SET check against the new
``_COMMIT_HELPER_BINARY_NAMES`` set ({``coordinator-safe-commit``,
``scoped-git-commit``}), and widening the two argv0-head normalization
passes (``_ARGV0_HEAD_NORMALIZE_NAMES``,
``_WINDOWS_ARGV0_HEAD_PATH_RE``) to recognize the same set -- so bare,
``bin/``-prefixed, absolute-path, ``python3 <path>``-prefixed, and Windows
``.cmd``-suffixed spellings of ``scoped-git-commit`` are all detected the
same way ``coordinator-safe-commit`` already was. This is defense-in-depth
for any agent type NOT already confined by C1's Bash allowlist (an
allowlist-confined executor/code-reviewer is already denied this shape by
C1) -- it does not re-engineer C1's confinement.

Ported from: coordinator_core/bash_guards/nudge_subagent_scoped_commit.py
  (git-commit detection regexes; scoped-pathspec exemption removed).
Spec backlink: docs/plans/2026-07-24-g4-execute-pipeline-two-repo-rebuild.md
  chunk M4 ("claude-klabauter -- subagent-commit enforcement gate").
Spec backlink: docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md
  chunk C2 ("close the two no-obfuscation holes in block_subagent_commit").
Spec backlink: docs/plans/2026-08-01-confine-subagent-bash-by-allowlist.md
  chunk C5 ("match the scoped-git-commit trampoline in block_subagent_commit").

2026-08-02 update, part 8 (B-commit-matchers review integration -- the
BLOCKED verdict this closes): part 6's ``_COMMITTING_OP_NAMES`` shipped
incomplete -- fourteen more registered, directly-invocable committing ops
were missing (``commit.exec_bit_change``, ``ceremony.post_commit_tail``,
five more ``fleet.*``/``handoff.*`` archive/reap/prune ops, and
``handoff.ship_and_archive``, which reaches a commit only by delegating to
``fleet.archive_shipped_handoffs``'s own handler rather than calling
``archive_and_commit`` itself). ``fleet.archive_shipped_handoffs`` alone was
confirmed to have landed a real commit (``d9282543f``) this session while
absent from the set -- the exact "closed hole reopens on a different name"
pattern this file's history keeps producing (2026-07-25 prefilter/matcher
drift, 2026-07-29 coordinator-safe-commit omission, now this).

BINDING RULE: ``_COMMITTING_OP_NAMES`` must cover every op registered in
``coordinator_core/ops/_registry_map.py::OP_MODULE_MAP`` whose handler
module's source directly calls one of the four known commit-sink helpers
(``archive_and_commit(``, ``rm_and_commit(``, ``commit_scoped(``,
``commit_with_message_file(``) or shells a literal ``"commit"``/``'commit'``
argv token. ``test_subagent_commit_prefilter_and_flags.py``'s
``test_committing_op_names_covers_registry_sink_scan`` enforces this
mechanically for the direct-call case on every test run -- a new op whose
handler calls a sink directly and is not added to this set fails that test.
STATED LIMIT (do not treat this as full coverage): the check is a
single-module static source scan, so it does NOT catch an op that reaches a
commit only by delegating to ANOTHER op module's helper function
(``handoff.ship_and_archive``'s route through ``archive_shipped_handoffs.
_handle_act`` is exactly this shape, and was added here by hand, not by the
test) -- that class of gap still needs a human re-grep like the one that
found it, same as this file's history has needed three times before.

2026-08-03 update, part 9 (C1 of
``docs/plans/2026-08-03-narrow-subagent-commit-confinement-two-classes.md``
-- the precondition chunk gating that plan's C2/C3): closes a hole none of
the (a)/(b)/scoped-git-commit fixes above touch -- a Python interpreter's
``-c`` payload was never treated as executed text at all, unlike a shell's.
Confirmed live this session: ``python3 -c "import subprocess;
subprocess.run(['scoped-git-commit','-m','x'])"`` returned ``False`` from
all three of ``_has_git_commit``, ``_has_coordinator_safe_commit``, and
``_has_committing_op_invoke`` -- the same "quoted ``-c`` argument tokenizes
as one inert-looking shlex word" shape BX-13 already fixed for
``sh``/``bash``/``zsh``/``dash``/``ksh``, just never extended to Python.
Fixed in two layers, both inside ``_wrapped_shell_c_payloads`` (the SAME
shared unwrap all three matchers already re-scan through, per BX-13)
rather than authoring a fourth near-duplicate matcher (AC2; the module's
own established pattern, see part 5/7 above):

1. Widened its interpreter-head check to also recognize a Python
   interpreter head -- bare (``python3``, ``python``), versioned
   (``python3.11``, ``python3.12.1``), and interpreter-path-prefixed/
   Windows-launcher-suffixed (``/usr/bin/python3``, ``.venv/bin/python``,
   a Windows ``...\\python.exe``-suffixed path) spellings, via a new
   ``_normalized_interpreter_head`` normalization step layered on top of
   the existing ``_normalize_executable_basename`` path/suffix/case fold.
2. Confirmed empirically that step 1 ALONE is insufficient: a Python
   ``-c`` payload is SOURCE TEXT, not shell syntax, and re-tokenizing
   Python source with the shlex-based ``_tokenize_full_command`` the same
   way a genuine shell ``-c`` payload already tokenizes correctly does
   NOT reliably isolate the argv-shaped words a ``subprocess``/
   ``os.system`` call embeds -- ``subprocess.run(['scoped-git-commit',
   '-m','x'])`` tokenizes as ONE opaque word, since shlex only splits on
   whitespace and there is none between the call syntax and the quoted
   list elements. Closed by ``_python_c_payload_argv_text``: extracts
   every quoted Python string literal from the payload (in order,
   ``_PYTHON_STRING_LITERAL_RE``) and rejoins them into a synthetic,
   space-separated argv-shaped line, which is fed back through this same
   generator so the existing matchers see ``scoped-git-commit -m x`` (or
   ``git commit -m x``, or ``python3 -m coordinator_core.invoke ceremony.
   scoped_git_commit ...``) as ordinary re-scannable text.

See the NEGATIVE SPEC paragraph above for why this does not violate that
paragraph's rule against widening the interpreter set to patch holes
(a)/(b) -- it closes a different, missing-unwrap-entirely hole instead.

This closes ONLY the syntactic ``-c``/interpreter-path forms above. It does
NOT and structurally CANNOT close a string-built payload (e.g.
``subprocess.run(['g'+'it','com'+'mit'])``) -- that defeats any text-
matching seam by construction, not merely one this fix did not get to. That
residual is permanent at this layer and is tracked as AC13 of the same
plan; it is not a bug in this fix and no later executor should attempt to
close it here.

2026-08-03 update, part 10 (C3 of the same plan -- the ONE deliberate
allow-path widening this module has ever granted, DR-125 Ruling 3): adds a
narrow, route-keyed commit exemption for ``coordinator:git-commit-agent``,
ALLOW only when all three legs hold -- (1) ``payload["agent_type"]`` (never
the disk-read ``subagent_type`` backpointer leg) resolves to exactly this
type, (2) the resolved command is a genuine ``ceremony.scoped_git_commit``
invocation (via the ``coordinator_core.invoke`` spelling or the
``scoped-git-commit`` trampoline), and (3) that invocation carries an
explicit, NON-SWEEPING pathspec (``_pathspec_element_is_sweeping`` --
rejects ``.``/``./``/``:/``/``:(...)``/globs/an empty element/the repo
root/any ancestor of it/``-A``/``-a``/``--all``) -- plus a fail-closed
landing-order safety net over this plan's C4 ownership-scope check
(``_assert_paths_in_session_scope``, imported above wrapped so a partial/
out-of-order landing denies rather than allows). See ``_GIT_COMMIT_AGENT_
TYPE``'s own docstring for why this is NOT implemented via
``_ALLOWED_SUBAGENT_TYPES`` membership, and ``_git_commit_agent_may_commit``'s
docstring for the full fail-closed enumeration.

Answers, rather than merely cites, the part-6-preceding section comment's
"the scoped-pathspec `_SCOPED_RE` exemption is deliberately NOT ported --
M4 denies every subagent git commit, scoped or not": that statement was
true given ITS OWN precondition -- at the time, no subagent had a
legitimate commit route AT ALL, so a scoped-pathspec exemption had nothing
to be an exemption FOR. DR-125 Ruling 3 is precisely the event that creates
that route for the first time. The old rationale is SUPERSEDED BY A
CHANGED PRECONDITION here, not overruled -- reviving a scoped-pathspec-
gated exemption, narrowed to exactly one ``subagent_type`` and one op
(unlike the retired module's blanket, type-unaware form), is not a
regression of that 2026-07-25 retirement.

2026-08-04 update, part 11 (LEG 3 orphan-adoption fix -- the C3 exemption's
own primary workload was silently self-denying, AMENDED same-day per
staff-eng REQUIRES_CHANGES review): ``_git_commit_agent_may_commit``'s call
into ``assert_paths_in_session_scope`` (LEG 3, the ownership-scope check)
previously took ``allow_orphans`` POSITIONALLY -- i.e. never passed it at
all, so it silently took that parameter's default, ``False``. Consequence,
confirmed live this session (a ``cross-repo-memo send`` moving a draft into
``sent/`` via Bash, then a two-path commit refused with "orphan -- dirty
but claimed by no session" for BOTH paths): ``hooks/track_touched_files.
_handler`` records a ``touched.txt`` claim only for the ``Write``/``Edit``/
``MultiEdit``/``NotebookEdit`` tool matcher (DR-258, ratified permanent) --
a path written through Bash (a CLI, a generator, an engine op writing a
state file) records NO claim, so ``compute_offer`` classifies it a dirty
orphan and withholds it from ``safe_paths``, and the strict scope check
denies. ``coordinator:git-commit-agent``'s characteristic workload IS
engine-authored state (ceremony receipts, sizing objects, review trails,
frontmatter stamps, delivered memos, sweep results) -- exactly the orphan
class -- so the fleet's only dispatchable committer was denied on its
primary job and allowed only on incidental hand-edited paths.

First attempt at the fix (same-day, superseded by the paragraph below):
passed a hard-coded ``allow_orphans=True`` at this one call site. REQUIRES_
CHANGES on review: the SINK this guard gates
(``coordinator_core.ops.ceremony.scoped_git_commit._handler``) still
defaults ``include_orphans=False`` and only enables adoption on an
EXPLICIT ``--include-orphans``/``"include_orphans": true`` opt-in in the
invocation itself -- a hard-coded ``True`` here PERMITTED what the sink
still REFUSED, so the guard leg stopped enforcing the sink's own
deliberate-opt-in requirement without actually unblocking the workload
end-to-end (a caller that omitted the flag was allowed past the guard only
to be refused by the op one line later).

Fixed instead by MIRRORING the invocation's own flag: ``_resolve_git_
commit_agent_pathspec`` (and the two per-spelling extractors it calls,
``_extract_invoke_scoped_git_commit_paths`` /
``_extract_trampoline_scoped_git_commit_paths``) now return
``(paths, include_orphans)`` together, reading the SAME text each already
scans for the pathspec -- the JSON params dict's ``include_orphans`` key
for the ``coordinator_core.invoke ceremony.scoped_git_commit`` spelling
(``bool(parsed.get("include_orphans", False))``, identical to how the sink
itself reads the wire param), and the literal ``--include-orphans`` token
appearing before the ``--`` separator for the ``scoped-git-commit``
trampoline spelling (the only form ``_parse_args`` recognizes -- no
``=value`` variant). ``_git_commit_agent_may_commit`` then calls
``assert_paths_in_session_scope(..., allow_orphans=include_orphans)`` --
KEYWORD-form still, for the same reason as before (``allow_orphans`` is
keyword-only on ``assert_paths_in_session_scope``'s own signature, so a
future positional-style call cannot silently regress to the strict default
without a ``TypeError``), but mirroring the invocation rather than a
standing ``True``. An invocation that omits the flag now behaves EXACTLY
as it did before ANY of today's changes -- strict, orphans denied --
because ``include_orphans`` resolves to ``False`` by construction when the
flag/key is absent, the same default both this helper and the sink already
had. This makes the grant an explicit, auditable act recorded in the
command text itself (the SAME trust level
``coordinator/bin/scoped-git-commit --include-orphans`` already grants a
human operator by hand for exactly this refusal -- its own help text calls
it "a deliberate response to that refusal") rather than a standing posture
this guard alone would have imposed on every invocation regardless of
whether the caller asked for it. Every OTHER caller of ``assert_paths_in_
session_scope`` (and that helper's own default) is UNCHANGED by this fix --
this is still a single-call-site change, not a widening of the helper's
default behaviour.

What the widening actually relaxes, stated plainly (F3, staff-eng review --
the original text above enumerated four things it does NOT relax and was
silent on what it DOES): (a) a DEAD peer session's uncommitted work becomes
adoptable -- ``compute_scope`` drops a non-live peer's claims from its
ownership math entirely, so a path a dead peer once touched but never
committed falls through to ``orphans`` and is adoptable under this flag
exactly like a path nobody ever claimed; (b) the liveness-enumeration
under-report residual ``scope_report._ORPHAN_ADOPTION_ENABLED``'s own
docstring already documents is reachable through THIS leg -- a live peer
session mis-classified as dead (an enumeration gap, not a bug in the
liveness check itself) has its in-flight work adopted here with no
degradation signal distinguishing that case from a genuinely-dead peer.
Relevant lesson: ``2026-07-31-a-liveness-keyed-rescue-reattributes-a-live-
sessions-work-7b1c4e02`` -- the harm named there is misattribution, not
data loss, which is the same shape this residual carries. Today's fix
(mirroring the invocation's own flag rather than a standing ``True``)
substantially narrows this: adoption is now an explicit, auditable act
recorded in the command text of one dispatched call, not a standing
posture every ``coordinator:git-commit-agent`` invocation carried
regardless of whether it needed to -- but the underlying liveness-
enumeration residual is unchanged by that narrowing and still applies
whenever the flag IS carried.

DR-246 (F4, staff-eng review): ``docs/plans/2026-07-31-unclaimed-dirty-
file-adoption.md`` chunk C1a deliberately STOPPED always-on unclaimed-
dirty-file adoption -- the sanctioned re-entry was a per-invocation,
explicit opt-in, not a standing default. This C3 exemption's orphan
adoption is that re-entry, not a regression of C1a's retirement: adoption
here is (1) reached only through the one narrow, route-keyed
``coordinator:git-commit-agent`` exemption (LEG 1, strict ``agent_type``
match -- every other subagent type is untouched), (2) gated on an explicit,
non-sweeping, EM-supplied pathspec (LEG 3's sweeping-pathspec rejection
runs first, unconditionally, before orphan adoption is ever consulted --
see below), and (3), as of today's fix, gated on the invocation's OWN
``--include-orphans``/``include_orphans: true`` opt-in rather than a
standing posture -- the same per-invocation shape C1a's sanctioned re-entry
named, mirrored at the guard leg instead of only at the human-operator CLI.
A sweeping, blanket, always-on adoption (the shape C1a stopped) is what
this is NOT: a caller cannot get orphan adoption without ALSO naming an
explicit, non-sweeping pathspec and, now, explicitly asking for it in the
same call.

Explicitly re-verified, not merely assumed, against ``assert_paths_in_
session_scope``'s own docstring before this change landed: (1)
``allow_orphans=True`` NEVER relaxes the peer-claimed case -- a path
claimed by a LIVE peer session still denies regardless of the flag
(incident 62e9a1f73); pinned by
``test_git_commit_agent_peer_claimed_path_still_denies_with_orphans_
allowed`` in ``test_block_subagent_commit.py``. This is the property that
makes the fix safe. (2) ``compute_offer``'s ``orphans`` is
``result.orphans`` MINUS ``skipped_paths`` (staff-eng F1), so a
withheld-for-another-reason path cannot resurface as an adoptable orphan --
this property is exercised only indirectly here (through the real-wiring
``TestRealOwnershipScopeWiring`` tests in ``test_block_subagent_commit.py``
and, at the helper layer itself, ``coordinator_core/ops/session/tests/
test_scope_report.py``); NEITHER file has a dedicated regression pinning
the skipped-and-orphan-candidate-overlap scenario directly, so this is a
documented coverage gap (F2, staff-eng review), not a pinned property --
flagged for a follow-up, not overstated as tested here. (3) On an
indeterminate call (agent-race overlap), ``orphans`` is returned EMPTY
OUTRIGHT, so the flag grants nothing and the guard still denies -- a
fail-closed degradation to pre-fix behaviour, correct as-is; pinned by
``test_ownership_leg_denial_names_indeterminate_classification``. (4)
Adoption additionally requires ``_session_has_positive_evidence``; when
that fails, the deny reason says ``include_orphans ignored``, and that
path still denies. As of part 12 (SC-DR-022) this scenario is unreachable
on the dispatched-agent path BY CONSTRUCTION -- the flag is now refused
before the ownership helper is ever consulted, so this classification
cannot be threaded back through here; the surviving claim is the earlier
denial itself, pinned by ``test_include_orphans_from_an_agent_denies_
before_the_ownership_leg``. ``include_orphans ignored`` remains live for
OPERATOR invocations at the sink, `scope_report`'s own contract to pin.
LEG 3's
existing sweeping-pathspec rejection (``_pathspec_element_is_sweeping`` --
``.``, ``-A``, globs, repo-root/ancestor paths) is UNCHANGED and still runs,
unconditionally, BEFORE the ownership-scope call (see
``_git_commit_agent_may_commit``'s own code above) -- it is what bounds the
blast radius to explicitly-named paths; orphan adoption without it would be
a sweeping-commit hole, and this fix does not touch that ordering; pinned
by ``test_git_commit_agent_sweeping_element_denies_before_ownership_leg_
even_reached`` (a call-recording spy, not a static-message coincidence --
see that test's own docstring for the mutation this closes).

2026-08-04 update, part 12 (SC-DR-022, claude-central-em -- a dispatched
``coordinator:git-commit-agent`` may never pass ``--include-orphans``):
supersedes part 11's MIRRORING fix for the dispatched-agent path ONLY, and
does not revive the hard-coded ``True`` that mirroring replaced -- the
correction runs the other way, to a hard deny. Part 11's mirroring
rationale is still CORRECT for what it defended against (a guard leg
granting unilaterally what the sink still refused); it was simply never a
judgment about WHO was asking. ``_git_commit_agent_may_commit`` now denies
outright, at a new leg (``_LEG_AGENT_ORPHAN_ADOPTION``), the moment
``_resolve_git_commit_agent_pathspec`` reports the invocation carried
``--include-orphans``/``include_orphans: true`` -- BEFORE ``assert_paths_
in_session_scope`` (LEG 3, the ownership-scope check) is ever called. The
call that follows, when this leg does not deny, now passes
``allow_orphans=False`` UNCONDITIONALLY -- the mirrored value part 11
introduced is gone; the only invocation shape that could have made it
``True`` is refused above, so passing the invocation's own flag through
would be dead code. Rationale, restated: adoption is safe for a human
operator because the adopter WAS THERE and knows what it just wrote --
provenance, not timing. A dispatched committer holds no such provenance by
construction; its scope arrives WITH THE BRIEF, not from having authored
the files. An agent adopting an orphan would be deriving scope from a
denial, the sweeping defect one level removed, laundered through a
compliant committer. See the ``if include_orphans:`` block inline in
``_git_commit_agent_may_commit`` below for the full inline rationale this
entry summarizes, and ``test_include_orphans_from_an_agent_denies_before_
the_ownership_leg`` for the pinning test.

2026-08-04 update, part 13 (the C1 payload-reconstruction leg denies
correctly but MESSAGES wrongly -- dispatched-executor report): part 9's
``_python_c_payload_argv_text`` reconstruction is working as designed, and
its verdict is not revisited here. Its cost is that a purely READ-ONLY
command naming a commit helper as DATA denies identically to one invoking
it -- confirmed live: ``python3 -c "import ast; ast.parse(open('coordinator/
bin/scoped-git-commit').read())"`` DENIES, while ``python3 -m py_compile
coordinator_core/ops/ceremony/scoped_git_commit.py`` ALLOWS. The message the
first command received was ``_deny_reason``'s generic final return ("Finish
your edits and report to the EM"), which names an action that resolves
nothing for a caller who never tried to commit -- and since the reconstruction
discards all Python syntax between the literals BY CONSTRUCTION, no argv
re-spelling of a read-only command containing that path can ever pass. That
is the SAME defect the 2026-08-03 correction above ``_GIT_COMMIT_AGENT_DENY_
REASON`` fixed for the ownership leg ("re-tries argv variants against a scope
failure that no argv change can fix"), and it is fixed the same way, in the
same three moves: tag the leg where it is KNOWN (``_wrapped_shell_c_payload_
legs``), thread the tag out through the matchers' ``legs`` collector
(``_record_payload_leg``) into ``check()``, and select on it in
``_deny_reason`` -- never re-derive it there by re-scanning the command.
MESSAGE SELECTION ONLY: every command that denied before this entry still
denies, every command that allowed still allows (pinned by
``test_payload_leg_threading_changes_no_verdict``).

2026-08-04 update, part 14 (the read-only false positive part 13 could only
MESSAGE about is now CLOSED for the provably-inert case): part 13 correctly
observed that the reconstruction leg cannot tell a literal that NAMES a
commit helper from a call that INVOKES it, and improved the message rather
than the verdict. Part 14 supplies the missing discriminator, at the only
place one exists: a Python ``-c`` payload cannot spawn ANYTHING without an
execution sink, so a payload with no sink cannot be a disguised commit no
matter which paths its string literals name. ``python3 -c "import ast;
ast.parse(open('coordinator/bin/scoped-git-commit').read())"`` now ALLOWS;
``python3 -c "import subprocess; subprocess.run(['coordinator/bin/scoped-
git-commit','-m','x'])"`` still DENIES.

The discriminator is ``_python_c_payload_is_provably_inert`` -- an ALLOWLIST
walk over ``ast.parse``'s tree that returns False on anything it does not
explicitly recognize (unparseable source, an import root outside
``_INERT_PAYLOAD_IMPORT_ROOTS``, an AST node type outside
``_INERT_PAYLOAD_NODE_TYPES``, a call target or attribute name outside
``_INERT_SAFE_CALLABLE_NAMES``, an ``open`` whose mode is not a
provably-read-only literal). See that function's own NEGATIVE SPEC for the
standing rule: a bypass here always means something UNRECOGNIZED was
cleared, so the fix is to narrow the allowlist, NEVER to extend a denylist.

WHAT IT PROVES, corrected by part 17 below because the claim as originally
written here was FALSE and a bypass turned on exactly that gap: it proves
the payload NAMES NOTHING outside a closed set of pure operations. It does
not prove "no execution sink is reachable" in general -- the first shape of
this checker tried to, by clearing every attribute absent from a forbidden
list, and that is a denylist however it is documented.

Scope, stated narrowly because it is the whole safety argument: inertness
suppresses ONLY the string-literal reconstruction leg for that one payload.
The payload is still yielded and still re-scanned as text; the recursive
unwrap beneath it still runs; all three matchers still run on the payload
and on the outer command; a shell ``-c`` payload is untouched (it is not
Python source and never reaches this check). ``_deny_reason``'s part-13
payload-leg message is unchanged and still fires for a payload that is not
provably inert -- including genuinely read-only shapes this allowlist
cannot clear, e.g. one reading the same file through ``pathlib``.

2026-08-04 update, part 15 (the reconstruction leg's ARGV0-POSITION bypass,
confirmed live at HEAD): parts 13 and 14 both recorded, as a documented
residual, that a slice of genuinely-committing Python ``-c`` payloads never
denied at all -- ``_python_c_payload_argv_text`` joins the payload's string
literals in APPEARANCE order, and the three matchers resolve binary identity
at ARGV0/segment-head, so a sink whose OWN literals come first pushes the
helper out of position 0. ``python3 -c "__import__('os').system('coordinator/
bin/scoped-git-commit -m x')"`` reconstructs to ``os coordinator/bin/scoped-
git-commit -m x`` and ALLOWED, as did the ``getattr(__import__('os'),
'system')(...)`` and ``eval("...")`` spellings, while the plain ``import os;
os.system(...)`` spelling correctly DENIED. That is a bypass of this gate's
core promise, not a cosmetic gap, and it is closed here.

The fix is `_has_reconstructed_commit_identity` -- a FOURTH matcher, wired
into ``check()`` the same way as the other three, which consumes ONLY
payloads tagged ``_PAYLOAD_LEG_PYTHON_STRING_LITERALS`` and walks EVERY token
of that synthetic line through the same shared, boundary-anchored
``token_matches_binary`` machinery the argv0 path already uses (see
`_tokens_reach_commit_identity_anywhere`). The reconstruction is not a
command line -- nothing executes it, and argv0 semantics do not meaningfully
apply to a line whose word order is an artifact of where quotes fell in
Python source -- so position-sensitivity there was the defect. Boundary
anchoring is NOT relaxed with it: ``evil-scoped-git-commit`` still does not
match, at any position.

Scope, stated as narrowly as part 14's: real shell segments, real ``sh -c``
payloads, and the outer command keep their argv0-anchored semantics exactly.
Widening those would be a different and much riskier change -- a trailing
mention of a helper in a real command line is an argument, not an invocation.

Parts 14 and 15 are a designed PAIR, and neither may be removed alone.
Matching at any position necessarily widens what the reconstruction flags,
which alone would deny read-only commands that merely NAME a helper in a
string literal; part 14's inertness check absorbs exactly that, because a
provably-inert payload never reaches the reconstruction leg and so never
reaches this matcher. Ordering is therefore load-bearing: inert check first
(skip the leg entirely), any-position matching for everything else.

2026-08-05 update, part 16 (the STRING-ASSEMBLY residual -- a name that was
never contiguous text): parts 13-15 all recover text the payload CONTAINS.
Part 15's own closing note stated the limit precisely -- it is
position-complete, not obfuscation-complete -- and named what still passed:
``os.system('scoped-git'+'-commit -m x')``,
``os.system(''.join(map(chr,[...])))``,
``exec(__import__('base64').b64decode('...'))``, and
``os.system(os.environ['X'])``. All four ALLOWED at HEAD. Closed here by two
mechanisms, in this order:

MECHANISM 1 -- constant folding (`_fold_python_c_payload`, the folder's
entrypoint). Statically evaluate the payload's compile-time-constant
expressions (``+`` concatenation, ``%``/``str.format``/f-strings, ``*``
repetition, slicing, ``''.join``, ``chr``/``ord``/``bytes``/``map`` unrolls,
``encode``/``decode``, ``bytes.fromhex``, the ``base64`` codecs, and
module-level single-assignment names) and feed the folded values through the
SAME boundary-anchored identity matching part 15 built
(`_has_folded_commit_identity` -> `_tokens_reach_commit_identity_anywhere`).
The folder NEVER EXECUTES PAYLOAD TEXT -- no ``eval``/``exec``/``compile``/
``ast.literal_eval`` over attacker source, no import of what the payload
names, no call to a payload-supplied callable -- and its bounds are
load-bearing security properties rather than tuning knobs, because an
unbounded folder inside a PreToolUse hook is itself a denial of service on
every Bash call. Those bounds are scoped PER ``python3 -c`` PAYLOAD, not per
Bash call; what caps the per-call cost is the tokenizer's 64 KiB command
ceiling, since each extra chained segment has to be spelled out in the
command text (measured at that ceiling: 329 segments, 1.6 s dispatch). The
constant block on `_MAX_FOLDED_VALUE_LEN` carries the numbers.
Every bound fails CLOSED into mechanism 2's "unresolved".

MECHANISM 2 -- opaque-sink refusal (`_has_opaque_execution_sink`). After
folding, a payload that is NOT provably inert and hands an execution sink an
argument that did not resolve is DENIED: not a claim that it is a commit,
but that we cannot know what it runs, and this seam does not start unknown
programs on a subagent's behalf. Sink identity is derived BY SUBTRACTION
from part 14's own name sets (`_EXECUTION_SINK_ATTRIBUTE_NAMES` et al), so a
name added there for reaching an execution sink appears here automatically
rather than in a second list that can drift.

Part 14's inertness check gates BOTH, exactly as it gates part 15: an inert
payload cannot run what its strings spell, so folding them could only
manufacture false positives.

Pre-filter, widened for the first time (see `_prefilter_mentions_commit`'s
LESSON rule): both mechanisms match text that is NOT present in ``cmd`` -- a
``chr``-built name contains no ``commit`` substring, and an opaque argument
names nothing -- so a commit-substring filter stopped being a superset.
`_may_carry_python_c_payload` restores it, and the three older matchers are
provably unmoved by the widening (each still requires the literal
``commit``/op-name text to fire).

Blast radius, MEASURED at landing rather than asserted, on two corpora
pinned in ``tests/test_python_c_constant_folding.py``: 19 of 22 adversarial
assembly/obfuscation shapes move ALLOW -> DENY (the three that do not are
correct: ``evil-scoped-git-commit`` still fails boundary anchoring, and two
fold-bomb rows deny through mechanism 2 rather than through folding), and 4
of 24 realistic dispatched-agent commands move -- all four passing a
sink a computed argument (``subprocess.run([sys.executable,...])``,
``cwd=``/``env=`` from a call). Mechanism 1 alone moves ZERO realistic
commands. That split is the operating envelope for any future widening.

2026-08-05 update, part 17 (the INERT CERTIFIER WAS UNSOUND -- confirmed P0,
found in staff-engineer review of parts 14-16 before they shipped): a payload
could certify ``inert=True``, and therefore skip parts 13, 15 AND 16 in one
move, while reaching a real execution primitive. Reproduced at the guard
level, ``GUARD=ALLOW``, process spawned:

    import typing, dataclasses
    s = ''.join([chr(c) for c in [<ord-encoded os.system(...) program>]])
    C = dataclasses.make_dataclass('C', [('a', s)])
    typing.get_type_hints(C)

THREE INDEPENDENT DEFECTS COMPOSED, and each is closed here, because closing
any one alone leaves the hole open:

DEFECT 1 -- the certifier was a DENYLIST wearing an allowlist's docstring.
``_python_c_payload_is_provably_inert`` cleared any attribute not named in
``_NON_INERT_ATTRIBUTE_NAMES``, so every callable nobody had thought of --
``typing.get_type_hints`` among them -- was inert BY DEFAULT. That is the
precise inversion the function's own NEGATIVE SPEC forbids, and no amount of
adding names to the forbidden set fixes the direction. Inverted for real:
``_INERT_SAFE_CALLABLE_NAMES`` is now the primary mechanism, every ``Call``
target and every ``Attribute`` name must be a member, and unrecognised means
NOT inert. Four import roots were dropped as unclearable
(``typing``/``dataclasses`` evaluate annotations; ``string`` reflects through
``Formatter``; ``functools`` applies caller-supplied callables) -- see
``_INERT_PAYLOAD_IMPORT_ROOTS`` for the per-root audit.

DEFECT 2 -- annotation evaluation was not modelled as an execution sink.
``typing.get_type_hints``, ``typing.ForwardRef._evaluate`` and
``dataclasses.make_dataclass`` evaluate strings as code at runtime; they are
sinks in exactly the sense ``eval``/``exec`` are, and part 16's
opaque-argument refusal never fired on them. ``_ANNOTATION_EVAL_ATTRIBUTE_
NAMES`` folds the whole family into ``_NON_INERT_ATTRIBUTE_NAMES``, which is
the single-sourcing part 16 already relies on: its sink sets are derived from
that set by subtraction, so they inherit the family without a second list.

DEFECT 3 -- the comprehension channel. The inert leg PERMITS comprehensions
while the folder does not model them, so a comprehension was an un-folded
string channel into any callable: ``''.join([chr(c) for c in [...]])``
resolved to nothing, and neither reconstruction nor folding ever saw the
assembled name. ``_fold_expr_uncached`` now refuses the four comprehension
node types EXPLICITLY rather than by fallthrough, which under mechanism 2 is
UNRESOLVED and therefore DENY. Folding comprehensions was considered and
rejected: it means running attacker-authored iteration in a PreToolUse hook.

Blast radius, MEASURED against every corpus row in
``tests/test_python_c_inert_payload_exemption.py``,
``tests/test_python_c_constant_folding.py`` and the sibling parity matrix,
baseline-vs-live in one process: ALLOW -> DENY = 2 (the weaponized composite
above, and ``typing.get_type_hints(int)``); DENY -> ALLOW = 0. The
legitimate-read corpus part 14 exists for -- ``ast.parse(open(h).read())``,
``open(h).read()``, ``json.loads(open(h).read())``, the ``difflib`` and
``hashlib`` reads -- moves ZERO rows, which is the constraint the callable
allowlist was sized against: ``split``/``join``/``sha256``/``hexdigest``/
``unified_diff`` are on it because that corpus needs them, not because the
list wanted rounding out.

2026-08-05 update, part 18 (SLOT 0 IS THE PROGRAM SLOT -- the residual part 16
measured, part 17 re-confirmed live, and this part closes). Part 16 exempted
the whole argv-vector sink family from mechanism 2's opaque-argument refusal
on measured evidence, and the exemption covered slot 0 with the rest. That is
the wrong shape: an unresolved ARGUMENT to a known program is honest usage, an
unresolved PROGRAM is exactly the "we cannot know what this runs" condition
mechanism 2 exists for. Confirmed live, ``GUARD=ALLOW``:

    python3 -c "import subprocess; subprocess.run(
        [''.join([chr(c) for c in [<ords of the helper>]]), '-m', 'x'])"

Part 17 closed the comprehension channel for FOLDING, but the assembled name
then landed in an argv vector whose exempt slot 0 nobody checked -- so the
payload allowed anyway.

THE FIX, in one sentence: for an argv-vector sink, slot 0 must resolve to a
constant, or the call denies through the same mechanism-2 path a
whole-command-text sink uses; slots 1..n keep part 16's exemption unchanged.
``_argv_program_slot`` resolves it structurally rather than by folding the
whole vector, so ``['python3'] + rest`` and ``['pytest', sys.argv[1]]`` still
name their programs and still allow, while a bare name, a call result, a
comprehension, a ``[*parts]`` star-args vector and an ``os.environ``-sourced
slot 0 do not and deny. Three shapes are explicitly preserved: a program
passed by keyword, the ``shell=True`` string form, and a non-list first
argument (whole-command-text, judged on content by mechanism 1). (The FIRST
of those three was a defect, not a decision, and part 19 below closes it: the
program slot is a property of the argument, not of how it was passed. The
other two stand.)

BLAST RADIUS, MEASURED baseline-vs-live in one process across every corpus row
in ``tests/test_python_c_constant_folding.py``,
``tests/test_python_c_inert_payload_exemption.py`` and the sibling parity
matrix: ALLOW -> DENY = 2, both ``subprocess.run([sys.executable, ...])``
(``pytest-argv-file``, ``py-compile-guard``); DENY -> ALLOW = 0; the
legitimate-read corpus moves ZERO. Two was the price accepted for this
closure -- see ``_ARGV_PROGRAM_SLOT_SINK_ATTRIBUTES`` for why those two are
cheap and what re-measuring means for a future editor who grows the number.

2026-08-05 update, part 19 (SINK IDENTIFICATION, not slot resolution -- the
four residuals part 18 measured and left, each confirmed ``GUARD=ALLOW`` at
HEAD). Parts 16-18 all asked "what does this sink receive?"; these four never
got that far, because the guard did not recognise the call as a sink at all:

    python3 -c "from subprocess import run as r; import sys; r(sys.argv[1:])"
    python3 -c "import os; os.posix_spawn(os.environ['X'],[],{})"
    python3 -c "import subprocess,sys; subprocess.run(args=sys.argv[1:])"
    python3 -c "import subprocess,os; subprocess.run(['python3','-c',os.environ['X']])"

GAP 1 -- SINK IDENTITY WAS THE SPELLED NAME. Every leg matched on the name as
written, so ``from subprocess import run as r`` renamed the primitive out of
reach. `_payload_bindings` resolves the payload's own ``import``/``from``
bindings (plus a module-level ``run = subprocess.run`` alias) into canonical
dotted targets, and `_resolved_call_identity` feeds BOTH identities -- as
spelled and as resolved -- to the sink sets. One rule covers ``from os import
system``, ``import subprocess as sp`` and ``from os import posix_spawn as
ps``. A rebinding it CANNOT resolve (``f = getattr(os, sys.argv[1])``) fails
closed: calling such a name is a sink of unknown identity
(`_call_target_is_opaque_rebinding`), judged through the whole-command-text
door, so an unresolved argument denies and a spelled-out one is still judged
on content.

GAP 2 -- ``("spawn", "exec")`` MATCHED WITH ``startswith``. ``posix_spawn``
does not START WITH either root, so a real execution primitive was invisible.
Adding two names would have fixed two spellings and left the class open, so
the MATCHING is fixed instead: `_name_is_process_creation` anchors each root
at a name-segment boundary (start, or after ``_``), which is what "the
``spawn`` family" always meant and covers any future ``<prefix>_spawn``. The
audited ``os`` process-creation surface is pinned as a set by
``tests/test_python_c_inert_payload_exemption.py``.

GAP 3 -- THE PROGRAM SLOT IS A PROPERTY OF THE ARGUMENT, NOT OF HOW IT WAS
PASSED. Part 18 read slot 0 out of the first POSITIONAL argument and recorded
``subprocess.run(args=...)`` as preserved behaviour; it is the same call.
`_argv_vector_argument` locates the vector positionally or by ``args=``, and
a ``**kwargs`` splat hides the slot entirely and is unknown by construction.
The ``shell=True`` string form is untouched.

GAP 4 -- A KNOWN PROGRAM THAT IS AN INTERPRETER IS NOT A KNOWN PROGRAM.
``subprocess.run(['python3','-c', <unresolved>])`` has a resolvable slot 0
and starts an interpreter running a program nobody can name.
`_argv_nested_interpreter_payload_is_unknown` refuses that, reusing the
EXISTING interpreter recognition (`_normalized_interpreter_head` over
`_PYTHON_INTERPRETER_NAMES`/`_C_FLAG_SHELL_INTERPRETERS`, so versioned,
path-prefixed and ``.exe`` spellings come along). A RESOLVABLE nested ``-c``
payload is not refused but RECURSED into through this same mechanism-2 pass,
depth-bounded and sharing the caller's fold budget.

BLAST RADIUS, MEASURED baseline-vs-live in one process over every corpus row
in ``tests/test_python_c_constant_folding.py``,
``tests/test_python_c_inert_payload_exemption.py`` and the sibling parity
matrix: ALLOW -> DENY = 0 and DENY -> ALLOW = 0 on the EXISTING corpora --
the realistic-usage corpus does not move at all, so part 18's priced two
stays two, and the legitimate-read corpus again moves ZERO. Everything this
part moves is a row it introduces (see ``_PART19_*`` in the folding corpus).
That is the operating envelope: sink IDENTIFICATION could be fixed without
buying any of the false positives argument-level widening costs, and a future
edit that starts moving realistic rows is a different trade needing the same
measurement rather than an argument.

Part 19 left one residual, CLOSED by part 20 below: ``subprocess.getoutput``
/ ``getstatusoutput`` run a shell command line and were in no sink set, so
``subprocess.getoutput(os.environ['X'])`` ALLOWED.

2026-08-05 update, part 20 (the residual above, closed -- and the shape it
needed). Both payloads were confirmed ``GUARD=ALLOW`` at part 19's HEAD:

    python3 -c "import subprocess,os; print(subprocess.getoutput(os.environ['X']))"
    python3 -c "import subprocess,os; print(subprocess.getstatusoutput(os.environ['X']))"

They are whole-command-text sinks in exactly the sense ``os.system`` is, and
they fell between the two doors: ``subprocess`` is subtracted from
`_OPAQUE_PROGRAM_SINK_MODULE_NAMES` because its surface is the argv-vector
family, and these two are not in that family, so neither door claimed them.

What part 19 could not do, and part 20 can, is admit them SAFELY. Every other
sink leg is identity-blind about the receiver -- affordable for ``system`` or
``execv``, not for ``getoutput``, a name honest code hangs off loggers and
test helpers. Part 19's `_payload_bindings` supplies the missing half: these
two are keyed on the CANONICAL DOTTED TARGET
(`_call_is_receiver_qualified_shell_sink` over
`_RECEIVER_QUALIFIED_SHELL_SINK_TARGETS`), so ``import subprocess as sp;
sp.getoutput(x)`` and ``from subprocess import getoutput as go; go(x)`` are
the same sink, while ``harness.getoutput(sys.argv[1])`` -- an unresolved or
non-``subprocess`` receiver -- is NOT a sink and still ALLOWS. That
receiver-qualified shape is the deliverable; the two names are the occasion
for it. The ``subprocess`` callable surface was audited rather than sampled
(see the constant): no third shell-out sibling remains uncovered.

BLAST RADIUS, MEASURED baseline-vs-live in one process over every corpus row
in ``tests/test_python_c_constant_folding.py``,
``tests/test_python_c_inert_payload_exemption.py`` and the sibling parity
matrix: ALLOW -> DENY = 0 and DENY -> ALLOW = 0 on the EXISTING corpora. The
realistic-usage corpus moves ZERO and the legitimate-read corpus moves ZERO --
which is precisely what receiver-qualification buys: a name-only admission of
``getoutput`` is what would have moved them. Everything this part moves is a
row it introduces (see ``_PART20_*`` in the folding corpus).

2026-08-05 update, part 21 (the RESOLVER parts 19 and 20 are built on RESOLVED
TOWARD SAFE -- a P0 found by adversarial review, plus the three claims that
review found overstated and one measured over-match).

THE P0. `_payload_bindings` wrote ``imports[name] = target`` with
last-``ast.walk``-visit-wins. A binding nested in a ``def`` body, a
``try/except ImportError`` fallback or a never-taken ``if`` is visited AFTER
its module-level sibling, so it OVERWROTE it and an aliased sink resolved to a
benign canonical target. Confirmed ALLOW at part 20's HEAD:

    import os
    from subprocess import run as r
    def f():
        from json import loads as r      # never called, no runtime effect
        return r
    r([os.environ['X'], 'commit'])       # bindings said r == json.loads

One defect, five re-opened legs: all four part-19 identification legs and part
20's receiver qualification read the same map. The fix is a ``Dict[str,
Set[str]]`` whose identities are UNIONED and read disjunctively -- if ANY
identity is a sink, it is a sink -- which is the discipline
`_resolved_call_identity` already applied to the spelled name. Union rather
than "move a multi-bound name to ``opaque``" on a measured basis: ``opaque``
routes to the harshest door unconditionally and would deny the honest
``try: import <fast> / except ImportError: import <stdlib>`` shape. The walk
still covers the WHOLE tree deliberately -- a top-level ``try``-guarded import
is honest usage, and restricting to ``tree.body`` would buy false positives.

THE THREE OVERSTATED CLAIMS, each a docstring outrunning its code, which is
exactly what part 17 exists to prevent:

* ``open`` was mode-checked only at ``Name`` position, so ``c.open(p,'w')``
  -- reachable as ``from json import codecs as c`` -- WROTE under an inert
  certificate. `_open_call_is_read_only` now applies wherever the call name
  resolves to ``open``.
* `_INERT_SAFE_CALLABLE_NAMES` claimed a bare reference to any unlisted
  callable was refused at ``Name``/``Attribute`` position. It was not:
  ``sorted = type`` and ``from ast import literal_eval as z; get = z;
  get('1+1')`` both certified inert. `_inert_load_name_is_cleared` and
  `_import_binding_launders_a_cleared_name` now enforce it, and the docstrings
  state the residual that remains rather than a closure they do not prove.
* ``ImportFrom`` never consulted `_NON_INERT_MODULE_NAMES`, so
  ``from ast import sys as z`` bound a module that set exists to refuse.

THE OVER-MATCH. `_PROCESS_CREATION_NAME_RE` took ``exec`` as a family root, so
``cursor.execute(sql)`` -- the universal DB idiom -- routed the harshest door
and DENIED. ``exec`` is dropped as a root and the eight ``os.exec*`` members
are enumerated (`_OS_EXEC_FAMILY_NAMES`) exactly as ``system``/``popen``
already were.

BLAST RADIUS, MEASURED HEAD-vs-live in one process: adversarial 113 rows,
realistic-usage 42, legitimate-read 9, parity matrix 15 -- ALLOW -> DENY = 0
and DENY -> ALLOW = 0 on ALL FOUR. The only movement is on this part's own
rows: seven shadowed-alias payloads and ``sorted = type`` move ALLOW -> DENY,
and four ``execute``-shaped rows (``cursor.execute``, ``cursor.executemany``,
``conn.exec_driver_sql``, ``db.db_exec``) move DENY -> ALLOW as the priced
intent of the root drop. ``run``/``call`` were deliberately NOT
receiver-qualified: part 20's qualification ADDS identity, qualifying those
would REMOVE it, and a subtractive rule leaning on a resolver this part just
had to fix would hand over the argv-vector family.
"""

from __future__ import annotations

import ast
import json
import posixpath
import re
import string
from functools import lru_cache
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

from coordinator_core.bash_guards._command_tokenizer import (
    _skip_wrapper_own_argv as _skip_wrapper_own_argv_for_commit,
    normalize_executable_basename as _normalize_executable_basename,
    segments_from_tokens_simple as _segments_from_tokens,
    token_matches_binary as _token_matches_binary,
    tokenize_full_command as _tokenize_full_command,
)
from coordinator_core.bash_guards._helpers import (
    resolve_git_root,
    _read_backpointer_subagent_type,
    emit_kind_resolution_failure_signal,
)
from coordinator_core.write_guards.block_subagent_plan_body_write import (
    _resolve_subagent_identity,
)

# C3 (2026-08-03-narrow-subagent-commit-confinement-two-classes.md) landing-
# order safety: C4's ownership-scope check (`assert_paths_in_session_scope`)
# is authored (C4a) but its own wiring chunk lands in a LATER commit than
# this one. `coordinator_core.ops.session.scope_report` transitively imports
# the `coordinator_core.ops` package (and, through it,
# `coordinator_core.hooks` -- see `test_bash_guards_avoid_hooks_package.py`),
# so it is deliberately NOT imported at module scope: this guard runs on
# EVERY Bash call, and the predicate below only matters for the rare
# `coordinator:git-commit-agent` commit-shaped command, so a module-level
# import would put a ~19-module hot-path cost on every other Bash
# invocation. `_import_assert_paths_in_session_scope` below performs the
# SAME import, lazily, at call time inside `_git_commit_agent_may_commit`,
# wrapped so an ImportError (or any other import-time exception) resolves to
# `None` rather than propagating -- a partial/out-of-order landing of this
# multi-chunk plan stays DENY-biased, not allow-biased. Never shell this out
# as the `session.scope_report` op -- see that module's own docstring for
# why an in-process import is required on a guard's fail-closed seam.
def _import_assert_paths_in_session_scope():
    try:
        from coordinator_core.ops.session.scope_report import (
            assert_paths_in_session_scope,
        )
    except Exception:
        return None
    return assert_paths_in_session_scope

CLASS = "hard-deny"
MATCHERS = ("Bash",)
PRIORITY = 40

#: The one narrow, route-keyed commit exemption this module grants (DR-125
#: Ruling 3, C3 of the plan named above) -- resolved STRICTLY from the
#: harness-supplied `payload["agent_type"]` leg, never the disk-read
#: `_read_backpointer_subagent_type` leg (see `_git_commit_agent_may_commit`'s
#: docstring and the module docstring's C3 entry for why). Deliberately NOT
#: added to `_ALLOWED_SUBAGENT_TYPES` below -- that set exempts a type on
#: EVERY command shape (including a bare `git add -A`), which DR-125 Ruling 3
#: never authorized; this constant instead keys a narrow, command-shape-aware
#: predicate applied only inside the already-resolved-and-matched branch.
_GIT_COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"

#: Forward-compatibility hook (the Director of Engineering Finding D6) -- still empty. Its original
#: docstring described it as reserved for "a future named-Opus member";
#: `coordinator:git-commit-agent` (C3 above) is the first live subagent
#: commit route this module grants, and it is Sonnet, not Opus, and is NOT a
#: member of this set (see `_GIT_COMMIT_AGENT_TYPE`'s own docstring for why
#: type-membership alone is the wrong shape for it). Model tier is
#: unobservable at this PreToolUse seam regardless (only agent_id/agent_type/
#: subagent_type are visible), so the Opus framing was never mechanically
#: enforced -- reaffirmed here, not retracted: this set remains reserved for
#: a future type that legitimately needs an UNCONDITIONAL exemption (every
#: command shape, not just a scoped commit route), which is a materially
#: different and stronger grant than C3's, and none exists today.
_ALLOWED_SUBAGENT_TYPES: frozenset = frozenset()

# --- Detection: git-commit invocation (with or without arbitrary global
#     options) plus the `coordinator-safe-commit` helper. Conceptually
#     descended from nudge_subagent_scoped_commit.py's `_GIT_COMMIT_RE` (the
#     scoped-pathspec `_SCOPED_RE` exemption is deliberately NOT ported --
#     M4 denies every subagent git commit, scoped or not), then HARDENED
#     2026-07-25 from a fixed-shape regex to a token walk after a confirmed
#     bypass report (see the module docstring's "Bypass fix" paragraph). ---

#: git global options that consume a SEPARATE following token as their
#: argument (``-C <path>``, ``-c <name>=<value>``). ``-C`` and ``-c`` are
#: DIFFERENT git options (repo-root override vs. config override) -- this
#: set is intentionally case-sensitive; lowercasing to "simplify" the check
#: would silently conflate them, which is exactly the kind of shortcut this
#: module's docstring now warns against.
_GIT_GLOBAL_OPTS_WITH_SEP_ARG = frozenset({"-C", "-c"})

_COORDINATOR_SAFE_COMMIT_BINARY = "coordinator-safe-commit"
_GIT_BINARY = "git"

#: ``coordinator/bin/scoped-git-commit`` (2026-08-01, C5): a second helper
#: that shells out to the SAME ``ceremony.scoped_git_commit`` op
#: ``coordinator-safe-commit`` itself dispatches -- see this module's
#: docstring entry of the same name (part 7) for the confirmed hole this
#: closes.
_SCOPED_GIT_COMMIT_BINARY = "scoped-git-commit"

#: Every helper binary that shells out to ``git commit`` under the hood and
#: is therefore in scope for ``_has_coordinator_safe_commit`` exactly like a
#: direct ``git commit`` invocation. A single named set so the identity
#: check, the argv0-head normalization passes, and the Windows spaced-path
#: regex below all consult the SAME membership rather than three
#: independently-hand-maintained name lists that could drift apart --
#: exactly the failure class ``_COMMITTING_OP_NAMES`` already exists to
#: prevent for the invoke-matcher/prefilter pair (see the module docstring's
#: part-6 entry).
_COMMIT_HELPER_BINARY_NAMES = frozenset(
    {_COORDINATOR_SAFE_COMMIT_BINARY, _SCOPED_GIT_COMMIT_BINARY}
)

#: The binary identities this module's argv0-head normalization passes
#: (``_normalize_windows_git_argv0`` and
#: ``_normalize_windows_argv0_head_path_with_spaces``) recognize at ARGV0
#: POSITION -- both matchers below (``_has_git_commit`` and
#: ``_has_coordinator_safe_commit``) need a Windows backslash-path argv0 to
#: be rewritten to a shlex-safe form before tokenizing, not just ``git``
#: (2026-07-29 part 4 -- see this module's docstring entry of the same
#: name for the confirmed bypass this closes; widened 2026-08-01 part 7 to
#: also cover ``scoped-git-commit``).
_ARGV0_HEAD_NORMALIZE_NAMES = frozenset({_GIT_BINARY}) | _COMMIT_HELPER_BINARY_NAMES

# --- Payload/quoting hardening (2026-07-26, prose false-positive report):
#     `_has_git_commit` used to segment `cmd` with a quote-BLIND
#     `re.split(r"[;&|]", cmd)` and then tokenize each fragment with a bare
#     `frag_norm.split(" ")` -- neither step knew a heredoc BODY is stdin
#     DATA (never executed as shell command tokens) nor that a quoted
#     argument is ONE word, not several. A staff-eng reviewer persisting its
#     findings sidecar via a heredoc (`cat <<'EOF' > review.md ... EOF`)
#     whose PROSE discussed this very guard's "git commit" enforcement was
#     denied: the heredoc BODY text supplied the literal adjacent tokens
#     ``git`` and ``commit`` to the naive per-fragment scan, even though the
#     executed command was `cat <<'EOF' > review.md` -- no git invocation at
#     all. The same quote-blindness independently false-positives on
#     `echo "reviewing git commit conventions"`: the naive splitter breaks
#     the quoted argument into separate ``git``/``commit`` tokens instead of
#     the one quoted word bash's own lexer sees.
#
#     Fixed by porting the SAME two-part machinery
#     `block_subagent_destructive_action.py` already uses for this exact
#     problem class (own-module copy, per this file's established pattern --
#     at the time, the retired `_extract_first_token`'s own docstring
#     explained why cross-module import was deliberately avoided; that
#     function no longer exists as of the 2026-07-29 part 4 entry, but the
#     tokenizer-trio own-module-copy choice this paragraph describes is
#     unaffected): heredoc-BODY stripping
#     (`_strip_heredoc_bodies`, ported verbatim) run BEFORE tokenization, and
#     a `shlex`-based quote-aware tokenizer (`_tokenize_full_command`/
#     `_segments_from_tokens`) in place of the naive regex split. `shlex` in
#     POSIX mode treats a bare backslash as an escape character, which would
#     silently mangle a Windows `C:\Git\bin\git` argv0 token into
#     `C:Gitbingit` BEFORE the git-binary boundary check ever runs --
#     `_normalize_windows_git_argv0` (also ported) rewrites a `git`-basename
#     Windows-path token to its forward-slash equivalent, AT ARGV0 POSITION
#     ONLY, before shlex ever sees it, so the existing Windows-path git
#     detection tests keep passing unaffected. `_has_coordinator_safe_commit`
#     is NOT touched by THIS fix -- it has no reported bug through a heredoc
#     or a quoted multi-word argument here, and none of the required
#     regression tests exercise it through this shape; rewriting working,
#     untested-as-broken code in the same change as a targeted bug fix is
#     unneeded regression surface on a
#     shared branch.
#
#     NEGATIVE SPEC: do not "simplify" this back to a bare `cmd.split(";")`
#     or a per-fragment `.split(" ")` on the theory that real git invocations
#     never need quote-awareness -- that theory is exactly what shipped the
#     bug this section fixes. Prose almost always needs quote-awareness;
#     git commands almost never do; the guard must handle both. ---

_QUOTE_OPEN_CHARS = "'\"`("
_ARGV0_HEAD_BOUNDARY_PRE = (
    r"(?:\A|[;&|\n])\s*(?:[" + re.escape(_QUOTE_OPEN_CHARS) + r"])?"
)
_RAW_HEAD_TOKEN_RE = re.compile(r"(" + _ARGV0_HEAD_BOUNDARY_PRE + r")([^\s;&|]+)")

#: Heredoc operator + delimiter word (``<<EOF``, ``<< EOF``, ``<<-EOF``,
#: ``<<'EOF'``, ``<<"EOF"``). Ported verbatim from
#: ``block_subagent_destructive_action.py``'s ``_HEREDOC_OP_RE`` -- same
#: false-positive class, same fix. Herestrings (``<<<``) have no body and
#: are intentionally NOT matched.
_HEREDOC_OP_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _strip_heredoc_bodies(cmd: str) -> str:
    """Remove heredoc BODY lines (stdin data) from ``cmd``, keeping the
    command line that introduces each heredoc and dropping everything from
    the next line through the closing delimiter line (inclusive).

    Ported verbatim from ``block_subagent_destructive_action.py``'s
    function of the same name (own-module copy, not a cross-module import --
    see the section comment above this function's siblings). A heredoc
    feeds literal data to a command's stdin; its content is never executed
    as shell commands, so the git-commit detectors below must not treat
    words appearing inside it as command tokens. Anti-bypass: this does NOT
    reduce protection against an interpreter fed BY a heredoc (``bash
    <<EOF ... EOF``) -- after the body is stripped, the residual
    ``bash <<'EOF'`` is untouched by THIS module (out of scope for a commit
    gate; that shape is `block_subagent_destructive_action.py`'s concern).
    Multiple heredocs queued on one line are consumed in order.
    """
    lines = cmd.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        i += 1
        for delim in [m.group(2) for m in _HEREDOC_OP_RE.finditer(line)]:
            while i < n and lines[i].strip() != delim:
                i += 1
            if i < n:  # consume the closing delimiter line too
                i += 1
    return "\n".join(out)


def _normalize_windows_git_argv0(cmd: str) -> str:
    """Rewrite a ``\\``-separated Windows path token, AT ARGV0 POSITION ONLY
    (the head of the command or of a ``;``/``&``/``|``/newline-separated
    segment), whose basename normalizes to ``git`` OR ``coordinator-safe-
    commit`` (``C:\\Git\\bin\\git``, ``.\\git.exe``,
    ``C:\\tools\\coordinator-safe-commit.cmd``, ...) into its forward-slash
    equivalent, BEFORE ``_tokenize_full_command`` runs.

    Originally ported verbatim from
    ``block_subagent_destructive_action.py``'s function of the same name,
    git-only; generalized 2026-07-29 part 4 to also recognize
    ``coordinator-safe-commit`` -- see this module's docstring entry of the
    same name for the confirmed bypass this closes (``_has_coordinator_
    safe_commit`` now shares this same normalization pass with
    ``_has_git_commit``, so it needs the same identities recognized here).
    POSIX ``shlex`` -- which ``_tokenize_full_command`` relies on for
    quote-aware splitting -- treats a bare backslash as an escape character
    and silently drops it, which would otherwise mangle
    ``C:\\Git\\bin\\git`` into ``C:Gitbingit`` before either binary's
    boundary check in ``_token_matches_binary`` ever runs. Scoped to a raw,
    pre-tokenization ARGV0 token whose basename normalizes to EXACTLY one of
    ``_ARGV0_HEAD_NORMALIZE_NAMES``: an unrelated backslash elsewhere in the
    command (heredoc-body prose already stripped by the time this runs, an
    ordinary argument) is untouched, and a token like
    ``evil-coordinator-safe-commit`` is untouched too (its normalized
    basename is the whole hyphenated string, not a member of the set).
    """

    def _rewrite(m: "re.Match[str]") -> str:
        boundary, token = m.group(1), m.group(2)
        if "\\" in token and _normalize_executable_basename(token) in _ARGV0_HEAD_NORMALIZE_NAMES:
            token = token.replace("\\", "/")
        return boundary + token

    return _RAW_HEAD_TOKEN_RE.sub(_rewrite, cmd)


#: Mirrors ``block_subagent_destructive_action.py``'s
#: ``_WINDOWS_ARGV0_HEAD_PATH_RE`` (own-module copy, not a cross-module
#: import -- see this module's established pattern). Originally narrowed to
#: ``git`` only; generalized 2026-07-29 part 4 to also recognize a
#: ``coordinator-safe-commit(.cmd)?`` terminal component -- the prior
#: narrowing (justified at the time by ``_has_coordinator_safe_commit``
#: being a separate, non-shlex, frag-based extractor that would mis-split
#: an already-quoted spaced path even if this regex quoted it first) no
#: longer holds: that extractor is retired below in favor of the same
#: canonical tokenizer ``_has_git_commit`` uses, so a normalized/quoted path
#: now tokenizes correctly for either binary. The alternation is still a
#: literal-name match at a path-separator boundary, same as
#: ``token_matches_binary`` -- ``evil-coordinator-safe-commit`` does not
#: match (nothing precedes ``evil-`` to anchor the boundary before the
#: literal ``coordinator-safe-commit`` alternative). ``[\\/]{1,2}`` (not a
#: single separator) admits a UNC path opening with two backslashes, same
#: reasoning as the sibling's own comment on that point. Widened 2026-08-01
#: part 7 to also recognize a ``scoped-git-commit(.cmd)?`` terminal
#: component, same boundary rule (``evil-scoped-git-commit`` does not
#: match).
_WINDOWS_ARGV0_HEAD_PATH_RE = re.compile(
    r"(?P<sep>\A|[;&|\n])(?P<ws>\s*)(?P<q>[\"']?)"
    r"(?P<path>(?:[A-Za-z]:)?[\\/]{1,2}(?:[^\\/\r\n]+?[\\/])*"
    r"(?:git(?:\.exe)?|coordinator-safe-commit(?:\.cmd)?|scoped-git-commit(?:\.cmd)?))"
    r"(?P=q)(?=[\s;&|]|\Z)",
    re.IGNORECASE,
)


def _normalize_windows_argv0_head_path_with_spaces(cmd: str) -> str:
    """Rewrite a Windows absolute (drive-letter- or root-rooted) argv0-head
    path -- possibly containing embedded-space components
    (``C:\\Program Files\\Git\\bin\\git.exe``) -- whose basename normalizes
    to ``git``, into its forward-slash equivalent, additionally
    single-quoting it when unquoted AND whitespace-containing, so the whole
    path lands as ONE ``shlex`` token at argv0 position instead of
    splitting on the embedded space.

    Mirrors ``block_subagent_destructive_action.py``'s function of the same
    name (own-module copy; see this module's 2026-07-29-part-3 docstring
    entry for why the ``git`` handling was originally ported as consistency/
    defense-in-depth rather than as a fix for a demonstrated bypass in THIS
    module's own execution model). 2026-07-29 part 4 generalized the
    underlying regex to also recognize ``coordinator-safe-commit`` -- see
    the regex's own comment for why the earlier "deliberately NOT included"
    exclusion no longer holds. Runs BEFORE ``_normalize_windows_git_argv0``:
    once a matched path is quoted here, it no longer contains a bare ``\\``
    outside the quotes, so the older pass's ``if "\\" in token`` gate finds
    nothing left to do for it -- a strict widening, not a replacement, of
    the older pass's coverage.
    """

    def _rewrite(m: "re.Match[str]") -> str:
        sep, ws, q, path = m.group("sep"), m.group("ws"), m.group("q"), m.group("path")
        normalized = path.replace("\\", "/")
        if not q and re.search(r"\s", normalized):
            quoted = normalized.replace("'", "'\\''")
            return sep + ws + "'" + quoted + "'"
        return sep + ws + q + normalized + q

    return _WINDOWS_ARGV0_HEAD_PATH_RE.sub(_rewrite, cmd)


def _tokens_reach_commit_after_git(tokens: list) -> bool:
    """Token walk: for every token in ``tokens`` that boundary-matches the
    ``git`` binary (see ``_token_matches_binary`` -- bare ``git``,
    ``bin/git``, an absolute POSIX path ending ``/git``, or an absolute
    Windows path ending ``\\git``; ``.exe``-suffixed forms are folded in via
    the canonical tokenizer (2026-07-29 part 2) and embedded-space Windows
    paths are folded in via ``_normalize_windows_argv0_head_path_with_spaces``
    (2026-07-29 part 3) -- both run in ``check()`` before this walk ever
    sees the tokens, so neither is out-of-scope any longer), walk forward past a
    run of git GLOBAL options -- separate-arg forms (``-C <path>``,
    ``-c <name>=<value>``, skipped two tokens at a time), attached long
    forms (``--git-dir=...``, ``--work-tree=...``, skipped one token), and
    no-argument boolean flags (``--no-pager``, skipped one token), in any
    combination and count -- looking for the ``commit`` subcommand token.
    Returns True on the first ``git ... commit`` chain found anywhere in
    ``tokens``.

    This is a walk, not a regex, because ``git -c user.name=x commit`` is
    not expressible as a fixed two-word sequence: the ``-c`` option's VALUE
    (``user.name=x``) is a separate token that is neither a flag (no
    leading ``-``) nor the ``commit`` subcommand, so a regex anchored on a
    literal ``"git commit"`` or ``"git -C ... commit"`` shape misses it
    entirely -- this was one of the three confirmed 2026-07-25 bypasses.
    """
    n = len(tokens)
    for start in range(n):
        if not _token_matches_binary(tokens[start], _GIT_BINARY):
            continue
        i = start + 1
        while i < n:
            tok = tokens[i]
            if tok == "commit":
                return True
            if not tok.startswith("-"):
                break
            if tok in _GIT_GLOBAL_OPTS_WITH_SEP_ARG:
                i += 2
                continue
            i += 1
    return False


#: Shell interpreters whose ``-c <string>`` argument is itself a shell
#: command line that will actually be EXECUTED -- ``env``/``nice``/``time``
#: prefix the invocation without changing what runs. BX-13 confirmed-live
#: bypass (2026-07-29, real-dispatcher attempt, not guard-in-isolation): a
#: git-commit invocation quoted as the single ``-c`` argument to one of
#: these interpreters (``sh -c "git commit -m x"``) was previously ALLOWED
#: outright, even under a resolved subagent identity -- the quoted string
#: tokenizes as ONE shlex word (the same "quoted argument is not executable
#: command text" property this module's own heredoc/quoting fix relies on
#: to correctly ALLOW `echo "reviewing git commit conventions"`), so
#: neither ``_has_git_commit`` nor ``_has_coordinator_safe_commit`` ever
#: examined its contents. Unlike ``echo``'s argument, a shell interpreter's
#: ``-c`` argument is not inert text -- it is the command the interpreter
#: executes -- so it must be unwrapped and re-scanned, not skipped.
_C_FLAG_SHELL_INTERPRETERS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})

#: C1 (2026-08-03) -- a Python interpreter's ``-c <string>`` argument is
#: EQUALLY the actual command executed, not inert prose, for exactly the
#: reason documented above for a shell's ``-c``. Deliberately kept as a
#: SEPARATE set from ``_C_FLAG_SHELL_INTERPRETERS`` rather than folded into
#: it -- see the module docstring's NEGATIVE SPEC amendment (2026-08-03) for
#: why widening THAT set stays forbidden while this parallel set does not
#: violate the same rule. Reuses ``_PYTHON_INTERPRETER_NAMES`` (defined
#: below, already consulted by ``_tokens_reach_committing_op_after_python``
#: for the unrelated ``-m coordinator_core.invoke`` shape) rather than a
#: second, independently-hand-maintained name list -- module-level name
#: lookup happens at call time, so the forward reference is safe.
#: ``_normalized_interpreter_head`` (below) additionally strips a dotted
#: Python version suffix so ``python3.11``/``python3.12.1`` match too.

#: Trailing dotted Python version suffix (``python3.11`` -> ``python3``,
#: ``python3.12.1`` -> ``python3``), stripped by ``_normalized_interpreter_
#: head`` below so a versioned interpreter binary is recognized identically
#: to its bare form. Own-module copy of ``block_subagent_destructive_
#: action.py``'s ``_PYTHON_VERSION_SUFFIX_RE`` / ``_normalize_interpreter_
#: basename``, per this file's established no-cross-module-coupling pattern
#: for small tokenizer-adjacent helpers (see ``_strip_env_prefix_for_
#: commit_unwrap``'s own docstring for the same pattern already in use
#: here).
_PYTHON_VERSION_SUFFIX_RE = re.compile(r"^(python3?)(?:\.\d+)+$")


def _normalized_interpreter_head(token: str) -> str:
    """Return ``token``'s executable identity normalized for the unwrap
    interpreter-membership check in ``_wrapped_shell_c_payloads``:
    path-separator-stripped, ``.exe``/``.cmd``-suffix-stripped, case-folded
    (``_normalize_executable_basename``), then a trailing dotted Python
    version suffix is ALSO stripped (``_PYTHON_VERSION_SUFFIX_RE``) so
    ``python3.11``, ``/usr/bin/python3.11``, and ``python3`` all normalize
    to the same ``python3`` identity. Non-python names, and bare
    ``python``/``python3`` with no version suffix, pass the second step
    unchanged.
    """
    base = _normalize_executable_basename(token)
    match = _PYTHON_VERSION_SUFFIX_RE.match(base)
    return match.group(1) if match else base


#: A Python interpreter's ``-c`` argument is Python SOURCE text, not shell
#: syntax -- unlike a shell's ``-c`` payload, re-tokenizing it with the
#: shlex-based ``_tokenize_full_command`` does NOT reliably isolate the
#: argv-shaped words a caller passes to ``subprocess``/``os.system`` (e.g.
#: ``subprocess.run(['scoped-git-commit','-m','x'])`` tokenizes as ONE
#: opaque token -- ``subprocess.run([scoped-git-commit,-m,x])`` -- since
#: shlex only splits on WHITESPACE, and there is none between the call
#: syntax and the quoted list elements; confirmed empirically this session,
#: the exact reason the naive "just re-scan the raw -c payload" approach
#: alone does not catch the repro this fix exists for). This regex instead
#: extracts every single- or double-quoted Python string LITERAL from the
#: payload, in appearance order, so they can be rejoined into a synthetic,
#: space-separated argv-shaped line and fed back through the SAME shared
#: unwrap/re-scan machinery -- see ``_python_c_payload_argv_text`` below.
#: Deliberately approximate, not a Python parser: it does not decode
#: backslash escapes inside a literal, does not evaluate string
#: concatenation/f-strings/``+``, and a literal that itself contains the
#: OTHER quote character unescaped is still matched correctly (the
#: negative-lookalike class in each alternative excludes only the quote
#: character terminating THAT alternative). This is sufficient for the
#: argv-shaped literals a subprocess/os.system call embeds -- a string-
#: built payload (``'g'+'it'``) reconstructs to ``"g it"``, two separate
#: words, NOT ``"git"`` -- so it correctly stays undetected, matching
#: AC13's documented, structurally-permanent residual rather than
#: accidentally over-fixing it.
_PYTHON_STRING_LITERAL_RE = re.compile(
    r"'(?:[^'\\]|\\.)*'" r'|"(?:[^"\\]|\\.)*"',
    re.DOTALL,
)


def _python_c_payload_argv_text(payload: str) -> str:
    """Reconstruct a synthetic, space-joined argv-shaped line from every
    quoted Python string literal found in ``payload`` (in appearance
    order), so a ``subprocess.run([...])``/``os.system('...')``-style
    Python ``-c`` payload can be re-scanned by the SAME shell-oriented
    matchers a genuine shell ``-c`` payload already is -- see
    ``_PYTHON_STRING_LITERAL_RE``'s docstring for why this reconstruction
    step is needed at all (naive re-tokenization of raw Python source does
    not isolate these words) and for the deliberate limits of this
    approximation (AC13). Returns ``""`` (falsy) when no literal is found,
    so the caller can skip yielding an empty candidate.
    """
    literals = [m.group(0)[1:-1] for m in _PYTHON_STRING_LITERAL_RE.finditer(payload)]
    return " ".join(literals)


#: Module roots a provably-inert Python ``-c`` payload may import (part 14,
#: 2026-08-04; NARROWED part 17, 2026-08-05). Pure data/text/format modules
#: with no process-spawn, no filesystem-mutation, no ANNOTATION-EVALUATION,
#: and no dynamic-import surface of their own. Anything absent from this set
#: -- including a module that merely LOOKS harmless -- makes the payload NOT
#: provably inert, which restores the pre-part-14 behaviour (run the
#: reconstruction leg, deny on a helper-name match). Submodule imports
#: (``a.b``) are checked on the ROOT package only, so an unlisted root can
#: never be reached through a listed one.
#:
#: FOUR ROOTS WERE DROPPED IN PART 17, and the reasons are recorded here
#: because "this module looks like data handling" is exactly the reasoning
#: that admitted the first two and produced a live bypass:
#:
#: * ``typing`` -- ``get_type_hints`` EVALUATES string annotations through
#:   ``eval``, and ``ForwardRef._evaluate`` compiles and evaluates its own
#:   string. Annotation evaluation is a documented, first-class eval path,
#:   not an obscure corner.
#: * ``dataclasses`` -- ``make_dataclass`` accepts an annotation as a plain
#:   string and routes it to the same evaluator; it was the other half of
#:   the confirmed weaponized payload.
#: * ``string`` -- ``string.Formatter`` walks arbitrary attribute chains off
#:   its format arguments (``'{0.__class__}'``), i.e. a reflection surface
#:   reached without naming a single dunder in the payload text.
#: * ``functools`` -- ``reduce``/``partial`` APPLY a caller-supplied
#:   callable, which turns "can this payload name a dangerous function" into
#:   a second, harder question this walk should not have to answer.
#:
#: The roots that remain are audited to expose no eval, exec, spawn, import,
#: or callable-application primitive: ``ast`` (parse/dump/walk build and read
#: trees; ``literal_eval``/``compile`` are refused at CALL position by
#: `_INERT_SAFE_CALLABLE_NAMES`), ``json``, ``re`` (``re.compile`` is refused
#: by the same callable allowlist and by `_NON_INERT_BUILTIN_NAMES`),
#: ``textwrap``, ``difflib``, ``collections``, ``unicodedata``, ``hashlib``,
#: ``base64``, ``math``, ``itertools``, ``decimal``, ``datetime``. Every one
#: of them is now doubly gated: importing a root buys nothing on its own,
#: because each individual call and attribute must ALSO clear
#: `_INERT_SAFE_CALLABLE_NAMES`.
_INERT_PAYLOAD_IMPORT_ROOTS = frozenset(
    {
        "ast",
        "json",
        "re",
        "textwrap",
        "difflib",
        "collections",
        "unicodedata",
        "hashlib",
        "base64",
        "math",
        "itertools",
        "decimal",
        "datetime",
    }
)

#: Module identities that make a payload NOT provably inert even when they
#: are never imported in that payload (a bare ``importlib.import_module(...)``
#: reference relies on an ambient binding this checker cannot see). Checked
#: against every ``Name`` in the payload, so the spawn/dynamic-import families
#: are refused by identity as well as by import statement.
_NON_INERT_MODULE_NAMES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "shutil",
        "runpy",
        "importlib",
        "pty",
        "signal",
        "socket",
        "multiprocessing",
        "threading",
        "ctypes",
        "pickle",
        "code",
        "codeop",
        "platform",
        "tempfile",
        "pathlib",
        "glob",
        "atexit",
        "site",
        "sysconfig",
        "webbrowser",
        "timeit",
        "pdb",
        "trace",
        "inspect",
        "gc",
        "builtins",
        "operator",
    }
)

#: Builtin identities that can reach an execution sink, a dynamic import, or
#: an attribute the static walk below would otherwise never see. A payload
#: referencing any of these -- called or bare -- is not provably inert.
#: ``open`` is handled separately (``_open_call_is_read_only``): reading a
#: file is the entire legitimate case this exemption exists for, so ``open``
#: is inert at CALL position with an absent or literal read-only mode, and
#: non-inert everywhere else (a bare ``open`` reference carries no mode to
#: check).
_NON_INERT_BUILTIN_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
        "breakpoint",
        "input",
        "memoryview",
        "super",
        "help",
    }
)

#: ``help`` is in the set above for a non-obvious reason worth stating: it
#: routes through ``pydoc``, which pages output through a SPAWNED pager and
#: imports arbitrary modules on ``help('modules')``. It is a sink wearing
#: documentation's clothes, and this checker's whole job is to refuse
#: anything it cannot prove is not one.

_OPEN_BUILTIN_NAME = "open"

#: Attribute names that reach a process spawn, a filesystem mutation, a
#: deserialization sink, or the reflection chain that walks from any object
#: back to ``Popen`` (``().__class__.__bases__[0].__subclasses__()``).
#: Matched on the ATTRIBUTE name alone, never on the object it hangs off --
#: the object's identity is exactly what a static walk cannot resolve.
#:
#: ``load``/``loads`` are deliberately ABSENT (the one omission from the
#: deserialization family): every module that could supply a dangerous one
#: (``pickle``, ``marshal``, ``shelve``, and any third-party equivalent) is
#: already outside `_INERT_PAYLOAD_IMPORT_ROOTS` AND, for the spawn family,
#: inside `_NON_INERT_MODULE_NAMES` as a bare name -- so no binding a
#: provably-inert payload can construct ever resolves ``loads`` to a
#: deserialization sink, while ``json.loads(open(p).read())`` is a first-
#: class read-only shape this exemption exists to permit. This is an
#: unreachable-sink argument about the allowlist, not a judgment that
#: ``loads`` is safe: if `_INERT_PAYLOAD_IMPORT_ROOTS` ever gains a module
#: with an executing deserializer, these two names come back.
#:
#: ANNOTATION-EVALUATION FAMILY (part 17, 2026-08-05) -- the names below are
#: execution sinks in the strict sense that they hand a STRING to ``eval``
#: at runtime, and they were invisible to every leg of this module until a
#: confirmed bypass used two of them together:
#:
#:     import typing, dataclasses
#:     s = ''.join([chr(c) for c in [...]])       # ord-encoded program
#:     C = dataclasses.make_dataclass('C', [('a', s)])
#:     typing.get_type_hints(C)                   # evaluates s as code
#:
#: They are folded into `_NON_INERT_ATTRIBUTE_NAMES` rather than kept in a
#: parallel list SO THAT part 16's sink sets pick them up by the subtraction
#: they are already derived through (`_EXECUTION_SINK_ATTRIBUTE_NAMES`),
#: which is what makes the opaque-argument refusal fire on them. Adding a
#: sibling name here is therefore the single edit that teaches both parts.
#: ``_evaluate`` is not a dunder (single leading underscore), so
#: `_identifier_is_dunder` never covered it.
_ANNOTATION_EVAL_ATTRIBUTE_NAMES = frozenset(
    {
        "get_type_hints",
        "make_dataclass",
        "ForwardRef",
        "_evaluate",
        "evaluate_forward_ref",
        "TypeVar",
        "ParamSpec",
        "TypeAliasType",
        "NamedTuple",
        "TypedDict",
        "NewType",
    }
)

#: THE ``os.exec*`` FAMILY (part 21, 2026-08-05) -- enumerated because the
#: ``exec`` PREFIX could not be, and the reasoning lives at
#: `_NON_INERT_ATTRIBUTE_PREFIXES`. Every ``exec``-named process-creation
#: callable the ``os`` module exports (3.11-3.14): ``execl``/``execv`` crossed
#: with the ``e``/``p``/``pe`` variants. ``posix_spawn``/``posix_spawnp`` are
#: NOT here -- they stay with the ``spawn`` family root, which does bound its
#: family. Folded into `_NON_INERT_ATTRIBUTE_NAMES` below rather than kept
#: parallel, so part 16's sink sets pick them up through the subtraction they
#: are already derived through and one edit teaches every leg.
_OS_EXEC_FAMILY_NAMES = frozenset(
    {
        "execl",
        "execle",
        "execlp",
        "execlpe",
        "execv",
        "execve",
        "execvp",
        "execvpe",
    }
)

_NON_INERT_ATTRIBUTE_NAMES = _ANNOTATION_EVAL_ATTRIBUTE_NAMES | _OS_EXEC_FAMILY_NAMES | frozenset(
    {
        "system",
        "popen",
        "fork",
        "forkpty",
        "run",
        "call",
        "check_call",
        "check_output",
        "communicate",
        "Popen",
        "startfile",
        "dup2",
        "write_text",
        "write_bytes",
        "unlink",
        "rmtree",
        "chmod",
        "rename",
        "replace",
        "__globals__",
        "__builtins__",
        "__subclasses__",
        "__class__",
        "__bases__",
        "__mro__",
        "__code__",
        "__getattribute__",
        "__dict__",
    }
)

#: Attribute-name FAMILY ROOTS in the same family as the set above, covering
#: the whole ``os.spawn*`` family without enumerating each arity and
#: environment variant.
#:
#: PART 19 (2026-08-05) -- these are matched by `_name_is_process_creation`,
#: NOT by ``startswith``, and the difference was a confirmed live ALLOW:
#: ``os.posix_spawn(os.environ['X'], [], {})`` starts a program and does not
#: START WITH either root, so no leg of this module saw a sink at all. Adding
#: the two ``posix_*`` names to a list would have fixed that one spelling and
#: left the CLASS open, because the same shape recurs for any future
#: ``<prefix>_spawn``. The regex below therefore anchors each root at a word
#: start -- string start or after an underscore -- which is what "the
#: ``spawn`` family" always meant.
#:
#: PART 21 (2026-08-05) -- ``exec`` IS NO LONGER A ROOT, and the ``os.exec*``
#: members are enumerated in `_OS_EXEC_FAMILY_NAMES` instead, exactly as
#: ``system``/``popen``/``fork`` already were. A root only pays for itself
#: when it bounds a family it can name: ``spawn`` does (every member is
#: ``spawn``+arity, and ``<prefix>_spawn`` recurs), ``exec`` does not -- it
#: also claimed ``execute``, ``executemany``, ``exec_driver_sql``,
#: ``db_exec`` and ``sql_execute``, and ``cursor.execute(sql)`` is the
#: universal DB idiom, not a process spawn. That collateral routed the
#: harshest door (`_sink_takes_opaque_program_text` denies on ANY unresolved
#: argument) at a measured DENY. The ``os`` exec surface is closed and short,
#: so enumerating it costs one list and removes a regex claiming a family it
#: could not bound.
_NON_INERT_ATTRIBUTE_PREFIXES = ("spawn",)

#: The members are `_OS_EXEC_FAMILY_NAMES`, defined with the attribute set
#: above because that is where they are consumed.

#: The family roots above, anchored at a NAME-SEGMENT boundary. Built from
#: `_NON_INERT_ATTRIBUTE_PREFIXES` rather than hand-spelled so a new root is
#: still exactly one edit, and consumed by every leg that used to spell its
#: own ``startswith`` loop (the inertness check, mechanism 2's sink identity,
#: and its opaque-program-text narrowing).
_PROCESS_CREATION_NAME_RE = re.compile(
    r"(?:^|_)(?:%s)" % "|".join(_NON_INERT_ATTRIBUTE_PREFIXES)
)


def _name_is_process_creation(name: str) -> bool:
    """True when ``name`` belongs to the ``spawn`` process-creation family at
    a name-segment boundary -- ``spawnl``, ``spawnve``, and the
    ``posix_spawn``/``posix_spawnp`` pair a plain prefix test missed
    (part 19).

    The whole audited ``os`` process-creation surface this module refuses is
    the union of THIS predicate and `_NON_INERT_ATTRIBUTE_NAMES`: ``system``,
    ``popen``, ``fork``, ``forkpty``, ``startfile``, the ``os.exec*`` members
    (`_OS_EXEC_FAMILY_NAMES`) and the ``subprocess`` family are named there;
    ``spawn*``/``posix_spawn*`` are matched here; ``pty.fork``/``pty.spawn``
    are reached by both, since every leg consuming this is identity-blind
    about the receiving object. Pinned as a set by
    ``test_python_c_inert_payload_exemption.py``.

    ``exec`` WAS A ROOT HERE AND IS NOT ANY MORE (part 21) -- it matched
    ``execute``/``executemany``/``exec_driver_sql`` and routed the universal
    DB-cursor idiom through the whole-command-text door. See
    `_NON_INERT_ATTRIBUTE_PREFIXES` for why the ``os`` exec members are
    enumerated instead and why ``spawn`` is not.
    """
    return _PROCESS_CREATION_NAME_RE.search(name) is not None

#: THE CALLABLE ALLOWLIST (part 17, 2026-08-05) -- the ONLY functions and
#: methods a provably-inert payload may invoke, and the only attribute names
#: it may even mention. Matched on the NAME alone at ``Name`` and
#: ``Attribute`` position both, identity-blind about the receiver exactly as
#: the rest of this checker is.
#:
#: WHY IT EXISTS, stated plainly because the shape it replaces read as an
#: allowlist and behaved as a denylist: before part 17 an attribute was inert
#: unless it appeared in `_NON_INERT_ATTRIBUTE_NAMES`, so EVERY unlisted
#: callable -- including ``typing.get_type_hints``, which evaluates strings
#: as code -- was cleared by default. That is the inversion this module's own
#: NEGATIVE SPEC forbids, and it produced a confirmed live bypass. The
#: direction is now: unrecognised means NOT inert, full stop.
#:
#: SOUNDNESS ARGUMENT, which is what keeps this list addable-to safely: every
#: entry denotes a PURE operation -- it reads bytes, parses or formats data,
#: or computes over values already in hand -- and none of them starts a
#: process, imports a module, deserialises executable state, evaluates a
#: string, or applies a callable this checker did not itself clear. That last
#: clause is why the callback-taking entries (``sorted``'s ``key``,
#: ``re.sub``'s callable ``repl``) are safe: a callable can only reach a
#: call or callback slot under a name this list clears.
#:
#: THAT CLAUSE IS ENFORCED, NOT ASSUMED, AS OF PART 21 (2026-08-05), and it
#: was false when first written -- the wording claimed "a bare reference to
#: anything else is refused at ``Name``/``Attribute`` position" while the walk
#: refused only dunders and the two forbidden name sets, so ``sorted = type``
#: and ``from ast import literal_eval as z; get = z; get('1+1')`` both
#: certified inert. Three legs now carry it, each with its own predicate:
#: `_inert_load_name_is_cleared` (a Name READ must be allowlisted, payload-
#: bound, or an ``except`` type), `_import_binding_launders_a_cleared_name`
#: (an import may not RENAME something into an allowlisted spelling), and the
#: import-position rule inside `_inert_load_name_is_cleared` (an import-bound
#: name may appear only as an attribute receiver or a call target, never as a
#: value handed to a callback). The residual is stated where it belongs, on
#: `_python_c_payload_is_provably_inert`.
#:
#: Entries, with the justification each was admitted on:
#:
#: * File inspection -- ``open`` (read mode only, enforced separately by
#:   `_open_call_is_read_only`), ``read``, ``readlines``. The entire
#:   legitimate use case this exemption exists for.
#: * Text/data slicing -- ``splitlines``, ``split``, ``rsplit``, ``join``,
#:   ``strip``, ``lstrip``, ``rstrip``, ``lower``, ``upper``, ``startswith``,
#:   ``endswith``, ``count``, ``find``, ``format``, ``encode``, ``decode``.
#:   Pure ``str``/``bytes`` methods. (``replace`` is deliberately ABSENT: it
#:   is refused by `_NON_INERT_ATTRIBUTE_NAMES` as ``os.replace``, and this
#:   list may never contradict that set -- pinned by a test.)
#: * Output/inspection builtins -- ``print``, ``len``, ``repr``, ``str``,
#:   ``int``, ``float``, ``bool``, ``bytes``, ``list``, ``tuple``, ``dict``,
#:   ``set``, ``sorted``, ``reversed``, ``enumerate``, ``zip``, ``range``,
#:   ``sum``, ``min``, ``max``, ``abs``, ``any``, ``all``, ``format``.
#:   Value construction and reduction; none reaches an interpreter.
#: * Container reads -- ``items``, ``keys``, ``values``, ``get``, ``append``.
#:   Needed by any payload that reads JSON and reports part of it.
#: * ``ast`` -- ``parse``, ``dump``, ``walk``. Building and printing a syntax
#:   tree is not running one; ``literal_eval`` and ``compile`` are NOT here.
#: * ``json`` -- ``load``, ``loads``, ``dumps``, ``dump``. See the
#:   unreachable-sink argument on `_NON_INERT_ATTRIBUTE_NAMES` for why
#:   ``load``/``loads`` cannot resolve to ``pickle``'s.
#: * ``re`` -- ``search``, ``findall``, ``match``, ``sub``, ``fullmatch``,
#:   ``group``, ``groups``. Pattern matching over text already read.
#:   ``re.compile`` is absent by design.
#: * ``hashlib`` -- ``sha256``, ``sha1``, ``sha512``, ``md5``, ``hexdigest``,
#:   ``digest``. Digesting a file that was read is arithmetic; the shape
#:   ``print(hashlib.sha256(open(p,'rb').read()).hexdigest())`` is a
#:   first-class read this exemption already permitted.
#: * ``difflib`` -- ``unified_diff``, ``ndiff``. Comparing two texts the
#:   payload already holds; the ``difflib`` read shape is pinned in the
#:   part-14 corpus.
#:
#: Add to this list ONLY with a justification of the same shape, and never in
#: response to a bypass report -- a bypass here means something unrecognised
#: was cleared, and the answer to that is always to narrow.
_INERT_SAFE_CALLABLE_NAMES = frozenset(
    {
        _OPEN_BUILTIN_NAME,
        "read",
        "readlines",
        "splitlines",
        "split",
        "rsplit",
        "join",
        "strip",
        "lstrip",
        "rstrip",
        "lower",
        "upper",
        "startswith",
        "endswith",
        "count",
        "find",
        "format",
        "encode",
        "decode",
        "print",
        "len",
        "repr",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "list",
        "tuple",
        "dict",
        "set",
        "sorted",
        "reversed",
        "enumerate",
        "zip",
        "range",
        "sum",
        "min",
        "max",
        "abs",
        "any",
        "all",
        "items",
        "keys",
        "values",
        "get",
        "append",
        "parse",
        "dump",
        "walk",
        "load",
        "loads",
        "dumps",
        "search",
        "findall",
        "match",
        "fullmatch",
        "sub",
        "group",
        "groups",
        "sha256",
        "sha1",
        "sha512",
        "md5",
        "hexdigest",
        "digest",
        "unified_diff",
        "ndiff",
    }
)

#: Any of these characters in an ``open`` mode string means the call can
#: WRITE (``w``/``a``/``x`` outright, ``+`` upgrading a read mode to
#: read-write), so the payload is not provably inert. A mode argument that is
#: not a literal string is likewise not provably inert -- unknown is never
#: treated as read-only.
_OPEN_WRITE_MODE_CHARS = frozenset("wax+")

#: The AST node types a provably-inert payload may contain. Marker nodes
#: (operators, comparison ops, expression contexts) are cleared by base class
#: below rather than enumerated. Anything absent -- a lambda, a def, a
#: decorator, a walrus, an ``async`` construct, a ``del``, a ``global``
#: statement, or a node type added by a future Python release -- makes the
#: payload NOT provably inert.
_INERT_PAYLOAD_NODE_TYPES: Tuple[type, ...] = (
    ast.Module,
    ast.Expr,
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Import,
    ast.ImportFrom,
    ast.alias,
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.ExceptHandler,
    ast.With,
    ast.withitem,
    ast.Assert,
    ast.Pass,
    ast.Break,
    ast.Continue,
    ast.Constant,
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Subscript,
    ast.Slice,
    ast.Starred,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
)

#: Marker node families cleared wholesale: they carry no callable surface of
#: their own, only which operator/context an already-checked node uses.
_INERT_PAYLOAD_MARKER_TYPES: Tuple[type, ...] = (
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
    ast.expr_context,
)


def _identifier_is_dunder(name: str) -> bool:
    """True for any ``__x__`` identifier, at NAME or ATTRIBUTE position.

    A blanket refusal, not an enumeration, and it closes a hole found while
    adversarially reviewing this checker: ``__builtins__.eval("...")`` reads
    as an ordinary ``Name``/``Attribute`` pair -- ``__builtins__`` is not a
    module in `_NON_INERT_MODULE_NAMES`, and ``eval`` at ATTRIBUTE position
    is not the ``eval`` NAME the builtin set refuses -- yet in a ``-c`` main
    module ``__builtins__`` IS the builtins module, so that spelling reaches
    every builtin this checker otherwise refuses by name. The same shape
    generalizes (``__loader__.load_module``, ``__spec__``, ``__reduce__``,
    and every reflection dunder the object graph exposes), so the rule is
    positional and total rather than another name list to keep current. A
    provably-inert text-processing payload has no business naming a dunder;
    losing ``if __name__ == "__main__"`` inside a ``-c`` one-liner is the
    entire cost.
    """
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def _attribute_name_is_non_inert(attr: str) -> bool:
    """True when ``attr`` names a spawn/mutation/reflection surface -- any
    dunder (`_identifier_is_dunder`), exact membership in
    `_NON_INERT_ATTRIBUTE_NAMES` or `_NON_INERT_BUILTIN_NAMES` (a builtin
    reached through an attribute is the same builtin), or a
    a `_name_is_process_creation` family match (the
    ``os.spawn*``/``os.posix_spawn*`` families; the ``os.exec*`` members are
    in `_NON_INERT_ATTRIBUTE_NAMES` by enumeration as of part 21).
    """
    if _identifier_is_dunder(attr):
        return True
    if attr in _NON_INERT_ATTRIBUTE_NAMES or attr in _NON_INERT_BUILTIN_NAMES:
        return True
    return _name_is_process_creation(attr)


def _open_call_is_read_only(node: "ast.Call") -> bool:
    """True when ``node`` is an ``open(...)`` call this checker can PROVE
    cannot write: no ``*args``/``**kwargs`` (either hides the mode slot), and
    a mode that is either absent or a literal ``str`` containing none of
    `_OPEN_WRITE_MODE_CHARS`. A computed mode (``open(p, m)``,
    ``open(p, 'r' + extra)``) is not provable and returns False.

    APPLIED WHEREVER THE CALL IS NAMED ``open``, at ATTRIBUTE position as well
    as ``Name`` (part 21, 2026-08-05). It used to be reached only from the
    ``Name`` leg, so ``codecs.open(p, 'w')`` -- reachable as
    ``from json import codecs as c`` -- certified INERT and wrote, which is a
    mutation `_INERT_SAFE_CALLABLE_NAMES`' own soundness argument says the
    certificate precludes. The receiver is irrelevant to the question this
    predicate answers: any callable spelled ``open`` that is handed a write
    mode is not a read.
    """
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return False
    if any(kw.arg is None for kw in node.keywords):
        return False
    mode: Optional[ast.expr] = None
    if len(node.args) >= 2:
        mode = node.args[1]
    for kw in node.keywords:
        if kw.arg == "mode":
            mode = kw.value
    if mode is None:
        return True
    if not isinstance(mode, ast.Constant) or not isinstance(mode.value, str):
        return False
    return not (set(mode.value) & _OPEN_WRITE_MODE_CHARS)


#: The name `_INERT_SAFE_CALLABLE_NAMES` admits for ``str.format``/``format``,
#: singled out because its template argument carries a field grammar the AST
#: walk cannot see (see `_format_template_names_a_dunder`).
_FORMAT_CALLABLE_NAME = "format"

#: CPython's own format-template reader, used as the grammar of record rather
#: than a hand-rolled brace scanner -- the field syntax this must agree with
#: is defined by the very object that will interpret the template at runtime.
_FORMAT_FIELD_PARSER = string.Formatter()

#: Splits a field name into its ``arg_name``/attribute/index components:
#: ``0.__class__.__bases__[0]`` -> ``0``, ``__class__``, ``__bases__``, ``0``.
_FORMAT_FIELD_COMPONENT_SPLIT = re.compile(r"[.\[\]]+")

#: Bound on how deep a nested format spec (``'{0:{1:{2}}}'``) is followed
#: when harvesting field names. Fails CLOSED: a template nested past the cap
#: is treated as unreadable, and an unreadable template is never cleared.
_MAX_FORMAT_FIELD_DEPTH = 8


def _format_template_names_a_dunder(template: str) -> bool:
    """True when ``template`` is a ``str.format`` template whose FIELD syntax
    reaches a dunder -- ``'{0.__class__}'``, ``'{0.__class__.__bases__[0]}'``,
    ``'{0[__globals__]}'`` -- or cannot be read at all.

    Part 22 (2026-08-05). ``str.format``'s field grammar
    (``arg_name ("." attribute | "[" index "]")*``) is a second, textual
    attribute-chaining mini-language living inside a plain `ast.Constant`, so
    `_identifier_is_dunder` and `_attribute_name_is_non_inert` -- which only
    ever inspect real ``ast.Name``/``ast.Attribute`` identifiers -- never see
    the names it traverses. That is the SAME capability the part-17 comment
    names as the reason ``string`` was refused as an import root
    (``string.Formatter`` walks arbitrary attribute chains off its arguments),
    reached here through a callable the allowlist admits on the ground that
    it "only ever returns a str".

    The channel is a READ, not an invocation: CPython's field grammar has no
    call operator, so a traversal can NAME ``__subclasses__`` but never apply
    it, and the certificate's spawn/write/commit guarantee was never breached.
    What was breached is the certifier's own stated invariant -- dunders are
    refused at name AND attribute position -- and closing the channel is
    cheaper than documenting the exception.

    Unreadable (a malformed template, or nesting past
    `_MAX_FORMAT_FIELD_DEPTH`) counts as naming a dunder, per this checker's
    unrecognised-means-not-inert rule. An ordinary template -- ``'{}'``,
    ``'{0}'``, ``'{name}'``, ``'{0:>10}'``, ``'{0:{1}}'`` -- names no
    attribute chain and stays inert.
    """
    return _format_fields_reach_a_dunder(template, _MAX_FORMAT_FIELD_DEPTH)


def _format_fields_reach_a_dunder(template: str, depth: int) -> bool:
    if depth <= 0:
        return True
    try:
        parsed = list(_FORMAT_FIELD_PARSER.parse(template))
    except Exception:
        return True
    for _literal, field_name, format_spec, _conversion in parsed:
        if field_name:
            for component in _FORMAT_FIELD_COMPONENT_SPLIT.split(field_name):
                if _identifier_is_dunder(component.strip()):
                    return True
        if format_spec and _format_fields_reach_a_dunder(format_spec, depth - 1):
            return True
    return False


def _string_constant_is_a_readable_template(value: str) -> bool:
    """True when ``value`` parses as a format template at all -- so that a
    string which merely CONTAINS a brace (``print('{' + x)``) is not judged
    against the field rule it was never going to be read under."""
    try:
        list(_FORMAT_FIELD_PARSER.parse(value))
    except Exception:
        return False
    return True


def _format_call_receiver_is_provable(node: "ast.Call") -> bool:
    """True when a ``.format(...)`` call's RECEIVER is one this checker can
    reason about: a literal ``str`` (checked directly by
    `_format_template_names_a_dunder`) or a plain ``Name`` (whose possible
    templates are covered by the payload-wide constant sweep in
    `_python_c_payload_is_provably_inert`).

    A COMPUTED receiver -- ``('{0.__' + 'class__}').format(())``,
    ``''.join(parts).format(())`` -- is refused, because template text
    assembled from fragments is exactly the shape the constant sweep cannot
    see. Fails closed, consistently with `_open_call_is_read_only`'s
    computed-mode rule.
    """
    receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
    if isinstance(receiver, ast.Constant):
        return isinstance(receiver.value, str)
    return isinstance(receiver, ast.Name)


def _import_binding_launders_a_cleared_name(imported: str, bound: str) -> bool:
    """True when an import RENAMES something into a name
    `_INERT_SAFE_CALLABLE_NAMES` has cleared -- ``from ast import literal_eval
    as get``, ``import json as sorted`` (part 21, 2026-08-05).

    The allowlist clears a name because of what that NAME denotes; an alias
    that points a cleared name at a different callable makes the clearance a
    statement about nothing. The call leg matches on the spelled name, so
    without this a payload could invoke any member of any listed root by
    importing it under an allowlisted spelling and calling it there. An
    alias that does NOT rename (``from json import loads``, ``import json``)
    is honest and stays inert.
    """
    return bound != imported and bound in _INERT_SAFE_CALLABLE_NAMES


def _inert_load_name_is_cleared(
    node: "ast.Name",
    *,
    import_bound_names: Set[str],
    store_bound_names: Set[str],
    attribute_receiver_name_ids: Set[int],
    call_func_name_ids: Set[int],
    except_type_name_ids: Set[int],
) -> bool:
    """True when a ``Name`` READ by the payload is one this checker has
    actually cleared (part 21, 2026-08-05) -- the leg
    `_INERT_SAFE_CALLABLE_NAMES`' soundness argument always assumed and the
    code never enforced.

    Before this, a bare reference was refused only if it was a dunder or a
    member of `_NON_INERT_BUILTIN_NAMES`/`_NON_INERT_MODULE_NAMES`, so every
    OTHER ambient builtin was free: ``sorted = type`` certified inert and then
    ran ``type('C', (), {})`` through an allowlisted spelling. Four ways a
    read is cleared, and nothing else is:

    * a name on `_INERT_SAFE_CALLABLE_NAMES` -- the enumerated pure surface;
    * a name this payload BOUND by import, and then only where an import can
      honestly be used: as an attribute RECEIVER (``json.dumps``) or as the
      call target itself (where the call leg has already required an
      allowlisted spelling). An import-bound name anywhere else is a callable
      travelling as a VALUE -- ``sorted(x, key=z)``, ``get = z`` -- which is
      how an unlisted member of a listed root would reach application;
    * a name this payload STORED, which holds a value computed by operations
      this same walk cleared;
    * a Name inside an ``except`` clause's type expression, which is compared
      against, never called, and never bound to anything (``except
      Exception:`` around a read is first-class legitimate usage).
    """
    if id(node) in except_type_name_ids:
        return True
    if node.id in import_bound_names:
        return id(node) in attribute_receiver_name_ids or id(node) in call_func_name_ids
    if node.id in store_bound_names:
        return True
    return node.id in _INERT_SAFE_CALLABLE_NAMES


def _python_c_payload_is_provably_inert(payload: str) -> bool:
    """True only when every construct in ``payload`` is one this checker
    explicitly recognizes as inert: an import from
    `_INERT_PAYLOAD_IMPORT_ROOTS`, an AST node type from
    `_INERT_PAYLOAD_NODE_TYPES`, and -- the load-bearing clause -- a call
    whose target and an attribute whose name are BOTH members of
    `_INERT_SAFE_CALLABLE_NAMES`. Used by `_wrapped_shell_c_payload_legs` to
    suppress the string-literal reconstruction leg
    (`_python_c_payload_argv_text`) for such a payload, and by parts 15/16 to
    skip their matchers for it.

    WHAT THIS ACTUALLY PROVES, stated precisely because the previous wording
    ("PROVE contains no execution sink") was FALSE AS WRITTEN and a confirmed
    bypass turned on the gap: it proves that the payload names nothing except
    a closed, enumerated set of pure operations. It does NOT reason about
    reachability of execution in general, and it never could -- the earlier
    shape tried to, by clearing any attribute absent from a forbidden list,
    which cleared ``typing.get_type_hints`` (an ``eval`` of a string
    annotation) and ``dataclasses.make_dataclass`` (which feeds one). The
    guarantee here is membership, not analysis: if a payload can do something
    dangerous, it must do it through a name, and every name is checked.

    A payload restricted to those operations cannot spawn anything, so it
    cannot be a disguised commit no matter which paths its string literals
    name -- which is what makes ``python3 -c "import ast;
    ast.parse(open('coordinator/bin/scoped-git-commit').read())"`` a read,
    not a commit attempt (part 14 of the module docstring).

    NEGATIVE SPEC -- this is an ALLOWLIST that FAILS CLOSED, and it must
    never be inverted into a denylist. Every leg returns False on anything
    it does not explicitly recognize: a parse failure, an unlisted import
    root, an unlisted AST node type, a call whose ``func`` is not a plain
    name or attribute NAMED ON THE CALLABLE ALLOWLIST, an attribute whose
    name is not on it, an ``open`` whose mode is not a provably-read-only
    literal AT EITHER NAME OR ATTRIBUTE POSITION, a Name READ that is neither
    allowlisted nor bound by this payload (`_inert_load_name_is_cleared`), an
    import that renames something into an allowlisted spelling
    (`_import_binding_launders_a_cleared_name`), or any construct a future
    Python release adds. "Not recognized" and "dangerous" are deliberately
    the same answer here; the cost of a false NOT-inert is a deny message the
    caller can act on, and the cost of a false inert is a commit route.

    THE RESIDUAL, STATED RATHER THAN LEFT TO A READER'S INFERENCE (part 21,
    2026-08-05, after an adversarial review found the previous closure
    argument overstated by exactly this much): a payload may still BIND any
    member of a listed root under a non-allowlisted local name
    (``from json import codecs as c``), and read allowlisted ATTRIBUTES off
    it (``c.encode(x, 'zlib_codec')``). What it cannot do is apply that
    member -- calling it needs an allowlisted spelling, which the laundering
    rule refuses, and passing it as a callback needs a value position, which
    the import-position rule refuses. So the certificate's guarantee is:
    EVERY CALLABLE APPLIED IS ONE `_INERT_SAFE_CALLABLE_NAMES` NAMES it,
    resolved over `_INERT_PAYLOAD_IMPORT_ROOTS`. It is NOT "no member of a listed root
    is reachable" -- that is the audit `_INERT_PAYLOAD_IMPORT_ROOTS` records,
    re-run per root, and it is why a new root is a security edit rather than
    a convenience one.

    Never widen this by adding a module/attribute/name to a *forbidden* set
    in response to a bypass report -- a bypass here means something
    UNRECOGNIZED was cleared, so the fix is always to narrow the allowlist
    (`_INERT_SAFE_CALLABLE_NAMES`, `_INERT_PAYLOAD_IMPORT_ROOTS`,
    `_INERT_PAYLOAD_NODE_TYPES`), never to extend a denylist. The forbidden
    sets that do exist (`_NON_INERT_MODULE_NAMES`,
    `_NON_INERT_BUILTIN_NAMES`, `_NON_INERT_ATTRIBUTE_NAMES`) are defence in
    depth over names the allowlist already refuses, and they carry a SECOND
    job that outlives their role here: part 16 derives its execution-sink
    identity from them by subtraction.
    """
    try:
        tree = ast.parse(payload)
    except Exception:
        return False

    nodes = list(ast.walk(tree))

    read_only_open_func_ids: Set[int] = set()
    call_func_name_ids: Set[int] = set()
    attribute_receiver_name_ids: Set[int] = set()
    except_type_name_ids: Set[int] = set()
    import_bound_names: Set[str] = set()
    store_bound_names: Set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attribute_receiver_name_ids.add(id(node.value))
        elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            store_bound_names.add(node.id)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                store_bound_names.add(node.name)
            for sub in ast.walk(node.type) if node.type is not None else ():
                if isinstance(sub, ast.Name):
                    except_type_name_ids.add(id(sub))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias_node in node.names:
                if alias_node.name == "*":
                    continue
                if alias_node.asname:
                    import_bound_names.add(alias_node.asname)
                elif isinstance(node, ast.Import):
                    import_bound_names.add(alias_node.name.split(".")[0])
                else:
                    import_bound_names.add(alias_node.name)

    payload_applies_format = False
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == _FORMAT_CALLABLE_NAME:
            payload_applies_format = True
            if not _format_call_receiver_is_provable(node):
                return False
        elif isinstance(func, ast.Name) and func.id == _FORMAT_CALLABLE_NAME:
            payload_applies_format = True
        if isinstance(func, ast.Name):
            call_func_name_ids.add(id(func))
            if func.id == _OPEN_BUILTIN_NAME:
                if not _open_call_is_read_only(node):
                    return False
                read_only_open_func_ids.add(id(func))
                continue
            if func.id not in _INERT_SAFE_CALLABLE_NAMES:
                return False
        elif isinstance(func, ast.Attribute):
            if func.attr not in _INERT_SAFE_CALLABLE_NAMES:
                return False
            if func.attr == _OPEN_BUILTIN_NAME:
                if not _open_call_is_read_only(node):
                    return False
                read_only_open_func_ids.add(id(func))
        else:
            # A computed call target (``('a'+'b')()``, ``f()()``, ``d['k']()``)
            # resolves to no name at all, so it can never be shown to be on
            # the allowlist.
            return False

    for node in nodes:
        if isinstance(node, _INERT_PAYLOAD_MARKER_TYPES):
            continue
        if not isinstance(node, _INERT_PAYLOAD_NODE_TYPES):
            return False
        if isinstance(node, ast.Import):
            for alias_node in node.names:
                if alias_node.name.split(".")[0] not in _INERT_PAYLOAD_IMPORT_ROOTS:
                    return False
                bound = alias_node.asname or alias_node.name.split(".")[0]
                if _import_binding_launders_a_cleared_name(alias_node.name, bound):
                    return False
        elif isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                return False
            if node.module.split(".")[0] not in _INERT_PAYLOAD_IMPORT_ROOTS:
                return False
            for alias_node in node.names:
                bound = alias_node.asname or alias_node.name
                if (
                    alias_node.name in _NON_INERT_BUILTIN_NAMES
                    or alias_node.name == _OPEN_BUILTIN_NAME
                    or _attribute_name_is_non_inert(alias_node.name)
                    or bound in _NON_INERT_BUILTIN_NAMES
                    or alias_node.name in _NON_INERT_MODULE_NAMES
                    or bound in _NON_INERT_MODULE_NAMES
                    or _import_binding_launders_a_cleared_name(alias_node.name, bound)
                ):
                    return False
        elif isinstance(node, ast.Constant):
            if (
                payload_applies_format
                and isinstance(node.value, str)
                and _string_constant_is_a_readable_template(node.value)
                and _format_template_names_a_dunder(node.value)
            ):
                return False
        elif isinstance(node, ast.Name):
            if node.id == _OPEN_BUILTIN_NAME:
                if id(node) not in read_only_open_func_ids:
                    return False
            elif (
                _identifier_is_dunder(node.id)
                or node.id in _NON_INERT_BUILTIN_NAMES
                or node.id in _NON_INERT_MODULE_NAMES
            ):
                return False
            elif isinstance(node.ctx, ast.Load) and not _inert_load_name_is_cleared(
                node,
                import_bound_names=import_bound_names,
                store_bound_names=store_bound_names,
                attribute_receiver_name_ids=attribute_receiver_name_ids,
                call_func_name_ids=call_func_name_ids,
                except_type_name_ids=except_type_name_ids,
            ):
                return False
        elif isinstance(node, ast.Attribute):
            if node.attr not in _INERT_SAFE_CALLABLE_NAMES:
                return False
            if _attribute_name_is_non_inert(node.attr):
                return False
            if node.attr == _OPEN_BUILTIN_NAME and id(node) not in read_only_open_func_ids:
                return False

    return True


#: Bound on ``sh -c 'sh -c "..."'``-style nesting depth, mirroring the
#: sibling indirection-wrapper engine in
#: ``block_subagent_destructive_action.py`` (``_MAX_INDIRECTION_DEPTH``).
#: Fails CLOSED implicitly: once the cap is hit, unwrapping simply stops and
#: the innermost un-inspected payload is never cleared, so a command that
#: nests past the cap does not thereby escape scanning of the shallower
#: levels already found commit-bearing.
_MAX_COMMIT_UNWRAP_DEPTH = 4


def _strip_env_prefix_for_commit_unwrap(tokens: list) -> list:
    """Strip a leading ``env [-i] [NAME=value ...]`` and/or bare
    ``NAME=value`` prefix so ``env sh -c '...'`` and ``FOO=1 sh -c '...'``
    still resolve to the wrapped interpreter head. Mirrors
    ``block_subagent_destructive_action.py``'s ``_strip_env_prefix`` (own-
    module copy, per this file's established no-cross-module-coupling
    pattern for small tokenizer-adjacent helpers).
    """
    i = 0
    while i < len(tokens) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i]):
        i += 1
    if i < len(tokens) and _normalize_executable_basename(tokens[i]) == "env":
        i += 1
        while i < len(tokens) and (
            tokens[i] in ("-i", "--ignore-environment")
            or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[i])
        ):
            i += 1
    return tokens[i:]


def _protect_interpreter_argv0_backslash_path(cmd: str) -> str:
    """Rewrite a ``\\``-separated Windows path token, AT ARGV0/SEGMENT-HEAD
    POSITION ONLY (reusing ``_RAW_HEAD_TOKEN_RE``, the same boundary-
    anchoring machinery ``_normalize_windows_git_argv0`` already uses for
    ``git``/``coordinator-safe-commit``/``scoped-git-commit``), whose
    basename normalizes to a recognized shell OR Python interpreter head
    (``_C_FLAG_SHELL_INTERPRETERS`` / ``_PYTHON_INTERPRETER_NAMES``, via
    ``_normalized_interpreter_head``) into its forward-slash equivalent,
    BEFORE ``_tokenize_full_command`` runs.

    Latent-bug fix (found while implementing C1, same function/module,
    minimal): confirmed empirically this session that ``_wrapped_shell_c_
    payloads`` NEVER protected an interpreter argv0 head this way for
    EITHER family -- ``runtime\\bash.exe -c "git commit -m x"`` already
    silently evaded the shell-``-c`` unwrap pre-C1 (POSIX ``shlex`` treats
    a bare backslash as an escape character and drops it, mangling
    ``runtime\\bash.exe`` into ``runtimebash.exe`` before the interpreter-
    head check ever runs -- the exact bug class ``_normalize_windows_git_
    argv0``'s own docstring already documents for ``git``). C1's own AC
    requires a Windows-backslash-path Python spelling to be recognized, so
    this had to be fixed to land that requirement; fixing it for the SAME
    already-existing shell-interpreter case in the same pass, rather than
    only for python, is a strict widening (only ever ADDS a recognized
    match; nothing that matched before stops matching) so it cannot
    regress any existing passing test. Deliberately a SEPARATE, own-module
    function/regex application from ``_normalize_windows_git_argv0`` (which
    stays scoped to git/commit-helper basenames for ITS callers) rather
    than widening that function's own name set -- this one only runs
    inside the interpreter unwrap path.
    """

    def _rewrite(m: "re.Match[str]") -> str:
        boundary, token = m.group(1), m.group(2)
        if "\\" in token and _normalized_interpreter_head(token) in (
            _C_FLAG_SHELL_INTERPRETERS | frozenset(_PYTHON_INTERPRETER_NAMES)
        ):
            token = token.replace("\\", "/")
        return boundary + token

    return _RAW_HEAD_TOKEN_RE.sub(_rewrite, cmd)


#: The leg tag `_wrapped_shell_c_payload_legs` attaches to a payload it
#: SYNTHESIZED from a Python `-c` payload's own quoted string literals
#: (`_python_c_payload_argv_text`), as opposed to a payload a shell or
#: Python interpreter genuinely received as executed text. Threaded through
#: the three matchers into `check()` and on to `_deny_reason` purely so the
#: deny message can NAME this leg -- it never participates in the verdict.
#:
#: Why it needs naming (2026-08-04, dispatched-executor report): the
#: reconstruction leg cannot tell a Python string literal that merely NAMES
#: a commit helper from a `subprocess.run([...])` call that INVOKES it --
#: by construction, since it discards all Python syntax between the
#: literals. So `python3 -c "import ast; ast.parse(open('coordinator/bin/
#: scoped-git-commit').read())"` -- a pure read -- denies identically to a
#: real commit, and the generic "finish your edits and report to the EM"
#: message names an action that resolves nothing: NO re-spelling of a
#: read-only command that contains that path will ever pass. Same defect
#: shape, and same fix shape, as the 2026-08-03 correction above
#: `_GIT_COMMIT_AGENT_DENY_REASON` (an agent re-trying argv variants
#: against a leg no argv change can fix).
_PAYLOAD_LEG_PYTHON_STRING_LITERALS = "payload-leg:python-string-literals"

#: The leg tag for part 16's mechanism 2 -- a Python ``-c`` payload whose
#: execution sink takes an argument the constant folder could not resolve.
#: Its own tag rather than a reuse of the one above, for MESSAGE ACCURACY:
#: `_PYTHON_C_PAYLOAD_DENY_REASON` tells the caller "no re-spelling passes",
#: which is true of a helper path named in a literal and FALSE here -- an
#: opaque argument spelled as a literal argv passes on the next attempt.
#: This module has already shipped one incident where a correct verdict came
#: with a message naming the wrong cause; see `_GIT_COMMIT_AGENT_LEG_
#: MESSAGES`' own note.
_PAYLOAD_LEG_PYTHON_OPAQUE_SINK = "payload-leg:python-opaque-sink"


def _wrapped_shell_c_payload_legs(
    cmd: str, depth: int = 0, leg: str = ""
) -> Iterator[Tuple[str, str]]:
    """The leg-tagged form of `_wrapped_shell_c_payloads` below -- yields
    ``(payload, leg)``, where ``leg`` is
    `_PAYLOAD_LEG_PYTHON_STRING_LITERALS` for a payload synthesized by
    `_python_c_payload_argv_text` (and for everything unwrapped BENEATH such
    a payload, which is reached only through that reconstruction) and ``""``
    for a payload a real interpreter received.

    A projection of `_wrapped_shell_c_payload_legs_with_head` (part 16), the
    single implementation all three views share -- it drops the interpreter
    head, which only the part-16 fold matchers need.

    NEGATIVE SPEC: the tag is a MESSAGE-SELECTION signal only. Nothing in
    this module may branch a verdict on it -- yield order, yield content,
    and the recursion bound are byte-identical to the untagged generator
    this was factored out of.
    """
    for payload, payload_leg, _head in _wrapped_shell_c_payload_legs_with_head(
        cmd, depth, leg
    ):
        yield payload, payload_leg


#: How many distinct ``(cmd, depth, leg)`` unwraps
#: `_wrapped_shell_c_payload_legs_with_head` remembers. Small on purpose:
#: one `check()` call re-unwraps the SAME command once per matcher (six, as
#: of part 16), and nothing needs a seventh command's history.
_MAX_UNWRAP_CACHE_ENTRIES = 32


@lru_cache(maxsize=_MAX_UNWRAP_CACHE_ENTRIES)
def _wrapped_shell_c_payload_legs_with_head(
    cmd: str, depth: int = 0, leg: str = ""
) -> Tuple[Tuple[str, str, str], ...]:
    """The single unwrap implementation this module has, returning
    ``(payload, leg, head)`` triples -- ``head`` being the normalized interpreter
    identity (`_normalized_interpreter_head`) that RECEIVED the payload, so
    a caller can tell Python SOURCE text from shell text without re-walking
    the command or re-deriving interpreter identity.

    Factored out in part 16 (2026-08-05) for exactly one consumer:
    `_python_c_source_payloads`, which must feed the constant folder Python
    source and nothing else. The two views above are projections, so this
    refactor is yield-for-yield identical to the pre-part-16 generator -- no
    existing matcher, message, or test that pins the leg list sees any
    change.

    NEGATIVE SPEC: ``head`` is a SOURCE-LANGUAGE discriminator, never an
    authorization signal. No verdict may be softened because a payload came
    from one interpreter rather than another; the only sanctioned use is
    deciding whether a payload is parseable as Python at all.

    MEMOIZED (part 16), and this is a hot-path correction rather than a
    micro-optimization: unwrapping is dominated by the shlex tokenizer,
    which is linear in COMMAND LENGTH, and every matcher in ``check()``
    unwraps the same command independently -- so the cost was already
    multiplied by the matcher count before part 16 added two more matchers
    and admitted more commands to this stage. Memoizing is verdict-neutral
    by construction (pure function of its three arguments; the returned
    tuple is immutable and shared, never mutated by a caller) and it makes
    the pre-part-16 worst case CHEAPER, not merely no worse. It returns a
    materialized tuple rather than a generator for the same reason: a
    generator cannot be cached, and every caller already consumes the whole
    sequence.
    """
    return tuple(_unwrap_payload_legs(cmd, depth, leg))


def _unwrap_payload_legs(cmd: str, depth: int, leg: str) -> Iterator[Tuple[str, str, str]]:
    """The uncached body of `_wrapped_shell_c_payload_legs_with_head` above
    -- see that function for the contract. Recursion deliberately goes back
    through the CACHED entrypoint, so a payload reached at two depths is
    tokenized once.
    """
    if depth > _MAX_COMMIT_UNWRAP_DEPTH:
        return
    cmd = _protect_interpreter_argv0_backslash_path(cmd)
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return
    for seg_tokens in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        stripped = _strip_env_prefix_for_commit_unwrap(list(seg_tokens))
        if not stripped:
            continue
        head = _normalized_interpreter_head(stripped[0])
        if head not in _C_FLAG_SHELL_INTERPRETERS and head not in _PYTHON_INTERPRETER_NAMES:
            continue
        # Finding 2 fix (2026-07-29, confirmed live): a bundled `-c` flag
        # (`sh -ic '...'`, `bash -ci "..."`) defeated the exact-token
        # `"-c" not in stripped[1:]` test -- the token is the literal string
        # `-ic`/`-ci`, never exactly `"-c"`, so the unwrap never fired and a
        # commit quoted as the bundled-flag payload was never re-scanned.
        c_flag_positions = [
            i for i in range(1, len(stripped)) if _BUNDLED_C_FLAG_RE.match(stripped[i])
        ]
        if not c_flag_positions:
            continue
        idx = c_flag_positions[0]
        if idx + 1 >= len(stripped):
            continue
        payload = stripped[idx + 1]
        yield payload, leg, head
        yield from _wrapped_shell_c_payload_legs_with_head(payload, depth + 1, leg)
        if head in _PYTHON_INTERPRETER_NAMES and not _python_c_payload_is_provably_inert(
            payload
        ):
            # C1 (2026-08-03): a Python `-c` payload is SOURCE TEXT, not
            # shell syntax -- re-tokenizing it above the way a shell `-c`
            # payload already tokenizes correctly does not reliably isolate
            # the argv-shaped words a `subprocess`/`os.system` call embeds
            # (see `_PYTHON_STRING_LITERAL_RE`'s docstring for the confirmed
            # empirical reason). Reconstruct a synthetic argv-shaped line
            # from the payload's own quoted string literals and feed THAT
            # back through this same generator too -- tagged, since a match
            # found only HERE is a match the reconstruction cannot tell
            # apart from a string literal that merely names the helper.
            #
            # Part 14 (2026-08-04): skipped entirely when the payload is
            # PROVABLY INERT (`_python_c_payload_is_provably_inert` -- an
            # allowlist walk that fails closed on anything it does not
            # recognize). A payload with no execution sink cannot spawn a
            # commit however its literals read, so reconstructing an argv
            # line from them can only produce a false positive there. Every
            # other leg -- the raw payload yield above, its own recursive
            # unwrap, and every matcher run against both -- is unchanged for
            # an inert payload, and a payload that is not PROVABLY inert
            # reaches this reconstruction exactly as it did before.
            argv_text = _python_c_payload_argv_text(payload)
            if argv_text:
                yield argv_text, _PAYLOAD_LEG_PYTHON_STRING_LITERALS, head
                yield from _wrapped_shell_c_payload_legs_with_head(
                    argv_text, depth + 1, _PAYLOAD_LEG_PYTHON_STRING_LITERALS
                )


def _wrapped_shell_c_payloads(cmd: str, depth: int = 0):
    """Yield the nested command-text payload of any ``sh -c``/``bash -c``/
    ``zsh -c``/``dash -c``/``ksh -c`` segment, OR any Python interpreter's
    ``-c`` segment (``python3 -c``/``python -c``, including versioned
    (``python3.11``) and interpreter-path-prefixed/Windows-launcher-suffixed
    spellings -- C1, 2026-08-03), in ``cmd`` (optionally ``env``-/
    assignment-prefixed, e.g. ``env sh -c '...'``, ``env python3 -c
    '...'``), so the caller can re-scan the ACTUAL executed text instead of
    treating the quoted ``-c`` argument as inert prose. See
    ``_C_FLAG_SHELL_INTERPRETERS``'s docstring for the confirmed shell
    bypass this originally closed, and the module docstring's 2026-08-03
    part-9 entry for the Python-interpreter bypass C1 closes the same way.
    Recurses (bounded by ``_MAX_COMMIT_UNWRAP_DEPTH``) so a wrapper nested
    inside a wrapper -- shell-in-shell, python-in-shell, or shell-in-python
    -- is also unwrapped.

    The untagged view of `_wrapped_shell_c_payload_legs` above (which holds
    the single implementation both share): every caller that only needs the
    payload TEXT keeps this signature, so no matcher, and no test pinning
    this generator's yield list, has to know about the leg tag.
    """
    for payload, _leg in _wrapped_shell_c_payload_legs(cmd, depth):
        yield payload


def _python_c_source_payloads(cmd: str) -> Iterator[str]:
    """Yield, in order and without repeats, every payload in ``cmd`` that a
    PYTHON interpreter received through ``-c`` -- i.e. genuine Python source
    text, never a shell payload and never the synthetic argv line
    `_python_c_payload_argv_text` rebuilds.

    The two exclusions are the point (part 16, 2026-08-05). The constant
    folder below parses what it is handed as Python; handing it shell text
    that happens to parse, or a reconstruction whose word order is an
    artifact of where quotes fell, would have it fold expressions no
    interpreter will ever evaluate. Both are filtered HERE, at the source,
    rather than by each consumer.
    """
    seen: Set[str] = set()
    for payload, leg, head in _wrapped_shell_c_payload_legs_with_head(cmd):
        if leg or head not in _PYTHON_INTERPRETER_NAMES:
            continue
        if payload in seen:
            continue
        seen.add(payload)
        yield payload


def _record_payload_leg(legs: Optional[Set[str]], leg: str) -> None:
    """Record the unwrap leg a matcher's match was reached through, when the
    caller supplied a collector.

    The three matchers below take ``legs`` for one reason only: so
    `_deny_reason` can NAME the leg instead of re-deriving it by re-scanning
    the command (which is how the guard would end up with two independently
    drifting notions of why it denied -- the exact prefilter/matcher drift
    this module's own history keeps producing). NEGATIVE SPEC: no verdict
    reads this collector, and a ``None`` collector must leave every matcher
    byte-identical in behaviour to before it existed.
    """
    if leg and legs is not None:
        legs.add(leg)


def _has_git_commit(cmd: str, *, legs: Optional[Set[str]] = None) -> bool:
    """True if any ``;``/``&``/``|``-delimited segment of ``cmd`` resolves
    to a ``git commit`` invocation, tolerating arbitrary git global options
    between ``git`` and ``commit`` (see ``_tokens_reach_commit_after_git``).

    2026-07-26 quote/heredoc hardening: segmentation and tokenization now go
    through ``_normalize_windows_git_argv0`` + ``_tokenize_full_command`` +
    ``_segments_from_tokens`` (shlex-based, quote-aware) instead of a
    quote-blind regex split -- see the section comment above
    ``_QUOTE_OPEN_CHARS`` for the false-positive this replaces. ``cmd`` is
    expected to already have heredoc BODIES stripped by the caller
    (``check()`` calls ``_strip_heredoc_bodies`` once, before either
    matcher, per the module-level fix note); this function does not strip
    heredocs itself so it is never called twice on the same input.

    On an unparseable ``cmd`` (unterminated quote after heredoc-stripping),
    fails CLOSED -- returns ``True`` (commit-bearing) rather than silently
    allowing a malformed command through unscanned.
    """
    normalized = _normalize_windows_git_argv0(cmd)
    tokens = _tokenize_full_command(normalized)
    if tokens is None:
        return True
    found = False
    for seg_tokens in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        if _tokens_reach_commit_after_git(seg_tokens):
            found = True
    if found:
        return True
    # BX-13: unwrap `sh -c '...'`/`bash -c "..."`/etc. and re-scan the
    # ACTUAL executed payload -- see `_C_FLAG_SHELL_INTERPRETERS` docstring.
    for payload, leg in _wrapped_shell_c_payload_legs(normalized):
        if _has_git_commit(payload, legs=legs):
            _record_payload_leg(legs, leg)
            return True
    return False


# ``_token_matches_binary`` (2026-07-29, part 2): no longer an own-module
# copy. It is now ``_command_tokenizer.token_matches_binary``, imported
# above alongside the tokenizer trio -- see that function's docstring for
# the ``.exe``-blindness bypass this consolidation closes (this module's
# own git-commit detection previously did NOT recognize a
# ``git.exe``/``GIT.EXE``-spelled commit at all; see the module docstring's
# 2026-07-25-part-2 entry, which flagged this as out-of-scope-by-design at
# the time -- that follow-up is this change). This helper carries no
# per-caller behavioral divergence from its sibling in
# ``block_reviewer_bash_outside_allowlist.py`` -- both were byte-for-byte
# identical modulo the shared ``.exe`` gap, so unifying them is a pure
# consolidation, not a behavior choice made for one caller over another.
#
# ``_extract_first_token`` (this module's OWN prior copy, quote/space-BLIND
# by construction -- split on the first raw space, quote-strip only a
# single token matched start-to-end) is RETIRED as of 2026-07-29 part 4: it
# is no longer imported or called anywhere in this module. See
# ``_has_coordinator_safe_commit`` below, which now shares
# ``_tokenize_full_command`` / ``_segments_from_tokens`` with
# ``_has_git_commit`` instead of hand-splitting.


#: Passthrough wrapper binaries that run their remaining argv unchanged --
#: see `_first_effective_token`'s BX-13 fix comment. Same set
#: `dispatch_checks.py`'s `_BYPASS_PREFIX` already tolerates.
#: Widened (2026-07-29, code-reviewer Finding 3) -- see
#: `block_subagent_destructive_action.py`'s sibling copy for the full
#: rationale: `setsid`/`strace`/`doas`/`busybox` were unrecognized
#: passthrough wrappers.
_PASSTHROUGH_WRAPPERS_FOR_COMMIT = frozenset(
    {
        "sudo", "command", "time", "exec", "nice", "nohup", "ionice", "timeout",
        "stdbuf", "which", "type", "setsid", "strace", "doas", "busybox",
    }
)

#: Bundled-or-standalone `-c` short flag, e.g. `-c`, `-ic`, `-ci` (Finding 2,
#: 2026-07-29 code review): a shell's CLI parser accepts bundled short
#: flags, so `sh -ic '<payload>'` behaves as `sh -i -c '<payload>'` -- an
#: exact `"-c" in tokens` check misses this entirely. Own-module copy of
#: `block_subagent_destructive_action.py`'s `_BUNDLED_C_FLAG_RE` (no-cross-
#: module-coupling convention).
_BUNDLED_C_FLAG_RE = re.compile(r"^-[a-zA-Z]*c[a-zA-Z]*$")

#: BX-14 fix (2026-07-29, confirmed live via the real dispatcher): the peel
#: below tolerated the wrapper BINARY token but never the wrapper's OWN
#: argument(s) -- `timeout 30 coordinator-safe-commit -m x`, `ionice -c2
#: coordinator-safe-commit -m x`, `stdbuf -oL coordinator-safe-commit -m x`
#: all resolved the "first effective token" to `30`/`-c2`/`-oL` (never the
#: real binary), so `_has_coordinator_safe_commit` never recognized the
#: invocation while it still ran for real. Own-module copy of
#: `dispatch_checks.py`'s `_BYPASS_WRAPPER_ARG_FLAGS` (no-cross-module-
#: coupling convention).
#: `_skip_wrapper_own_argv_for_commit` itself now lives in
#: `_command_tokenizer.py` as `_skip_wrapper_own_argv` (2026-07-30, M8
#: consolidation) -- imported above under this file's own prior name rather
#: than hand-maintained here; see that module's own docstring for the
#: five-copy history this closes.


def _peeled_effective_tokens(seg_tokens: list) -> list:
    """Return ``seg_tokens`` with every no-op prefix stripped -- brace
    grouping, leading assignments, an ``env`` invocation (with its own
    assignments/``-i``), and a run of passthrough wrappers
    (``_PASSTHROUGH_WRAPPERS_FOR_COMMIT``) -- WITHOUT collapsing to just the
    resulting head token the way ``_first_effective_token`` does.

    Split out of ``_first_effective_token`` (2026-08-03, C3 of
    ``2026-08-03-narrow-subagent-commit-confinement-two-classes.md``) so the
    scoped-git-commit pathspec extractors below (``_extract_trampoline_
    scoped_git_commit_paths``) can reach the TRAILING arguments of a
    wrapper-peeled invocation too, not merely identify its binary. Pure
    refactor -- ``_first_effective_token`` now delegates here and its
    observable behaviour (including every fix documented on its own
    docstring below) is unchanged.
    """
    if not seg_tokens:
        return []
    tokens = list(seg_tokens)
    # UNIFIED-LOOP FIX (2026-07-29, code-reviewer Finding 1, confirmed live):
    # the four peel stages below used to run as four SEQUENTIAL blocks, each
    # exactly once, in a fixed order -- unlike this same module's sibling
    # unified peels (`block_subagent_destructive_action.py`'s
    # `_strip_leading_subshell_and_env`, `_sentinel_creation_guard.py`'s
    # `_env_skip_index`), which re-check every stage each iteration. Because
    # the one-shot `env` check ran BEFORE the wrapper-peel loop and was never
    # revisited, a wrapper-THEN-env stacking (`nice env FOO=1
    # coordinator-safe-commit -m x`) was never fully peeled: once `nice` was
    # consumed by the wrapper loop, the loop never went back to re-check for
    # `env`, so this function returned `"env"`, not `"coordinator-safe-
    # commit"`, and `_has_coordinator_safe_commit` missed the invocation. The
    # reverse order (`env nice cmd`) resolved correctly, because the one-shot
    # `env` check happened first -- that asymmetry was the tell. Wrapping all
    # four stages in an outer loop that re-runs until a full pass makes no
    # further progress closes the gap for any stacking order/depth, mirroring
    # the sibling modules' unified-loop shape without importing across
    # modules (this file's established no-cross-module-coupling convention
    # for small tokenizer-adjacent helpers -- see
    # `_strip_env_prefix_for_commit_unwrap`'s docstring).
    while True:
        before = len(tokens)
        # BRACE-GROUPING FIX (2026-07-29, code-reviewer Finding 1, confirmed
        # live): `{ coordinator-safe-commit -m x; }` was never peeled here --
        # the leading `{` token was left in place, so the "first effective
        # token" resolved to `{` itself, never the invoked binary. Bash
        # requires a space after `{` (a reserved word, not an operator like
        # `(`), so `shlex.split` always yields it as its own token. The plain
        # `git commit` path (`_tokens_reach_commit_after_git`) is unaffected
        # by this same shape -- it scans every token in the segment for a
        # `git` match rather than requiring one at position 0.
        while tokens and tokens[0] == "{":
            tokens = tokens[1:]
        # BX-13 fix (2026-07-29, confirmed live): a leading `VAR=value`
        # assignment (`FOO=1 coordinator-safe-commit -m x`) was never skipped
        # here, so the "first effective token" was the assignment itself,
        # never the invoked binary -- unlike `_tokens_reach_commit_after_git`
        # (the sibling `git`-commit matcher), which scans the WHOLE segment
        # for a `git` token rather than requiring it at position 0, so it was
        # never affected by this same gap.
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens = tokens[1:]
        # Same fix, `env` form: `env coordinator-safe-commit -m x` -- an
        # `env` invocation (optionally with its OWN assignments/`-i`) is a
        # no-op passthrough, so peel it the same way
        # `_strip_env_prefix_for_commit_unwrap` already does for the `sh -c`
        # unwrap path above.
        if tokens and _normalize_executable_basename(tokens[0]) == "env":
            tokens = tokens[1:]
            while tokens and (
                tokens[0] in ("-i", "--ignore-environment")
                or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0])
            ):
                tokens = tokens[1:]
        # Same fix, no-op passthrough-wrapper form (`nice coordinator-safe-
        # commit -m x`, `time ...`, etc.) -- peel a run of these too, same
        # set `dispatch_checks.py`'s `_BYPASS_PREFIX` already tolerates.
        while tokens and _normalize_executable_basename(tokens[0]) in _PASSTHROUGH_WRAPPERS_FOR_COMMIT:
            base = _normalize_executable_basename(tokens[0])
            tokens = tokens[1:]
            skip = _skip_wrapper_own_argv_for_commit(tokens, 0, base)
            tokens = tokens[skip:]
        if len(tokens) == before:
            break
    return tokens


def _first_effective_token(seg_tokens: list) -> str:
    """Return the token that identifies the invoked binary at the head of
    ``seg_tokens`` -- the first token, or the SECOND token if the first is
    the literal ``python3`` (the ``python3 <path>`` invocation form; see
    ``_has_coordinator_safe_commit``'s docstring). Returns ``""`` for an
    empty segment.

    This is the shlex-tokenized replacement for the retired
    ``_extract_first_token``'s ``python3 ``-prefix-stripping step -- with
    real tokens (not a raw string), the prefix is just "is the first token
    exactly ``python3``", no string slicing or re-joining involved. Thin
    wrapper over ``_peeled_effective_tokens`` (2026-08-03 split) -- see that
    function's docstring for the full peel-stage history.
    """
    tokens = _peeled_effective_tokens(seg_tokens)
    if not tokens:
        return ""
    if tokens[0] == "python3" and len(tokens) >= 2:
        return tokens[1]
    return tokens[0]


def _has_coordinator_safe_commit(cmd: str, *, legs: Optional[Set[str]] = None) -> bool:
    """True if any ``;``/``&``/``|``-delimited segment of ``cmd`` invokes
    any binary in ``_COMMIT_HELPER_BINARY_NAMES`` -- currently
    ``coordinator-safe-commit`` and ``scoped-git-commit`` -- bare,
    ``bin/``-prefixed, absolute-path, ``python3 <path>``-prefixed, or a
    Windows ``.cmd``-suffixed spelling -- since each helper shells out to
    ``git commit`` (or, for ``scoped-git-commit``, dispatches the same
    ``ceremony.scoped_git_commit`` op) and is therefore in scope for this
    gate exactly like a direct ``git commit`` invocation (see the module
    docstring's deny-by-default paragraph). ``coordinator-safe-commit`` was
    previously entirely unchecked -- one of the three confirmed 2026-07-25
    bypasses.

    2026-07-29 part 4 rebuild (closes the quote-blindness the part-3 entry
    found and deliberately left open): this used to hand-split ``cmd`` with
    a quote-BLIND ``re.split`` + a local ``_extract_first_token`` that broke
    on the first raw space regardless of quoting -- an already-quoted or
    ordinary-unquoted Windows argv0-head path with an embedded space (a
    ``coordinator-safe-commit`` installed under a spaced directory, e.g. a
    Windows username with a space in it) was mis-split into a path FRAGMENT
    that matched neither this binary nor ``git``, so the invocation slipped
    through as ALLOW -- confirmed through the live ``check()`` entrypoint.
    Now shares the SAME canonical machinery ``_has_git_commit`` uses:
    ``cmd`` is expected pre-normalized by the caller (``check()`` runs
    ``_normalize_windows_argv0_head_path_with_spaces`` once, before either
    matcher, per the module-level fix note) for the embedded-space case;
    this function additionally runs ``_normalize_windows_git_argv0`` (now
    generalized to recognize these binaries too, not just ``git`` -- see
    that function's own docstring) for the plain backslash-path case, then
    tokenizes with ``_tokenize_full_command`` and walks each segment's
    effective first token through the shared, boundary-anchored
    ``token_matches_binary``. Fails CLOSED on an unparseable ``cmd``
    (unterminated quote after heredoc-stripping), matching
    ``_has_git_commit``'s fail-direction, rather than silently allowing a
    malformed command through unscanned.

    2026-08-01 part 7 (C5): generalized from a single-binary
    (``coordinator-safe-commit``-only) check to a basename-SET check against
    ``_COMMIT_HELPER_BINARY_NAMES``, rather than adding a fourth
    near-duplicate matcher function for ``coordinator/bin/scoped-git-commit``
    -- the newly-discovered helper that spawns
    ``python3 -m coordinator_core.invoke ceremony.scoped_git_commit`` as a
    raw subprocess from INSIDE an already-permitted Bash-tool process, so
    the PreToolUse chain never re-inspects the real commit. Confirmed
    unmatched by any of this module's three prior matchers before this fix:
    ``_has_git_commit`` needs a token whose basename is exactly ``git``,
    this function needed exactly ``coordinator-safe-commit``, and
    ``_has_committing_op_invoke`` needs the literal
    ``python3 -m coordinator_core.invoke`` shape at the CALLING shell
    layer (``scoped-git-commit`` hides that shape one process down). Same
    boundary discipline as before: ``evil-scoped-git-commit`` still does not
    match (nothing precedes ``evil-`` to anchor a path-separator boundary
    before the literal name).
    """
    # Review: coordinator:code-reviewer (B-commit-matchers Finding 3, nit) --
    # this call is NOT confirmed redundant despite check()'s outer
    # `_normalize_windows_argv0_head_path_with_spaces` pass: that outer pass
    # runs once, on the TOP-LEVEL `cmd_for_scan`, but `_wrapped_shell_c_
    # payloads` recursion below calls this function again on an UNWRAPPED
    # `sh -c`/`bash -c` payload that never went through the outer pass --
    # for that recursive path, this is the only normalization applied
    # (backslash-only Windows argv0 case; the embedded-space case is a
    # separate, out-of-scope gap at nesting depth, same as `_has_git_
    # commit`'s equivalent recursive call). Left in place; not dropped.
    normalized = _normalize_windows_git_argv0(cmd)
    tokens = _tokenize_full_command(normalized)
    if tokens is None:
        return True
    found = False
    for seg_tokens in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        first_token = _first_effective_token(seg_tokens)
        if any(
            _token_matches_binary(first_token, name)
            for name in _COMMIT_HELPER_BINARY_NAMES
        ):
            found = True
    if found:
        return True
    # BX-13: same unwrap as `_has_git_commit` -- a `coordinator-safe-commit`
    # invocation quoted as a shell interpreter's `-c` argument is executed,
    # not inert text.
    for payload, leg in _wrapped_shell_c_payload_legs(normalized):
        if _has_coordinator_safe_commit(payload, legs=legs):
            _record_payload_leg(legs, leg)
            return True
    return False


#: The op names this gate treats as committing ops -- each one, invoked via
#: ``python3 -m coordinator_core.invoke <op> ...``, lands a real ``git
#: commit`` on the shared worktree just like a plain ``git commit`` or
#: ``coordinator-safe-commit`` does. ``ceremony.scoped_git_commit`` is the
#: claude-klabauter-native op ``coordinator-safe-commit`` itself shells out to (see
#: the module docstring's "2026-07-29 update, part 5" entry). The other four
#: -- ``session.boot_sweep`` (3 ``git commit`` sites), ``distill.apply_
#: disposal``, ``memo.send``, and ``ceremony.wsc_tail`` (which goes straight
#: to ``run_commit_pipeline``) -- were added 2026-08-01 (hole (a) fix, see
#: the module docstring's part-6 entry): none of their names contain the
#: substring ``commit``, so the OLD ``"commit" in cmd`` pre-filter
#: short-circuited to ALLOW before identity resolution ever ran for any of
#: them, with no obfuscation required. This is a single named set both
#: ``_prefilter_mentions_commit`` and the invoke-matcher below consult, so
#: the two cannot independently drift the way the pre-filter/full-matcher
#: pair already drifted once before (see the module docstring's 2026-07-25
#: "LESSON" entry).
_COMMITTING_OP_NAMES = frozenset(
    {
        "ceremony.scoped_git_commit",
        "session.boot_sweep",
        "distill.apply_disposal",
        "memo.send",
        "ceremony.wsc_tail",
        # Review: coordinator:code-reviewer (B-commit-matchers Finding 1, P0,
        # confirmed live by fleet.archive_shipped_handoffs landing commit
        # d9282543f this session) -- six more registered, directly-invocable
        # committing ops verified against source, none containing the
        # substring "commit" in their op name:
        "commit.exec_bit_change",           # ceremony/commit_exec_bit.py -- _git(["commit", ...])
        "ceremony.post_commit_tail",        # ceremony/post_commit_tail.py -- commit_scoped(...)
        "fleet.archive_shipped_handoffs",   # fleet/archive_shipped_handoffs.py -- archive_and_commit(...)
        "fleet.archive_release_accumulator",  # fleet/archive_release_accumulator.py -- archive_and_commit(...)
        "fleet.reap_unintegrated_findings",  # fleet/reap_unintegrated_findings.py -- rm_and_commit(...)
        "fleet.reap_integrated_findings",   # fleet/reap_integrated_findings.py -- rm_and_commit(...)
        # Review: coordinator:review-integrator sibling sweep (same finding,
        # same session) -- the reviewer's own six did not exhaust the ops
        # tree; a fresh grep for every registered op whose handler calls
        # archive_and_commit(/rm_and_commit(/commit_scoped( (or delegates to
        # one, as handoff.ship_and_archive does via archive_shipped_handoffs'
        # own _handle_act) turned up eight more:
        "fleet.archive_actioned_memos",     # fleet/archive_actioned_memos.py -- archive_and_commit(...)
        "fleet.archive_completed_handoffs",  # fleet/archive_handoffs.py -- archive_and_commit(...)
        "fleet.archive_paper_trail",        # fleet/archive_paper_trail.py -- archive_and_commit(...)
        "fleet.archive_completed_plans",    # fleet/archive_plans.py -- archive_and_commit(...)
        "fleet.archive_queue_entry",        # fleet/archive_queue_entry.py -- archive_and_commit(...)
        "fleet.prune_closed_bugs",          # fleet/prune_bugs.py -- archive_and_commit(...)
        "handoff.archive_transition",       # handoff_archive_transition.py -- archive_and_commit(...)
        "handoff.ship_and_archive",         # handoff_ship_archive.py -- delegates to archive_shipped_handoffs._handle_act (archive_and_commit)
    }
)
_CEREMONY_INVOKE_MODULE = "coordinator_core.invoke"
_PYTHON_INTERPRETER_NAMES = ("python3", "python")

#: ``coordinator_core.invoke``'s own optional flags that may precede the
#: ``<op>`` positional (see ``coordinator_core/invoke/__main__.py::
#: _build_arg_parser`` for the authoritative flag surface this set is
#: derived from) -- split by whether the flag consumes a SEPARATE following
#: token as its value. ``--repo`` and ``--params-file`` do; ``--dump-op-
#: timeouts``, ``--bare``, and argparse's own ``-h``/``--help`` do not.
_INVOKE_FLAGS_WITH_VALUE = frozenset({"--repo", "--params-file"})
_INVOKE_FLAGS_NO_VALUE = frozenset({"--dump-op-timeouts", "--bare", "-h", "--help"})


def _first_positional_after_invoke_module(tokens: list, start: int) -> Optional[int]:
    """Return the index of the first POSITIONAL token at/after ``start`` --
    the CLI's ``<op>`` argument -- skipping any of ``coordinator_core.
    invoke``'s own leading optional flags in between, INCLUDING value-taking
    ones (``--repo <path>``, ``--params-file <path>``, skipped two tokens)
    and ``--flag=value`` attached forms (skipped one token). Returns
    ``None`` if no positional token is found before the end of ``tokens``.

    Hole (b) fix (2026-08-01, see the module docstring's part-6 entry): the
    OLD matcher required the op token IMMEDIATELY after the module name,
    but ``--repo`` is effectively required by this CLI (AC-5's no-implicit-
    cwd-fallback rule), so the documented spelling
    ``python3 -m coordinator_core.invoke --repo /path ceremony.scoped_git_
    commit '{}'`` walked straight past a positional-adjacency check. Op
    resolution must be flag-tolerant, not positional-adjacency-based. A
    flag not in either named set above (this CLI's flag surface is closed
    and fully enumerated from ``_build_arg_parser``) is conservatively
    treated as taking no separate value -- it is skipped one token at a
    time, which keeps the scan making forward progress either way.
    """
    i = start
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok.startswith("-"):
            return i
        flag_name = tok.split("=", 1)[0]
        if flag_name in _INVOKE_FLAGS_WITH_VALUE:
            i += 1 if "=" in tok else 2
            continue
        i += 1
    return None


def _tokens_reach_committing_op_after_python(tokens: list) -> bool:
    """Token walk, mirroring ``_tokens_reach_commit_after_git``'s own
    full-segment-scan shape: for every token that boundary-matches the
    ``python3`` or ``python`` binary (``_token_matches_binary`` -- bare,
    ``.exe``-suffixed, case-folded, or an absolute/path-prefixed spelling),
    walk forward past a run of Python's own leading global flags
    (``-u``, ``-B``, ``-O``, ``-I``, ...; no argument-consuming flag needs
    special-casing here since we stop looking the moment a non-flag token
    or ``-m`` appears) looking for the literal ``-m`` flag, then require the
    module argument to be exactly ``coordinator_core.invoke``, then resolve
    the first POSITIONAL after the module name (flag-tolerant --
    ``_first_positional_after_invoke_module``, hole (b) fix) and check it
    against ``_COMMITTING_OP_NAMES`` (hole (a) fix -- any committing op
    name, not just ``ceremony.scoped_git_commit``). A full-array scan (not
    a position-0-only check) means a preceding wrapper/env-assignment
    prefix (``env FOO=1 python3 -m ...``, ``timeout 30 python3 -m ...``)
    needs no separate peeling step -- the loop simply finds the
    ``python``/``python3`` token wherever it sits in the segment, exactly
    like the sibling ``git``-commit walk already does for a preceding
    wrapper.
    """
    n = len(tokens)
    for start in range(n):
        if not any(
            _token_matches_binary(tokens[start], name)
            for name in _PYTHON_INTERPRETER_NAMES
        ):
            continue
        i = start + 1
        while i < n and tokens[i] != "-m" and tokens[i].startswith("-"):
            i += 1
        if i >= n or tokens[i] != "-m":
            continue
        i += 1
        if i >= n or tokens[i] != _CEREMONY_INVOKE_MODULE:
            continue
        i += 1
        op_idx = _first_positional_after_invoke_module(tokens, i)
        if op_idx is not None and tokens[op_idx] in _COMMITTING_OP_NAMES:
            return True
    return False


def _has_committing_op_invoke(cmd: str, *, legs: Optional[Set[str]] = None) -> bool:
    """True if any ``;``/``&``/``|``-delimited segment of ``cmd`` invokes
    ``python3 -m coordinator_core.invoke <op>`` for any op in
    ``_COMMITTING_OP_NAMES`` (or the ``python``-spelled / interpreter-path-
    prefixed / wrapper-prefixed / flag-preceded equivalents -- see
    ``_tokens_reach_committing_op_after_python``).

    Shares ``_has_git_commit``'s BX-13 ``sh -c``/``bash -c``/etc. unwrap (a
    shell interpreter's ``-c`` argument is executed text, not inert prose)
    and relies on the caller (``check()``) having already stripped heredoc
    BODY lines before this function ever sees ``cmd``, same as the other
    two matchers. Fails CLOSED on an unparseable ``cmd`` (unterminated
    quote after heredoc-stripping), matching both sibling matchers'
    fail-direction.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return True
    found = False
    for seg_tokens in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        if _tokens_reach_committing_op_after_python(seg_tokens):
            found = True
    if found:
        return True
    for payload, leg in _wrapped_shell_c_payload_legs(cmd):
        if _has_committing_op_invoke(payload, legs=legs):
            _record_payload_leg(legs, leg)
            return True
    return False


def _tokens_reach_commit_identity_anywhere(tokens: list) -> bool:
    """True if ANY token position in ``tokens`` carries a commit identity --
    a `_COMMIT_HELPER_BINARY_NAMES` binary, a ``git ... commit`` chain
    (`_tokens_reach_commit_after_git`), or a ``python3 -m
    coordinator_core.invoke <committing-op>`` shape
    (`_tokens_reach_committing_op_after_python`).

    Used ONLY by `_has_reconstructed_commit_identity` below, against the
    synthetic argv line `_python_c_payload_argv_text` rebuilds from a Python
    ``-c`` payload's string literals. The helper leg is the one this adds:
    the other two matchers already scan a whole segment rather than its
    head, and are folded in here so the reconstruction has ONE notion of
    commit identity instead of a second set that can drift from the first.

    NEGATIVE SPEC: this does not re-derive identity resolution. Every token
    goes through the same shared, boundary-anchored `_token_matches_binary`
    the argv0 path uses, so ``evil-scoped-git-commit`` matches here exactly
    as little as it does there -- position is the ONLY thing relaxed. Never
    call this on a real shell segment, a real ``sh -c`` payload, or the
    outer command: those are genuine command lines where argv0 selects the
    binary and a trailing mention is an argument, not an invocation.
    """
    for tok in tokens:
        if any(
            _token_matches_binary(tok, name) for name in _COMMIT_HELPER_BINARY_NAMES
        ):
            return True
    if _tokens_reach_commit_after_git(tokens):
        return True
    return _tokens_reach_committing_op_after_python(tokens)


def _nested_reconstruction_chain(payload: str) -> Iterator[str]:
    """Yield ``payload``, then each successive re-extraction of the string
    literals still inside it (`_python_c_payload_argv_text`), until no
    literal remains or `_MAX_COMMIT_UNWRAP_DEPTH` is reached.

    One reconstruction pass is not enough for the code-in-a-string family:
    ``eval("__import__('os').system('<helper>')")`` has exactly ONE
    top-level literal, so the first pass reconstructs that literal's whole
    body as a single whitespace-free token -- the helper is inside it, but
    not AS a token, so no boundary-anchored matcher can see it. Re-running
    the same extraction over that body yields ``os <helper>``, where it is a
    token again. ``exec(...)`` and ``exec(compile("...", '<s>', 'exec'))``
    collapse the same way, one level deeper.

    NEGATIVE SPEC: consumed ONLY by `_has_reconstructed_commit_identity`,
    and only for a payload already tagged
    `_PAYLOAD_LEG_PYTHON_STRING_LITERALS`. This is not a Python evaluator
    and makes no claim about what the nested source would DO -- it recovers
    word boundaries the outer quoting swallowed, nothing more. It is
    terminating by construction: each pass keeps only the INTERIORS of
    quoted runs, so the text strictly shortens or the chain stops.
    """
    seen = payload
    depth = 0
    while depth <= _MAX_COMMIT_UNWRAP_DEPTH:
        yield seen
        nested = _python_c_payload_argv_text(seen)
        if not nested or nested == seen:
            return
        seen = nested
        depth += 1


def _has_reconstructed_commit_identity(
    cmd: str, *, legs: Optional[Set[str]] = None
) -> bool:
    """True if the SYNTHETIC argv line reconstructed from a Python ``-c``
    payload's own quoted string literals carries a commit identity at any
    token position (`_tokens_reach_commit_identity_anywhere`).

    Part 15 (2026-08-04): closes a confirmed-live bypass of the three
    matchers above. `_python_c_payload_argv_text` joins the payload's
    literals in APPEARANCE order, and `_has_coordinator_safe_commit`
    resolves the helper at the segment's effective FIRST token -- so a sink
    whose own literals come first pushes the helper out of argv0 and the
    match never happens: ``__import__('os').system('<helper> -m x')``
    reconstructs to ``os <helper> -m x`` and ALLOWED. The reconstruction is
    not a command line; nothing executes it and argv0 semantics do not
    meaningfully apply to it, so position-sensitivity there is a defect
    rather than a discipline.

    Scope, stated narrowly because it is the whole safety argument: this
    consumes ONLY payloads tagged `_PAYLOAD_LEG_PYTHON_STRING_LITERALS` --
    the reconstruction and whatever unwraps beneath it. Real shell
    segments, real ``sh -c`` payloads, and the outer command keep their
    argv0-anchored semantics untouched, in the three matchers above.

    Designed PAIR with `_python_c_payload_is_provably_inert` (part 14), and
    the ordering between them is load-bearing in BOTH directions: matching
    at any position necessarily widens what the reconstruction flags, which
    on its own would deny read-only commands that merely NAME a helper in a
    string; the inert check absorbs exactly that, because a provably-inert
    payload never reaches the reconstruction leg at all and so never
    reaches this matcher. A future editor removing either one alone
    re-opens the other's defect -- part 14 alone leaves this bypass live,
    this matcher alone re-introduces part 14's read-only false positive in
    a wider form.

    Nested-literal re-extraction (`_nested_reconstruction_chain`) is applied
    HERE rather than in the leg generator, deliberately: it is what reaches
    the ``eval("...")``/``exec("...")``/``compile(...)`` family, whose
    payload collapses to ONE literal holding a whole second program, and
    keeping it local means `_wrapped_shell_c_payload_legs`' yield list --
    and every matcher and test pinned to it -- is untouched by part 15.

    Fails CLOSED on an unparseable reconstruction, matching all three
    sibling matchers' fail-direction.
    """
    for payload, leg in _wrapped_shell_c_payload_legs(cmd):
        if leg != _PAYLOAD_LEG_PYTHON_STRING_LITERALS:
            continue
        for text in _nested_reconstruction_chain(payload):
            tokens = _tokenize_full_command(text)
            if tokens is None:
                _record_payload_leg(legs, leg)
                return True
            for seg_tokens in _segments_from_tokens(tokens):
                if not seg_tokens:
                    continue
                if _tokens_reach_commit_identity_anywhere(seg_tokens):
                    _record_payload_leg(legs, leg)
                    return True
    return False


# --- Part 16 (2026-08-05): the STRING-ASSEMBLY residual parts 13-15 all
#     documented and none could reach -- a name that was never contiguous
#     text (`'scoped-git'+'-commit'`, `''.join(map(chr,[...]))`, a base64
#     blob handed to `exec`, `os.environ['X']`). Two mechanisms, ordered:
#     (1) fold the compile-time-constant expressions and feed the folded
#     values into the same identity matching part 15 built; (2) refuse a
#     non-inert payload whose execution sink takes an argument the fold
#     could NOT resolve, because "we cannot know what it runs" is not a
#     reason to let it run in this seam. ---

#: Hard bounds on the constant folder below. LOAD-BEARING SECURITY
#: PROPERTIES, NOT TUNING KNOBS: this folder runs on attacker-authored text
#: inside a PreToolUse hook on every Bash call, so an unbounded folder is a
#: denial-of-service on the hot path (``'a' * 10**9``, a 50k-term
#: concatenation) and a memory bomb in a process the caller cannot restart.
#:
#: SCOPE, STATED EXACTLY (part 22, 2026-08-05, after a review found the
#: framing below overstated): every bound here is PER ``python3 -c`` PAYLOAD,
#: not per Bash call. `_fold_python_c_payload` builds a fresh `_FoldBudget`
#: for each payload `_python_c_source_payloads` yields, so a command chaining
#: N distinct ``python3 -c`` segments gets N independent budgets and total
#: fold work for one `check()` scales linearly in N. What bounds the PER-CALL
#: cost is therefore not these constants but the tokenizer's command-length
#: ceiling (``_MAX_TOKENIZABLE_COMMAND_CHARS``, 64 KiB): spelling a segment
#: costs characters, so the ceiling caps how many segments one command can
#: carry. Measured worst case at that ceiling (2026-08-05): 329 chained
#: segments in 64,810 chars -> 86 ms in the matcher, 1,578 ms for a full
#: dispatch. Bounded, and the outer bound is the ceiling doing the work.
#:
#: Every bound below FAILS CLOSED -- exceeding one makes the value
#: UNRESOLVABLE, which mechanism 2 then treats exactly as it treats
#: ``os.environ['X']`` (deny), never as "small enough to be safe". Raising
#: any of them buys a marginally deeper fold at the cost of a wider DoS
#: window; do not raise them to make one corpus row fold.
_MAX_FOLDED_VALUE_LEN = 4096
#: The AGGREGATE bound, and it is not implied by the per-value one: a
#: payload whose outer expression does not fold still contributes every
#: INNER value that does (``('a'*4000)*4000`` folds the inner half), so the
#: collected line is bounded by per-value length TIMES node count -- 8MB of
#: transient string in a hook process without this cap. Found by a corpus
#: row, not by inspection, which is the argument for keeping that row.
_MAX_FOLDED_TOTAL_LEN = 65536
_MAX_FOLD_NODES = 2000
_MAX_FOLD_DEPTH = 12
_MAX_FOLD_FIELD_WIDTH = 4096
_MAX_FOLD_CACHE_ENTRIES = 32


class _UnresolvedValue:
    """The folder's "I could not compute this" sentinel -- distinct from
    every legitimate Python value a fold can produce (``None``, ``0``,
    ``""`` and ``False`` are all resolvable RESULTS and must never be
    confused with a failure to resolve).
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<unresolved>"


_FOLD_UNRESOLVED = _UnresolvedValue()


class _FoldBudget:
    """Mutable node-visit budget threaded through one payload's fold.

    ``exceeded`` latches True the moment any bound in this part's constant
    block is hit, and is never reset -- a payload that blew a bound is
    reported as such for the whole fold, so no caller can read a partial
    result as a complete one.
    """

    __slots__ = ("nodes", "exceeded")

    def __init__(self) -> None:
        self.nodes = _MAX_FOLD_NODES
        self.exceeded = False

    def spend(self) -> bool:
        if self.nodes <= 0:
            self.exceeded = True
            return False
        self.nodes -= 1
        return True


class _FoldedPayload(NamedTuple):
    """One payload's fold result. ``text`` is the space-joined line of every
    statically-resolved string the payload builds (mechanism 1's input);
    ``opaque_sink_call`` is mechanism 2's trigger; ``bounds_exceeded`` and
    ``parsed`` record WHY a fold came back thin, so a caller can tell "no
    strings" from "gave up".
    """

    text: str
    opaque_sink_call: bool
    bounds_exceeded: bool
    parsed: bool


_FOLD_EMPTY = _FoldedPayload(text="", opaque_sink_call=False, bounds_exceeded=False, parsed=False)

#: Builtins the folder evaluates itself, by reimplementing their result for
#: constant inputs. Never resolved by calling an arbitrary object: the name
#: must be one of these AND every argument must already have folded.
_FOLDABLE_BUILTIN_NAMES = frozenset({"chr", "ord", "str", "int", "bytes", "bytearray", "list", "tuple"})

#: The ``map(<f>, <constants>)`` shapes the folder unrolls -- exactly the
#: element-wise builtins an obfuscator uses to spell a name out of code
#: points (``''.join(map(chr,[115,99,...]))``).
_FOLDABLE_MAP_FUNCTIONS = frozenset({"chr", "ord", "str", "int"})

#: ``base64`` decoder/encoder names, matched on the ATTRIBUTE/NAME alone so
#: ``base64.b64decode(x)``, ``__import__('base64').b64decode(x)`` and a
#: ``from base64 import b64decode`` binding all fold identically. The object
#: they hang off is exactly what a static walk cannot resolve, and it does
#: not need to: these names are distinctive enough that treating them as the
#: stdlib functions can only ever make the folder resolve MORE text, which
#: is deny-side.
_BASE64_CODEC_NAMES = frozenset(
    {
        "b64decode",
        "b64encode",
        "urlsafe_b64decode",
        "urlsafe_b64encode",
        "standard_b64decode",
        "standard_b64encode",
        "b32decode",
        "b32encode",
        "b16decode",
        "b16encode",
        "a85decode",
        "b85decode",
    }
)

#: ``str``/``bytes`` methods the folder applies to an already-folded
#: constant receiver. Pure, allocation-bounded (every result goes back
#: through `_fold_bounded`), and deliberately NOT extensible by pattern:
#: ``zfill``/``ljust``/``center``/``expandtabs`` are absent because their
#: argument is an unbounded repetition count -- the same bomb class
#: `_MAX_FOLDED_VALUE_LEN` exists to stop.
_FOLDABLE_TEXT_METHODS = frozenset(
    {
        "join",
        "format",
        "replace",
        "strip",
        "lstrip",
        "rstrip",
        "upper",
        "lower",
        "title",
        "swapcase",
        "split",
        "rsplit",
        "splitlines",
        "decode",
        "encode",
        "hex",
    }
)

#: Mechanism 2's sink sets, DERIVED BY SUBTRACTION from part 14's sets
#: rather than re-enumerated. This is the whole drift argument: a name added
#: to `_NON_INERT_ATTRIBUTE_NAMES`/`_NON_INERT_BUILTIN_NAMES`/
#: `_NON_INERT_MODULE_NAMES` because it reaches an execution sink appears
#: here automatically, and the only way to keep it OUT is to name it in one
#: of the subtracted sets below, in the open, with a reason.
#:
#: The subtractions are the filesystem-mutation family (part 14 refuses
#: those to prove a payload INERT, a strictly wider question than "does this
#: run a program"), plus three builtins that reach no execution at all.
#: ``replace`` in particular MUST be subtracted: it is ``str.replace`` far
#: more often than ``os.replace``, and mechanism 2 keying on it would deny
#: ordinary text munging.
_NON_EXECUTION_MUTATION_ATTRIBUTES = frozenset(
    {"write_text", "write_bytes", "unlink", "rmtree", "chmod", "rename", "replace", "dup2"}
)
_NON_EXECUTION_BUILTINS = frozenset({"input", "memoryview", "super"})
_NON_EXECUTION_MODULES = frozenset(
    {
        "sys",
        "signal",
        "socket",
        "threading",
        "platform",
        "tempfile",
        "pathlib",
        "glob",
        "atexit",
        "site",
        "sysconfig",
        "trace",
        "inspect",
        "gc",
        "operator",
        "shutil",
    }
)
_EXECUTION_SINK_ATTRIBUTE_NAMES = _NON_INERT_ATTRIBUTE_NAMES - _NON_EXECUTION_MUTATION_ATTRIBUTES
_EXECUTION_SINK_BUILTIN_NAMES = _NON_INERT_BUILTIN_NAMES - _NON_EXECUTION_BUILTINS
_EXECUTION_SINK_MODULE_NAMES = _NON_INERT_MODULE_NAMES - _NON_EXECUTION_MODULES

#: MECHANISM 2's NARROWING, and the one place this part departs from its
#: brief -- ON MEASURED EVIDENCE, recorded here so the choice is auditable
#: rather than discovered later as an unexplained subset.
#:
#: The brief specified mechanism 2 over EVERY execution sink. Measured
#: against a 24-command realistic-usage corpus (dispatched-agent commands
#: that mention commit-ish text), that shape moved FOUR commands ALLOW ->
#: DENY, three of them unambiguously reasonable:
#: ``subprocess.run([sys.executable,'-m','pytest',sys.argv[1]])``,
#: ``subprocess.run([...], cwd=os.getcwd())``, ``subprocess.run([...],
#: env=os.environ)``. All four share a shape: an ARGV-VECTOR sink, where an
#: unresolved element is ordinary honest usage rather than obfuscation.
#:
#: Narrowed to sinks that take a whole PROGRAM or COMMAND LINE as one
#: argument -- ``os.system``/``os.popen``, ``eval``/``exec``/``compile``/
#: ``__import__``, ``importlib.import_module``, ``runpy.run_*``, the
#: ``exec*``/``spawn*`` families, the reflection dunders -- plus any
#: argv-vector sink invoked with ``shell=True``, which converts its first
#: argument back into a command line. On the same two corpora that shape
#: keeps every adversarial denial mechanism 2 was there for (including
#: ``os.system(os.environ['X'])``, the brief's own row) and moves ZERO
#: realistic commands.
#:
#: Residual as this constant shipped, CLOSED by part 18 below:
#: ``subprocess.run([<unresolved>, 'commit'])`` -- an argv vector whose
#: PROGRAM SLOT does not resolve -- allowed. It is now refused by
#: `_argv_vector_program_slot_is_unknown` without re-widening this set, so
#: the four measured false positives that re-widening would have cost stay
#: allowed and only the two priced rows move.
_ARGV_VECTOR_SINK_ATTRIBUTES = frozenset(
    {"run", "call", "check_call", "check_output", "communicate", "Popen"}
)

#: PART 18 (2026-08-05) -- SLOT 0 OF AN ARGV VECTOR IS THE PROGRAM SLOT, AND
#: THE EXEMPTION ABOVE MUST NOT COVER IT.
#:
#: Part 16 exempted the whole argv-vector family from mechanism 2 because an
#: unresolved ELEMENT of an argv vector is ordinary honest usage
#: (``subprocess.run(['pytest', sys.argv[1]])`` names its program perfectly
#: well). Slot 0 is not an element in that sense: it IS the program. An
#: unresolved slot 0 is precisely the condition mechanism 2 exists for -- we
#: cannot say what binary starts -- and it was reachable, confirmed live:
#:
#:     python3 -c "import subprocess; subprocess.run(
#:         [''.join([chr(c) for c in [<ords of helper>]]), '-m', 'x'])"
#:
#: So slot 0 must resolve to a constant; slots 1..n keep part 16's exemption
#: and may stay unresolved.
#:
#: THE PRICE, MEASURED RATHER THAN ARGUED, and priced deliberately rather
#: than overlooked: exactly TWO rows of the realistic-usage corpus move
#: ALLOW -> DENY -- ``pytest-argv-file`` and ``py-compile-guard``, both
#: ``subprocess.run([sys.executable, ...])``. Both bite only a command that
#: BOTH routes through ``python3 -c`` AND mentions commit-ish text, and the
#: workaround is trivial (invoke pytest directly rather than through an
#: interpreter payload, or spell the program as ``'python3'``). That trade
#: was accepted at a cost of two; a future edit that grows it is a different
#: trade and needs re-measuring against the corpus, not reasoning about.
#:
#: ``communicate`` is subtracted: it is the one member of the family above
#: whose first argument is stdin DATA rather than argv, so a slot-0 rule
#: there would refuse unresolved input bytes handed to an already-started
#: process -- a program this leg has, by then, already judged.
_ARGV_PROGRAM_SLOT_SINK_ATTRIBUTES = _ARGV_VECTOR_SINK_ATTRIBUTES - frozenset({"communicate"})
_OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES = (
    _EXECUTION_SINK_ATTRIBUTE_NAMES - _ARGV_VECTOR_SINK_ATTRIBUTES
)
#: ``os`` and ``builtins`` drop out of the MODULE leg here (their
#: program-starting members are already named in the attribute set above),
#: so ``os.makedirs(sys.argv[1])`` is not a mechanism-2 deny; ``subprocess``
#: drops out because its whole surface is the argv-vector family, re-admitted
#: by ``shell=True``.
_OPAQUE_PROGRAM_SINK_MODULE_NAMES = _EXECUTION_SINK_MODULE_NAMES - frozenset(
    {"os", "builtins", "subprocess"}
)
_SHELL_KEYWORD = "shell"

#: PART 20 (2026-08-05) -- THE WHOLE-COMMAND-TEXT SINKS THAT COULD ONLY BE
#: ADMITTED BY RECEIVER, closing part 19's stated residual.
#:
#: ``subprocess.getoutput(cmd)`` / ``getstatusoutput(cmd)`` run ``cmd``
#: through a shell. They are whole-command-text sinks in exactly the sense
#: ``os.system`` is -- one argument, and it is a command line -- yet both
#: ALLOWED at part 19's HEAD, confirmed live:
#:
#:     python3 -c "import subprocess,os; print(subprocess.getoutput(os.environ['X']))"
#:
#: They are not reachable by the sets above by construction: ``subprocess``
#: is subtracted from `_OPAQUE_PROGRAM_SINK_MODULE_NAMES` (its whole surface
#: is the argv-vector family, which is deliberately narrowed out of
#: mechanism 2), and these two are not in that family, so they fell between
#: the two doors.
#:
#: NEGATIVE SPEC -- WHY THIS IS A DOTTED TARGET SET AND NOT TWO MORE NAMES IN
#: `_OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES`: every other sink leg here is
#: identity-blind about the RECEIVER (part 14's reasoning: the object an
#: attribute hangs off is what a static walk cannot resolve, so the attribute
#: name carries the decision). That is affordable for ``system``/``popen``/
#: ``execv``, which are not words honest code hangs off arbitrary objects.
#: ``getoutput`` is: a logger, a test helper, a CLI wrapper may all define
#: one, and denying ``harness.getoutput(sys.argv[1])`` on the name alone
#: would be a false positive bought for nothing. So membership is keyed on
#: the CANONICAL DOTTED TARGET resolved through the payload's own import
#: bindings (`_call_is_receiver_qualified_shell_sink`) -- part 19's
#: `_payload_bindings` is what made that available. A receiver that does not
#: resolve to ``subprocess`` is NOT a sink on the strength of the name.
#:
#: THE SUBPROCESS SURFACE, AUDITED RATHER THAN SAMPLED: the module's callable
#: exports are ``Popen``/``call``/``check_call``/``check_output``/``run``
#: (the argv-vector family, covered by parts 16/18), ``getoutput``/
#: ``getstatusoutput`` (here), ``list2cmdline`` (formats a command line and
#: executes nothing) and the exception classes. There is no third shell-out
#: sibling left uncovered.
_RECEIVER_QUALIFIED_SHELL_SINK_TARGETS = frozenset(
    {"subprocess.getoutput", "subprocess.getstatusoutput"}
)


def _fold_bounded(value: Any, budget: _FoldBudget) -> Any:
    """Return ``value``, or `_FOLD_UNRESOLVED` when it exceeds
    `_MAX_FOLDED_VALUE_LEN` -- the single choke point every fold result
    passes through, so no code path can produce an unbounded value by
    forgetting to check.
    """
    if isinstance(value, (str, bytes, bytearray, list, tuple)) and len(value) > _MAX_FOLDED_VALUE_LEN:
        budget.exceeded = True
        return _FOLD_UNRESOLVED
    return value


def _fold_template_is_bounded(template: Any) -> bool:
    """True when a ``%``/``str.format``/format-spec template cannot expand
    its output past `_MAX_FOLDED_VALUE_LEN` through a WIDTH field.

    ``'%9999999999d' % 1`` and ``'{:>999999999}'.format('x')`` each allocate
    gigabytes from a few source bytes -- a length bound applied AFTER the
    operation is too late. Any digit run in the template above the bound
    means the template is refused before it is ever applied.
    """
    if isinstance(template, (bytes, bytearray)):
        try:
            template = template.decode("latin-1")
        except Exception:
            return False
    if not isinstance(template, str):
        return False
    if len(template) > _MAX_FOLDED_VALUE_LEN:
        return False
    return all(int(run) <= _MAX_FOLD_FIELD_WIDTH for run in re.findall(r"\d+", template))


def _fold_environment(tree: "ast.Module", budget: _FoldBudget, memo: Dict[int, Any]) -> Dict[str, Any]:
    """Resolve module-level ``name = <constant expression>`` bindings, so
    ``h = 'scoped-git' + '-commit'; os.system(h)`` folds as well as the
    inlined spelling does.

    Conservative to the point of bluntness, because an over-eager
    environment would make the folder claim a value the interpreter will not
    see: a name bound more than ONCE anywhere in the payload (a second
    assignment, an augmented assignment, a ``for`` target, a ``with ... as``,
    an ``except ... as``, an import alias) is not bound here at all, and any
    payload containing a ``def``/``class``/``lambda`` gets an EMPTY
    environment, since those defer execution to a point this walk does not
    model.
    """
    binds: Dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            return {}
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            binds[node.id] = binds.get(node.id, 0) + 1
        elif isinstance(node, ast.alias):
            bound = node.asname or node.name.split(".")[0]
            binds[bound] = binds.get(bound, 0) + 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binds[node.name] = binds.get(node.name, 0) + 1
    env: Dict[str, Any] = {}
    for stmt in tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or binds.get(target.id, 0) != 1:
            continue
        value = _fold_expr(stmt.value, env, budget, memo, 0)
        if value is not _FOLD_UNRESOLVED:
            env[target.id] = value
    return env


def _fold_expr(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Statically evaluate ``node`` when every operand it depends on is a
    compile-time constant; otherwise return `_FOLD_UNRESOLVED`.

    NEGATIVE SPEC -- THIS FOLDER NEVER EXECUTES PAYLOAD TEXT. It does not
    call ``eval``, ``exec``, ``compile``, or ``ast.literal_eval`` on
    attacker text, does not import the payload or any module the payload
    names, and never invokes a callable the payload supplies. It computes
    results itself, for a closed set of operators, builtins and ``str``/
    ``bytes`` methods, over values it has already resolved -- so a payload
    can make this function produce a STRING, and nothing else, no matter
    what it says. The bounds in this part's constant block are the second
    half of that guarantee and are equally load-bearing: they are security
    properties, not tuning knobs (see `_MAX_FOLDED_VALUE_LEN`).

    Folding is monotone in the DENY direction and can never open a hole: it
    only ever adds resolved text to what mechanism 1 matches against, and
    every unrecognized construct returns the sentinel, which mechanism 2
    then reads as "unknown" rather than "safe".
    """
    if depth > _MAX_FOLD_DEPTH:
        budget.exceeded = True
        return _FOLD_UNRESOLVED
    cached = memo.get(id(node), _FOLD_UNRESOLVED)
    if cached is not _FOLD_UNRESOLVED:
        return cached
    if not budget.spend():
        return _FOLD_UNRESOLVED
    value = _fold_expr_uncached(node, env, budget, memo, depth)
    if value is not _FOLD_UNRESOLVED:
        memo[id(node)] = value
    return value


def _fold_expr_uncached(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    if isinstance(node, ast.Constant):
        return _fold_bounded(node.value, budget)
    if isinstance(node, ast.Name):
        return env.get(node.id, _FOLD_UNRESOLVED)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        items = []
        for element in node.elts:
            folded = _fold_expr(element, env, budget, memo, depth + 1)
            if folded is _FOLD_UNRESOLVED:
                return _FOLD_UNRESOLVED
            items.append(folded)
        return _fold_bounded(items, budget)
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for part in node.values:
            folded = _fold_expr(part, env, budget, memo, depth + 1)
            if not isinstance(folded, str):
                return _FOLD_UNRESOLVED
            parts.append(folded)
        return _fold_bounded("".join(parts), budget)
    if isinstance(node, ast.FormattedValue):
        return _fold_formatted_value(node, env, budget, memo, depth)
    if isinstance(node, ast.BinOp):
        return _fold_binop(node, env, budget, memo, depth)
    if isinstance(node, ast.Subscript):
        return _fold_subscript(node, env, budget, memo, depth)
    if isinstance(node, ast.Call):
        return _fold_call(node, env, budget, memo, depth)
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        # EXPLICIT, NOT A FALLTHROUGH (part 17, 2026-08-05). A comprehension
        # is a loop with its own scope, and modelling one here would mean
        # evaluating attacker-authored iteration inside a PreToolUse hook --
        # exactly what `_fold_expr`'s negative spec forbids. So it resolves
        # to UNRESOLVED, which mechanism 2 reads as "we cannot know what this
        # runs" and DENIES.
        #
        # Why it is called out rather than left to the final ``return``: the
        # inert leg PERMITS comprehensions (`_INERT_PAYLOAD_NODE_TYPES`),
        # which made an un-modelled comprehension a silent string channel
        # into any callable -- ``''.join([chr(c) for c in [<ints>]])``
        # resolved to nothing, so neither the literal reconstruction nor the
        # fold ever saw the assembled name. The fail-closed direction is the
        # fix and it must stay legible as a decision, so that a future editor
        # adding comprehension folding has to delete this comment to do it.
        return _FOLD_UNRESOLVED
    return _FOLD_UNRESOLVED


def _fold_formatted_value(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Fold one f-string replacement field: ``f'{h}'``, ``f'{h!r}'``, and a
    constant format spec (``f'{h:>8}'``). A computed spec, or a value that
    does not fold, leaves the whole f-string unresolved.
    """
    value = _fold_expr(node.value, env, budget, memo, depth + 1)
    if value is _FOLD_UNRESOLVED:
        return _FOLD_UNRESOLVED
    if node.conversion == 115:
        value = str(value)
    elif node.conversion == 114:
        value = repr(value)
    elif node.conversion == 97:
        value = ascii(value)
    elif node.conversion not in (-1, 0):
        return _FOLD_UNRESOLVED
    spec = ""
    if node.format_spec is not None:
        spec = _fold_expr(node.format_spec, env, budget, memo, depth + 1)
        if not isinstance(spec, str) or not _fold_template_is_bounded(spec):
            return _FOLD_UNRESOLVED
    try:
        return _fold_bounded(format(value, spec), budget)
    except Exception:
        return _FOLD_UNRESOLVED


def _fold_binop(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Fold ``+`` (sequence concatenation), ``*`` (sequence repetition) and
    ``%`` (percent formatting) over already-folded operands.

    ``**`` is deliberately absent: ``10**9**9`` is a cheap parse and a
    multi-second, multi-gigabyte integer, and no obfuscation needs it -- a
    repetition count that does not fold simply leaves the repetition
    unresolved, which is the correct answer anyway.
    """
    left = _fold_expr(node.left, env, budget, memo, depth + 1)
    if left is _FOLD_UNRESOLVED:
        return _FOLD_UNRESOLVED
    right = _fold_expr(node.right, env, budget, memo, depth + 1)
    if right is _FOLD_UNRESOLVED:
        return _FOLD_UNRESOLVED
    if isinstance(node.op, ast.Add):
        if isinstance(left, str) and isinstance(right, str):
            pass
        elif isinstance(left, (bytes, bytearray)) and isinstance(right, (bytes, bytearray)):
            pass
        elif isinstance(left, list) and isinstance(right, list):
            pass
        elif isinstance(left, tuple) and isinstance(right, tuple):
            pass
        else:
            return _FOLD_UNRESOLVED
        if len(left) + len(right) > _MAX_FOLDED_VALUE_LEN:
            budget.exceeded = True
            return _FOLD_UNRESOLVED
        return _fold_bounded(left + right, budget)
    if isinstance(node.op, ast.Mult):
        sequence, count = left, right
        if isinstance(left, int) and not isinstance(left, bool):
            sequence, count = right, left
        if not isinstance(sequence, (str, bytes, bytearray, list, tuple)):
            return _FOLD_UNRESOLVED
        if not isinstance(count, int) or isinstance(count, bool):
            return _FOLD_UNRESOLVED
        # Bound BEFORE allocating -- the entire point of this branch.
        if count > 0 and len(sequence) * count > _MAX_FOLDED_VALUE_LEN:
            budget.exceeded = True
            return _FOLD_UNRESOLVED
        return _fold_bounded(sequence * count, budget)
    if isinstance(node.op, ast.Mod):
        if not isinstance(left, (str, bytes, bytearray)) or not _fold_template_is_bounded(left):
            return _FOLD_UNRESOLVED
        try:
            return _fold_bounded(left % right, budget)
        except Exception:
            return _FOLD_UNRESOLVED
    return _FOLD_UNRESOLVED


def _fold_subscript(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Fold indexing and slicing of an already-folded sequence
    (``'Xscoped-git-commit'[1:]``). Bounded for free: a slice of a bounded
    sequence is bounded.
    """
    value = _fold_expr(node.value, env, budget, memo, depth + 1)
    if not isinstance(value, (str, bytes, bytearray, list, tuple)):
        return _FOLD_UNRESOLVED
    key = node.slice
    if isinstance(key, ast.Slice):
        bounds = []
        for part in (key.lower, key.upper, key.step):
            if part is None:
                bounds.append(None)
                continue
            folded = _fold_expr(part, env, budget, memo, depth + 1)
            if not isinstance(folded, int) or isinstance(folded, bool):
                return _FOLD_UNRESOLVED
            bounds.append(folded)
        if bounds[2] == 0:
            return _FOLD_UNRESOLVED
        return _fold_bounded(value[slice(*bounds)], budget)
    index = _fold_expr(key, env, budget, memo, depth + 1)
    if not isinstance(index, int) or isinstance(index, bool):
        return _FOLD_UNRESOLVED
    try:
        return _fold_bounded(value[index], budget)
    except Exception:
        return _FOLD_UNRESOLVED


def _fold_call(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Fold the closed set of calls this folder computes itself -- an
    element-wise builtin, a ``base64`` codec, a ``map`` unroll, or a
    whitelisted ``str``/``bytes`` method on an already-folded receiver.

    Nothing else folds, and nothing here calls a payload-supplied callable:
    the FUNCTION is selected by NAME from a frozen set, and the
    implementation invoked is the interpreter's own (see `_fold_expr`'s
    negative spec).
    """
    if any(isinstance(arg, ast.Starred) for arg in node.args):
        return _FOLD_UNRESOLVED
    if any(kw.arg is None for kw in node.keywords):
        return _FOLD_UNRESOLVED
    func = node.func
    if isinstance(func, ast.Name) and func.id == "map":
        return _fold_map_call(node, env, budget, memo, depth)
    args = []
    for arg in node.args:
        folded = _fold_expr(arg, env, budget, memo, depth + 1)
        if folded is _FOLD_UNRESOLVED:
            return _FOLD_UNRESOLVED
        args.append(folded)
    kwargs = {}
    for kw in node.keywords:
        folded = _fold_expr(kw.value, env, budget, memo, depth + 1)
        if folded is _FOLD_UNRESOLVED:
            return _FOLD_UNRESOLVED
        kwargs[kw.arg] = folded
    if isinstance(func, ast.Name):
        if func.id in _BASE64_CODEC_NAMES:
            return _fold_base64_call(func.id, args, budget)
        if func.id in _FOLDABLE_BUILTIN_NAMES:
            return _fold_builtin_call(func.id, args, budget)
        return _FOLD_UNRESOLVED
    if isinstance(func, ast.Attribute):
        if func.attr in _BASE64_CODEC_NAMES:
            return _fold_base64_call(func.attr, args, budget)
        if func.attr == "fromhex" and isinstance(func.value, ast.Name) and func.value.id in ("bytes", "bytearray"):
            if len(args) != 1 or not isinstance(args[0], str):
                return _FOLD_UNRESOLVED
            try:
                return _fold_bounded(bytes.fromhex(args[0]), budget)
            except Exception:
                return _FOLD_UNRESOLVED
        if func.attr not in _FOLDABLE_TEXT_METHODS:
            return _FOLD_UNRESOLVED
        receiver = _fold_expr(func.value, env, budget, memo, depth + 1)
        if receiver is _FOLD_UNRESOLVED:
            return _FOLD_UNRESOLVED
        return _fold_text_method(receiver, func.attr, args, kwargs, budget)
    return _FOLD_UNRESOLVED


def _fold_map_call(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Unroll ``map(chr, [115, 99, ...])`` and its ``ord``/``str``/``int``
    siblings over a folded sequence. The mapped function is matched by NAME
    against `_FOLDABLE_MAP_FUNCTIONS` and applied by this module's own
    `_fold_builtin_call`, never by resolving the payload's binding for it.
    """
    if len(node.args) != 2 or node.keywords:
        return _FOLD_UNRESOLVED
    mapper = node.args[0]
    if not isinstance(mapper, ast.Name) or mapper.id not in _FOLDABLE_MAP_FUNCTIONS:
        return _FOLD_UNRESOLVED
    sequence = _fold_expr(node.args[1], env, budget, memo, depth + 1)
    if not isinstance(sequence, (str, bytes, bytearray, list, tuple)):
        return _FOLD_UNRESOLVED
    if len(sequence) > _MAX_FOLDED_VALUE_LEN:
        budget.exceeded = True
        return _FOLD_UNRESOLVED
    out = []
    for element in sequence:
        if not budget.spend():
            return _FOLD_UNRESOLVED
        folded = _fold_builtin_call(mapper.id, [element], budget)
        if folded is _FOLD_UNRESOLVED:
            return _FOLD_UNRESOLVED
        out.append(folded)
    return _fold_bounded(out, budget)


def _fold_builtin_call(name: str, args: List[Any], budget: _FoldBudget) -> Any:
    """Compute the result of one element-wise builtin over folded
    arguments. Every branch is this module's own arithmetic over values it
    already holds -- no dispatch through a payload-controlled object.
    """
    try:
        if name == "chr":
            if len(args) != 1 or not isinstance(args[0], int) or isinstance(args[0], bool):
                return _FOLD_UNRESOLVED
            if not 0 <= args[0] <= 0x10FFFF:
                return _FOLD_UNRESOLVED
            return chr(args[0])
        if name == "ord":
            if len(args) != 1 or not isinstance(args[0], (str, bytes, bytearray)) or len(args[0]) != 1:
                return _FOLD_UNRESOLVED
            return ord(args[0])
        if name == "str":
            if len(args) != 1 or not isinstance(args[0], (str, int, float)):
                return _FOLD_UNRESOLVED
            return _fold_bounded(str(args[0]), budget)
        if name == "int":
            if len(args) != 1 or not isinstance(args[0], (str, int, float)):
                return _FOLD_UNRESOLVED
            return int(args[0])
        if name in ("bytes", "bytearray"):
            if len(args) != 1 or not isinstance(args[0], (list, tuple)):
                return _FOLD_UNRESOLVED
            if not all(isinstance(item, int) and not isinstance(item, bool) and 0 <= item <= 255 for item in args[0]):
                return _FOLD_UNRESOLVED
            return _fold_bounded(bytes(args[0]), budget)
        if name in ("list", "tuple"):
            if len(args) != 1 or not isinstance(args[0], (str, bytes, bytearray, list, tuple)):
                return _FOLD_UNRESOLVED
            built = list(args[0]) if name == "list" else tuple(args[0])
            return _fold_bounded(built, budget)
    except Exception:
        return _FOLD_UNRESOLVED
    return _FOLD_UNRESOLVED


def _fold_base64_call(name: str, args: List[Any], budget: _FoldBudget) -> Any:
    """Decode/encode a constant through the ``base64`` codec named by
    ``name``. Imported lazily -- this guard runs on every Bash call, and a
    payload that mentions no codec must not pay for one (the same hot-path
    reasoning `_import_assert_paths_in_session_scope` records).
    """
    if not args or not isinstance(args[0], (str, bytes, bytearray)):
        return _FOLD_UNRESOLVED
    if len(args[0]) > _MAX_FOLDED_VALUE_LEN:
        budget.exceeded = True
        return _FOLD_UNRESOLVED
    import base64

    codec = getattr(base64, name, None)
    if codec is None:
        return _FOLD_UNRESOLVED
    try:
        return _fold_bounded(codec(args[0]), budget)
    except Exception:
        return _FOLD_UNRESOLVED


def _fold_text_method(receiver: Any, name: str, args: List[Any], kwargs: Dict[str, Any], budget: _FoldBudget) -> Any:
    """Apply a whitelisted ``str``/``bytes`` method to an already-folded
    receiver. ``join`` and ``format`` are pre-bounded (a join's total length
    and a template's width fields) because both can expand far past their
    source length; every other result is bounded on the way out.
    """
    if not isinstance(receiver, (str, bytes, bytearray)) or name not in _FOLDABLE_TEXT_METHODS:
        return _FOLD_UNRESOLVED
    if name == "join":
        if len(args) != 1 or not isinstance(args[0], (list, tuple)):
            return _FOLD_UNRESOLVED
        items = args[0]
        if not all(isinstance(item, type(receiver)) for item in items):
            return _FOLD_UNRESOLVED
        if sum(len(item) for item in items) + len(receiver) * len(items) > _MAX_FOLDED_VALUE_LEN:
            budget.exceeded = True
            return _FOLD_UNRESOLVED
    if name == "format" and not _fold_template_is_bounded(receiver):
        return _FOLD_UNRESOLVED
    try:
        return _fold_bounded(getattr(receiver, name)(*args, **kwargs), budget)
    except Exception:
        return _FOLD_UNRESOLVED


def _fold_text_values(value: Any, out: List[str], seen: int = 0) -> None:
    """Flatten one folded value into the strings mechanism 1 will match on.
    ``bytes`` count as text: a base64 blob decodes to bytes, and the
    invocation it spells is exactly what this part exists to see.
    """
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, (bytes, bytearray)):
        try:
            out.append(bytes(value).decode("utf-8"))
        except Exception:
            out.append(bytes(value).decode("latin-1"))
    elif isinstance(value, (list, tuple)) and seen < _MAX_FOLD_DEPTH:
        for item in value:
            _fold_text_values(item, out, seen + 1)


def _fold_collect_text(tree: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any]) -> List[str]:
    """Walk ``tree`` in source order, collecting the MAXIMAL folded strings:
    when a node folds, its text is taken and its children are not descended
    into (their values are already inside the parent's result). Source order
    is preserved because the reconstructed line is fed to matchers that read
    adjacency (``git`` then ``commit``).
    """
    out: List[str] = []
    total = 0
    stack: List[Any] = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.expr):
            folded = _fold_expr(node, env, budget, memo, 0)
            if folded is not _FOLD_UNRESOLVED:
                collected_before = len(out)
                _fold_text_values(folded, out)
                total += sum(len(item) for item in out[collected_before:])
                if total > _MAX_FOLDED_TOTAL_LEN:
                    budget.exceeded = True
                    return out
                continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))
    return out


class _PayloadBindings(NamedTuple):
    """PART 19 (2026-08-05) -- what the payload's own ``import``/assignment
    statements say about the NAME a call is spelled with.

    ``imports`` maps a local name to the SET of canonical dotted targets it
    was bound to (``r`` -> ``{'subprocess.run'}``, ``sp`` ->
    ``{'subprocess'}``); ``opaque`` holds names rebound by a statement this
    walk cannot resolve, which are fail-closed rather than assumed harmless.

    A SET, not a string, and part 21 (2026-08-05) is why: one name can carry
    several import identities (``try: from subprocess import run as r /
    except ImportError: from json import loads as r``, an ``if``-guarded
    fallback, a same-named import inside a ``def`` body). The map was
    ``Dict[str, str]`` with last-visit-wins, so the SECOND spelling silently
    replaced the first and an aliased sink resolved to a benign target --
    a confirmed mis-resolve TOWARD SAFE that defeated every part-19/20 leg.
    Every consumer below reads this as "any of these identities", so a name
    with one sink identity among several IS a sink.
    """

    imports: Dict[str, Set[str]]
    opaque: Set[str]


_EMPTY_BINDINGS = _PayloadBindings(imports={}, opaque=frozenset())


def _payload_bindings(tree: Any) -> _PayloadBindings:
    """Resolve the payload's local name bindings, so sink identity survives
    an ALIAS -- the first of part 19's four sink-IDENTIFICATION gaps, each
    confirmed live at HEAD.

    ``from subprocess import run as r; r(sys.argv[1:])`` reaches exactly the
    primitive ``subprocess.run(sys.argv[1:])`` reaches, and ALLOWED, because
    every leg of this module matched sinks on the SPELLED name. Resolving the
    binding first makes the alias irrelevant, which is the only shape that
    generalises: ``from os import system``, ``import subprocess as sp``,
    ``from os import posix_spawn as ps`` are all one rule, not three.

    ``import os.path`` binds ``os`` (canonical ``os``); ``import os.path as
    p`` binds ``p`` to ``os.path``; ``from X import y as z`` binds ``z`` to
    ``X.y``. A RELATIVE import (``from . import x``) resolves to no canonical
    root and is left unbound -- there is no sibling package inside a ``-c``
    payload, and guessing one would be a name this checker made up.

    A NAME BOUND TWICE CARRIES BOTH IDENTITIES, never the last one (part 21,
    2026-08-05, closing a confirmed mis-resolve toward SAFE). This walk is
    ``ast.walk`` over the WHOLE tree -- deliberately, because a top-level
    ``try: import X / except ImportError: import Y`` is honest usage and
    restricting to ``tree.body`` would buy false positives -- so a nested
    binding is visited AFTER a module-level sibling. Assigning would let
    ``from subprocess import run as r`` be overwritten by a never-taken
    ``from json import loads as r`` in a ``def`` body, and the sink would
    resolve to a benign target. Identities are therefore UNIONED and read
    disjunctively downstream, which is the same discipline
    `_resolved_call_identity` already applies to the spelled name: it only
    ever ADDS an identity, never removes one. Union rather than "move a
    multi-bound name to ``opaque``" because ``opaque`` routes a call to the
    harshest door unconditionally, which would deny the honest
    ``try: import ujson as j / except ImportError: import json as j`` shape;
    the union denies exactly when one of the identities is a sink.

    A LOCAL ALIAS OF AN IMPORT IS STILL THE IMPORT: ``run = subprocess.run;
    run([...])`` binds the same primitive ``import subprocess as sp`` does,
    so a module-level single assignment whose value resolves through the
    import map is recorded as an alias too. That precision is what keeps the
    fail-closed rule below from costing false positives -- the shape is
    common in honest one-liners, and treating it as UNKNOWN would refuse
    ``run(['pytest', sys.argv[1]])`` while allowing the identical
    ``subprocess.run(['pytest', sys.argv[1]])``.

    FAIL CLOSED ON EVERY OTHER REBINDING, which is the half that keeps the
    resolution honest: a name some statement STORES to (assignment, walrus,
    ``for`` target, ``with ... as``, ``except ... as``) from anything this
    walk cannot resolve is not a name it may claim to have resolved --
    ``f = getattr(os, sys.argv[1]); f(...)`` is a sink whose identity is
    chosen at runtime. Those names land in ``opaque`` and
    `_call_is_execution_sink` treats a CALL to one as a sink of unknown
    identity. Names bound by ``def``/``class`` are excluded: their bodies are
    ordinary statements this same walk visits, so any sink inside is found
    where it is written rather than where it is called.
    """
    imports: Dict[str, Set[str]] = {}
    stored: Set[str] = set()
    store_counts: Dict[str, int] = {}
    code_defs: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    imports.setdefault(alias.asname, set()).add(alias.name)
                else:
                    root = alias.name.split(".")[0]
                    imports.setdefault(root, set()).add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*" or not module:
                    continue
                imports.setdefault(alias.asname or alias.name, set()).add(
                    "%s.%s" % (module, alias.name)
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            code_defs.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stored.add(node.id)
            store_counts[node.id] = store_counts.get(node.id, 0) + 1
        elif isinstance(node, ast.ExceptHandler) and node.name:
            stored.add(node.name)
            store_counts[node.name] = store_counts.get(node.name, 0) + 1
    for name in stored:
        imports.pop(name, None)
    opaque = stored - code_defs
    for stmt in getattr(tree, "body", []):
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or store_counts.get(target.id, 0) != 1:
            continue
        dotted = _dotted_import_targets(stmt.value, imports)
        if not dotted:
            continue
        imports.setdefault(target.id, set()).update(dotted)
        opaque.discard(target.id)
    return _PayloadBindings(imports=imports, opaque=opaque)


def _dotted_import_targets(node: Any, imports: Dict[str, Set[str]]) -> Set[str]:
    """Return EVERY canonical dotted target a ``Name``/``Attribute`` chain may
    resolve to through ``imports`` (``subprocess.run`` ->
    ``{'subprocess.run'}``), or an empty set when it resolves to none -- a
    call result, a subscript, a literal, or a chain rooted in an unimported
    name.

    A SET because the root name may carry several import identities (see
    `_PayloadBindings`): a chain rooted in a name bound twice resolves to one
    dotted target per root binding, and every consumer treats the payload as
    reaching ANY of them.
    """
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return set()
    parts.append(node.id)
    parts.reverse()
    roots = imports.get(parts[0])
    if not roots:
        return set()
    return {".".join([root] + parts[1:]) for root in roots}


def _resolved_call_identity(func: Any, bindings: _PayloadBindings) -> Tuple[Set[str], Set[str]]:
    """Return ``(names, modules)`` -- every identity a call target may carry
    once the payload's own import bindings are applied (part 19).

    ``names`` always contains the SPELLED name (``sp.run`` -> ``run``), which
    is what kept this module identity-blind about receivers, plus the last
    segment of EVERY resolved dotted target (``r`` -> ``subprocess.run`` ->
    ``run``). ``modules`` likewise contains the spelled receiver and every
    resolved dotted ROOT, so ``sp.run`` reaches the ``subprocess`` module leg
    and ``from pickle import loads as L`` reaches the ``pickle`` one.

    ADDITIVE ONLY, and part 21 made the import map obey the same rule: a name
    bound more than once contributes one identity per binding, so a shadowed
    ``from subprocess import run as r`` still reaches the ``run`` leg even
    when a later, never-taken ``from json import loads as r`` binds the same
    name. Resolution here may only ADD an identity to a call target; nothing
    in this module removes one on the strength of a resolution.
    """
    names: Set[str] = set()
    modules: Set[str] = set()
    if isinstance(func, ast.Attribute):
        names.add(func.attr)
        if isinstance(func.value, ast.Name):
            modules.add(func.value.id)
    elif isinstance(func, ast.Name):
        names.add(func.id)
    for dotted in _dotted_import_targets(func, bindings.imports):
        segments = dotted.split(".")
        names.add(segments[-1])
        modules.add(segments[0])
    return names, modules


def _call_target_is_opaque_rebinding(func: Any, bindings: _PayloadBindings) -> bool:
    """True when the call target is a bare local name the payload REBOUND to
    something this walk could not resolve (part 19) -- see
    `_payload_bindings` for why that fails closed rather than open.
    """
    return isinstance(func, ast.Name) and func.id in bindings.opaque


def _call_is_receiver_qualified_shell_sink(func: Any, bindings: _PayloadBindings) -> bool:
    """True when the call target RESOLVES to one of
    `_RECEIVER_QUALIFIED_SHELL_SINK_TARGETS` -- part 20's whole-command-text
    sinks, which may only be admitted with their receiver established.

    Resolution is `_dotted_import_targets` over the payload's own bindings and
    nothing else, so ``import subprocess``, ``import subprocess as sp`` and
    ``from subprocess import getoutput as go`` are one rule, while a bare
    ``getoutput(...)`` or an unrelated ``harness.getoutput(...)`` resolves to
    no canonical target and is NOT a sink here. That asymmetry is the point of
    this leg rather than an omission from it -- see the negative spec at
    `_RECEIVER_QUALIFIED_SHELL_SINK_TARGETS`.

    ANY of the resolved targets suffices (part 21): a name bound to
    ``subprocess.getoutput`` in one statement and to something benign in
    another is still a name that may reach ``subprocess.getoutput``.
    """
    return any(
        dotted in _RECEIVER_QUALIFIED_SHELL_SINK_TARGETS
        for dotted in _dotted_import_targets(func, bindings.imports)
    )


def _call_is_execution_sink(node: Any, bindings: _PayloadBindings = _EMPTY_BINDINGS) -> bool:
    """True when ``node`` is a call that can start a program, import
    dynamically, deserialize, or reflect its way to any of those --
    resolved through part 14's own name sets, narrowed by subtraction to the
    EXECUTION family (see `_EXECUTION_SINK_ATTRIBUTE_NAMES`).

    Deliberately identity-blind about the receiver, exactly as part 14 is:
    the object an attribute hangs off is what a static walk cannot resolve,
    so the ATTRIBUTE name carries the decision, plus a module-name leg for
    the ``pickle.loads``/``runpy.run_path`` shapes whose attribute alone is
    not distinctive.

    PART 19 adds the two identity legs the name-only match was blind to: the
    payload's import bindings (`_resolved_call_identity`, so an ALIASED sink
    is the same sink) and its unresolvable local rebindings
    (`_call_target_is_opaque_rebinding`, which is a sink of unknown identity
    rather than a non-sink). ``bindings`` defaults to empty so a caller that
    has no tree -- a node-level test, say -- gets exactly the pre-part-19
    name-only behaviour.

    PART 20 adds the RECEIVER-QUALIFIED leg
    (`_call_is_receiver_qualified_shell_sink`). It changes no verdict here on
    its own -- ``subprocess.getoutput`` already reached this function through
    the module leg -- and is stated anyway so sink IDENTITY is decided in one
    place: a future narrowing of the module leg must not silently un-identify
    a sink part 20 admitted deliberately. The verdict it does move is in
    `_sink_takes_opaque_program_text`, which is the door these two sinks fell
    between.
    """
    func = node.func
    if _call_target_is_opaque_rebinding(func, bindings):
        return True
    if _call_is_receiver_qualified_shell_sink(func, bindings):
        return True
    names, modules = _resolved_call_identity(func, bindings)
    #: A BARE name keeps its pre-part-19 treatment for the process-creation
    #: families (``execv('x')`` as an unimported local name is not evidence of
    #: anything); an IMPORT-RESOLVED one does not, because ``from os import
    #: posix_spawn`` says exactly which primitive it named.
    process_creation_applies = isinstance(func, ast.Attribute) or (
        isinstance(func, ast.Name) and func.id in bindings.imports
    )
    for name in names:
        if (
            name in _EXECUTION_SINK_BUILTIN_NAMES
            or name in _EXECUTION_SINK_ATTRIBUTE_NAMES
            or _identifier_is_dunder(name)
        ):
            return True
        if process_creation_applies and _name_is_process_creation(name):
            return True
    if isinstance(func, (ast.Attribute, ast.Name)):
        return any(module in _EXECUTION_SINK_MODULE_NAMES for module in modules)
    return False


def _sink_takes_opaque_program_text(
    node: Any,
    env: Dict[str, Any],
    budget: _FoldBudget,
    memo: Dict[int, Any],
    bindings: _PayloadBindings = _EMPTY_BINDINGS,
) -> bool:
    """True when this sink call's arguments are a PROGRAM or COMMAND LINE
    rather than an argv vector -- the narrowing mechanism 2 ships with, and
    the measured reason for it, are both recorded at
    `_OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES`.

    ``shell=True`` re-admits an argv-vector sink, because that keyword IS
    the conversion: ``subprocess.run(x, shell=True)`` hands ``x`` to a shell
    as a command line, so an unresolved ``x`` there is the same unknowable
    program ``os.system(x)`` would start.

    PART 19: identity is read through `_resolved_call_identity`, so an
    ALIASED whole-command sink (``from os import system as s``) lands in this
    door rather than slipping past it, and a call target the payload rebound
    to something unresolvable lands here too -- an unknown callable handed an
    unknown argument is the exact condition mechanism 2 exists for, and there
    is no argv-vector shape to narrow it to.

    PART 20: ``subprocess.getoutput``/``getstatusoutput`` enter this door too,
    but ONLY with their receiver resolved
    (`_call_is_receiver_qualified_shell_sink`) -- they take a command LINE,
    not an argv vector, so an unresolved argument is an unknowable program,
    while a bare or unrelated ``.getoutput()`` is a name this leg has no
    business claiming. Their constant-argument form is untouched and is judged
    on CONTENT by mechanism 1, exactly like ``os.system('...')``.
    """
    func = node.func
    if _call_target_is_opaque_rebinding(func, bindings):
        return True
    if _call_is_receiver_qualified_shell_sink(func, bindings):
        return True
    names, modules = _resolved_call_identity(func, bindings)
    for name in names:
        if (
            name in _OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES
            or name in _EXECUTION_SINK_BUILTIN_NAMES
            or _identifier_is_dunder(name)
            or _name_is_process_creation(name)
        ):
            return True
    if any(module in _OPAQUE_PROGRAM_SINK_MODULE_NAMES for module in modules):
        return True
    for kw in node.keywords:
        if kw.arg == _SHELL_KEYWORD and _fold_expr(kw.value, env, budget, memo, 0) is True:
            return True
    return False


class _EmptyVectorValue:
    """"This argv vector is statically EMPTY" -- determined, not unknown.

    Distinct from `_FOLD_UNRESOLVED` on purpose: ``subprocess.run([])`` and
    ``subprocess.run(['x'] * 0)`` start no program at all, so there is no
    unknowable program to refuse, and folding them to the unresolved
    sentinel would deny a command that cannot execute anything. It is also
    the identity element for the ``+`` walk below: ``[] + parts`` takes its
    slot 0 from ``parts``.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<empty-argv>"


_ARGV_SLOT_EMPTY = _EmptyVectorValue()


def _argv_program_slot(node: Any, env: Dict[str, Any], budget: _FoldBudget, memo: Dict[int, Any], depth: int) -> Any:
    """Resolve SLOT 0 -- the program slot -- of an argv-vector expression,
    returning the folded value, `_ARGV_SLOT_EMPTY` for a statically empty
    vector, or `_FOLD_UNRESOLVED` when the slot cannot be known.

    Deliberately WEAKER than folding the whole vector, and that is the
    point: the later slots are arguments, and part 16's measurement says an
    unresolved argument is honest usage. ``['pytest', sys.argv[1]]`` does
    not fold, yet its slot 0 is the constant ``'pytest'`` and this returns
    it. Structural spellings that keep slot 0 determinable are walked rather
    than folded whole -- ``['python3'] + rest`` (concatenation takes slot 0
    from its left operand, or from the right one when the left is empty) and
    ``['python3'] * n`` (repetition keeps slot 0 for any count >= 1).

    NEGATIVE SPEC: this executes nothing. It is `_fold_expr`'s own closed
    arithmetic plus a structural walk over ``+``/``*``, and every shape it
    does not recognise resolves to the unresolved sentinel, which denies.
    A `ast.Starred` in slot 0 (``[*parts]``, ``[*prefix, 'commit']``) is
    unknowable by construction and never resolves.
    """
    if depth > _MAX_FOLD_DEPTH:
        budget.exceeded = True
        return _FOLD_UNRESOLVED
    if isinstance(node, (ast.List, ast.Tuple)):
        if not node.elts:
            return _ARGV_SLOT_EMPTY
        first = node.elts[0]
        if isinstance(first, ast.Starred):
            return _FOLD_UNRESOLVED
        return _fold_expr(first, env, budget, memo, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _argv_program_slot(node.left, env, budget, memo, depth + 1)
        if left is not _ARGV_SLOT_EMPTY:
            return left
        return _argv_program_slot(node.right, env, budget, memo, depth + 1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        for sequence, count_node in ((node.left, node.right), (node.right, node.left)):
            count = _fold_expr(count_node, env, budget, memo, depth + 1)
            if isinstance(count, int) and not isinstance(count, bool):
                if count < 1:
                    return _ARGV_SLOT_EMPTY
                return _argv_program_slot(sequence, env, budget, memo, depth + 1)
        return _FOLD_UNRESOLVED
    value = _fold_expr(node, env, budget, memo, depth)
    if value is _FOLD_UNRESOLVED:
        return _FOLD_UNRESOLVED
    if isinstance(value, (list, tuple)):
        return value[0] if value else _ARGV_SLOT_EMPTY
    return value


#: PART 19 (2026-08-05) -- the keyword ``subprocess.run``/``Popen`` accept
#: the argv vector under. Part 18 read the program slot out of the FIRST
#: POSITIONAL argument only, so ``subprocess.run(args=sys.argv[1:])`` -- the
#: same call, spelled with a keyword -- was exempt from the program-slot rule
#: entirely and ALLOWED at HEAD. The slot is a property of the ARGUMENT, not
#: of how it was passed.
_ARGV_PROGRAM_KEYWORD = "args"

#: PART 19 -- the interpreter flags whose argument is another PROGRAM: a
#: ``-c`` payload is source text the nested interpreter executes, a ``-m``
#: argument is a module it runs. Both are consumed by
#: `_argv_nested_interpreter_payload_is_unknown`.
_NESTED_INTERPRETER_CODE_FLAGS = ("-c", "-m")

#: How deep the nested-interpreter recursion may go before it fails CLOSED.
#: ``python3 -c`` inside ``python3 -c`` inside ``python3 -c`` is already well
#: past any honest usage, and the bound keeps a hand-nested payload from
#: turning a PreToolUse hook into a parser bomb -- same rule as every other
#: bound in this part: exceeding it is UNKNOWN, never "fine".
_MAX_NESTED_INTERPRETER_DEPTH = 3


class _UnknownVectorArgument:
    """"This call's argv vector is hidden from the walk" -- a ``**kwargs``
    splat (``subprocess.run(**opts)``), where the program slot is not merely
    unresolved but not even LOCATABLE. Distinct from ``None`` (the call names
    no vector at all, e.g. ``p.communicate()``), which is not a program
    question.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "<unknown-argv-argument>"


_ARGV_VECTOR_UNKNOWN = _UnknownVectorArgument()


def _argv_vector_argument(node: Any) -> Optional[Any]:
    """Return the expression carrying an argv-vector sink's VECTOR --
    ``node.args[0]``, or the ``args=`` keyword when the call passes it that
    way (part 19) -- or `_ARGV_VECTOR_UNKNOWN` when a ``**kwargs`` splat
    hides it, or ``None`` when the call names no vector at all.
    """
    if node.args:
        return node.args[0]
    if any(kw.arg is None for kw in node.keywords):
        return _ARGV_VECTOR_UNKNOWN
    for kw in node.keywords:
        if kw.arg == _ARGV_PROGRAM_KEYWORD:
            return kw.value
    return None


def _argv_vector_program_slot_is_unknown(
    node: Any,
    env: Dict[str, Any],
    budget: _FoldBudget,
    memo: Dict[int, Any],
    bindings: _PayloadBindings = _EMPTY_BINDINGS,
) -> bool:
    """PART 18: True when an argv-vector sink call cannot be shown to name
    its own program -- the case part 16's exemption left open, and the
    reason it is not an exemption-shaped question is recorded at
    `_ARGV_PROGRAM_SLOT_SINK_ATTRIBUTES`.

    Two shapes deliberately return False, because each is existing behaviour
    this leg must not regress: an argument that resolves to a string rather
    than a vector (the whole-command-text form, judged on its CONTENT by
    mechanism 1), and any sink outside the argv-vector family (already
    handled, in full, by `_sink_takes_opaque_program_text`).

    PART 19 removes the THIRD shape that used to return False -- "the program
    came in by keyword". ``subprocess.run(args=<unresolved>)`` starts exactly
    the program ``subprocess.run(<unresolved>)`` starts, so the vector is
    located by `_argv_vector_argument` and judged identically however it was
    passed; a ``**kwargs`` splat hides the slot completely and is unknown by
    construction. Sink identity is read through the payload's import
    bindings, so an aliased ``from subprocess import run as r`` is the same
    sink (`_resolved_call_identity`).
    """
    names, _modules = _resolved_call_identity(node.func, bindings)
    if not (names & _ARGV_PROGRAM_SLOT_SINK_ATTRIBUTES):
        return False
    vector = _argv_vector_argument(node)
    if vector is None:
        return False
    if vector is _ARGV_VECTOR_UNKNOWN or isinstance(vector, ast.Starred):
        return True
    return _argv_program_slot(vector, env, budget, memo, 0) is _FOLD_UNRESOLVED


def _argv_nested_interpreter_payload_is_unknown(
    node: Any,
    env: Dict[str, Any],
    budget: _FoldBudget,
    memo: Dict[int, Any],
    bindings: _PayloadBindings = _EMPTY_BINDINGS,
    depth: int = 0,
) -> bool:
    """PART 19 -- A KNOWN PROGRAM THAT IS AN INTERPRETER IS NOT A KNOWN
    PROGRAM: True when an argv-vector sink starts an interpreter whose
    ``-c``/``-m`` argument cannot be resolved.

    Part 18 asked the right question one level too shallowly.
    ``subprocess.run(['python3','-c', os.environ['X']])`` has a perfectly
    resolvable slot 0, so the program-slot rule passes it -- and what it
    starts is an interpreter running a program nobody can name, which is the
    same "we cannot know what this runs" condition mechanism 2 exists for,
    one indirection out. Confirmed ALLOW at HEAD.

    Interpreter identity reuses `_normalized_interpreter_head` and the two
    existing sets (`_PYTHON_INTERPRETER_NAMES`, `_C_FLAG_SHELL_INTERPRETERS`)
    rather than a third spelling of "what is a python", so versioned
    (``python3.11``), path-prefixed (``/usr/bin/python3``) and ``.exe``
    spellings are recognised here exactly as they are at the outer unwrap.

    A RESOLVABLE nested payload is not denied blanket -- it is EXAMINED. A
    resolved Python ``-c`` payload is parsed and walked through this same
    mechanism-2 pass (its own sinks, its own program slots, its own nesting,
    to `_MAX_NESTED_INTERPRETER_DEPTH`), while its TEXT is already visible to
    mechanism 1 as a literal of the outer payload. A resolved shell ``-c``
    payload and a resolved ``-m`` module name are likewise judged on content
    rather than refused: the claim of this leg is UNKNOWABILITY, and a
    payload spelled out in full is not unknowable.

    NEGATIVE SPEC: nothing here executes anything. The nested payload is
    parsed with `ast.parse` and walked; it is never eval'd, exec'd, compiled
    or imported, and the same node/depth/length budget bounds the whole
    recursion. Non-literal vectors (``['python3'] + rest``) return False --
    the flags are not visible, and refusing them would re-open the measured
    false positives part 18 deliberately kept.
    """
    if depth >= _MAX_NESTED_INTERPRETER_DEPTH:
        return True
    names, _modules = _resolved_call_identity(node.func, bindings)
    if not (names & _ARGV_PROGRAM_SLOT_SINK_ATTRIBUTES):
        return False
    vector = _argv_vector_argument(node)
    if not isinstance(vector, (ast.List, ast.Tuple)) or not vector.elts:
        return False
    program = _argv_program_slot(vector, env, budget, memo, 0)
    if not isinstance(program, str):
        return False
    head = _normalized_interpreter_head(program)
    is_python = head in _PYTHON_INTERPRETER_NAMES
    if not is_python and head not in _C_FLAG_SHELL_INTERPRETERS:
        return False
    elements = vector.elts
    for index, element in enumerate(elements[1:-1], start=1):
        flag = _fold_expr(element, env, budget, memo, 0)
        if flag not in _NESTED_INTERPRETER_CODE_FLAGS:
            continue
        nested = _fold_expr(elements[index + 1], env, budget, memo, 0)
        if nested is _FOLD_UNRESOLVED:
            return True
        if flag == "-c" and is_python and isinstance(nested, str):
            if _nested_python_source_has_opaque_sink(nested, budget, depth + 1):
                return True
    return False


def _nested_python_source_has_opaque_sink(source: str, budget: _FoldBudget, depth: int) -> bool:
    """Run mechanism 2 over a nested Python ``-c`` payload this module
    RESOLVED (part 19) -- the recursion `_argv_nested_interpreter_payload_is_
    unknown` promises instead of a blanket refusal.

    Fresh ``env``/``memo`` per nested tree, never the caller's: the memo is
    keyed by ``id(node)`` and the nested tree is a different object graph, so
    sharing one could read a cached fold for an unrelated node. The BUDGET is
    shared on purpose -- the bound exists to cap total work per ``check()``,
    and handing a nested payload a fresh allowance would be a way to buy
    unbounded parsing. An unparseable nested payload is UNKNOWN, hence True.
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return True
    memo: Dict[int, Any] = {}
    env = _fold_environment(tree, budget, memo)
    return _fold_finds_opaque_sink(tree, env, budget, memo, depth=depth)


def _fold_finds_opaque_sink(
    tree: Any,
    env: Dict[str, Any],
    budget: _FoldBudget,
    memo: Dict[int, Any],
    depth: int = 0,
) -> bool:
    """MECHANISM 2: True when some execution-sink call in ``tree`` takes at
    least one argument the folder could not resolve to a constant.

    The claim is narrow and is the whole justification: we are not asserting
    the command is a commit, we are asserting we CANNOT KNOW what it runs
    (``os.system(os.environ['X'])``, ``exec(fetched_text)``), and an unknown
    program is not something this seam may start on a subagent's behalf. A
    resolvable argument is judged on its CONTENT by mechanism 1 instead, so
    an honest ``subprocess.run(['python3','-m','pytest', ...])`` is untouched
    by this leg -- as is an argv-vector sink with an unresolved ELEMENT, per
    the measured narrowing at `_OPAQUE_PROGRAM_SINK_ATTRIBUTE_NAMES`
    (`_sink_takes_opaque_program_text`).

    TWO DOORS, not one (part 18): a whole-command-text sink denies on ANY
    unresolved argument, while an argv-vector sink denies only on an
    unresolved SLOT 0 (`_argv_vector_program_slot_is_unknown`) -- the
    program slot, which the narrowing above should never have exempted.
    ``depth`` is the nested-interpreter recursion counter, not a fold depth.

    PART 19 threads the payload's own name bindings (`_payload_bindings`)
    through every identity decision here, so an ALIASED sink is the same
    sink, and adds the second half of the argv door: a resolved slot 0 that
    is an INTERPRETER with an unresolved ``-c``/``-m`` argument
    (`_argv_nested_interpreter_payload_is_unknown`) is a known program
    starting an unknown one.
    """
    bindings = _payload_bindings(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _call_is_execution_sink(node, bindings):
            continue
        if not _sink_takes_opaque_program_text(node, env, budget, memo, bindings):
            if _argv_vector_program_slot_is_unknown(node, env, budget, memo, bindings):
                return True
            if _argv_nested_interpreter_payload_is_unknown(
                node, env, budget, memo, bindings, depth
            ):
                return True
            continue
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                return True
            if _fold_expr(arg, env, budget, memo, 0) is _FOLD_UNRESOLVED:
                return True
        for kw in node.keywords:
            if kw.arg is None:
                return True
            if _fold_expr(kw.value, env, budget, memo, 0) is _FOLD_UNRESOLVED:
                return True
    return False


@lru_cache(maxsize=_MAX_FOLD_CACHE_ENTRIES)
def _fold_python_c_payload(payload: str) -> _FoldedPayload:
    """THE FOLDER'S ENTRYPOINT (part 16): statically evaluate ``payload``'s
    constant expressions and report both mechanisms' inputs in one pass --
    the folded text (mechanism 1) and whether an execution sink takes an
    argument that did not resolve (mechanism 2).

    NEGATIVE SPEC, restated at the entrypoint because this is the function
    other code will call: it NEVER EXECUTES THE PAYLOAD. No ``eval``,
    ``exec``, ``compile``, ``ast.literal_eval`` over attacker text, no
    import of what the payload names, no call to a callable the payload
    supplies -- only `ast.parse` plus this module's own arithmetic over
    already-resolved values. Its bounds (`_MAX_FOLDED_VALUE_LEN`,
    `_MAX_FOLD_NODES`, `_MAX_FOLD_DEPTH`, `_MAX_FOLD_FIELD_WIDTH`) are
    LOAD-BEARING SECURITY PROPERTIES rather than tuning knobs: this runs
    inside a PreToolUse hook on every Bash call, so exceeding a bound must
    -- and does -- resolve to "unknown", which denies, and never to a
    partial value treated as complete.

    THE BUDGET IS PER PAYLOAD, NOT PER BASH CALL: a fresh `_FoldBudget` is
    built below for each payload, and one command may chain many ``python3
    -c`` segments. The per-call bound is the tokenizer's command-length
    ceiling, not these constants -- see the constant block for the measured
    worst case at that ceiling.

    Memoized (`lru_cache`) because ``check()`` runs both mechanisms over the
    same payloads; the function is pure over its string argument, so the
    cache can only save work, never change a verdict.
    """
    try:
        tree = ast.parse(payload)
    except Exception:
        return _FOLD_EMPTY
    budget = _FoldBudget()
    memo: Dict[int, Any] = {}
    env = _fold_environment(tree, budget, memo)
    text = " ".join(_fold_collect_text(tree, env, budget, memo))
    opaque = _fold_finds_opaque_sink(tree, env, budget, memo)
    return _FoldedPayload(
        text=text, opaque_sink_call=opaque, bounds_exceeded=budget.exceeded, parsed=True
    )


def _has_folded_commit_identity(cmd: str, *, legs: Optional[Set[str]] = None) -> bool:
    """MECHANISM 1, as a matcher: True when the text a Python ``-c``
    payload ASSEMBLES at runtime -- rather than the text it contains --
    carries a commit identity at any token position.

    Part 15 recovered word boundaries that quoting swallowed; it could not
    recover a name that was never contiguous text. ``'scoped-git' +
    '-commit'``, ``''.join(map(chr,[...]))``, ``f'{a}{b}'`` and a base64
    blob handed to ``exec`` all named the helper without ever spelling it,
    and all ALLOWED. `_fold_python_c_payload` computes those names without
    running anything, and the folded line then goes through the SAME
    boundary-anchored `_tokens_reach_commit_identity_anywhere` part 15 built
    -- no second notion of commit identity, and ``evil-scoped-git-commit``
    still does not match after folding either.

    Ordered AFTER the part-14 inertness check, which is load-bearing for the
    same reason it is in part 15: a payload with no execution sink cannot
    run whatever its strings spell, so folding its literals could only
    manufacture false positives. Inert payloads are skipped entirely.
    """
    for payload in _python_c_source_payloads(cmd):
        if _python_c_payload_is_provably_inert(payload):
            continue
        folded = _fold_python_c_payload(payload)
        if not folded.text:
            continue
        for text in _nested_reconstruction_chain(folded.text):
            tokens = _tokenize_full_command(text)
            if tokens is None:
                continue
            for seg_tokens in _segments_from_tokens(tokens):
                if seg_tokens and _tokens_reach_commit_identity_anywhere(seg_tokens):
                    _record_payload_leg(legs, _PAYLOAD_LEG_PYTHON_STRING_LITERALS)
                    return True
    return False


def _has_opaque_execution_sink(cmd: str, *, legs: Optional[Set[str]] = None) -> bool:
    """MECHANISM 2, as a matcher: True when a non-inert Python ``-c``
    payload hands an execution sink an argument the folder cannot resolve
    (`_fold_finds_opaque_sink`).

    This is the ONLY leg in this module that denies without having resolved
    a commit identity, and it is deliberately the narrowest such claim
    available: not "this is a commit" but "this starts a program nobody can
    name from the text, inside the one seam whose job is to keep a subagent
    from starting a committing program." A caller who meant something else
    can always spell the argv as literals.

    Its false-positive surface is real and was MEASURED, not asserted, at
    landing -- see this part's entry in the module docstring and the
    realistic-usage corpus in
    ``tests/test_python_c_constant_folding.py``. A future editor widening
    `_EXECUTION_SINK_ATTRIBUTE_NAMES` (or forgetting a subtraction) widens
    THIS, not just the inertness check: re-measure that corpus rather than
    reasoning about it.
    """
    for payload in _python_c_source_payloads(cmd):
        if _python_c_payload_is_provably_inert(payload):
            continue
        if _fold_python_c_payload(payload).opaque_sink_call:
            _record_payload_leg(legs, _PAYLOAD_LEG_PYTHON_OPAQUE_SINK)
            return True
    return False


#: A ``-c``-shaped flag run, ANYWHERE a shell token could start it. Anchored
#: only on "not preceded by a word character", so it covers the bundled
#: spellings `_BUNDLED_C_FLAG_RE` accepts (``-ic``, ``-cO``) and the quoted
#: one (``python3 "-c" '...'``, where the preceding character is a quote
#: rather than whitespace). Over-matching (``--capture``) is deliberate and
#: harmless: this only decides whether the full matchers RUN.
_C_FLAGISH_RUN_RE = re.compile(r"(?<![\w])-[A-Za-z]*c", re.IGNORECASE)


def _may_carry_python_c_payload(cmd: str) -> bool:
    """Cheap, deliberately COARSE over-approximation of "this command could
    hand Python source to an interpreter's ``-c``" -- a case-insensitive
    ``python`` substring plus a ``-c``-shaped flag run, nothing more.

    Required by `_prefilter_mentions_commit`'s LESSON rule, and the reason
    is specific to part 16: every earlier matcher needed the literal text
    ``commit`` (or a `_COMMITTING_OP_NAMES` name) to appear in ``cmd``, so a
    commit-substring pre-filter was a strict superset of all of them.
    Mechanism 1 breaks that property by design -- ``''.join(map(chr,[...]))``
    spells the helper with no commit substring anywhere in the command --
    and mechanism 2 breaks it further, since an opaque argument names
    nothing at all. Both live only inside a Python ``-c`` payload, and every
    interpreter token this module recognizes normalizes from a basename
    containing ``python`` (`_normalized_interpreter_head`), so this
    substring is a strict superset of both.

    Both legs are over-approximations, and each is checked against the
    matcher it must not be narrower than -- the pre-filter LESSON's exact
    failure mode, which this module has shipped twice. The interpreter leg:
    `_normalized_interpreter_head` derives identity from a case-folded
    basename, so every spelling it resolves to ``python``/``python3``
    (versioned, ``.exe``-suffixed, path-prefixed) contains ``python``
    case-insensitively. The flag leg: the unwrap requires a token matching
    `_BUNDLED_C_FLAG_RE`, and such a token can only begin after whitespace,
    a quote, or a segment separator -- never after a word character -- which
    is exactly what `_C_FLAGISH_RUN_RE` accepts. A plain ``python3 -m
    pytest <path>`` therefore stays out of the full-matcher stage, which is
    the point: admitting it would put the tokenizer's command-length cost on
    the most common Python command a dispatched agent runs.
    """
    return "python" in cmd.lower() and _C_FLAGISH_RUN_RE.search(cmd) is not None


def _prefilter_mentions_commit(cmd: str) -> bool:
    """Cheap pre-filter (before identity-resolution cost) -- 2026-07-25
    HARDENED to a strict over-approximation after a confirmed bypass
    report. Widened again 2026-08-01 (hole (a) fix, see the module
    docstring's part-6 entry): a bare ``"commit" in cmd`` check was a valid
    strict superset of ``_has_git_commit`` and ``_has_coordinator_safe_
    commit`` (both require the literal substring ``commit`` somewhere in
    their match), but ``_has_committing_op_invoke`` was generalized from
    checking only ``ceremony.scoped_git_commit`` (which DOES contain
    ``commit``) to checking every name in ``_COMMITTING_OP_NAMES`` --
    ``session.boot_sweep``, ``distill.apply_disposal``, and ``memo.send``
    contain no ``commit`` substring at all, and ``ceremony.wsc_tail``
    doesn't either. A bare ``"commit" in cmd`` check alone is therefore no
    longer a superset of the full matcher set: fixed by ALSO checking for
    each committing op's literal name as a substring, consulting the SAME
    ``_COMMITTING_OP_NAMES`` set the full matcher does (rather than a
    second, independently-hand-maintained name list that could drift from
    the first).

    All matchers this gates (``_has_git_commit``, ``_has_coordinator_safe_
    commit``, and ``_has_committing_op_invoke``) REQUIRE the literal,
    case-sensitive substring ``"commit"`` OR one of ``_COMMITTING_OP_
    NAMES`` to appear somewhere in ``cmd``: ``_has_git_commit`` requires it
    as the standalone token ``commit`` itself, ``_has_coordinator_safe_
    commit`` requires it as a suffix of ``coordinator-safe-commit``, and
    ``_has_committing_op_invoke`` requires the literal op name (one of
    ``_COMMITTING_OP_NAMES``) to appear as a token. So
    ``"commit" in cmd or any(op in cmd for op in _COMMITTING_OP_NAMES)`` is
    a valid strict superset of every matcher's true-positive set -- and it
    is cheaper than the shape check it replaces.

    LESSON -- do not narrow this again: the PREVIOUS pre-filter required
    the literal two-word sequence ``"git commit"`` OR ``"git -C"``
    followed later by ``"commit"``. That shape is narrower than the git-
    commit regex it gated, which also matched
    ``git -c user.name=x commit`` (arbitrary global option) and
    ``git  commit`` (extra whitespace) -- and it did not check for
    ``coordinator-safe-commit`` at all, contradicting this module's own
    docstring claim that the helper is in scope. Three confirmed bypasses
    (2026-07-25 report) resulted from that one gap. This is the SECOND
    guard in this repo to ship a bypass from a cheap-filter/full-matcher
    pair where the cheap filter stopped short of the full matcher's shape
    (see ``block_reviewer_bash_outside_allowlist.py``'s boundary-anchoring
    fix for the first instance). A pre-filter's ONLY legitimate job is to
    cheaply reject inputs the full matcher PROVABLY cannot match -- it must
    never encode a narrower shape than the matcher on the theory that the
    narrower shape is "the common case." When this module grows a new
    detection shape, widen this pre-filter's over-approximation FIRST, then
    add the precise matcher -- never the other way round.

    Re-checked 2026-07-25, part 2 (git-binary boundary-anchoring fix):
    widening ``_tokens_reach_commit_after_git`` to recognise ``git`` via
    path-separator-boundary matching (``/usr/bin/git``, ``bin/git``, a
    Windows ``\\git`` suffix) instead of literal-token equality does NOT
    change what this pre-filter must over-approximate -- the widened
    matcher still requires the literal, case-sensitive standalone token
    ``commit`` to appear after the recognised ``git`` token; only the
    ``git``-recognition step widened, not the ``commit``-recognition step.
    ``"commit" in cmd`` remains a strict superset of the widened matcher's
    true-positive set. Confirmed by inspection, not by assumption -- this
    is exactly the kind of asymmetric-hardening gap the paragraph above
    warns about, so it gets a fresh confirmation each time either matcher
    changes, not a one-time pass.

    Re-checked 2026-08-03 (C1 -- ``_wrapped_shell_c_payloads`` widened to
    also unwrap a Python interpreter's ``-c`` payload, see the module
    docstring's part-9 entry): this pre-filter runs on the TOP-LEVEL
    ``cmd`` string, before any unwrapping happens, and a ``-c`` payload is
    by construction a literal substring of that top-level string (shlex
    quoting wraps it; it does not remove or transform its text) -- so
    whatever committing text a matcher finds AFTER unwrapping was already
    present, verbatim, inside the ``cmd`` this pre-filter already scans.
    ``"commit" in cmd or any(op in cmd for op in _COMMITTING_OP_NAMES)``
    therefore REMAINS a strict superset after C1: no widening of this
    pre-filter was needed, because the new detection shape adds a new WAY
    to reach the same three matchers' existing true-positive text, not a
    new true-positive text shape of its own. This holds for every nesting
    depth the recursive unwrap reaches, by the same substring argument
    applied inductively. Confirmed by inspection this session, not by
    assumption, per the LESSON paragraph above.

    Re-checked 2026-08-03, part 2 (Finding 5, security review -- the
    reconstructed-argv-text case explicitly): the substring argument above
    reasons about a ``-c`` payload as raw, contiguous text -- it does not,
    on its face, cover ``_python_c_payload_argv_text``'s synthetic,
    space-joined reconstruction of a Python payload's quoted string
    literals (e.g. ``['scoped-git-commit', '-m', 'x']`` rejoins to
    ``"scoped-git-commit -m x"``, which is NOT a literal substring of the
    original comma-and-quote-separated source when it spans more than one
    literal). Traced by hand and confirmed it still holds: reconstruction
    only ever inserts a synthetic SPACE *between* two string literals and
    never merges characters *across* a literal boundary, so any keyword a
    downstream matcher subsequently finds in the reconstructed text lies
    wholly WITHIN one original literal -- and that literal, unmodified, IS a
    literal substring of ``cmd``. The reconstruction step can only ever
    split a true-positive match apart at a boundary this pre-filter does not
    need (it looks for a bare substring, not an adjacent-token shape), never
    fabricate one that was not already present verbatim in ``cmd``.

    Re-checked 2026-08-04, part 15 (a FOURTH matcher,
    ``_has_reconstructed_commit_identity``, now gates on this pre-filter
    too), per the LESSON paragraph's standing rule that a new detection
    shape gets a fresh confirmation rather than an assumed one: that matcher
    resolves the SAME three identities the other three do -- a
    ``_COMMIT_HELPER_BINARY_NAMES`` basename (each of which ends in the
    literal ``commit``), a ``git ... commit`` chain (the standalone token
    ``commit``), or a ``_COMMITTING_OP_NAMES`` op name -- and relaxes only
    WHERE in the token list it will accept them. Relaxing position cannot
    introduce a true positive whose text is not one of the substrings this
    pre-filter already over-approximates, and the part-2 paragraph above
    already establishes that reconstruction never fabricates text absent
    from ``cmd``. No widening was needed.

    WIDENED 2026-08-05, part 16 -- the first widening this pre-filter has
    ever needed, and the LESSON paragraph above is why it happened here
    rather than after a bypass report. Parts 13-15 all matched TEXT PRESENT
    in ``cmd``; part 16's two mechanisms do not. Mechanism 1 matches text
    the payload ASSEMBLES (``''.join(map(chr,[115,99,...]))`` spells the
    helper with no ``commit`` substring anywhere in the command), and
    mechanism 2 denies an execution sink whose argument names nothing at
    all. Both are confined to Python ``-c`` payloads, so the superset
    property is restored by OR-ing in `_may_carry_python_c_payload` -- see
    that function for why a coarse ``python`` substring, and not a narrower
    ``-c``-shaped test, is the right over-approximation.

    The three older matchers are unaffected by the widening: each still
    requires the literal ``commit`` substring or a `_COMMITTING_OP_NAMES`
    name to match, so admitting more commands to the full-matcher stage
    cannot make any of THEM fire where they did not before -- pinned by
    ``test_prefilter_widening_moves_no_pre_part16_verdict``.
    """
    return (
        "commit" in cmd
        or any(op in cmd for op in _COMMITTING_OP_NAMES)
        or _may_carry_python_c_payload(cmd)
    )


# --- C3 (2026-08-03-narrow-subagent-commit-confinement-two-classes.md):
#     the ONE deliberate allow-path widening in this module -- a narrow,
#     route-keyed exemption for `coordinator:git-commit-agent`, DR-125
#     Ruling 3. Prior art: `nudge_subagent_scoped_commit.py`'s retired
#     `_SCOPED_RE` scoped-pathspec exemption was deliberately NOT ported when
#     that module was retired (see this module's docstring, part 6's
#     preceding section comment, and the 2026-07-25 entry it references) --
#     the stated reason was "M4 denies every subagent git commit, scoped or
#     not". That reason is SUPERSEDED BY A CHANGED PRECONDITION here, not
#     overruled: at the time, no subagent had a legitimate commit route at
#     all, so a scoped-pathspec exemption had nothing to be an exemption FOR.
#     DR-125 Ruling 3 creates that route for the first time
#     (`coordinator:git-commit-agent`), so reviving a scoped-pathspec-gated
#     exemption -- narrowed to exactly one `subagent_type` and one op, unlike
#     the retired module's blanket form -- is not a regression of the
#     2026-07-25 retirement; it answers a precondition that no longer holds.
#
#     All three legs below are AND-ed together in `_git_commit_agent_may_
#     commit`; `check()` additionally requires LEG 1 (the strict
#     `payload["agent_type"]` check) before ever calling it -- see that
#     function's own docstring for why LEG 1 lives in `check()` rather than
#     here. ---


def _extract_invoke_scoped_git_commit_paths(
    seg_tokens: list,
) -> Optional[Tuple[List[str], bool]]:
    """LEG 2 (the ``python3 -m coordinator_core.invoke ceremony.scoped_git_
    commit ...`` spelling) + LEG 3's raw pathspec extraction, combined: find
    a genuine ``ceremony.scoped_git_commit`` invocation (mirroring
    ``_tokens_reach_committing_op_after_python``'s python-token/``-m``/
    module-name/flag-tolerant-positional walk, but requiring the op to be
    EXACTLY ``ceremony.scoped_git_commit`` -- not any member of
    ``_COMMITTING_OP_NAMES`` -- since only this op takes an explicit
    pathspec at all) and return ``(paths, include_orphans)`` -- ``paths``
    parsed from the JSON params positional's ``paths`` key, and
    ``include_orphans`` from that SAME parsed dict's ``include_orphans`` key
    (``bool(parsed.get("include_orphans", False))``, mirroring exactly how
    ``ceremony.scoped_git_commit._handler`` itself reads the flag off the
    wire params -- see that module's docstring; this is the JSON-body
    spelling, there is no separate CLI-flag form for this invocation shape).
    2026-08-04 (F0 fix): previously this function returned ``paths`` alone
    and the caller hard-coded ``allow_orphans=True`` regardless of whether
    the invocation actually asked for it -- now the caller mirrors the
    invocation's own opt-in instead.

    Returns ``None`` when no such invocation is found in ``seg_tokens``, OR
    when a matched invocation's pathspec is not determinable from argv text
    at all -- ``--params-file`` (the payload lives in a file/stdin this
    guard cannot read), unparseable JSON, a non-object payload, or a
    ``paths`` value that is not a JSON array. The caller treats this
    identically to "no explicit pathspec was given" (deny) -- AC6's
    absent-pathspec case, not a distinct outcome.
    """
    n = len(seg_tokens)
    for start in range(n):
        if not any(
            _token_matches_binary(seg_tokens[start], name)
            for name in _PYTHON_INTERPRETER_NAMES
        ):
            continue
        i = start + 1
        while i < n and seg_tokens[i] != "-m" and seg_tokens[i].startswith("-"):
            i += 1
        if i >= n or seg_tokens[i] != "-m":
            continue
        i += 1
        if i >= n or seg_tokens[i] != _CEREMONY_INVOKE_MODULE:
            continue
        i += 1
        op_idx = _first_positional_after_invoke_module(seg_tokens, i)
        if op_idx is None or seg_tokens[op_idx] != "ceremony.scoped_git_commit":
            continue
        j = op_idx + 1
        while j < n:
            tok = seg_tokens[j]
            if tok.startswith("-"):
                flag_name = tok.split("=", 1)[0]
                if flag_name == "--params-file":
                    # Paths live in a file/stdin this guard cannot read --
                    # not determinable from argv text (see docstring above).
                    return None
                if flag_name in _INVOKE_FLAGS_WITH_VALUE:
                    j += 1 if "=" in tok else 2
                else:
                    j += 1
                continue
            try:
                parsed = json.loads(tok)
            except (ValueError, TypeError):
                return None
            if not isinstance(parsed, dict):
                return None
            paths = parsed.get("paths")
            if not isinstance(paths, list):
                return None
            return [str(p) for p in paths], bool(parsed.get("include_orphans", False))
        return None
    return None


def _extract_trampoline_scoped_git_commit_paths(
    seg_tokens: list,
) -> Optional[Tuple[List[str], bool]]:
    """LEG 2 (the ``scoped-git-commit(.cmd)?`` trampoline spelling) + LEG
    3's raw pathspec extraction, combined: peel wrapper/env/assignment/
    ``python3``-prefix noise via ``_peeled_effective_tokens`` (the same
    peel ``_first_effective_token`` uses, minus its head-token collapse),
    confirm the resulting head token boundary-matches
    ``_SCOPED_GIT_COMMIT_BINARY`` specifically (NOT the wider
    ``_COMMIT_HELPER_BINARY_NAMES`` set -- ``coordinator-safe-commit`` takes
    no pathspec argument at all and must always deny, per AC6), then return
    ``(paths, include_orphans)`` -- ``paths`` is every token after the CLI's
    own required ``--`` separator (see ``coordinator/bin/scoped-git-commit``'s
    ``_parse_args`` -- everything after ``--`` is the pathspec, never a
    flag), and ``include_orphans`` is whether the literal ``--include-orphans``
    token appears BEFORE that separator (the only spelling
    ``_parse_args`` recognizes -- no ``=value`` form, and a token after
    ``--`` is a path, not a flag, so this scan is bounded to ``rest[:sep_idx]``
    deliberately, not the whole token list). 2026-08-04 (F0 fix): previously
    this function returned ``paths`` alone and the caller hard-coded
    ``allow_orphans=True`` regardless of whether the invocation actually
    carried this flag.

    Returns ``None`` when the head token is not this binary, or no ``--``
    separator is present (an unscoped/malformed invocation this CLI itself
    rejects with a usage error at runtime -- AC6's absent-pathspec case).
    """
    tokens = _peeled_effective_tokens(seg_tokens)
    if not tokens:
        return None
    head = tokens[0]
    rest = tokens[1:]
    if head == "python3" and rest:
        head, rest = rest[0], rest[1:]
    if not _token_matches_binary(head, _SCOPED_GIT_COMMIT_BINARY):
        return None
    if "--" not in rest:
        return None
    sep_idx = rest.index("--")
    include_orphans = "--include-orphans" in rest[:sep_idx]
    return _pathspec_tokens_before_redirection(rest[sep_idx + 1 :]), include_orphans


#: A shell REDIRECTION token as `_command_tokenizer.tokenize_full_command`
#: emits it -- `>file`, `>>file`, `2>file`, `<file`, and (post-
#: `join_redirection_operator_tokens`) the fd-duplication spellings `2>&1`,
#: `>&2`, `&>file`. A redirection and its target are shell syntax, not argv,
#: so they are never part of the pathspec the CLI itself receives.
_REDIRECTION_TOKEN_RE = re.compile(r"^(?:&?\d*(?:>>?|<)|\d+<)")


def _pathspec_tokens_before_redirection(tokens: List[str]) -> List[str]:
    """Strip every redirection (operator + its own target) out of a raw
    post-``--`` token run, keeping every real pathspec token -- including
    ones that follow a redirection.

    ``scoped-git-commit -m x -- a.py > /tmp/out.log`` previously resolved a
    pathspec of ``['a.py', '>', '/tmp/out.log']`` -- the redirection operator
    and its target were handed to the ownership-scope check as if the shell
    were going to commit them. The real CLI never sees those tokens (the
    shell consumes them), so the guard was evaluating a pathspec the sink
    would not receive: a deny naming ``/tmp/out.log`` as out-of-scope is a
    false cause, and an in-repo redirect target could have padded the
    inspected pathspec with a path the commit never touched.

    Review: coordinator:code-reviewer -- this previously `break`ed at the
    first redirection-shaped token and discarded every token after it, so
    ``-- a.py 2>&1 b.py`` (a real path trailing a mid-pathspec redirect)
    dropped ``b.py`` from the guard's own ownership check entirely (the
    sink's independent re-validation of the untruncated argv backstopped
    this, but the pre-check silently under-validated). A self-contained
    fd-duplication token (`_command_tokenizer.join_redirection_operator_tokens`
    already folded its target in -- `2>&1`, `&>2`, `<&0`) carries a literal
    `&` and consumes only itself; every other redirection operator (`>`,
    `>>`, `<`, `2>`, ...) is followed by a SEPARATE target token that this
    function must also skip. Skipping only the redirection (and, when
    present, its own target) rather than truncating keeps every later real
    path visible to the ownership-scope check.
    """
    kept: List[str] = []
    i = 0
    total = len(tokens)
    while i < total:
        token = tokens[i]
        if _REDIRECTION_TOKEN_RE.match(token):
            i += 1
            if "&" not in token and i < total:
                # Bare operator (`>`, `>>`, `<`, `2>`, ...) -- its target is
                # a separate following token, never itself a real pathspec
                # entry (the shell consumes it), so skip that too.
                i += 1
            continue
        kept.append(token)
        i += 1
    return kept


def _command_is_single_segment(cmd: str) -> bool:
    """Finding-1 fix (2026-08-03 P0, security-review-confirmed live bypass):
    a POSITIVE precondition on the C3 allow branch -- ALLOW only when
    ``cmd`` resolves to exactly ONE non-empty ``;``/``&``/``|``-delimited
    segment.

    ``check()``'s C3 branch returns ``None`` (allow) for the ENTIRE
    ``tool_input.command`` string, not merely the one segment
    ``_resolve_git_commit_agent_pathspec`` happens to validate.  That
    function returns on the FIRST segment matching a ``ceremony.scoped_git_
    commit`` shape and never inspects the remainder, so a compound command
    chaining a legitimate scoped commit alongside a second, wholly
    unvalidated committing segment (``scoped-git-commit -m x -- <path> ;
    git commit -a -m sweep``, the same shape joined with ``&&``, ``||``,
    ``&``, or piped with ``|``) was previously ALLOWED IN FULL -- the
    second segment executes as part of the same shell invocation once
    ``check()`` returns ``None``, and no sink downstream ever re-inspects a
    bare ``git commit`` that never routed through the
    ``ceremony.scoped_git_commit`` op layer.

    The sanctioned route grants exactly one narrow capability -- a single
    ``ceremony.scoped_git_commit`` invocation -- and there is no legitimate
    reason for that invocation to be compound, so "exactly one segment" is a
    PRECONDITION on the grant, not a limitation carved out of it.
    Implemented as a positive allow-list requirement (segment count == 1),
    never as a blocklist of separator characters -- segmentation uses the
    SAME tokenizer/segmenter (``_tokenize_full_command`` +
    ``_segments_from_tokens``) every matcher in this module already scans
    with, so a separator character INSIDE a quoted string (e.g. a commit
    message containing a literal ``;``) is correctly not counted as a
    segment boundary here either -- this is not a naive ``cmd.split(";")``.

    Fails CLOSED (``False``, not-single-segment) when ``cmd`` is
    unparseable (``_tokenize_full_command`` returns ``None``) -- an
    unparseable command has no reliable segment count to reason about, and
    the safe direction on this allow-path's own precondition is deny.

    Interpreter/wrapper-payload note: this precondition operates on
    ``cmd_for_scan`` (the heredoc-stripped, argv0-normalized text every
    matcher scans), the SAME text ``_resolve_git_commit_agent_pathspec``
    itself scans -- and that function deliberately does NOT unwrap ``sh
    -c``/``python -c`` payloads (see its own docstring), so a second
    committing segment hidden inside a ``-c`` payload string is not visible
    to LEG 2 either: the payload tokenizes as ONE opaque quoted word at the
    top level (one segment, containing text LEG 2's own matchers cannot
    parse into a ``ceremony.scoped_git_commit`` invocation, since the
    python-interpreter/``-m``/module-name walk requires those as LITERAL,
    unquoted, top-level tokens -- see ``_extract_invoke_scoped_git_commit_
    paths``), so LEG 2 fails and the whole command denies via the ordinary
    path already, independent of this precondition. Confirmed by inspection
    this session, not by assumption.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return False
    segments = [seg for seg in _segments_from_tokens(tokens) if seg]
    return len(segments) == 1


def _resolve_git_commit_agent_pathspec(cmd: str) -> Optional[Tuple[List[str], bool]]:
    """Scan every ``;``/``&``/``|``-delimited segment of ``cmd`` for a
    genuine ``ceremony.scoped_git_commit`` invocation (either spelling) and
    return ``(paths, include_orphans)`` on the first match -- both extracted
    together by whichever of ``_extract_invoke_scoped_git_commit_paths`` /
    ``_extract_trampoline_scoped_git_commit_paths`` matched, never
    re-derived by a second pass over ``cmd`` (F0 fix, 2026-08-04: the
    invocation's own ``--include-orphans``/``"include_orphans": true`` opt-in
    is read from the SAME scan that already extracts the pathspec). Returns
    ``None`` when no matching invocation is found anywhere in ``cmd``, OR
    ``cmd`` itself is unparseable.

    Deliberately does NOT unwrap ``sh -c``/``python -c`` payloads the way
    the three deny-side matchers do -- the allow predicate this feeds is the
    one deliberate allow-path widening in this module, so it stays
    conservative by construction: an indirected invocation this scan misses
    simply falls through to the ordinary deny path (still correct, since
    denying is always the safe direction here), rather than gaining a wider
    ALLOW surface than the three legs strictly require.
    """
    tokens = _tokenize_full_command(cmd)
    if tokens is None:
        return None
    for seg_tokens in _segments_from_tokens(tokens):
        if not seg_tokens:
            continue
        result = _extract_invoke_scoped_git_commit_paths(seg_tokens)
        if result is not None:
            return result
        result = _extract_trampoline_scoped_git_commit_paths(seg_tokens)
        if result is not None:
            return result
    return None


#: Git pathspec magic signature -- `man gitglossary` § pathspec -- is spelled
#: EVERY way as a leading `:` (long form `:(...)`, top-magic shorthand `:/`,
#: and the shorthand negative/exclude form `:!<pattern>`, equivalent to
#: `:(exclude)<pattern>`). AC14 named `:/` and `:(top)` as EXAMPLES of this
#: class, not an exhaustive enumeration -- Finding 4 (2026-08-03 security
#: review) confirmed enumerating spellings one at a time (as the prior
#: `_PATHSPEC_MAGIC_PREFIX`/`_PATHSPEC_TOP_MAGIC` pair did, catching `:(` and
#: `:/` but missing `:!`) is exactly the gap-reopens-on-a-different-spelling
#: shape this file's history keeps producing. Generalized instead to: any
#: pathspec element beginning with `:` is treated as (potentially) magic and
#: rejected wholesale, rather than allow-listing named forms one by one. A
#: plain Windows drive-letter path (`C:\...`, `C:/...`) is NOT a false
#: positive here -- its colon is never the FIRST character (the drive letter
#: precedes it), so `candidate.startswith(":")` never fires for one.

#: `-A`/`-a`/`--all` are `git commit`/`git add` FLAGS, not paths -- AC14
#: requires rejecting them if they appear as a pathspec element (a caller
#: could smuggle one into a `ceremony.scoped_git_commit` JSON `paths` array
#: the same way any other string element arrives).
_SWEEPING_FLAG_TOKENS = frozenset({"-A", "-a", "--all"})

#: Characters that make a pathspec element a glob rather than a literal
#: path -- AC14 rejects "any glob pattern".
_GLOB_CHARS = frozenset("*?[")


def _pathspec_element_is_sweeping(path: Any, git_root: str) -> bool:
    """AC14: True if ``path`` is a sweeping pathspec element -- the repo
    root, an ancestor directory of it, `.`/`./`, a magic-pathspec form
    (`:/`, `:(top)`, ...), a glob pattern, an empty string, or an
    `-A`/`-a`/`--all` flag token. False for an ordinary literal path
    (including an ordinary SUBDIRECTORY pathspec, e.g. `src/` -- AC14 does
    not require every directory pathspec to be rejected, only the repo root
    and its ancestors; `ceremony.scoped_git_commit`'s own docstring treats a
    directory pathspec as an accepted input shape).

    Resolved with ``posixpath`` (not ``os.path``) deliberately -- a git
    pathspec is always forward-slash-normalized regardless of host OS, so
    resolution must not depend on which platform this guard happens to run
    on (Windows is first-class here, same as everywhere else in this repo).
    ``git_root`` is REQUIRED (non-empty) -- the caller
    (``_git_commit_agent_may_commit``) already denies before ever reaching
    this function when ``git_root`` is unresolvable, so there is no
    "resolve against nothing" case to define a fallback for; a caller that
    somehow reaches this with a falsy ``git_root`` gets the fail-closed
    ``True`` (sweeping) rather than a silent pass.

    DOCUMENTED, ACCEPTED RESIDUAL (Finding 3, 2026-08-03 security review):
    resolution below is LEXICAL (``posixpath``), never ``os.path.realpath``/
    ``os.readlink`` -- deliberately, not an oversight. This is a PreToolUse
    hot-path check; adding filesystem I/O here would also introduce a
    TOCTOU window (the resolved target could change between this check and
    the actual `git commit`). Consequence: an in-repo symlink pointing at
    the repo root or an ancestor of it (e.g. `src/self -> /repo`) is NOT
    detected as sweeping by this function alone -- `src/self` resolves
    lexically to `.../src/self`, neither `root` nor a lexical ancestor of
    it.

    Independently mitigated, not merely hoped-for: this function's verdict
    is not the only gate a candidate path passes through.
    `_git_commit_agent_may_commit` ALSO requires every pathspec element to
    be a member of `assert_paths_in_session_scope`'s `safe_paths`
    (`coordinator_core/ops/session/scope_report.py` composing
    `safe_commit_offer.compute_offer`) -- verified this session by reading
    that ownership chain: `safe_paths` is built ONLY from this session's own
    claimed dirty paths (`touched.txt` entries ∪ mtime-dirty files ∪ this
    session's dispatched sub-agent fan-out), each an ORDINARY REPO-RELATIVE
    DIRTY FILE PATH from `git diff --name-only`/`git ls-files --others`, and
    membership is a literal string equality test (`p not in safe_set`) --
    never a filesystem walk, never symlink-aware expansion of an arbitrary
    candidate element. A symlink-to-root element like `src/self` is
    therefore in `safe_paths` ONLY if `src/self` (the symlink file itself,
    not anything reachable through it) happens to literally be one of this
    session's own claimed dirty paths -- an ordinary, narrow membership
    check with no traversal-through-the-symlink case at all. So a
    symlink-to-root element that is NOT itself one of this session's dirty
    files denies on the ownership leg regardless of this function's lexical
    blindness; the residual above is real but does not, by itself, grant an
    unscoped commit.
    """
    if not isinstance(path, str):
        return True
    candidate = path.strip()
    if not candidate:
        return True
    if candidate in (".", "./"):
        return True
    if candidate.startswith(":"):
        return True
    if candidate in _SWEEPING_FLAG_TOKENS:
        return True
    if any(ch in candidate for ch in _GLOB_CHARS):
        return True
    if not git_root:
        return True
    root = posixpath.normpath(git_root.replace("\\", "/"))
    candidate_posix = candidate.replace("\\", "/")
    resolved = (
        candidate_posix
        if posixpath.isabs(candidate_posix)
        else posixpath.normpath(posixpath.join(root, candidate_posix))
    )
    resolved = posixpath.normpath(resolved)
    if resolved == root:
        return True
    # Ancestor-of-repo-root check: walk UP from `root` (never an unbounded
    # walk -- `posixpath.dirname` reaches a fixed point, `/`, in a bounded
    # number of steps for any real path) looking for `resolved` among the
    # ancestors. A path elsewhere entirely (neither the root, an ancestor of
    # it, nor inside it) is NOT flagged here -- AC14 names only the repo
    # root and its ancestors, not an arbitrary out-of-repo path; the
    # ownership-scope check (`assert_paths_in_session_scope`) is this
    # predicate's separate, independent defense against that case.
    parent = root
    while True:
        next_parent = posixpath.dirname(parent)
        if next_parent == parent:
            return False
        if next_parent == resolved:
            return True
        parent = next_parent


def _git_commit_agent_may_commit(
    cmd: str,
    git_root: Optional[str],
    session_id: str,
    cwd: Optional[str],
) -> "Tuple[bool, str]":
    """LEG 2 + LEG 3 of the C3 allow predicate, plus the required landing-
    order safety net (module docstring's C3 entry / the `_assert_paths_in_
    session_scope` import comment above). LEG 1 -- the strict
    `payload["agent_type"] == _GIT_COMMIT_AGENT_TYPE` check -- is `check()`'s
    own responsibility and is NOT re-checked here; this function is only
    ever called once LEG 1 already holds, so it does not accept or inspect
    `agent_type`/`subagent_type` at all (there is nothing here that could
    correctly re-derive LEG 1's harness-supplied-only constraint from a
    `git_root`/`session_id`/`cwd` triple).

    Returns ``(allowed, deny_reason)``. ``allowed`` is IDENTICAL to this
    function's pre-existing bool-only contract (verdict logic unchanged by
    the message-threading fixes below -- see the module's ``_deny_reason``/
    ``check()`` docstrings). ``deny_reason`` names WHICH leg denied, and is
    one of three shapes:

      - the ``_reason`` leg `assert_paths_in_session_scope` returns, threaded
        through ONLY when this function actually reached and called that
        helper (never re-derived, never guessed);
      - one of the ``_LEG_*`` sentinels below -- an early leg that denied
        before the ownership check was ever consulted, and whose real cause
        `_GIT_COMMIT_AGENT_DENY_REASON`'s pathspec-scope prose actively
        MIS-NAMES (2026-08-04 incident: a dispatched agent denied on the
        compound-command leg was told to re-check a pathspec that was
        already correct, so it re-checked the pathspec forever);
      - ``""`` -- ALLOW (nothing to report), or an early leg with nothing
        agent-actionable to say (git_root unresolvable, the helper not
        importable, the helper raising).

    Fails CLOSED (returns ``(False, ...)``, meaning "fall through to the
    ordinary deny path") on every one of:
      - `git_root` unresolvable -- LEG 3's sweeping-pathspec check has
        nothing to resolve a candidate path against, and LEG 1 (agent_type)
        is satisfiable with no repo at all, so this is reachable with
        `git_root is None`.
      - `cmd` does not resolve to exactly ONE non-empty command segment
        (`_command_is_single_segment`, Finding-1 fix, 2026-08-03 P0) -- a
        `;`/`&`/`|`-chained command can smuggle a second, unvalidated
        committing segment alongside one legitimate scoped invocation; see
        that function's own docstring for the confirmed bypass this closes.
      - `_assert_paths_in_session_scope` not importable (lazily resolved to
        `None` here via `_import_assert_paths_in_session_scope`, at call
        time rather than module load, to keep the hot Bash-guard path free
        of the `coordinator_core.ops`/`coordinator_core.hooks` import cost)
        -- a partial/out-of-order landing of this plan's C4 chunk must not
        silently grant an unconstrained commit route.
      - no matched `ceremony.scoped_git_commit` invocation found in `cmd`
        (LEG 2 fails), or its pathspec is empty/absent/undeterminable from
        argv text (LEG 3's non-empty requirement). The SAME extraction that
        resolves `paths` also resolves whether THIS invocation carried
        `--include-orphans` (trampoline spelling) or `"include_orphans":
        true` (invoke-module spelling) -- carrying either now denies
        outright, at `_LEG_AGENT_ORPHAN_ADOPTION`, before `assert_paths_in_
        session_scope` is ever called (SC-DR-022, 2026-08-04 -- see the
        module docstring's part-12 entry, which supersedes part 11's
        MIRRORING fix for this dispatched-agent path).
      - ANY pathspec element is sweeping per `_pathspec_element_is_sweeping`
        (LEG 3's AC14 requirement -- a single sweeping element among
        otherwise-fine ones still denies the whole pathspec).
      - the ownership-scope check itself denies, or raises (never allowed
        to propagate past this function -- the `except` arm below still
        returns `(False, "")`, same as before this change, so the fail-
        closed-on-exception behaviour is byte-for-byte unchanged).
    """
    if not git_root:
        return False, ""
    if not _command_is_single_segment(cmd):
        return False, _LEG_COMPOUND_COMMAND
    assert_paths_in_session_scope = _import_assert_paths_in_session_scope()
    if assert_paths_in_session_scope is None:
        return False, ""
    resolved = _resolve_git_commit_agent_pathspec(cmd)
    if resolved is None:
        return False, _LEG_NO_PATHSPEC
    paths, include_orphans = resolved
    if not paths:
        return False, _LEG_NO_PATHSPEC
    if any(_pathspec_element_is_sweeping(p, git_root) for p in paths):
        return False, _LEG_SWEEPING_PATHSPEC
    if include_orphans:
        # SC-DR-022 (claude-central-em, 2026-08-04), the structural half of a
        # ruling that was prose-only when it landed: adoption is safe because
        # the adopter WAS THERE and knows what it just wrote -- provenance,
        # not timing. A dispatched committer holds no such provenance by
        # construction; it did not author the files, and its whole design
        # rests on provenance arriving WITH THE BRIEF. An agent adopting an
        # orphan is deriving scope from a denial, which is the sweeping
        # defect one level removed, laundered through a compliant committer.
        #
        # This supersedes part 11's MIRRORING rationale for the dispatched-
        # agent path ONLY, and does not revive the hard-coded `True` that
        # mirroring replaced -- the correction runs the other way. Mirroring
        # remains correct for what it was defending against (a guard granting
        # unilaterally what the sink still refused); it was simply never a
        # judgment about WHO was asking, and this function only ever runs
        # when the answer is "a dispatched subagent" (LEG 1 gates on
        # `effective_type == _GIT_COMMIT_AGENT_TYPE`, resolved from the
        # harness-supplied, non-cooperative `agent_id`).
        #
        # Denied loudly rather than silently downgraded to `False`: an agent
        # that asked for adoption and got a quiet strict-mode refusal would
        # read the stock orphan message, which advertises the very
        # re-invocation this forbids, and loop on it.
        return False, _LEG_AGENT_ORPHAN_ADOPTION
    try:
        # `allow_orphans=False`, KEYWORD-form (keyword-only on
        # `assert_paths_in_session_scope`'s own signature, so a future
        # positional-style call here cannot silently regress without a
        # `TypeError`) -- and now unconditionally strict, because the only
        # invocation that could have made it `True` is refused above by
        # SC-DR-022. Kept explicit rather than dropped so the strictness is
        # a stated property at the call site, not an inherited default.
        allowed, reason = assert_paths_in_session_scope(
            session_id, paths, cwd, allow_orphans=False
        )
    except Exception:
        return False, ""
    allowed = bool(allowed)
    return allowed, ("" if allowed else (reason or ""))


#: AC16's specialized deny message, for `effective_type ==
#: _GIT_COMMIT_AGENT_TYPE` only -- the generic message below asserts
#: "subagents may not commit" / "Only the EM ... may commit" / "There is NO
#: subagent-honored override", all now FALSE for this one type, and its
#: "Safe forward path" names the wrong action (report to the EM) instead of
#: the actual one (re-issue via the sanctioned route with a fixed pathspec).
#: Every OTHER type's message is untouched -- see `_deny_reason` below.
#:
#: 2026-08-03 correction (spike-verdict `2026-08-03-git-commit-agent-leg3-
#: payload-triple.md`, two wrong-leg investigations across two sessions):
#: the wording previously named ONLY the argv-shape leg (LEG 3's `_pathspec_
#: element_is_sweeping` reject-list). `_git_commit_agent_may_commit` also
#: fails closed on the ownership-scope leg (`_assert_paths_in_session_scope`
#: -- every pathspec element must resolve inside THIS session's own scope),
#: and that is the leg observed failing in the field: an agent invokes the
#: exact prescribed form, is denied identically, and (per the message as it
#: read then) has no way to tell the two legs apart -- it re-tries argv
#: variants against a scope failure that no argv change can fix. The message
#: now names both legs and orders the check: if the prescribed form was
#: already used verbatim, the argv leg is presumptively fine and the next
#: thing to check is whether the pathspec is in-scope, not the argv shape
#: again. Verdict logic (`_git_commit_agent_may_commit`, `check()`) is
#: UNCHANGED by this correction -- text only.
_GIT_COMMIT_AGENT_DENY_REASON = (
    "BLOCKED: git-commit-agent commits only via a non-sweeping, in-scope "
    "pathspec -- use instead: `scoped-git-commit -m <subj> -- <path>...` "
    "(reject `.`, `-A`, globs, repo-root/ancestor paths). Already used "
    "that form? Check path scope, not argv shape."
)

#: The deny message for `_PAYLOAD_LEG_PYTHON_STRING_LITERALS` -- reached
#: when the ONLY text that matched a commit shape was the synthetic argv
#: line rebuilt from a Python `-c` payload's own string literals.
#:
#: The generic message below is wrong for this leg in the way that matters:
#: the caller may not have tried to commit at all (a read-only
#: `ast.parse(open('coordinator/bin/scoped-git-commit').read())` denies
#: identically to `subprocess.run(['coordinator/bin/scoped-git-commit', ...])`
#: -- see `_PAYLOAD_LEG_PYTHON_STRING_LITERALS`), so "finish your edits and
#: report to the EM" names an action that resolves nothing, and no argv
#: variant of a read-only command that spells that path can ever pass. This
#: message therefore states the leg, states that the ambiguity is structural
#: rather than a detection bug to argue with, and names the two commands
#: that DO resolve it. Verdict is unchanged either way -- message selection
#: only. Prose stays inside `_message_size.MESSAGE_PROSE_CAP_BYTES`
#: (backtick spans in the `Use instead:` cue window exempt) -- pinned by
#: `test_python_c_payload_leg_message_fits_prose_cap`.
_PYTHON_C_PAYLOAD_DENY_REASON = (
    "BLOCKED: commit-helper path inside an interpreter `-c` payload -- the "
    "guard cannot tell a literal naming it from a subprocess call running it. "
    "No re-spelling passes. Use instead: `Read` for contents, or "
    "`python3 -m py_compile <file>` to syntax-check."
)

#: The deny message for `_PAYLOAD_LEG_PYTHON_OPAQUE_SINK` (part 16,
#: mechanism 2). Unlike the leg above, this one IS resolvable by
#: re-spelling, and the message must say so: the guard denied because it
#: could not read the argument, so writing the argv out as literals -- or
#: running the command as a plain Bash call, where the guard can read it --
#: is a genuine next action rather than advice that resolves nothing. Prose
#: stays inside `_message_size.MESSAGE_PROSE_CAP_BYTES`, pinned by
#: ``test_opaque_sink_message_fits_prose_cap``.
_PYTHON_C_OPAQUE_SINK_DENY_REASON = (
    "BLOCKED: interpreter `-c` payload starts a program built at runtime -- "
    "the guard cannot read what it runs, so it cannot clear it. Use instead: "
    "spell the argv as literals, or run the command directly as Bash."
)

#: Sentinels `_git_commit_agent_may_commit` returns in its ``deny_reason``
#: slot for the early legs -- the ones that deny BEFORE the ownership-scope
#: check runs, and which `_GIT_COMMIT_AGENT_DENY_REASON`'s "check path
#: scope, not argv shape" prose therefore describes wrongly. Deliberately
#: not free prose: `_deny_reason` maps them through
#: `_GIT_COMMIT_AGENT_LEG_MESSAGES` below, so an unrecognized value can only
#: ever fall back to the static message, never splice caller text into a
#: capped envelope.
#:
#: 2026-08-04 incident (four `coordinator:git-commit-agent` dispatches, two
#: denied): a trailing `2>&1` made an ordinary single-command invocation
#: read as compound (fixed at the tokenizer -- see `join_redirection_
#: operator_tokens`), and the denial then told the agent its PATHSPEC was
#: out of scope. Both agents had already verified the pathspec with
#: `git status --porcelain -- <paths>`, so the message sent them to re-verify
#: the one thing that was correct. A guard whose message names the wrong
#: cause is a defect independent of whether the verdict is right.
#: Namespace prefix every `_LEG_*` sentinel carries. `assert_paths_in_
#: session_scope`'s own reasons never start with it (they open with `"path
#: outside session ..."`), so `_deny_reason` can tell a sentinel from a real
#: ownership reason without a second parameter -- and a sentinel added later
#: but not registered in `_GIT_COMMIT_AGENT_LEG_MESSAGES` falls back to the
#: static message rather than leaking a raw identifier into agent-facing
#: prose.
_LEG_SENTINEL_PREFIX = "leg:"

_LEG_COMPOUND_COMMAND = "leg:compound-command"
_LEG_NO_PATHSPEC = "leg:no-pathspec"
_LEG_SWEEPING_PATHSPEC = "leg:sweeping-pathspec"
_LEG_AGENT_ORPHAN_ADOPTION = "leg:agent-orphan-adoption"

#: Per-leg deny prose. Each names its OWN cause and the single edit that
#: fixes it; none sends the reader to the pathspec-scope check unless the
#: pathspec-scope check is what denied. Prose stays inside
#: `_message_size.MESSAGE_PROSE_CAP_BYTES` (backtick spans exempt) -- pinned
#: by `test_git_commit_agent_leg_messages_fit_prose_cap`.
_GIT_COMMIT_AGENT_LEG_MESSAGES = {
    _LEG_COMPOUND_COMMAND: (
        "BLOCKED: git-commit-agent commits via ONE uncompounded command -- "
        "yours chained a second segment (`;`, `&&`, `||`, `|`, newline). "
        "Re-issue the `scoped-git-commit` call alone. Pathspec never "
        "inspected -- do not re-check."
    ),
    _LEG_NO_PATHSPEC: (
        "BLOCKED: git-commit-agent found no `--`-separated pathspec here -- "
        "use `scoped-git-commit -m <subj> -- <path>...`. Any invocation "
        "without `--` (including `--help`) lands here; path scope was never "
        "checked."
    ),
    _LEG_SWEEPING_PATHSPEC: (
        "BLOCKED: git-commit-agent rejected a SWEEPING pathspec element -- "
        "`.`, `-A`, globs, and repo-root/ancestor paths are refused. Name "
        "each file explicitly; path scope was never checked."
    ),
    _LEG_AGENT_ORPHAN_ADOPTION: (
        "BLOCKED: orphan adoption is an operator's answer, not an agent's. "
        "An orphan offer is addressed to your EM -- for you it is "
        "information to RELAY, never authorization. Re-issue without it and "
        "report the refusal upward."
    ),
}

#: The stable prefix `assert_paths_in_session_scope`'s deny ``reason``
#: always opens with (its own docstring: `"path outside session %s scope:
#: %r (%s)"` for the FIRST denied path) -- `_ownership_leg_summary` below
#: strips everything up to and including this marker so the summary starts
#: at the `%r (%s)` fragment (the path + its classification) rather than
#: repeating the session id.
_OWNERSHIP_LEG_REASON_SCOPE_MARKER = "scope: "

#: `assert_paths_in_session_scope`'s reason additionally appends a
#: per-denied-path breakdown and a committable-remainder note (SC-DR-019) --
#: both useful for a human reading the raw op result, but far past this
#: module's `MESSAGE_PROSE_CAP_BYTES` budget once threaded into a guard
#: deny message. `_ownership_leg_summary` keeps only the FIRST-denied-path
#: fragment (everything before this marker), which already names the one
#: path and classification that matters for an agent that only sent one
#: bad path in an otherwise-fine pathspec.
_OWNERSHIP_LEG_REASON_ENUMERATION_MARKER = "; denied paths"

#: Bound on the (already backtick-exempted, per `_message_size`'s cue-window
#: rules) dynamic fragment `_deny_reason` splices into its ownership-leg
#: branch -- generous enough for a real path + classification pair, still
#: bounded so a pathological path/classification cannot blow the prose
#: budget by growing the backtick-delimiter overhead alone.
_OWNERSHIP_LEG_SUMMARY_MAX_BYTES = 70


def _ownership_leg_summary(reason: str, *, max_bytes: int = _OWNERSHIP_LEG_SUMMARY_MAX_BYTES) -> str:
    """Extract a short, operator-facing "which path, why" fragment from an
    `assert_paths_in_session_scope` deny ``reason`` string, bounded to
    ``max_bytes`` UTF-8 bytes (ellipsised, never mid-codepoint).

    Returns ``""`` for an empty/falsy ``reason`` -- the caller (`_deny_
    reason`) reads that as "the ownership leg was never reached", never as
    "it ran and said nothing" (that shape cannot occur: `assert_paths_in_
    session_scope` never returns `(False, "")`).
    """
    if not reason:
        return ""
    head = reason.split(_OWNERSHIP_LEG_REASON_ENUMERATION_MARKER, 1)[0]
    marker_idx = head.find(_OWNERSHIP_LEG_REASON_SCOPE_MARKER)
    fragment = (
        head[marker_idx + len(_OWNERSHIP_LEG_REASON_SCOPE_MARKER):]
        if marker_idx != -1
        else head
    )
    fragment = fragment.strip()
    encoded = fragment.encode("utf-8")
    if len(encoded) > max_bytes:
        fragment = encoded[:max_bytes].decode("utf-8", errors="ignore") + "..."
    return fragment


def _deny_reason(
    agent_id: str,
    effective_type: str,
    agent_type: str,
    cmd: str,
    ownership_reason: str = "",
    command_leg: str = "",
) -> str:
    """Build the operator-facing deny message.

    ``agent_id``/``cmd``/``effective_type`` are accepted for call-site parity
    and future diagnostics but deliberately NOT interpolated into the
    returned prose -- unbounded caller-supplied text (a long ``cmd``) cannot
    coexist with the fixed ``MESSAGE_PROSE_CAP_BYTES`` budget (spec:
    ``docs/plans/2026-08-02-guard-message-size-discipline.md`` chunk C8).
    ``ownership_reason`` (new) is likewise bounded before use -- see
    ``_ownership_leg_summary`` -- for the same reason.

    ``agent_type`` (the strict, harness-supplied leg -- NOT ``effective_
    type``, which ORs in the disk-read, subagent-writable ``subagent_type``
    backpointer) IS read here (AC16) -- ``coordinator:git-commit-agent`` gets
    a message naming its sanctioned route instead of the now-false
    "subagents may not commit" absolutes; every other type's message is
    byte-identical to before this branch existed.

    Finding-2 fix (2026-08-03, security review): the C3 ALLOW branch in
    `check()` is deliberately keyed STRICTLY on `agent_type` -- never
    `effective_type` -- precisely because `subagent_type` is read off a
    disk backpointer file the subagent being gated can itself write (AC15's
    adversary). Message SELECTION must honor the same asymmetry: gating this
    branch on `effective_type` would let that same adversary -- empty/absent
    `agent_type` plus a forged backpointer resolving to `coordinator:git-
    commit-agent` -- confirm/probe route-specific behavior via message
    content even though the verdict itself still correctly denies. Gating on
    `agent_type` alone closes that probing seam without changing any
    verdict.

    Ownership-leg naming (this dispatch's fix): ``_git_commit_agent_may_
    commit`` threads its ``ownership_reason`` leg through to ``check()``,
    which passes it here. When non-empty (the ownership-scope check
    actually ran and denied), the returned message names the offending
    path/classification instead of the generic ``_GIT_COMMIT_AGENT_DENY_
    REASON`` static text.

    Early-leg naming (2026-08-04 fix): when ``ownership_reason`` is one of
    the ``_LEG_*`` sentinels, the message comes from
    ``_GIT_COMMIT_AGENT_LEG_MESSAGES`` and names THAT leg -- a compound
    command, an absent pathspec, a sweeping element -- rather than the
    static text's pathspec-scope advice, which is false for all three and
    was observed sending dispatched agents to re-verify a correct pathspec.
    ``_GIT_COMMIT_AGENT_DENY_REASON`` still fires, unchanged, whenever
    ``ownership_reason`` is empty (a leg with nothing agent-actionable to
    say: an unresolvable git root, an unimportable helper, the helper
    raising). This is a MESSAGE-SELECTION change only;
    ``check()``'s own allow/deny verdict is computed before this function is
    ever called and is untouched by which branch below fires.

    Payload-leg naming (2026-08-04 fix, same precedent as the ownership-leg
    correction above): ``command_leg`` is the unwrap leg the MATCHERS
    reported the match through -- resolved in ``check()`` from the collector
    the three matchers fill (`_record_payload_leg`) and threaded here
    exactly the way ``ownership_reason`` already is. It is deliberately NOT
    recomputed here by re-scanning ``cmd``: a second, independently-derived
    notion of why this guard denied is the drift shape this module's history
    keeps producing.

    Ordering, stated because it is load-bearing: the ``command_leg`` branch
    fires FIRST, ahead of the ``agent_type`` branch, because it is keyed on
    COMMAND SHAPE rather than identity and its cause is the more proximate
    one -- for a command that only matched through the literal
    reconstruction, no pathspec/route advice is actionable. The
    identity-keyed branch below is otherwise untouched, and no
    identity-keyed selection is widened: ``effective_type`` still selects
    nothing (Finding-2 asymmetry above), and this new branch reads no
    identity field at all, so it cannot become a probing seam.
    """
    if command_leg == _PAYLOAD_LEG_PYTHON_STRING_LITERALS:
        return _PYTHON_C_PAYLOAD_DENY_REASON
    if command_leg == _PAYLOAD_LEG_PYTHON_OPAQUE_SINK:
        return _PYTHON_C_OPAQUE_SINK_DENY_REASON
    if agent_type == _GIT_COMMIT_AGENT_TYPE:
        if ownership_reason.startswith(_LEG_SENTINEL_PREFIX):
            return _GIT_COMMIT_AGENT_LEG_MESSAGES.get(
                ownership_reason, _GIT_COMMIT_AGENT_DENY_REASON
            )
        summary = _ownership_leg_summary(ownership_reason)
        if summary:
            return (
                "BLOCKED: git-commit-agent commits only via a non-sweeping, "
                "in-scope pathspec -- use instead: `scoped-git-commit -m "
                "<subj> -- <path>...`. Argv shape was fine; denied on path "
                "scope: `%s`." % summary
            )
        return _GIT_COMMIT_AGENT_DENY_REASON
    return (
        "BLOCKED: subagents may not commit -- EM-only, enforced by caller "
        "identity, no cooperative override. Finish your edits and report to "
        "the EM instead: hand off changed files; the EM runs `git commit`."
    )


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Evaluate the subagent-commit gate against a PreToolUse payload.

    Returns ``None`` (allow) or the nested hard-deny envelope.
    """
    if (payload.get("tool_name") or "") not in MATCHERS:
        return None

    tool_input = payload.get("tool_input") or {}
    cmd = (tool_input.get("command") if isinstance(tool_input, dict) else None) or ""
    if not cmd:
        return None
    cmd = cmd.replace("\r", "")

    # Heredoc BODY lines are stdin data, never executed shell tokens (2026-07-26
    # fix -- see the ``_QUOTE_OPEN_CHARS`` section comment above). Scan the
    # heredoc-stripped form for both the pre-filter and the full matchers;
    # ``cmd`` itself (unstripped) is kept only for the human-facing deny
    # message below, where showing the full original invocation is more
    # useful context and no scanning is performed on it.
    cmd_for_scan = _strip_heredoc_bodies(cmd)

    # 2026-07-29 fix, part 3 (SPACED-WINDOWS-PATH ARGV0, ported for
    # consistency/defense-in-depth -- see this module's docstring entry of
    # the same name for why): quote-and-normalize an unquoted Windows
    # argv0-head path with an embedded-space component
    # (``C:\Program Files\Git\bin\git.exe``) BEFORE either full matcher
    # below runs, so the whole path lands as one token instead of splitting
    # on the space and evading both matchers' argv0-position checks.
    cmd_for_scan = _normalize_windows_argv0_head_path_with_spaces(cmd_for_scan)

    # Cheap pre-filter BEFORE any identity-resolution cost -- the
    # overwhelming majority of subagent Bash calls are not commits at all.
    if not _prefilter_mentions_commit(cmd_for_scan):
        return None

    # The two memos below are WITHIN-ONE-CHECK caches, not cross-call ones,
    # and clearing them here is what makes that true. Six matchers unwrap
    # (and fold) the same command, so the memo is worth having; but a memo
    # that SURVIVES a call would let a test -- or any future monkeypatch of
    # a predicate these functions consult, e.g. the part-14 inertness check
    # the unwrap gates a leg on -- read a stale result computed under the
    # unpatched predicate. Verdict correctness must not depend on cache
    # state, so the cache never outlives the verdict it served.
    _wrapped_shell_c_payload_legs_with_head.cache_clear()
    _fold_python_c_payload.cache_clear()
    # `payload_legs` collects WHICH unwrap leg the matching text came
    # through, for message selection only (`_record_payload_leg`'s own
    # NEGATIVE SPEC) -- the `or` short-circuit, and therefore the verdict,
    # is byte-identical to the collector-free form.
    payload_legs: Set[str] = set()
    if not (
        _has_git_commit(cmd_for_scan, legs=payload_legs)
        or _has_coordinator_safe_commit(cmd_for_scan, legs=payload_legs)
        or _has_committing_op_invoke(cmd_for_scan, legs=payload_legs)
        or _has_reconstructed_commit_identity(cmd_for_scan, legs=payload_legs)
        # Part 16, in the order the two mechanisms are specified: identity
        # over FOLDED text first (a resolved name is a better answer than
        # "unknown"), then the opaque-sink refusal for what did not resolve.
        or _has_folded_commit_identity(cmd_for_scan, legs=payload_legs)
        or _has_opaque_execution_sink(cmd_for_scan, legs=payload_legs)
    ):
        return None

    # No agent_id in payload -> top-level EM Bash call -> allow. This is
    # the ONE non-cooperative signal that grants the allow -- it is
    # harness-supplied, not settable by a subagent. Presence of the RAW
    # field (not whether it canonicalizes) is the EM/subagent
    # discriminator: "is this a subagent at all" cannot fail the way "what
    # KIND of subagent" can, so it is the boolean this guard gates on.
    raw_agent_id = payload.get("agent_id")
    if not raw_agent_id:
        return None

    cwd = payload.get("cwd")
    git_root = resolve_git_root(cwd)
    session_id = payload.get("session_id") or ""
    agent_id = _resolve_subagent_identity(raw_agent_id, session_id)

    agent_type = payload.get("agent_type") or ""
    subagent_type = ""
    if agent_id and git_root:
        subagent_type = _read_backpointer_subagent_type(git_root, agent_id)
    effective_type = agent_type or subagent_type or ""

    if effective_type and effective_type in _ALLOWED_SUBAGENT_TYPES:
        return None

    # C3 (2026-08-03-narrow-subagent-commit-confinement-two-classes.md): the
    # one deliberate allow-path widening in this module. LEG 1 is checked
    # HERE, against `agent_type` alone -- deliberately NOT `effective_type`
    # (which ORs in the disk-read `subagent_type` backpointer leg a subagent
    # can write to itself). See `_git_commit_agent_may_commit`'s docstring
    # for legs 2/3 and the fail-closed landing-order safety net; this branch
    # only ever calls it once LEG 1 already holds, so a NAMED (teammate)
    # dispatch -- where `agent_type` carries the teammate's NAME and the
    # real type resolves only via the forgeable backpointer leg into
    # `subagent_type` -- never reaches it at all (AC19).
    ownership_reason = ""
    if agent_type == _GIT_COMMIT_AGENT_TYPE:
        may_commit, ownership_reason = _git_commit_agent_may_commit(
            cmd_for_scan, git_root, session_id, cwd
        )
        if may_commit:
            return None

    # Everything below this line -- including an unresolvable canonical
    # agent_id (unrecognised shape) or an unresolved effective_type (empty
    # git_root, or a missing/unreadable/malformed backpointer chain) --
    # DENIES rather than fail-open-allows (fixed 2026-07-30). A subagent
    # whose kind we could not determine is still a subagent, and confining
    # it as one is the fail-CLOSED default this CLASS = "hard-deny" guard
    # is supposed to have.
    kind_unresolved = not effective_type

    # Leg precedence, message-selection only: a RESOLVED commit identity is
    # the more specific cause and wins over "an argument did not resolve",
    # whichever collector filled first.
    command_leg = ""
    if _PAYLOAD_LEG_PYTHON_STRING_LITERALS in payload_legs:
        command_leg = _PAYLOAD_LEG_PYTHON_STRING_LITERALS
    elif _PAYLOAD_LEG_PYTHON_OPAQUE_SINK in payload_legs:
        command_leg = _PAYLOAD_LEG_PYTHON_OPAQUE_SINK
    reason = _deny_reason(
        agent_id or raw_agent_id,
        effective_type,
        agent_type,
        cmd,
        ownership_reason,
        command_leg,
    )
    verdict = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    # Measurement only -- emitted AFTER the verdict above is fully computed,
    # and passed that exact value, so the reported disposition is read off
    # what this guard actually returns rather than asserted separately.
    if kind_unresolved:
        emit_kind_resolution_failure_signal("block_subagent_commit", agent_id, git_root, verdict)
    return verdict
