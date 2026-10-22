---
title: "Bash-guard consolidated execution — the confinement band split, the duty-of-care ladder, and the typed alternative record as one plan"
created: "2026-07-29"
author: EM
status: approved
branch: "work/machine-b/2026-07-21to26"
plan_id: pln-bash-guard-consolidated-execution-c51bda
scope_mode: architecture
reviewer: "the Director of Engineering/eng-director (typed-seam plan, standalone primary); EM-as-second-reviewer (merged-shape plan); consolidation review pending"
source: |
  Structural merge of two separately PM-authorized plans:
    - docs/plans/2026-07-29-bash-guard-merged-execution-shape.md
      (pln-bash-guard-merged-execution-shape-a71e05; execution_authorized_sha e66d5ce3a3b43ebedb3c982c227854a5fe72271a)
    - docs/plans/2026-07-29-typed-alternative-record-message-composition-seam.md
      (pln-typed-alternative-record-seam-a41f07; execution_authorized_by PM, 2026-07-29)
  Neither source is superseded as a record; both stay on disk and are cited by section.
predecessor_handoff: |
  state/handoffs/2026-07-29_213341_bash-guard-merged-shape-wave-one-landed.md
  state/handoffs/2026-07-29-typed-alternative-record-message-composition-seam.md
execution_authorized_at: 2026-07-29
execution_authorized_by: PM
execution_authorized_note: |
  Inherited from both sources, which were each separately PM-authorized, plus the PM's
  in-session direction to consolidate the two batons and execute the combined workflow.
  This plan adds no scope beyond § Consolidation delta, which is exhaustive: every
  divergence from a byte-for-byte concatenation of the two sources is enumerated there.
  The one row that was awaiting PM ratification in either source (M14) is STRUCK here, so
  no unratified row executes.
agent_sessions:
  - "2d03ff"
  - "236628"
  - "c51bda"
---

# Bash-guard consolidated execution

Two sessions spent a four-document memo thread negotiating a boundary between them. The PM
handed both batons to one session so the boundary could be dissolved instead of negotiated.
This plan is that dissolution: one owner, one package, one wave order.

The engineering content of the consolidation is **ordering**, not new work. Both source
plans rewrite messages in the same guard modules — one by promoting prose up the rung
ladder, one by replacing prose with a typed record. Run in either order without
coordination, the same message gets authored twice. The thread existed to prevent that
across a session boundary; § Consolidated wave order prevents it inside one plan.

## How this artifact was made

**This is a structural merge, not a rewrite.** It was produced by a transform
(`scratchpad/merge-bash-guard-plans.py`, committed alongside it and re-runnable) that:

1. concatenates the two source task spines — no re-keying was needed, because the `M*` and
   `S*` id namespaces are disjoint (unlike the two plans the `M*` spine itself merged, which
   both used `C1`–`C5` for different things);
2. applies the delta in § Consolidation delta as explicit per-row field edits plus an
   appended `CONSOLIDATION:` note, leaving every original row body byte-for-byte intact;
3. carries each source plan's prose sections through unmodified, dropping exactly two
   (listed in § Consolidation delta with the reason each is resolved).

So every instruction below was written by the session that reviewed it and got it
authorized. Nothing was re-authored from a summary, and nothing was silently dropped: the
sections that did not come across are named, and the rows that changed disposition say so
in their own bodies. Superseded and moot passages are kept in place rather than deleted —
this workstream's own convention, so a later reader does not re-derive a settled question
from the evidence alone.

**Read the row bodies as the work instructions and the `CONSOLIDATION:` notes as the
amendments to them.** Where the two disagree, the note wins; it is younger and it knows
about both plans.

## Consolidation delta

Exhaustive. Every way this artifact differs from a plain concatenation of its two sources
is here.

### Rows whose disposition changed

| Row | Change | Why it changed |
|-----|--------|----------------|
| `M14` | **STRUCK** | Its rationale was preventing two concurrent sessions colliding in `dispatch_checks.py`. One session now owns both batons, so the collision is unreachable. This also retires the only row in either plan that was still awaiting PM ratification — it is cut, not ratified. |
| `M14S` | **NEW** | The one piece of `M14` that was not collision-motivated: delete `M1`'s deferred-import shim and repoint its ~40 importers. Named in the merged plan's own § Findings recorded but not fixed; would have been lost with `M14`. |
| `M15` | **un-deferred, then ABSORBED INTO `S4`** | Its named deferral reason was that `S1` sat behind a PM authorization neither session controlled. The typed-seam plan holds that authorization, so the named gate has cleared. `S4` already migrates the package; `M15` is the same edit on the modules `S4` had excluded as peer-owned. |
| `S4` | **scope amended** to include `guard_grep_via_bash.py`, `guard_multiprobe_banner.py`, `guard_plumbing_and_loops.py` | Excluded from `S4` on ownership grounds only. Not an aggregate increase — `M15` already carried them under the merged plan's authorized scope. |
| `S3C` → `S3B` | **new ordering constraint** | `S3C` captures the rendered baseline; `S3B` is an 18-site refactor claiming rendered output is unchanged. Capturing first turns that claim into a differential proof, which is this workstream's own ratified bar. Neither plan could sequence the other's row. |

### Schema violations fixed in passing

Both are break-class defects found while merging, in the source merged-shape plan's own
spine, and both are **also corrected at source** — a cut row that no tool can read as cut
will resurface as work.

| Row | Defect | Fix |
|-----|--------|-----|
| `M11` | `disposition: struck` is not a member of the plan-tasks disposition enum (`open`/`coded`/`spun_off`/`backlogged`/`wont_do`), so the row's resolution state routed nowhere on harvest. Its `disposition_ref` was *also* schema-forbidden on a declined row, *and* a dead path — the thread it named was archived to `archive/memos/`. | `disposition: wont_do`; `disposition_ref` dropped with the live archive location folded into `disposition_detail`. The strike itself is unchanged. |
| `M12` | Cut by PM ruling, and its body says so — but it carried `deferred: true` with **no `disposition` at all**, so the deferred-harvest read a declined row as future work still pending. | `disposition: wont_do` plus the required `disposition_detail`, matching what the body already said. |

### Obligations discharged structurally rather than performed

Each of these was a real commitment between two sessions. None is being dropped on
convenience; each one's *mechanism* was a cross-session signal, and there is no longer a
second session for the signal to reach.

| Obligation | Was | Now |
|-----------|-----|-----|
| `M10`'s settle note | a one-line note published to the reconciliation thread when `M10` lands, from which the peer infers it is safe to capture | a wave boundary: `S3C` runs after `M10`. Enforced by § Consolidated wave order. **Do not send a note; its absence is not an outstanding obligation.** |
| `S1`'s signature publication | time-critical — publish before the peer's wave 3 dispatches or the window closes | void. `S1` still lands early, to pin the field set `S4` consumes, not to hit a window. |
| `S5` condition (a)'s named test | the peer signals its half with a named test scoped to its ownership | one named test over every guard in the package. **The named-test requirement stands** — a test, not a report, so the signal cannot rot. |
| `S3C`'s per-band staged capture | capture this band before conversion, the peer's band after their `M10` settles | same ordering, one population. Capture after `M10`, before `S4`. |
| `S4`'s cross-band ask | a concretely specified first-wave ask the peer could accept, decline, or counter-propose | nothing is asked. The "what if the peer declines" open dependency is closed. |
| the Director of Engineering's jurisdiction ruling — do not use the claude-klabauter grant to convert the peer's guards | authority existed, jurisdiction did not | **satisfied, not overridden.** The ruling was against splitting one guard's ownership down the middle; end-to-end by-guard ownership includes the message. One owner ⇒ no split. The grant remains the authority these commits land under, as in waves 1–2. |
| `commit_tripwires.py` function-list split; `dispatch_checks.py` extraction-as-resolution | mixed-file ownership agreements | moot. Both files convert whole. |

### Sections not carried across

| Section | From | Why |
|---------|------|-----|
| `## Authorization state — the one thing that blocks` | merged-execution-shape | Resolved. It blocked on a PM stamp; the stamp landed (`execution_authorized_sha: e66d5ce3a3b43ebedb3c982c227854a5fe72271a`, `execution_authorized_by: PM`) and waves 1-2 executed under it. A section describing a cleared gate as blocking would misreport this plan's state. |
| `## Open for the PM — one stamp, three questions inside it` | merged-execution-shape | All three answered, so the section is dropped rather than carried as open. (1) The merged scope was authorized. (2) F9's widening was assented to — M1 landed with BOTH extractions (`061134b1`), which is the widened form. (3) F5 and F6 were accepted: the audience split routed to the typed seam (now S2, in this plan), and the rung ratchet was cut (M12). Only the three questions are dropped; the section's task spine is carried in full, and its anti-scope bullets live in § Carried unchanged from both plans, which is carried whole. |

### Recorded, deliberately not acted on

- **`M4`'s residue** (`_STAGE_BUILDERS`, ~0.6–0.9% of grep-shaped traffic). `M10` declined it
  partly because it was the peer's surface — that ground is gone — but its other ground
  stands: `M4` returned verdict **(c)**, the composed-pipeline row is strictly dominated.
  Available and not taken. No new row.
- **`security-audit-worker`'s unenforced read-only-Bash claim** (carried forward,
  `cf-security-audit-worker-policy-c41d08`). It claims read-only Bash but is absent from the
  `bash_policy` table, so nothing enforces the claim. Both sources call this a PM call and
  out of scope, and it stays out of scope here. Noting the disagreement rather than burying
  it: the fix is mechanical — one table row mirroring `code-reviewer`'s — and an unenforced
  safety claim is break-class. It is left alone because narrowing an agent's actual Bash
  reach is a policy change with its own blast radius, which is a genuine direction-class
  tradeoff, not because it is hard.
- **Windows validation** (`cf-windows-validation-9a3e17`). PM-deferred, blocked on a Windows
  host, until after this plan succeeds. Every measurement in this workstream is
  macOS-observed and the cross-platform branching is reasoned but unverified. **Deferred, not
  cancelled — must never be reported as covered.**
- **The reviewer pipe-denial leg** (`cf-reviewer-pipe-denial-7b2f04`). `git show | sed` denying
  is correct; `git show | head` denying is not explained by the message text and could not be
  settled empirically, because `check()` fail-opens on an unresolvable agent id. Unresolved.

## What the two sessions were coordinating, and what dissolved

Worth stating plainly, because the memo thread is archived and a later reader will find six
citations into it: **both sides were right, and the coordination was necessary while the
boundary existed.** The thread found real defects in both plans' in-flight work — it is not
an example of wasted process. What it could not do is make the boundary cheap.

The boundary was the PM-ratified by-guard band cut: each session owned its guards end to
end, message included. Under that cut, a type that every guard must construct is
inherently a joint surface, so the thread had to negotiate a type signature, a conversion
schedule, a baseline-capture point, a deletion condition, and a settlement signal — five
agreements, each with a rot risk, to land one dataclass.

Consolidation replaces all five with wave ordering. That is the entire win, and it is
large: the surviving constraint is one that a single owner can simply *schedule*.

**What did NOT dissolve** — do not read consolidation as a licence here:

- The **three-band chain structure** (`M1`) stands. Bands are how the chain orders its
  guards, unrelated to who owns them.
- The **band cut as doctrine** stands. It is ratified, it is in the threat-model wiki, and
  `S6` amends that wiki. Consolidation is one session holding both sides today, not a repeal.
- The **differential-render obligation** stands and gets stricter (`S3C` now also proves
  `S3B`). It never depended on there being two parties.
- The **named-test completion signal** stands. Its point was that a test cannot rot the way
  a report can — true regardless of audience.

## Consolidated wave order

Supersedes the four-wave shape in § The wave shape (below, kept as record). Two reasons for
a new order: `M*` rows and `S*` rows touch the same modules and neither plan could sequence
the other's; and waves 1–2 have already landed, so the remaining graph is different.

**The governing constraint, stated once:** every row that rewrites message *prose* (`M7`,
`M10`) must land before `S3C` captures the rendered baseline, and `S3C` must land before
`S4` converts anything. Violating this authors the same message twice — the exact failure
the memo thread existed to prevent.

**Wave width is capped at two lanes.** Not an efficiency choice: six of seven agents died on
transient API errors on the first wide attempt, and one died mid-extraction leaving the
guard package unimportable for every session on the machine. Two-lane waves have been
reliable since. Fleet-critical rows run solo.

| Wave | Lanes | Rows | Files | Why these together |
|------|-------|------|-------|--------------------|
| **A** | 2 | `M5`, `M7` | `dispatch_checks.py` · `guard_head_tail_rewrite.py` | Disjoint only because `M1` extracted `M7`'s guard into its own file. Do not re-serialize. |
| **B** | 2 | `M6`, `S0` | `guard_offer_git_c.py` · `_alternative_liveness.py` + new capability manifest | Disjoint. `S0` starts early because `S1` blocks on it. |
| **C** | 1 | `M8` | 5 wrapper-walk copies + `block_subagent_destructive_action.py` | **Solo — fleet-critical.** Deletes shared symbols across five modules and re-applies the captured ladder-tail diff. Enumerate the full importer set before touching anything. |
| **D** | 1 | `M14S` | `dispatch_checks.py` | **Solo — fleet-critical.** Moves shared symbols; same file as `M8`, so it follows rather than parallels. |
| **E** | 2 | `M10`, `S1` | 3 advisory guard modules · new `coordinator_core/guard_messages/` | Disjoint: `S1` creates a new package. **This is the last prose-promotion row** — after `M10`, message text stops moving. |
| **F** | 2 | `S2`, `S3` | `guard_messages/` + dispatcher · `_alternative_liveness.py` | `S2` needs `S1`'s record to exist (audience is a field, never a filter over rendered text). |
| **G** | 1 | `S3C` | `LIVE_TRIGGERS` + new baseline fixture | **Solo.** Extends the trigger corpus from ~25 to ~94 branches and captures the rendered baseline. Key on **chain-entry name**, never module path — `M1` and `M8` both move message-bearing functions between modules, and a path-keyed corpus goes silently *missing* rather than failing loudly. |
| **H** | 1 | `S3B` | 18 envelope sites + dispatcher + `write_guards/INTERFACE.md` | **Solo — package-wide.** Narrows `check()` to `AlternativeRecord \| None`. Prove rendered output byte-identical against wave G's baseline. |
| **I** | 2/wave | `S4` (absorbing `M15`) | ~94 sites, fanned out by disjoint file | The big one. Per-file executor dispatch, EM-serial commits, differential render green after each. Real risk is a silent downgrade of the 33 messages already at rung A or B. |
| **J** | 1 | `S5` | rung computation + prose-extractor deletion | Needs `S4` complete for the population being graded. Delete the extractor; do not retain it behind a flag. |
| **K** | 2 | `S6`, `M13` | threat-model wiki · combined diff | `M13` is the closing gate: **no `code-reviewer` pass has run on any of this workstream's diff, on either side of the merge.** One pass over the combined diff. Do not let these commits read as reviewed. |

### Gate that binds every wave, restated because it has already bitten

**Verify a restructure differentially, never by running it.** `M1`'s "pure move" claim was
checked by confirming all 28 chain entries were byte-identical in order against `HEAD` —
that is the bar. Two rows in the source workstreams shipped a correction because someone ran
new code and liked the output.

**Brief every symbol move with its full importer set, enumerated before dispatch.** This is
the single most expensive lesson of the workstream and it wedged the whole fleet once. It
binds `M8` and `M14S` specifically. Read
`state/lessons/2026-07-29-a-dispatch-onto-a-live-shared-surface-needs-the-whole-importer-set.yaml`
before dispatching either.

**Promotion means adding a working alternative.** It never means making the override line
clearer or more prominent. Binds `M10`, and the reason is empirical: a read-only Explore
subagent with no knowledge of any plan spontaneously reported this package's own advisories
as prompt injection and discarded them.

**Baseline you inherit:** guard suite green at 2107 passed, 12 xfailed, 0 failed;
alternative-liveness gate 67/67. Any failure you see is yours.

---

*Everything from here down is carried byte-for-byte from the two source plans. Section
headings are prefixed with their origin.*

## [typed-seam] Problem, oracle, and acceptance criteria

## Problem

A bash guard refuses or reshapes a command on an agent's behalf, and every refusal is a
caller-facing message. Those 94 messages were graded on a duty-of-care ladder: **A** hands over a
ready-to-run alternative, **B** an offer needing adaptation, **C** describes the problem and
offers only the `COORDINATOR_ALLOW_*` escape hatch, **D** refuses bare. The inventory found
A=6, B=27, C=45, D=16.

The defect is structural, not editorial: **in every one of the 45 rung-C messages, the override
line is the only copy-pasteable text.** The single easiest action the guard package offers an
agent is turning the guard off. Everything else it might do, it must re-derive.

Two attempts to fix this by inspection have now failed, and their failure is the argument for
this plan:

- **A liveness gate** — assert every named alternative still works — cannot fail a single rung-C
  or rung-D row. A message whose only actionable text is `export COORDINATOR_ALLOW_X=1` is
  *maximally live* the moment the guard genuinely reads that variable. Liveness and rung are
  orthogonal.
- **A rung-floor gate with a pinned per-guard baseline** cannot either. Pinning today's grade
  grandfathers all 61 known-bad rows, so on the day it lands it fails zero of them. It prevents
  regression; it does not produce improvement.

Both were killed by review for the same reason in different costumes: *the test of a gate is not
what it asserts, it is which currently-shipping rows it fails on the day it lands.* A gate reads
prose that has already been written. It complains afterwards. It cannot stop the prose being
written in the first place.

### Five findings that make the prose itself the problem

**The extraction is guesswork, permanently.** Both gates work by parsing our own emitted English
to discover what alternative a message named. That parser has been tuned once already; backticked
shape names and Python API references read as candidate executables. Every future message shape is
a new parsing risk, and the parser can never be more than a heuristic over free text we control.

**A false capability claim shipped inside the guard package itself.** `guard_grep_via_bash`
asserted to every caller that "there is no harness-native (zero-fork) search from this session"
long after `guard_inprocess_search` landed and began answering greps in-process. It was found by a
reviewer whose own greps were being answered in-process while it read the message saying they
could not be. Prose asserts capabilities; nothing binds the assertion to the capability.

**The offer is frequently impossible for the caller receiving it.** `coordinator:code-reviewer` is
granted exactly `ls, cat, head, tail, wc, find, file, stat, grep` plus read-only git. **`env` is
absent. `export` is absent.** The confining guard ships no escape hatch of its own. So *no*
override in the package is settable by that class — yet the liveness probe grades all of them
LIVE, because it sets the variable inside the gate's own process where no sandbox policy applies.
Rung is being treated as a property of a message when it is a property of a **(message, caller
class)** pair.

**Free-prose guard messages read as injected content to their own intended reader.** <!-- band
enumeration (2026-07-29): observed, not derived. --> A dispatched read-only subagent, working
this codebase with no knowledge of this plan, independently reported that several Bash tool
outputs "contained injected 'advisory'/'reminder' text (styled as coordinator-hook system
messages) urging me to set override environment variables or change search behavior. These did
not come from you or from the dispatching EM — I treated them as untrusted content embedded in
file/command output, took no action on them." The subagent had no way to distinguish a genuine
guard advisory from a prompt-injection attempt, because both arrive as free English text in tool
output with no structural marker separating them. Stated factually, not as an overclaimed
security finding: the point is that the message shape is indistinguishable from injection to its
own intended reader, whatever else is true of it. It also independently corroborates the PM
ruling below — that subagent both could not use the override it was shown and correctly
distrusted it.

**The unconfined audience the override line is legitimately addressed to does not use it
either.** <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
reconciliation-close.md § 5), 2026-07-29, attributed to the peer session's own session-log
self-report rather than an instrumented measurement. --> The peer session in the reconciliation
thread is the opposite audience from the subagent finding above: an unconfined EM, the one
reader the override is legitimately addressed to under the PM's EM-vs-IC ruling below. Across
that session it received on the order of dozens of guard advisories ending in a `To bypass:
export COORDINATOR_ALLOW_*` / `COORDINATOR_OVERRIDE_*` offer, and acted on zero of them — not
from distrust, since it was the authorized audience and could have used any, but because the
bypass was never the right move in a single case. Twice the advisory itself changed its
behaviour (it stopped chaining `cd` into git commands; it reached for the in-process search
path); the override was never the answer. So the override line has a weak claim on either
audience: unusable and injection-shaped to the confined caller, inert to the unconfined one.
**This is recorded as evidence for this plan's record shape; it does not change the design.**
Whether the EM rendering should carry the advisory without the override switch revisits the PM
ruling below and is an open question reserved for the PM — not decided here, and S2's design,
TA5, and its acceptance criterion are unchanged by it.

### The PM ruling that reframes the ladder

**"Set a variable to turn me off" is an EM call, not an IC call** (PM, 2026-07-29). A dispatched
subagent should never be handed the off-switch for a safety check it just tripped. The EM still
sees it.

This is not a politeness rule; it is a reading of who the caller actually is. The caller is a
capable, eager, amnesiac agent optimising to complete its task — not an adversary, and not a
careless operator. Handing that caller a sanctioned-looking bypass is the path of least resistance
pointed at the wrong exit. Removing it is worth more than promoting a dozen messages one grade,
because it changes what the easiest available action IS.

The ruling also dissolves the reachability defect for the population it most affected: stop
advertising to confined subagents an override they could never type.

### What this plan builds

Guards stop returning free prose and return a **typed alternative record**. A bare-override
message then has no argument slot to exist in — the rung-C shape becomes *unwritable* rather than
detectable. Rung is computed from the record, not parsed from a sentence, which retires the whole
extraction false-positive class at the root instead of tuning it. And because the record is
structured, the composer can render it differently for the EM and for a subagent from one place,
rather than at 94 call sites.

This is the repo's discharge test applied to our own package: *for every rule, what artifact
discharges it? If the answer is "the author remembers," the work is not finished.* Today the rule
is "don't write a message whose only offer is the override," and the discharging artifact is the
author's memory. After this plan, it is the type.


## Oracle — the found facts this plan's slate must cover

> Read these as claims against a moving tree. The predecessor plan had nine of nineteen oracle
> rows go stale or invert within a day. Re-verify at source before building on a row.

| # | Found fact | Source |
|---|---|---|
| T1 | 45 rung-C messages where the override line is the only copy-pasteable text | `state/audits/2026-07-29-bash-guard-caller-text-inventory.md` |
| T2 | 16 rung-D messages offering nothing at all | same |
| T3 | A liveness gate cannot fail any C or D row — liveness and rung are orthogonal | the Director of Engineering review F1 |
<!-- Review: coordinator:plan-coverage-checker — T3 covered-by-design (no explicit slate citation): the design choice it motivates (typed record over gate-parsing) is the plan's whole premise, stated in ## Problem and discharged structurally by S1/S5. Cited here so a later pass does not read it as missed. -->
| T4 | A baseline-pinned rung gate fails zero of the 61 rows on the day it lands | the Staff Engineer review F0 |
<!-- Review: coordinator:plan-coverage-checker — T4 covered-by-design (no explicit slate citation): same background-rationale shape as T3 — the reason gate-based approaches were abandoned, discharged by the seam itself (S1/S5) rather than named in a task row. -->
| T5 | The rung criterion `{override}` cannot fail a rung-D message: an empty alternative set is not `{override}`, and the gate module's negative-spec forbids failing empty-set messages | the Staff Engineer review F1 |
<!-- Review: coordinator:plan-coverage-checker — T5 covered-by-design (no explicit slate citation): TA1's "no bare-string refusal envelope" and TA5's "never empty — carries an escalation route" jointly discharge the rung-D failure mode this row names. -->
| T6 | Gate population is guards (~25 triggers, 33 alternatives); ladder population is 94 messages. A per-guard grade samples ~1/4 of the population and is silent on the rest | the Staff Engineer review F7 |
| T7 | Prose extraction produced a false-DEAD class once already (backticked shape names, Python API references read as executables) | the Director of Engineering review F4 |
| T8 | A false harness-capability claim shipped live in `guard_grep_via_bash` while the gate reported green over it | the Staff Engineer review F3; fixed in claude-klabauter `6f3ae246` |
| T9 | `env` and `export` are both absent from `coordinator:code-reviewer`'s allowlist; the confining guard ships no hatch — no override in the package is typeable by that class | the Staff Engineer review F12, verified against `coordinator/subagent-sandbox-policy.yaml` |
| T10 | `probe_override` sets the variable in the gate's own process, so it certifies readability and never reachability | verified at `_alternative_liveness.py` `probe_override` |
| T11 | Rung is a property of a (message, caller class) pair, not of a message | the Staff Engineer review F13 |
| T12 | `guard_inprocess_search` returns a DENY as its SUCCESS shape ("already handled"), so any rung logic keying on verdict class grades the best-behaved guard as a hard refusal | the Staff Engineer review, edge cases |
| T13 | The effective caller-class type is resolved via the same OR-resolver `block_reviewer_bash_outside_allowlist.check()` uses — `agent_type = payload.get('agent_type') or ''` OR'd with `subagent_type` from the backpointer resolver, not the raw `agent_type` payload field alone; `subagent-sandbox-policy.yaml` is DoE-owned and the oracle needs no peer coordination | the Staff Engineer follow-up |
<!-- Review: the Director of Engineering (eng-director) — F10 (minor): T13's original wording ("the payload already carries agent_type... needs no peer coordination") understates the mechanism. `block_reviewer_bash_outside_allowlist.check()` ORs TWO sources — `agent_type = payload.get('agent_type') or ''` (verified 2026-07-29 at line 1525) and `subagent_type = _read_backpointer_subagent_type(...)` (line 1528) — then resolves confinement as `_is_confined_type(agent_type, policy) or _is_confined_type(subagent_type, policy)` (line 1544) and picks `effective_type` from whichever matched (line 1552). A resolver keyed on the raw payload field alone under-resolves exactly the confined-subagent population TA6/TA5 exist for. S2/S3 must specify the effective type via this same OR-resolver (or a shared helper extracted from it), not `payload['agent_type']` directly. -->


## Acceptance Criteria

| ID | Criterion | Test | Binding-Class | Status |
|----|-----------|------|---------------|--------|
| TA1 | A guard cannot emit a caller-facing refusal except by constructing an alternative record; free-string emission is removed from the guard-facing API, not merely discouraged | no guard module constructs a refusal envelope from a bare string; asserted by introspection over the package | mechanical | pending realization |
<!-- prior-art-check (2026-07-29): TA1/TA8's completeness argument — whole-package introspection proving no unconverted site remains, not a per-site review — is backed by `state/lessons/2026-07-29-closing-one-shape-of-a-bypass-is-not-closing-the-class.yaml` (universal scope): "Closing one shape of a bypass reads as closing the class — enumerate the class, then attack every member... unless N was enumerated." -->
<!-- Review: the Director of Engineering (eng-director) — F4 (major, cited again at TA8 below): the same lesson class applies to the named-exception-set problem — an uncapped exception list is the allowlist-degeneration shape that lesson family names. -->
<!-- Review: the Director of Engineering (eng-director) — F1 (critical): the guard-facing `check()` contract returns the harness's own nested envelope (`{"hookSpecificOutput": {...}}`), with the caller text a free string. So long as `check()` returns a dict, a guard can hand-build the envelope directly and bypass the record; "asserted by introspection over the package" is a heuristic over our own Python, the same class of artifact as the prose extractor TA3 deletes. Verified 2026-07-29 at claude-klabauter `coordinator_core/bash_guards/block_worktree_creation.py:437-443` (envelope literal unchanged at that line range) and `coordinator_core/write_guards/INTERFACE.md:34` (fidelity rule 2, "Preserve every permissionDecisionReason / advisory string BYTE-FOR-BYTE" — still present, still unamended, and it directly contradicts TA1's premise unless narrowed in the same change). -->
<!-- Review: the Director of Engineering (eng-director) — F3 (major): "type-level" overclaims — Python's type system cannot express this invariant, it is a runtime check on the construction path. Restated as a construction-time invariant, with an explicit statement of what it does and does not catch (bare override vs. degenerate companion). See also S1's body for the corresponding "load-bearing row" correction. -->
| TA2 | The record has no field in which a bare override can be the sole actionable content — an override is a distinct, optional, EM-audience field that cannot stand as the primary alternative | construction-time invariant, not type-level: nothing in Python's type system expresses "this field may not be the only one populated"; a frozen dataclass raises in `__post_init__` when `override` is populated and no other actionable member is, on every construction unconditionally, with a test asserting the failure. This catches the BARE-override shape; it does not catch a degenerate companion (e.g. boilerplate stuffed into `escalation` alongside the override) — that shape is caught by TA9's differential render, not by this invariant. | mechanical | pending realization |
| TA3 | Rung is computed from the record, never parsed from rendered text; the prose extractor is deleted, not retained as a fallback | no extraction path remains in the rung computation; asserted by absence plus a test that a record with adversarial prose in its free-text fields still grades on structure | mechanical | pending realization |
| TA4 | Rung is recorded against a (message, caller class) pair; a single-keyed grade fails the schema | schema test over the rung inventory | mechanical | pending realization |
| TA5 | A subagent-audience rendering never contains an override/disable set-instruction as actionable content, and is never empty — it carries an escalation route naming what to report upward | per-guard rendering test over both audiences | mechanical | pending realization |
| TA6 | An alternative is graded reachable only if expressible under the receiving caller class's `bash_policy` entry; `coordinator:code-reviewer` receiving an `env`-prefixed alternative fails | reachability probe resolving the policy table against the OR-resolved effective caller class (T13), with the verified `env`/`export` absence as a fixture | mechanical | pending realization |
| TA7 | A capability claim in a record is a reference to a capability oracle, not a sentence — a claim naming a capability that the oracle does not report fails at construction | test planting the exact `guard_grep_via_bash` false-capability defect and asserting it cannot be constructed | mechanical | pending realization |
<!-- Review: the Director of Engineering (eng-director) — F6 (major): TA7 as written depends on a capability oracle that
did not exist at review time -- `probe_harness_capability` (claude-klabauter
_alternative_liveness.py:979) returns UNVERIFIABLE unconditionally by deliberate design, and the
structured oracle TA7 needs was filed as an ask in
state/audits/2026-07-29-rewrite-path-api-asks.md rather than built. The Director of Engineering's recommendation was to
narrow TA7 to a hand-maintained registry-key reference instead of building the oracle. PM decision
(2026-07-29, follow-up pass): build the oracle in scope rather than narrow the criterion -- see new
row S0. TA7's text is therefore left exactly as written; it is correct-as-stated once S0 lands, not
aspirational. -->
<!-- Review: the Director of Engineering (eng-director) — F4 (major): a named exception set with no cap, no shrink schedule, and no written-reason schema is the per-file-allowlist shape `state/lessons/2026-07-22-a-gate-with-a-per-file-allowlist-enforce-534d431a970b.yaml` (universal scope) prohibits — "any per-file exemption list degenerates into 'whatever exists today'... hands a future agent the exact affordance the gate exists to deny." Same lesson the Staff Engineer turned on C8's baseline. Fix: exact-set equality test (not subset), a pinned maximum count that may only decrease, and a required written reason per member naming a physics/impossibility constraint, not an appetite. If the intended count is zero, pin it at zero. -->
| TA8 | Migration is complete and verifiable: every one of the ~94 message sites is converted, with any deliberate exception in an EXACT-SET-EQUALITY-tested named set, count pinned and monotonically non-increasing (0 unless a physics/impossibility reason is argued and written per member) | introspection asserting no unconverted site remains outside the named set; the named-set test is an equality check against the pinned count, not a subset check | mechanical | pending realization |
<!-- Review: the Director of Engineering (eng-director) — F5 (major, highest-value amendment on the list): TA9 as specified is a wish — it requires "old text vs new rendering, per site" for ~94 sites, but a guard message cannot be rendered without firing the branch that emits it, and the package's only trigger corpus (LIVE_TRIGGERS, ~25 triggers) reaches roughly a quarter of the 94 branches. The fix for that population gap (one trigger per known message branch, the Staff Engineer F7) was scoped to the now-retired C8, and its prerequisite is orphaned by that retirement. Re-homing it here (new row before S4, see Tasks) plus a committed rendered-baseline fixture converts TA9 from a judgment row (human-or-reviewer confirmed — "the reviewer remembers", the north star's named failure state) into a mechanical one: a test that renders every branch post-conversion and diffs against the fixture. Backed by `state/lessons/2026-07-29-verify-a-rewrite-differentially-not-by-running-it.yaml` (universal scope): "Test a rewrite by executing BOTH sides against the same fixtures and diffing the observable output byte-for-byte." -->
| TA9 | Rendered output for every converted site is reviewed against its pre-migration text, so the conversion does not silently downgrade a message that was already rung-A or rung-B | differential render: old text vs new rendering, per site, against a committed pre-migration baseline fixture built from a per-branch trigger corpus (see task row before S4) and captured PER BAND, immediately before that band's own conversion, not once globally up front — a global baseline mixes a peer band's in-flight promotion diffs into this criterion's conversion-regression signal and destroys it (reconciliation reply § 4, 2026-07-29; see S3C, S5); the corpus is keyed on chain-entry name, never module path or qualified function name, because a corpus that silently loses rows to a rename reports green over branches it can no longer find (reconciliation close § 3, 2026-07-29; see S3C); rung-A/rung-B sites require an explicit reviewed acknowledgement to change | mechanical | pending realization |
| TA10 | No cell in the 304-cell confinement attack corpus changes state | `test_confinement_attack_corpus.py` | mechanical | pending realization |
| TA11 | A guard whose success shape is a DENY (`guard_inprocess_search`) is graded on record content, not verdict class, and does not appear as a refusal in any rung report | test over that guard specifically | mechanical | pending realization |


## Anti-scope

- **Do not keep the prose extractor as a fallback.** A structured path plus a prose fallback is
  the worst of both: the fallback becomes the path of least resistance for the next author, and
  the false-positive class this plan exists to retire survives inside it. If a site cannot be
  converted, it goes in the named exception set with a reason — not down a parsing path.
- **Do not let the record become a string with extra steps.** A single free-text field that
  callers stuff a whole message into reproduces today's problem behind a type. Actionable content
  is structured; free text is for context only and must not be where the alternative lives.
- **Never edit the shared tokenizer, the env/wrapper peeler, or `_shape_classifier.py`**, and
  never hand-roll a parallel command parser. This package found five quote-blind matchers in a
  single day; an N+1th is the exact defect class this work removes.
- **Do not widen a guard's verdict while converting it.** Migration changes how a message is
  constructed and rendered, not when a guard fires or what it decides. A verdict change riding
  along inside a 94-site migration is unreviewable.
- **Do not report Windows as covered.** Every measurement in this workstream is macOS-observed;
  cross-platform branching is reasoned and unverified. Deferred by PM direction — deferred, not
  cancelled.
- **Do not treat the audience split as cosmetic rendering.** If the subagent rendering is produced
  by string-filtering the EM rendering, a future message shape leaks the override through. The
  audience is a property of the field, decided at construction.


## Out of scope

- **Promoting individual messages up the ladder.** That is the predecessor plan's `D2`, running in
  parallel. This plan changes the medium; that one improves the content. They meet where a
  promotion lands on top of a converted site rather than under it.
- **The liveness gate.** It survives this plan unchanged — a typed record still names something
  that can be dead, and only execution can tell. It is not retired here.
- **The rung ratchet** (predecessor `C8`). Its retirement condition is this plan landing; its
  baselines are expected to be discarded rather than migrated.
- **Bypass hardening and the confinement band's guards.** <!-- band enumeration (2026-07-29):
  the `block_*` prefix is NOT the band criterion — `block_illegal_filename.py` is
  `CLASS = "advisory"` (its own docstring: "Posture: ADVISORY-ONLY — this arm NEVER blocks")
  and carries the prefix while being advisory, not confinement; conversely
  `check_raw_pid_liveness.py`, `check_test_suite_invocation.py`, and (see S4's body) three
  `guard_*`-named hard-deny modules are confinement without the prefix. The actual criterion is
  the wiki's own, per `coordinator/docs/wiki/bash-guard-threat-model.md` § Two bands: "Confinement
  guards answer *may this run at all*; advisory/rewrite guards answer *is there a cheaper way to
  run it*" — mechanically, `fail_closed=True` in `dispatch.py`'s `guard_chain`. Naming the prefix
  as the boundary would mis-scope it in both directions; stated here so a later reader does not
  re-derive the same mistake. -->
- **Tool grants and agent policy**, except read-only consumption of the `bash_policy` table for
  reachability.


## [merged-shape] Reconcile, merge findings, gates, and the superseded wave shape

## Reconcile — what changed under both plans while they waited

Verified against disk and git at authoring time, not carried from either handoff.

- **The claude-klabauter working tree is clean across `coordinator_core/bash_guards/`.** Both
  handoffs park scope around a peer's uncommitted work in `dispatch_checks.py`,
  `guard_multiprobe_banner.py`, and `_alternative_liveness.py`. That work has landed. The
  "parked pending transfer" reason is spent.
- **DoC-C5 and DoC-C6 are landed and committed** (`9760fd213`, `1959a8266`, `232be2ee1`). Both
  read as open in the duty-of-care handoff's row dispositions.
- **DoC-C1 and DoC-C7 are landed** — the liveness gate exists at
  `coordinator_core/bash_guards/_alternative_liveness.py` and its suite runs 67 passed, 0 failed.
- **DoC-C3 (`e74f7417`) and DoC-C4 (`e733fa0d`) are landed**, as their own bodies state.

**Both of the confinement plan's blocking coordination asks are therefore already answered on
disk, and the plan's own § Cross-plan coordination is stale in saying they are not:**

1. *"What property does the liveness gate assert, and what surface does it read?"* — It enumerates
   every guard in the package, classifies each named alternative
   (`command`/`executable`/`flag`/`harness_capability`/`override`), and probes liveness **by
   execution**, with an explicit named `UNTRIGGERED` set carrying written reasons and a count
   ceiling. Surface: `_alternative_liveness.py`; oracle:
   `tests/test_alternative_liveness_gate.py`. It is green. CBS-C1's band model and CBS-C3's
   wrapper table must come out the far side of it green — that obligation is now executable,
   because the gate is readable.
2. *"Which two confinement modules are transferring, by name?"* —
   **`block_reviewer_bash_outside_allowlist.py`** (three D-rung rows) and
   **`block_subagent_destructive_action.py`** (the git-mv row, C→B). Named in the frontmatter of
   `state/audits/2026-07-29-ladder-tail-confinement-rows-handover.md`.

CBS-C3 and CBS-C5 were held on those two asks. They are unblocked.


## Merge findings

Findings from reviewing the two plans against each other. F1, F2, F3 and F7 change what executes;
F4 is discharged above; F5 and F6 are scheduling calls that need a PM answer inside the same stamp.

**F1 — `DoC-C9` and `CBS-C1` are the same edit and must be one chunk.** DoC-C9 extracts
`check_offer_git_c` out of `dispatch_checks.py` and re-registers it in `dispatch.py`. CBS-C1
rewrites `dispatch.py`'s entire `guard_chain` into three bands. Run as separate chunks they
rewrite the same registration block twice, and — worse — each row instructs its executor to
"coordinate rather than land unilaterally" with a peer session that, post-merge, does not exist;
an executor reading either row in isolation will stall waiting for a counterparty. Merged as
**M1**. The merge also makes CBS's own AC-1 cheaper: with `check_offer_git_c` in its own module,
band membership is answerable by reading the file, which is exactly the O19 defect DoC-C9 exists
to close.

**F2 — `CBS-C5` is serialized behind `CBS-C3` in the unmerged shape and parallel to it in the
merged one. This is the merge's headline wallclock win.** CBS-C5 wires `find_git_segment` into
`check_offer_git_c`; CBS-C3 rewrites the seven cohort-A guards. Today both live in
`dispatch_checks.py`, so they cannot run concurrently in a shared tree. After M1's extraction they
are different files and run as separate lanes in the same wave. Ordering the extraction first
converts a serialization into a fan-out at zero extra cost.

**F3 — `DoC-C5`'s revert already happened, and the merged shape must schedule the *restore*, not
the handover.** DoC-C5 captured finished, tested promotions to the two confinement modules as a
diff and then reverted them (`git checkout --` on four paths) so a peer would inherit a clean
tree. The audit says plainly: *"None of it is in claude-klabauter's working tree anymore. This
document is the only surviving record."* Under one execution shape there is no peer and no tree to
keep clean — that revert was pure waste, and leaving the row as written strands working code in a
markdown fence. **The merged shape re-applies the captured diff** (row M9, folded into M8). Cheap:
the diff is complete and was verified live before reverting. Do not re-derive it.

**F4 — Both confinement coordination asks are discharged.** See § Reconcile. No action beyond
recording it.

**F5 — `DoC-C12` (audience split) and the spun-off typed seam are the same mechanism, and
building C12 standalone duplicates a peer's in-flight work.** C12's own body concedes this: *"an
alternative record carrying an audience field lets the composer decide EM-vs-subagent rendering in
one place instead of at 94 call sites… fold it into the seam's record shape when it is [ready]."*
The typed seam has its own plan
(`docs/plans/2026-07-29-typed-alternative-record-message-composition-seam.md`) and was claimed by
a peer session within a minute of being spun off. Building C12 as an independent 94-site sweep is
simultaneously the largest avoidable token cost in this plan and its largest merge-conflict
generator — a broad-shallow sweep across files every other lane is editing. **Recommendation: send
the audience field to the seam as a coordination ask, and keep the standalone sweep as a
dated fallback.** This is a PM call because it accepts schedule risk on a peer session; it is
posed as one in § Open for the PM.

**F6 — `DoC-C8`'s retirement condition is already in flight, so it may be a bridge shorter than
its own construction time.** C8's body: *"this gate's retirement condition is D3 (the typed
message-composition seam) landing… the per-guard baselines pinned here from prose extraction have
to be discarded, not migrated."* D3 is claimed and executing. Building a rung ratchet whose
baselines are guaranteed to be thrown away, concurrently with the thing that throws them away, is
negative work unless the seam slips materially. **Recommendation: cut C8 from this execution and
re-raise it only if the seam misses.** Also a PM call — C8 is regression prevention, and cutting it
accepts a window with no rung ratchet.

**F7 — `CBS-C2`'s `surface:` field understates it, and hides a serialization.** C2 is listed with
`surface: _command_tokenizer.py`, but its AC-8 half threads the resolved position through
`evaluate_payload_json` — a `dispatch.py` edit, in the same function CBS-C1 restructures. Two
chunks editing one function in one file is a collision the chunk table does not show. **Split in
the merged shape:** the tokenizer extension is M2 (wave 1, own file), the threading is folded into
M5 (wave 2, after M1 has settled `dispatch.py`).

**F8 — No `code-reviewer` pass has run on either workstream's diff.** Stated in the duty-of-care
handoff's § Carried Forward. The merged shape carries one review gate over the combined diff (M13)
rather than two per-plan passes. This is a gap being closed, not a new requirement.

**F9 — M1 is widened beyond either plan's ratified scope, and that is called out rather than
absorbed.** M1 extracts `check_head_tail_plumbing_rewrite` as well as `check_offer_git_c`.
Neither plan authorizes the second extraction. The rationale is the same one DoC-C9 already
carries (O19: an advisory-band guard sitting inside the confinement-heavy file makes the band cut
un-mechanical), and the payoff is concrete: without it, `dispatch_checks.py` is wanted by four
separate rows and becomes a four-wave serial chain on its own; with it, that chain is two waves.
**This is an addition to ratified scope and needs the PM's assent inside the same stamp.** If
declined, M7 collapses back into M5 and the plan runs one wave longer.


### Superseded — the four-wave shape

Superseded by § Consolidated wave order above. Kept because its per-row lane rationale is
still the best statement of why particular rows can run in parallel, and because waves 1–2
executed under it.

## The wave shape

All code lands in one shared claude-klabauter working tree. No worktrees. Parallelism is bounded
by **file-scope disjointness**, and every wave below is a set of lanes with pairwise-disjoint
file scopes. Agents edit only; the EM commits per lane with a path-limited commit.

**Critical path: M1 → M5 → M8 → M11 — four waves.** Fully serialized, the same twelve rows are
twelve units. The merge buys roughly 3× on wallclock, and F2 and F9 are where most of it comes
from.

### Wave 1 — four lanes, no interdependencies

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M1** — three-band chain split + two guard extractions | `dispatch.py`, `dispatch_checks.py` *(extraction only)*, new `guard_offer_git_c.py`, new `guard_head_tail_rewrite.py`, delete `tests/test_hard_denies_precede_rewrites.py` | CBS-C1 + DoC-C9 + F9 widening |
| β | **M2** — tokenizer: resolve-once entry point, `find_git_segment`, classified wrapper table | `_command_tokenizer.py` | CBS-C2 *(tokenizer half)* + CBS-C5 *(API half)* + CBS-C3 *(table half)* |
| γ | **M3** — drop the head/tail fork without recognizing the upstream generator | `guard_plumbing_and_loops.py` | DoC-C11 |
| δ | **M4** — re-derive the grep guard's target class over the 69,329-command corpus; return verdict (a) rewrite here / (b) belongs in the peer's answerer / (c) row dies | coordinator-content-repo `state/audits/` only — **read-only on claude-klabauter** | DoC-C10 |

M1 is the longest lane and sets the wave. M4 is in a different repo and cannot collide with
anything.

**M1 sequencing is load-bearing and is not the executor's to reorder.** Delete
`test_hard_denies_precede_rewrites.py` **last**, and only after band membership is asserted for
every *registered* guard rather than only the named ones (CBS AC-2, Finding 9). Preserve
`inprocess-search` at head-of-band in the advisory-rewrite band — reordering that band by any
other criterion silently makes that seam unreachable for exactly the commands it exists to answer.
Preserve `_crash_deny`'s immediate-return property.

### Wave 2 — three lanes, after M1 and M2

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M5** — thread resolve-once through `evaluate_payload_json`; migrate cohort A's seven legacy guards onto M2's entry point | `dispatch.py`, `dispatch_checks.py` | CBS-C2 *(threading half, per F7)* + CBS-C3 |
| β | **M6** — consume `find_git_segment` so `prefix1` and `git_prefix` derive from one walk | `guard_offer_git_c.py` | CBS-C5 *(consumer half)* |
| γ | **M7** — short-circuit the head slice, bound the tail slice, stop eating shell redirections as path operands | `guard_head_tail_rewrite.py` | DoC-C2 |

M5 carries both the threading and its first consumers in one agent deliberately — they have to
agree on the kwarg contract, and splitting them across lanes would race on that contract rather
than on a file.

**M5's two hard requirements, both from CBS anti-scope:** preserve
`COORDINATOR_OVERRIDE_NO_VERIFY` / `_GIT_CLEAN` / `_GIT_REVERT` / `_BLANKET_ADD` exactly, and do
not make them subagent-reachable as a side effect of restructuring how the guard reads its
environment. Record the before/after token-walk count — AC-8 is a *reduction* criterion, and a
non-regression-only reading of it passes without the reduction happening.

**M7's equivalence is proven differentially** against the pre-change emitter, not by running the
new one and liking the result. Both handoffs record that exact mistake being caught.

### Wave 3 — two lanes, after wave 2

| Lane | Row | File scope | Source rows |
|------|-----|-----------|-------------|
| α | **M8** — delete the five hand-maintained `_skip_wrapper_own_argv` copies; **M9** — restore the reverted ladder-tail promotions from the captured diff | `_sentinel_creation_guard.py`, `block_subagent_destructive_action.py`, `block_subagent_commit.py`, `block_worktree_creation.py`, `block_reviewer_bash_outside_allowlist.py`, `dispatch_checks.py` | CBS-C4 + DoC-C5 *(restore, per F3)* |
| β | **M10** — execute the ladder promotions on the three advisory guard modules, per the landed worklist | `guard_grep_via_bash.py`, `guard_multiprobe_banner.py`, `guard_plumbing_and_loops.py` | DoC-D2 |

M9 folds into M8's agent because both touch `block_subagent_destructive_action.py`. M8's measure
of success is **deleted lines, not added ones** — the five copies are at
`block_worktree_creation.py:204`, `dispatch_checks.py:864`,
`block_subagent_commit.py:816` (`_skip_wrapper_own_argv_for_commit`),
`_sentinel_creation_guard.py:281`, `block_subagent_destructive_action.py:1233`.

M10 consumes M4's verdict for the grep guard's composed-pipeline row. If M4 returned (b) or (c),
M10 drops that row and files the coverage ask against the peer's answerer instead of promoting a
second-best path — a plan whose subject is duty of care must not offer agents the slower route.

### Wave 4 — serial, tree-quiet

| Row | Disposition |
|-----|-------------|
| **M11** — audience split: a subagent gets an escalation route, never a switch to flip | **STRUCK, and the fallback is withdrawn** (2026-07-29). F5 routed it to the typed seam; the typed-seam session then asked for exactly this in writing and the two agreements were exchanged — see `state/2026-07-29-typed-alternative-record-band-reconciliation-response.md` § 3. The audience requirement is now an obligation on *their* record's shape, carrying the four constraints restated in that document. **No interim string-filter ships from this side**, because an interim would create the dual-path steady state their plan's anti-scope forbids. |
| **M12** — rung ratchet, regression prevention only | **CUT per F6** (PM, 2026-07-29). |
| **M14** — extend M1's extraction to the rest of `dispatch_checks.py`'s advisory tail | **NEW, agreed with the typed-seam session** — the durable answer to their sub-file-ownership ask, recommended over a line-range split (line ranges rot; that file moved 150–200 lines inside a day). Makes ownership of every function in the file filename-answerable. |
| **M15** — convert this session's guard modules' message construction to the `AlternativeRecord` type | **NEW, agreed** — a follow-on AFTER M3/M7/M10's promotions land, opened once their `S1` pins the import path and field set. Promotions land against prose now; records replace the prose later. Signal completion with a **named test** asserting no owned guard constructs an envelope by the prose path — their extractor deletion is gated on it. |
| **M13** — `code-reviewer` pass over the combined diff | Not optional — F8. One pass over the merged diff, not two per-plan passes. |

**M11 must never be implemented as deletion.** Removing the override line and leaving nothing
behind converts a rung-C message into a rung-D one for subagents — a downgrade wearing a safety
costume. The escalation route is mandatory.


## Gates that bind every wave

- **The 304-cell confinement corpus stays green throughout.** A cell that flips does so as an
  `XPASS`-forced conversion to a real assertion, never by editing the shape or the guard list.
  A cell turning red means the band line was crossed — that tripwire firing is it doing its job.
- **The liveness gate stays green.** M1's band model and M5's wrapper table are obligated to
  satisfy it (§ Reconcile ask 1). A band assignment that cannot satisfy the gate is a finding, not
  a reason to weaken the gate.
- **The override-unreachability boundary holds**, pinned by
  `tests/test_override_unreachability_boundary.py` (claude-klabauter) and
  `coordinator/tests/test_bash_guard_hook_stays_per_event.py` (coordinator-content-repo). CBS AC-9.
- **Before trusting any run of the liveness gate**, run `git status --porcelain` scoped to
  `_alternative_liveness.py` and its test file. That module has previously produced four different
  failure counts in one session while a peer was mid-write on it.
- **No new command parser.** A chunk that adds a resolution implementation without deleting the
  ones it replaces has failed, however green. The point is subtraction.
- **Windows is not covered and must not be reported as covered.** Every measurement and fix here
  is macOS-observed; the cross-platform branching is reasoned and unverified. PM-deferred to the
  capable-host milestone — deferred, not cancelled.


## Carried unchanged from both plans

Anti-scope and out-of-scope from both source plans bind unchanged. The ones most likely to be
crossed by an executor working from this document alone:

- Do not fix the twelve remaining `setsid`/`busybox` corpus cells (CBS-D1, deliberately declined).
- Do not invert the fail direction (CBS-D2 — needs its own PM ruling, must not land inside a
  refactor where its regressions would be indistinguishable from the refactor's).
- Do not evaluate skipping site-module loading (DoC-D1 — deferred, unratified; the safety audit
  *is* the work).
- Do not absorb the typed message-composition seam (DoC-D3 — spun off and claimed by a peer).
- Do not fix a guard the liveness gate finds dead. The gate reports; fixing is the owning row's
  job.
- Tool grants and agent policy, including the `security-audit-worker` bash-policy gap, are PM
  calls and out of scope.


## Tasks

Concatenation of both source spines, `M*` then `S*`, with § Consolidation delta applied.
Row bodies are otherwise byte-for-byte from their source plan; `CONSOLIDATION:` notes are
the amendments.

```yaml plan-tasks
- id: M1
  title: Split the chain into three bands and extract check_offer_git_c and check_head_tail_plumbing_rewrite into their own modules
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch.py
  disposition_ref: 061134b1
  disposition: coded
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane α. Merges CBS-C1 and DoC-C9, plus the F9 widening.
    Their bodies and ACs (CBS AC-1, AC-2; DoC O19) bind unchanged.

    Three bands: confinement-deny, then advisory-rewrite, then
    platform-conditioned-deny LAST. Band membership is an explicit `band` enum
    field, never derived from `fail_closed` -- that field is exception-routing
    policy and is orthogonal to banding. The three platform guards carry
    fail_closed=True AND must run after the rewrite band; promoting them ahead
    was tested empirically and reverted for inverting their behaviour on both
    platforms.

    Sequencing inside this chunk is load-bearing and not the executor's to
    reorder: delete test_hard_denies_precede_rewrites.py LAST, only after band
    membership is asserted for every REGISTERED guard rather than only the named
    ones, and only after the band-membership assertion carries the retired test's
    third leg -- failing on a misspelled or stale guard name in any band's named
    set, so a typo cannot silently turn the check into a no-op.

    Preserve inprocess-search at head-of-band in the advisory-rewrite band.
    Preserve _crash_deny's immediate-return property. First deny wins within a
    band; later denies in the same band are discarded.

    F9: the second extraction (check_head_tail_plumbing_rewrite) is beyond both
    plans' ratified scope. It removes two waves from the critical path by giving
    DoC-C2 its own file. If the PM declines it, drop it and M7 folds back into M5.

    CONSOLIDATION: **LANDED** (claude-klabauter `061134b1`, with `M0`). Stamped `coded`
    during consolidation -- the row shipped with no disposition, so it read as pending.
    Verified on disk rather than taken from the handoff: `dispatch.py` carries the `band` field,
    `guard_offer_git_c.py` and `guard_head_tail_rewrite.py` both exist as extracted modules,
    `test_hard_denies_precede_rewrites.py` is deleted (the invariant is held by construction
    now), and `test_guard_band_membership.py` is present.
- id: M2
  title: Extend _command_tokenizer.py with the resolve-once entry point, find_git_segment, and the classified wrapper table
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/_command_tokenizer.py
  disposition_ref: 2e6820d4
  disposition: coded
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane beta. Merges CBS-C2's tokenizer half, CBS-C5's API half, and
    CBS-C3's wrapper-table half into the one file all three touch.

    Mostly a PROMOTION, not new code -- cohort B already contains a near-correct
    implementation smeared across five files. If this reads as greenfield
    authoring, it has gone wrong.

    Two axes on the wrapper table, and collapsing them into one is the failure
    mode: semantic class (execs-its-argv / inspects-without-execing /
    applet-dispatcher) governs whether a word may EVER be rewrite-facing;
    rewrite-facing membership is a NARROWER explicitly-initialized subset,
    initialized to today's _FIND_WRAPPER_WORDS verbatim and widened only by
    explicit decision. setsid/strace/doas/ionice are execs-its-argv AND
    deliberately NOT rewrite-facing. Building this single-axis would widen the
    rewrite-facing view by construction, which Anti-scope forbids.

    Before deleting any of the six source literals, diff the built table against
    all six so a per-word omission fails loudly rather than silently narrowing a
    confinement guard. `time` was dropped from an earlier draft of this table and
    is present in all six on-disk allowlists.

    Streams carry a resolution confidence (resolved / peeled / unresolved).
    Reported here, consumed by nobody in this plan -- that is intentional, and
    makes the fail-direction question decidable later without another refactor.

    CONSOLIDATION: **LANDED** (claude-klabauter `2e6820d4`, with `M3`). Stamped `coded`
    during consolidation. Verified on disk: `find_git_segment` is defined at
    `_command_tokenizer.py:921`.
- id: M3
  title: Drop the head/tail fork without needing to recognize the upstream generator
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_plumbing_and_loops.py
  disposition_ref: 2e6820d4
  disposition: coded
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane gamma. DoC-C11 unchanged. Self-contained to one module; no
    dependency on any other wave-1 lane.

    CONSOLIDATION: **LANDED** (claude-klabauter `2e6820d4`, with `M2`). Stamped `coded`
    during consolidation.
    Carry forward one correction the landing produced, because it bounds what the liveness gate
    can prove: this row's alternative was described as removing the head/tail fork. It does not
    -- for `git log | head -n 3` the original spawns 2 processes and the alternative spawns 3,
    one an interpreter startup. The real win is the platform the guard is banded for, a Windows
    caller with no `head`, and the message now says that. **The liveness gate structurally
    cannot catch this class**: it proves an alternative runs, never that it is cheaper than what
    it replaces.
- id: M4
  title: Re-derive the grep guard's target class over the 69,329-command corpus and return a verdict
  change_kind: verification
  surface: state/audits/2026-07-29-grep-invocation-shape-corpus.md
  disposition_ref: 29665cd57
  disposition: coded
  queue_scope: project
  deferred: false
  body: |
    Wave 1, lane delta. DoC-C10's measurement precondition, run as its own row
    because its outcome decides whether M10 carries a grep row at all.

    Read-only on claude-klabauter. Re-derive (grep-shaped traffic) MINUS (what
    search.answer already answers). The numbers exist: the answerer's docstring
    states 14.65% coverage against the rewrite's 1.068%. Return one of:
    (a) the residue is worth a rewrite in this guard;
    (b) the coverage belongs in _STAGE_BUILDERS / search.answer, which is the
        peer's surface and a coordination ask rather than a row we execute;
    (c) the row dies.
    (b) is the likeliest and is strictly better for callers.

    CONSOLIDATION: **LANDED** (coordinator-content-repo `29665cd57`,
    `state/audits/2026-07-29-grep-guard-target-class-residue.md`). Stamped `coded` during
    consolidation. Returned verdict **(c)** against the plan's own (b) leaning: rewriting only
    the grep segment of a composed pipeline saves ZERO forks in exactly the cases
    `search.answer` declines, so the composed-pipeline row is strictly dominated rather than
    merely superseded. `M10` no longer carries it.
- id: M5
  title: Thread resolve-once through evaluate_payload_json and migrate cohort A's seven legacy guards
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch_checks.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane alpha. Depends on M1 and M2. Merges CBS-C2's threading half
    (per F7 -- its surface field understated it as a tokenizer-only edit) with
    CBS-C3. One agent owns both because they must agree on the kwarg contract;
    splitting them across lanes would race on that contract rather than on a file.

    Guards: check_no_verify, check_destructive_git_orphan, check_destructive_rm,
    check_destructive_git_clean, check_destructive_git_revert,
    check_blanket_git_add, check_runaway_find. Strangler-style, one at a time,
    corpus as the gate after each.

    Thread the resolution exactly as policy_file and host_is_windows are threaded
    today. Record a before/after token-walk count -- AC-8 is a positive reduction
    criterion, and a non-regression-only reading passes without the reduction
    happening.

    HARD REQUIREMENT: COORDINATOR_OVERRIDE_NO_VERIFY / _GIT_CLEAN / _GIT_REVERT /
    _BLANKET_ADD are preserved exactly and must not become subagent-reachable as
    a side effect of restructuring how the guard reads its environment.

    Add cells for `cd X && which git status`, `cd X && busybox git status`, and
    `cd X && setsid git status` asserting no auto-rewrite is emitted.
- id: M6
  title: Consume find_git_segment so prefix1 and git_prefix derive from one walk
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_offer_git_c.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane beta. CBS-C5's consumer half. Runs in parallel with M5 only
    because M1 extracted this guard into its own file -- see F2, the merge's
    headline wallclock win.

    The proof of this chunk is the single-walk consolidation, not a rung
    promotion: check_offer_git_c already auto-rewrites the prefixed spelling as
    of 2d12c6dc. The remaining deliberate non-promotion is the prefix0 case,
    which stays at deny+offer; if that restraint reads as wrong, it is a finding
    to report, not to act on.
- id: M7
  title: Short-circuit the head slice, bound the tail slice, and stop eating shell redirections as path operands
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_head_tail_rewrite.py
  queue_scope: project
  deferred: false
  body: |
    Wave 2, lane gamma. DoC-C2, in its own file post-M1. Covers DoC AC-8, AC-9,
    O5 and O7.

    The redirection defect is the more serious half: an auto-applied rewrite
    silently ran a different command than the caller typed. Where a redirection
    changes observable output the guard must DECLINE to rewrite rather than
    rewrite approximately -- the package already has that pattern in
    _seam_confirmed_rewrite.

    Equivalence for the short-circuit is proven DIFFERENTIALLY against the
    pre-change emitter, not by observing that the new one runs. Both source
    workstreams recorded catching exactly that mistake.

    If the PM declines F9, this row folds into M5 and the plan runs one wave
    longer.
- id: M8
  title: Delete the five hand-maintained wrapper-walk copies, and restore the reverted ladder-tail promotions
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Wave 3, lane alpha. CBS-C4 plus DoC-C5-restore (M9, folded here because both
    touch block_subagent_destructive_action.py).

    Copies to delete: block_worktree_creation.py:204, dispatch_checks.py:864,
    block_subagent_commit.py:816 (_skip_wrapper_own_argv_for_commit),
    _sentinel_creation_guard.py:281, block_subagent_destructive_action.py:1233.
    Plus _normalize_windows_argv0_head_path_with_spaces in three copies and the
    bundled-`-c` fix in at least five. The measure of success is deleted lines.

    RESTORE, per F3: state/audits/2026-07-29-ladder-tail-confinement-rows-handover.md
    carries the complete diff of finished, tested promotions to
    block_reviewer_bash_outside_allowlist.py (three D-rung rows) and
    block_subagent_destructive_action.py (git-mv row C to B), which were reverted
    so a peer session would inherit a clean tree. There is no peer under the merge.
    Re-apply the captured diff; do not re-derive the work.
- id: M10
  title: Execute the ladder promotions on the three advisory guard modules
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/guard_grep_via_bash.py
  queue_scope: project
  deferred: false
  pm_approved: true
  body: |
    Wave 3, lane beta. DoC-D2, PM-ratified and un-deferred. Primary carrier of
    O1 and O2 -- the 45 rung-C and 16 rung-D messages are discharged by promoting
    them, not by any gate. The `D` prefix on the source row was retained only so
    cross-references resolved; read it as active.

    Worklist is landed at state/audits/2026-07-29-guard-module-ladder-worklist.md
    (1959a8266, 232be2ee1) -- this executes cold from it.

    The stale "there is no harness-native zero-fork search" claim in the grep
    guard's own message was already corrected (claude-klabauter 6f3ae246), so the
    blocking reason this row originally carried is discharged.

    Consumes M4's verdict, WHICH IS IN: **(c) -- the row dies.** M4 landed
    (`29665cd57`, `state/audits/2026-07-29-grep-guard-target-class-residue.md`) and
    returned (c) rather than the plan's own (b) leaning, with numbers: rewriting
    only the grep segment saves ZERO forks in exactly the cases `search.answer`
    declines, so the row is strictly dominated rather than merely superseded. Drop
    the composed-pipeline row. The narrow real residue (~0.6-0.9% of grep-shaped
    traffic -- `sed`/`awk`/`xargs` absorption) belongs to `_STAGE_BUILDERS`, which
    is the peer's surface: file it as a coordination ask, do not build it here.

    HARD CONSTRAINT ON EVERY PROMOTION IN THIS ROW -- read
    `state/lessons/2026-07-29-a-guard-message-offering-an-override-reads-as-prompt-injection.yaml`
    before starting. **Promotion means ADDING A WORKING ALTERNATIVE. It never means
    making the override line clearer, more prominent, or better worded.** Without
    that stated, "promote this message" invites exactly the polish that makes the
    message MORE discardable.

    The evidence, which is empirical rather than theoretical: a read-only Explore
    subagent, dispatched for an unrelated enumeration and with no knowledge of any
    plan, spontaneously reported this package's own guard advisories as prompt
    injection -- "styled as coordinator-hook system messages, urging me to set
    override environment variables... I treated them as untrusted content." Its
    intended reader classified our working-as-designed message as an attack and
    discarded it. A prose imperative arriving inside tool output, unsigned, telling
    the reader to disable a safety check, has no feature distinguishing it from a
    genuine attack -- because the distinguishing feature would have to be
    structural, and prose has no structure.

    So the override line fails in both directions at once: a well-behaved caller
    distrusts it and gets nothing, and a caller that trusts it gets the off-switch.
    Never put a disable-the-check instruction in a message addressed to a confined
    caller.

    Corroborating data point from the executing session, worth knowing because it
    widens the finding: this EM -- the unconfined, authorized audience the override
    line is legitimately addressed to under the EM-vs-IC ruling -- received dozens
    of these advisories in one session and acted on ZERO overrides. Not from
    distrust, but because the *advisory* was useful and the *switch* was never what
    it wanted. Twice the advisory changed behaviour; the bypass never did. Do not
    assume the EM rendering needs the switch either.

    SETTLE-NOTE OBLIGATION: when this row commits, publish a one-line note to the
    reconciliation thread. The typed-seam session captures this side's message
    branches into its S3C differential-render baseline only after THIS row lands
    (not after M3 -- this row touches `guard_plumbing_and_loops.py` a second time,
    so an earlier capture would baseline a half-promoted message). They infer
    settlement from the note, not from the module being touched.

    CONSOLIDATION: the SETTLE-NOTE OBLIGATION at the end of this row is **discharged
    structurally, not performed.** There is no peer session to publish a one-line note to.
    The obligation existed only to let the other session know when it was safe to capture
    this side's message branches; that is now an ordering constraint inside one plan -- S3C
    captures after this row lands, enforced by wave order (§ Consolidated wave order), not by
    a note anyone has to remember to send. Do not send a note; do not treat its absence as an
    outstanding obligation.
    The M4 residue this row declines to build (`_STAGE_BUILDERS`, ~0.6-0.9% of grep-shaped
    traffic) was declined on the grounds that it "is the peer's surface: file it as a
    coordination ask, do not build it here." That surface is now this session's. The decline
    STANDS on its other stated ground -- M4 returned verdict (c), the row is strictly
    dominated -- so this is recorded as available-and-not-taken, not as newly in scope. No new
    row.
- id: M11
  title: Address the override line to the EM only — a subagent gets an escalation route, never a switch to flip
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: true
  pm_approved: true
  disposition: wont_do
  disposition_detail: "STRUCK 2026-07-29 in favour of the typed-seam plan's S2, and the standalone-sweep fallback is withdrawn with it. F5 routed the audience split to the seam; the typed-seam session independently asked for exactly that, offering strike-or-label-interim as its two exits, and this session took the strike. An interim string-filter here would create the dual-path steady state their anti-scope forbids. The requirement did not disappear -- it is now an obligation on their record's shape, with the four constraints restated in the response document (escalation route not deletion; the verified untypeable-override basis; rung as a (message, caller class) pair; reuse the shipped discriminator)."
  body: |
    STRUCK -- see disposition_detail. Body retained as the rationale record so the
    reasoning is not re-derived, NOT as a work instruction. Do not build this.

    Wave 4. DoC-C12, PM-ratified. Highest-leverage row on the board: it does not
    promote 45 messages one at a time, it removes the worst property of all of
    them at once -- that the single easiest action a guard offers is disabling
    the guard -- for the caller population where that property does most damage.

    Verified: coordinator/subagent-sandbox-policy.yaml grants
    coordinator:code-reviewer exactly ls/cat/head/tail/wc/find/file/stat/grep
    plus read-only git. `env` is absent. `export` is absent. So no override in
    the package is typeable by that class, and every message advertising one to
    it is offering something structurally impossible.

    DO NOT implement as deletion. Removing the override line and leaving nothing
    behind converts rung C to rung D for subagents -- a downgrade wearing a
    safety costume. The escalation route naming what to report upward is
    mandatory.

    SCHEDULING, per F5 -- PM call inside the authorization stamp. Preferred: send
    the audience field to the typed seam
    (docs/plans/2026-07-29-typed-alternative-record-message-composition-seam.md,
    claimed by a peer) as a coordination ask, so the composer decides rendering
    in one place rather than at 94 call sites. Fallback: standalone sweep, placed
    in wave 4 because a broad-shallow sweep must run against a quiet tree or it
    serializes every other lane behind itself.

    The EM-vs-subagent discriminator already ships --
    block_reviewer_bash_outside_allowlist.check() does it today: no agent_id on
    the payload means a top-level EM call. Do not build a second one.

    CONSOLIDATION: schema fix, carried back to the source plan as well.
    `disposition: struck` is **not a member of the plan-tasks disposition enum**
    (`open`/`coded`/`spun_off`/`backlogged`/`wont_do`), so this row's resolution state routed
    nowhere on harvest -- a cut row that no tool could see as cut. Corrected to `wont_do`,
    which is the enum's name for "explicitly declined". No change of meaning: the row is
    struck exactly as `disposition_detail` describes.
    `disposition_ref` is dropped for two independent reasons: the schema FORBIDS it on
    `wont_do` (detail required, ref forbidden), and its value
    (`state/2026-07-29-typed-alternative-record-band-reconciliation-response.md`) is a dead
    path -- the four-document thread was archived to `archive/memos/` on settlement. The live
    location is
    `archive/memos/2026-07-29-typed-alternative-record-band-reconciliation-response.md`,
    recorded here so the pointer survives its own deletion.
    The strike itself is unchanged and correct: this row's work is done by `S2` in this plan.
- id: M12
  title: Build the rung ratchet — regression prevention only; it does NOT discharge the ladder
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  disposition_detail: |
    CUT by PM ruling 2026-07-29 (merge finding F6 accepted). Its own retirement condition was
    the typed seam landing, and the seam is now rows S0-S6 of this same plan -- its per-guard
    baselines are pinned from prose extraction and must be DISCARDED rather than migrated once
    the typed record replaces the prose, so building it concurrently with the thing that throws
    its baselines away is negative work. Re-raise only if the typed seam slips materially. Body
    retained as the rationale record, not a work instruction. Disposition added during
    consolidation: the row carried `deferred: true` with NO disposition, so a cut row read to
    the deferred-harvest as future work still pending.
  disposition: wont_do
  queue_scope: project
  deferred: true
  pm_approved: false
  body: |
    Wave 4. DoC-C8. **CUT by PM ruling, 2026-07-29** (F6 accepted). Do not build
    it in this execution. Re-raise only if the typed seam slips materially. The
    body below is retained as the rationale record so the reasoning is not
    re-derived, not as a work instruction.

    Its own retirement condition is the typed seam landing, and the seam is
    claimed and executing. Its per-guard baselines are pinned from prose
    extraction and must be DISCARDED, not migrated, once the typed record
    replaces the prose. Building it concurrently with the thing that throws its
    baselines away is negative work unless the seam slips materially.

    If built: it covers regression prevention ONLY. Its earlier claim to carry
    O1/O2/O11 is withdrawn -- a baseline pinning each guard's current rung
    grandfathers all 61 known-bad rows and fails ZERO of them on landing day.
    Its criterion also cannot fail a rung-D message at all, since an empty
    alternative set is not {override}. Do not quietly re-claim O2.

    Never land red. Never let the suppression list decay into "whatever exists
    today" either. Both failure directions apply.

    CONSOLIDATION: schema/bookkeeping fix, carried back to the source plan as well.
    This row was CUT by PM ruling, and its body says so plainly -- but it carried
    `deferred: true` and **no `disposition` at all**, so every mechanical reader saw a deferred
    row awaiting future work rather than a declined one. `disposition: wont_do` plus the
    required `disposition_detail` now match what the body already said. Nothing about the
    decision changes; only its machine-readable record.
- id: M14
  title: Extend M1's extraction to the rest of dispatch_checks.py's advisory tail
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch_checks.py
  disposition_detail: |
    STRUCK during consolidation 2026-07-29. Its stated rationale was preventing two concurrent
    sessions colliding in dispatch_checks.py -- making ownership of every function answerable by
    filename. One session now holds both batons, so the collision is unreachable and the
    filename handoff it existed to enable does not happen. Striking it also retires the only row
    in either source plan still awaiting PM ratification, rather than asking the PM to ratify a
    row whose motivation has evaporated. The one non-collision-motivated piece -- removing M1's
    deferred-import shim -- is re-homed to the new row M14S. Restore condition: the by-guard cut
    being re-split across sessions.
  disposition: wont_do
  queue_scope: project
  deferred: true
  pm_approved: false
  body: |
    NEW, agreed with the typed-seam session 2026-07-29 (see
    state/2026-07-29-typed-alternative-record-band-reconciliation-response.md § 2).
    Not yet PM-ratified -- it is an addition to the merged scope on the same
    rationale the PM already accepted for M1's widening, and should be confirmed
    in the same breath as that precedent rather than assumed.

    M1 extracts the two worst cases (check_offer_git_c,
    check_head_tail_plumbing_rewrite). It does NOT empty the advisory tail: the
    remainder of the contiguous tail block still sits inside a
    confinement-majority file, so a bare filename handoff would still land two
    concurrent sessions in the same file -- the exact collision the by-guard cut
    exists to prevent.

    Extract the rest of the advisory tail so ownership of every function in that
    file is answerable by reading a filename. This is the discharge-test answer:
    an artifact, rather than two sessions remembering where a line was.

    Chosen over a line-range ownership split deliberately. Line ranges rot, and
    this file is hot -- it moved 150-200 lines inside a single day while the two
    source plans were waiting for review. A function-list split is available as an
    interim if the peer needs to start before this lands; a line-range split is
    not on the table.

    Same constraints as M1: pure move, behaviour unchanged, every importer
    enumerated BEFORE the move and repointed in the same pass, tree importable at
    every step. Read the lesson at
    state/lessons/2026-07-29-a-dispatch-onto-a-live-shared-surface-needs-the-whole-importer-set.yaml
    before dispatching this -- it was written from this workstream wedging the
    fleet on exactly this operation.

    CONSOLIDATION: **STRUCK.** This row's stated rationale was a two-session collision
    hazard: "a bare filename handoff would still land two concurrent sessions in the same
    file -- the exact collision the by-guard cut exists to prevent," and "ownership of every
    function in that file is answerable by reading a filename." Both batons are now held by
    one session, so there is no second session to collide with and no filename handoff to
    make answerable. The row's own body also recorded it as "Not yet PM-ratified ... should be
    confirmed in the same breath as [M1's] precedent" -- striking it retires that open
    ratification rather than asking the PM to ratify a row whose motivation has evaporated.
    Kept as a rationale record, not a work instruction, per this workstream's convention for
    cut rows. The one piece of M14 that was NOT collision-motivated -- removing M1's
    deferred-import shim -- is re-homed to M14S below. If the by-guard cut is ever re-split
    across sessions, restore this row; the reasoning above is the whole restore condition.
- id: M14S
  title: Delete M1's deferred-import shim in dispatch_checks.py and repoint its importers
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/dispatch_checks.py
  queue_scope: project
  deferred: false
  body: |
    CONSOLIDATION: NEW row, carrying the one part of the struck M14 that was not
    motivated by two-session collision-avoidance. Verbatim from the merged plan's
    predecessor handoff, § Findings recorded but not fixed:

      "**`M1` left one deferred-import shim** in `dispatch_checks.py` for the two
      extracted names, because a module-level re-export caused a genuine circular
      import and ~40 call sites across three test files still reach them by the old
      path. A permanent shim defeats the extraction -- fold the repointing into `M14`,
      which is already touching that file."

    M14 is struck, so this needs its own row or the finding is lost. Scope is exactly
    the shim and its importers -- NOT the advisory-tail extraction M14 carried.

    Same constraints as M1, and they are the expensive ones: pure move, behaviour
    unchanged, every importer enumerated BEFORE the change and repointed in the same
    pass, tree importable at every step. Read
    state/lessons/2026-07-29-a-dispatch-onto-a-live-shared-surface-needs-the-whole-importer-set.yaml
    first -- it was written from this workstream wedging every session on the machine
    on exactly this operation.

    Verify differentially, not by running it: confirm the chain entries are in
    byte-identical order against HEAD, which is the bar M1's "pure move" claim was
    held to.
- id: M15
  title: Convert this session's guard modules' message construction to the AlternativeRecord type
  change_kind: code-edit
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  pm_approved: false
  body: |
    NEW, agreed with the typed-seam session 2026-07-29 (see the reconciliation
    response § 5, and the thread close § 4). Deferred on a NAMED reason, not
    appetite: it cannot start until the typed-seam plan's S1 pins the import path
    and field set. There is nothing to convert TO before that.

    THIS PLAN CLOSES OUT WITHOUT THIS ROW, deliberately. S1 is gated on a PM
    execution authorization that the typed-seam plan does not yet hold and that
    neither session controls -- it came back REQUIRES_CHANGES with eleven findings
    and sits at its execution gate. Holding this plan open on another plan's
    unauthorized row would make this close-out hostage to their gate and would
    misreport these waves as unfinished.

    The commitment does NOT lapse with the close-out. It is recorded here with S1
    as its unblock condition and in the reconciliation thread; when their signature
    is published to that thread, this row is picked up as its own dispatch. Their
    extractor-deletion condition (both halves) survives this plan closing -- the
    close-out is not an escape from it.

    If their authorization lands before wave 3, fold the conversion into M10
    instead of deferring, which avoids the double-authoring entirely.

    Scope: this session's owned guard modules stop building message envelopes by
    the prose path and build AlternativeRecords instead. Consume their type; do
    NOT re-declare it -- two shapes would be worse than one.

    Verdict behaviour must not change. When a guard fires and what it decides are
    out of scope for the conversion; a verdict change riding inside a message
    migration is unreviewable.

    SEQUENCING, and this is the part that moves their S5's date: M3, M7 and M10
    are rewriting message text in the exact modules this row converts. Those
    promotions land against PROSE, in the current waves. This conversion is the
    follow-on. Converting messages while simultaneously rewriting them would
    author the same message twice. This is the same sequencing their own parent
    plan prescribes from the other direction -- promotions land on top of the
    typed record where the seam has reached, and against prose where it has not.

    Two obligations inherited with the type:
      - The construction-time invariant: a record whose only actionable member is
        an override raises at construction. Runtime invariant on the emission
        path, not a static type property.
      - The differential-render obligation (their TA9): every converted site's
        rendered output is diffed against a committed pre-migration baseline, so a
        conversion cannot silently downgrade a message already at rung A or B.
        This side's message branches must be in their S3C trigger corpus --
        treat their ABSENCE from it as a blocker, never as coverage.

    Completion signal is a NAMED TEST asserting that no guard module in this
    session's ownership constructs a message envelope by the prose path -- a test,
    not a report, so the signal cannot rot. Their prose-extractor deletion (S5) is
    gated on it, so this is a cross-plan commitment, not an internal checkbox.

    CONSOLIDATION: **ABSORBED INTO S4.** Two changes, both consequences of one session
    holding both batons.
    (1) UN-DEFERRED. This row's named deferral reason was that "S1 is gated on a PM execution
    authorization that the typed-seam plan does not yet hold and that neither session
    controls." That plan now holds it (`execution_authorized_by: PM`,
    `execution_authorized_at: 2026-07-29`). The named gate has cleared, so the named reason is
    discharged -- this is not an appetite-based un-deferral.
    (2) ABSORBED. S4 migrates all ~94 message sites package-wide. This row is the same edit on
    the modules S4's own scope note had excluded as "owned end-to-end by the peer session."
    With one owner there is one population and one row: S4 executes it, and S4's scope note is
    amended accordingly (see S4's consolidation note). The row body is retained because its
    two inherited obligations (the construction-time invariant, the differential-render
    obligation) and its verdict-behaviour-must-not-change constraint bind S4's execution and
    are stated more sharply here than in S4.
    The cross-plan commitment this row recorded does not need to survive as a commitment --
    it is now internal ordering.
- id: M13
  title: code-reviewer pass over the combined merged diff
  change_kind: verification
  surface: <CLAUDE_KLABAUTER_ROOT>/coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Wave 4, closing gate. F8: no code-reviewer pass has run on either
    workstream's diff -- the duty-of-care handoff states this plainly and warns
    against letting those commits read as reviewed. One pass over the merged
    diff, not two per-plan passes.

    Verification to date on both sides was direct execution plus plan-level
    reviews (the Director of Engineering on the confinement plan, the Staff Engineer on the duty-of-care plan).
    Neither is a diff review.
- id: S0
  title: Build the structured harness-capability oracle TA7 depends on
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F6, major; PM decision, follow-up
    pass: build the oracle in scope rather than narrow TA7 -- the Director of Engineering's
    narrowing recommendation was NOT taken). Discharges TA7's dependency and
    must land before S1, since S1's negative-construction test for TA7
    references this oracle.

    Filed status, read at source before this row was written: the ask is
    state/audits/2026-07-29-rewrite-path-api-asks.md § "Ask: a structured
    (non-prose) harness-capability oracle for the alternative-liveness
    gate" -- it proposes "a structured capability manifest -- e.g. a
    YAML/JSON mapping of capability_id -> {available: true|false|unknown,
    per host/agent-type if it varies}". This row SATISFIES that filed ask
    (does not supersede it): the ask's shape is sound and unbuilt, not
    wrong or stale, so this row builds what it specifies rather than
    replacing its design. Close the ask entry (git mv to archive per this
    repo's queue-closure convention) once the manifest lands.

    Build the manifest as a small, hand-maintained, committed
    capability_id -> {available, per-host/per-agent-type if it varies}
    mapping, seeded from the currently-known true capability set (at
    minimum: the guard_inprocess_search in-process-search capability that
    guard_grep_via_bash falsely denied, per T8/state/audits/2026-07-29-
    search-capability-roster-truth.md).

    `probe_harness_capability` (claude-klabauter
    coordinator_core/bash_guards/_alternative_liveness.py:979) currently
    returns UNVERIFIABLE unconditionally, by deliberate design (its own
    docstring: a prior revision keyed a substring match against a prose
    audit document as corroboration, and the Director of Engineering's review on that revision
    caught it inverting the gate's founding defect -- a REFUTATION document
    read as agreement). Read at source before writing this row. That
    function does NOT get replaced by this row -- it becomes the oracle's
    CONSUMER: it looks up `alt.detail`'s capability id in the manifest and
    returns LIVE/DEAD when the manifest has an entry, and continues
    returning the same honest UNVERIFIABLE (never a guessed verdict) when
    the id is absent from the manifest. The manifest is keyed by id, never
    by prose match, so the exact inversion defect the docstring records
    cannot recur through this path.

    Negative-spec (the failure mode this row exists to prevent, named by
    the Director of Engineering's F6 and by the Staff Engineer on the predecessor plan's AC5): a FIXTURE
    oracle that only asserts the test harness itself works satisfies TA7's
    letter while discharging nothing -- it proves a stub answers a stub.
    The oracle built here must report REAL capability state for the
    package's actual guards, and TA7's test -- planting the exact
    `guard_grep_via_bash` false-capability defect (T8) and asserting it
    cannot be constructed -- must fail when run against this REAL oracle,
    not against a stand-in. A green TA7 test that only exercises a fixture
    is not a discharge of this row.

    Cost, named plainly for the record: this row adds a previously-unbuilt
    subsystem (a capability manifest plus its lookup path) to a plan that
    already carries a ~94-site migration (S4/TA8). That is a deliberate
    scope increase the PM accepted in the follow-up pass that produced this
    row, in preference to the Director of Engineering's narrower registry-key alternative -- not
    scope creep discovered later.
- id: S1
  title: Design the alternative-record type and prove a bare-override message is unconstructible
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA2, TA7. (TA1's structural enforcement is discharged by the new
    row before S4 that narrows check() -- see below.) This row makes the fix
    EXPRESSIBLE; it does not by
    itself make anything happen. Review correction (the Director of Engineering/eng-director, F3,
    major): applying the plan's own test -- which currently-shipping rows
    does a gate fail on the day it lands? -- S1 fails ZERO rows, because on
    landing day no site yet constructs the record. That is fine: S1 is not a
    gate, it is the thing that makes a gate unnecessary. The row that
    actually discharges the obligation, by failing the 45 rung-C rows as they
    convert, is S4, gated on TA8 -- S4/TA8 is the load-bearing row, not this
    one.
    The record distinguishes, at minimum: a ready-to-run command; an offer
    needing adaptation; a capability reference resolved against an oracle
    rather than asserted in prose; an escalation route; and an override, which
    is EM-audience, optional, and CANNOT stand as the sole actionable member.
    Rung falls out of which members are populated, which is what makes TA3
    possible.
    Acceptance argument is a negative one and must be written first: a test
    that TRIES to construct the 45 rung-C shape and fails. A type that merely
    discourages the shape has not discharged anything.
    Homing (the Director of Engineering/eng-director review, F8, major): coordinator_core/bash_guards/
    is where the record is FIRST CONSUMED, not where it is OWNED. The
    identical envelope is constructed at 44 sites in
    coordinator_core/write_guards/ and at 27 DoE-side hook scripts under
    coordinator/hooks/scripts/ -- two of which already ship the same
    "To bypass: export COORDINATOR_ALLOW_*" shape this plan exists to make
    unconstructible. Those populations were never graded because the 94-
    message audit was bash_guards-scoped by construction; their absence from
    the count is a scoping artifact, not a clean bill. Home the type one
    level up (coordinator_core/guard_messages/ or equivalent) so write_guards
    and any future PreToolUse cohort can consume the same type without an
    upward dependency on bash_guards. Converting the sibling cohorts is out
    of scope for this plan; making them unable to consume the type is not.

    CONSOLIDATION: the **schedule urgency on this row is void.** Both the predecessor
    handoff and the reconciliation thread made S1 time-critical -- "publish its signature to
    the reconciliation thread the moment it exists ... but only if the signature lands before
    that wave dispatches. If the window has already closed, say so to them." There is no peer
    wave and no window. S1 still lands EARLY, for a different and purely internal reason: it
    pins the field set and import path that S4's fan-out and M15's absorbed conversion both
    consume, so landing it before the wide waves avoids re-shaping converted sites. See
    § Consolidated wave order. Nothing is published anywhere.
- id: S2
  title: Add the audience dimension — EM sees the override, a subagent gets an escalation route
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA5 and the PM ruling. Audience is a property of a record FIELD,
    resolved at construction, never a filter applied to rendered text — a
    string filter leaks the next message shape through.
    The caller-class oracle already exists on our side of the band cut (T13):
    the payload carries agent_type and the policy table is DoE-owned.

    Do NOT build a new discriminator. `block_reviewer_bash_outside_allowlist.
    check()` already does this discrimination in production, and its sequence
    is the precedent: NO `agent_id` on the payload means a top-level EM call
    (it returns early before paying identity-resolution cost); an `agent_id`
    present means a subagent, and `_resolve_subagent_identity` +
    `_read_backpointer_subagent_type` resolve which type.
    That gives three tiers, not two: EM; subagent of known type (whose
    bash_policy entry says what it can actually express); and subagent whose
    identity does not resolve. Decide the third deliberately — that guard fails
    OPEN there, correct for a confinement check and wrong for an audience
    decision. An unresolvable caller gets the SUBAGENT rendering: handing the
    override to an unknown caller is the exact failure this ruling prevents.
    Negative-spec: removing the override from the subagent rendering and
    leaving nothing behind converts rung C to rung D for that audience. The
    escalation route is mandatory, not a nicety — name what to report upward
    and what needs authorizing.
    Review correction (the Director of Engineering/eng-director, F2, critical): the prior wording
    here claimed this row was "independently shippable ahead of the full
    migration". That contradicts the anti-scope's own ban on the filter shape
    (below): the audience property is a record FIELD, resolved at
    construction, so it cannot exist before S1's record does. The fix is not
    to reorder task ids -- S1 already precedes S2 in this spine -- it is that
    S1 must land FIRST in wall-clock time rather than being treated as
    deferrable while S2 goes first. S1 is small (one frozen dataclass plus a
    constructor invariant, hours not a wave), so landing it first does not
    defer the PM ruling's value: S1 then S2 still land in the same wave.

    <!-- Reconciliation response (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-response.md § 3), 2026-07-29: C12 is STRUCK, not labelled --
    exit 1 of the two offered, taken. Not a concession on reading our input:
    the decision had already landed independently, via the cross-plan review
    finding F5 (docs/plans/2026-07-29-bash-guard-merged-execution-shape.md),
    which concluded C12 and this plan's seam are the same mechanism -- C12's
    own body already said as much ("fold it into the seam's record shape when
    it is [ready]"). The merged plan routes the audience field to this seam as
    a coordination ask, with a standalone 94-site sweep kept only as a
    fallback if this seam slips; the PM accepted F5 explicitly. No interim
    string-filter implementation ships from the peer side, so the dual-path
    steady state this plan's anti-scope forbids does not arise from C12's
    direction. The audience requirement itself does not disappear -- it
    becomes an obligation on THIS record's shape, already carried by S2 above
    (escalation route mandatory, never an override, never implemented as
    deletion). C12 and this plan's S1-then-S2 sequencing are no longer an
    open cross-plan dependency. -->
    Prior open cross-plan dependency (RESOLVED, see note above): the parent
    plan (docs/plans/2026-07-29-duty-of-care-the-liveness-gate-and-the-l.md,
    row C12) and this plan's HANDOFF both instructed "ship the audience split
    first, independently" -- the same ordering this finding corrects here.
    C12 is struck in favor of S2; no further reconciliation is owed on this
    point.
- id: S3
  title: Build the reachability probe — an alternative is live only if the receiving caller can express it
  change_kind: script-edit
  surface: coordinator_core/bash_guards/_alternative_liveness.py
  queue_scope: project
  deferred: false
  body: |
    Covers TA6, T9, T10. Today probe_override sets the variable inside the
    gate's own process, where no sandbox policy applies, so it proves the guard
    READS the variable and never that the caller can SET it.
    Resolve the receiving caller class's bash_policy entry and assert the
    alternative is expressible under it. The verified env/export absence for
    coordinator:code-reviewer is the fixture — it is a real, currently-shipping
    example, not a synthetic one.
    Keep this SEPARATE from the existing liveness assertion. Different actor,
    different oracle; folding them lets one green cell imply a property it
    never tested.
- id: S3B
  title: Narrow check() to return AlternativeRecord | None, with the dispatcher as the single envelope-renderer
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F1, critical). Covers TA1. Discharges
    the enforcement TA1 requires as a structural property rather than an
    introspection heuristic.
    Today check() returns the harness's own nested envelope
    ({"hookSpecificOutput": {...}}), a dict, with the caller text a free
    string. So long as check() returns a dict, a guard can hand-build an
    envelope directly and bypass the record entirely -- TA1's "asserted by
    introspection over the package" is then a grep over our own Python, the
    same class of artifact as the prose extractor TA3 deletes. Verified
    2026-07-29 at claude-klabauter
    coordinator_core/bash_guards/block_worktree_creation.py:437-443 (one of
    18 envelope-construction sites in the package).
    Narrow the guard-facing return type to AlternativeRecord | None. The
    DISPATCHER becomes the single site that renders a record into the
    harness envelope. "No guard constructs a bare envelope" is then enforced
    by the dispatcher's signature and by the absence of hookSpecificOutput
    anywhere except one module -- structural, not a heuristic. It is an
    18-site change in bash_guards, not a project.
    Amend coordinator_core/write_guards/INTERFACE.md fidelity rule 2 in the
    same change: it currently states "Preserve every permissionDecisionReason
    / advisory string BYTE-FOR-BYTE" (verified still present at line 34,
    2026-07-29). That rule was a porting-fidelity rule for the bash-to-Python
    migration; left unamended it is a documented, written reason for the next
    author to keep emitting bare strings. Note write_guards is a sibling
    cohort, not this plan's surface (S1's scope is bash_guards/) -- this row
    edits only the fidelity-rule DOCUMENT that governs both cohorts, not
    write_guards' own 44 call sites, which remain out of scope per this
    plan's own "record homed under its first consumer" scoping (see S1/S6
    generic-substrate note).

    CONSOLIDATION: **runs after S3C, not before.** See S3C's consolidation note -- this
    row's byte-identical-rendering claim is proven against S3C's committed baseline rather
    than asserted. No scope change; ordering only.
- id: S3C
  title: Extend the trigger registry to one trigger per known message branch, and capture a rendered pre-migration baseline fixture
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    New row (the Director of Engineering/eng-director review, F5, major -- the single highest-value
    amendment on this review). Covers TA9's prerequisite; re-homes the Staff Engineer F7
    now that the predecessor's C8 (which F7 was originally scoped to) is
    retired and would otherwise orphan it.
    A guard message cannot be rendered without firing the branch that emits
    it, and the package's only trigger corpus today (LIVE_TRIGGERS) reaches
    roughly a quarter of the 94 message branches (~25 triggers; T6). TA9 as
    originally specified compares against a corpus that does not exist.
    Extend LIVE_TRIGGERS to one trigger per known message branch (the Staff Engineer
    F7's fix), then capture the RENDERED TEXT of all ~94 branches as a
    committed baseline fixture, taken before S4 converts any site.
    This flips TA9 from a judgment row ("human-or-reviewer confirmed") to a
    mechanical one: a test that renders every branch post-conversion and
    diffs against this fixture, with rung-A/rung-B sites requiring an
    explicit reviewed acknowledgement to change. Backed by
    state/lessons/2026-07-29-verify-a-rewrite-differentially-not-by-running-it.yaml
    (universal scope): a rewrite is verified by diffing both sides against
    the same fixtures, not by running the new side and eyeballing it.
    <!-- Reconciliation reply (state/2026-07-29-typed-alternative-record-
    band-reconciliation-reply.md § 4), 2026-07-29: the baseline is captured
    PER BAND, immediately before that band's conversion -- not once,
    globally, up front. The peer band's promotions (their M3/M7/M10) land
    against prose in waves already in flight, agreed correct in S5's body;
    their message text keeps changing until those waves settle. A single
    global baseline captured before that settling would freeze the
    pre-promotion text, so when the peer's conversion eventually runs, the
    differential render shows diffs that are their INTENDED promotions, not
    conversion regressions -- the signal TA9 exists to produce drowns in
    expected noise, exactly the "reviewer remembers" failure state TA9 was
    rewritten to escape. The ordering in S5 is not the defect; the baseline
    timing is. This band's own branches enter the corpus before this band's
    conversion; the peer's branches enter after their promotions settle. The
    corpus is shared, the capture is staged per band. -->

    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md §§ 2, 3), 2026-07-29: two precisions, one of them
    load-bearing for this row's correctness. -->

    **The corpus MUST key on the guard's chain-entry name (e.g. `offer-git-c`,
    `head-tail-plumbing-rewrite`, `grep-via-bash-guard`), never on module path or
    fully-qualified function name.** The peer session's merged plan carries a row (`M14`) that
    extends an in-flight extraction moving message-bearing guard functions between modules --
    that is the row's whole purpose, and an earlier row in the same plan (`M1`) already did this
    to two guards today (`check_offer_git_c` -> `guard_offer_git_c.py`,
    `check_head_tail_plumbing_rewrite` -> `guard_head_tail_rewrite.py`). If this corpus keys on
    module path or qualified function name, `M14` renames every key underneath it -- and the
    failure is the dangerous one: the baseline does not fail loudly, it goes MISSING, so a corpus
    that silently loses rows reports green over the branches it can no longer find. This is our
    own `UNTRIGGERED`-suppression hazard arriving from a direction this plan does not otherwise
    watch. Verified, not assumed: the peer session checked `M1`'s extraction differentially
    before committing it, and the 28 chain entries are in byte-identical order at HEAD and after
    the extraction, across a change that moved two guards between files -- chain-entry name is
    the one identifier in this package that survives the refactor this baseline has to outlive.
    Corollary: `test_guard_band_membership.py` (landed with the peer's `M1`) fails when a named
    guard matches nothing registered, so a chain-entry name that genuinely must change becomes a
    loud cross-plan failure rather than a silent gap.

    **Negative-spec: a corpus that can silently lose rows is worse than no corpus at all**, because
    it converts an absent signal into a green one -- a reviewer or gate reading the corpus as
    complete has no way to tell "covered" from "used to be covered, key renamed out from under
    it." Key on chain-entry name specifically because it is the identifier verified above to
    survive the extraction that would otherwise trigger this failure.

    **The peer's branches enter the corpus after their `M10`, not after their `M3`.** M3, M7, and
    M10 land in separate waves and M10 touches `guard_plumbing_and_loops.py` a second time, so
    capturing after M3 would baseline a half-promoted message and then diff a fully-promoted one
    against it -- see S5's body for the full sequencing detail; `M10` is this row's settle point
    for the peer's population, signalled by a one-line note the peer publishes to the
    reconciliation thread when it lands, not inferred from a module being touched.

    CONSOLIDATION: two changes.
    (1) ORDERING -- this row now runs **before S3B**, which is a constraint neither source plan
    carried, because neither could see the other's row as its own to sequence. S3B narrows
    `check()` across 18 envelope-construction sites and claims rendered output is unchanged;
    capturing this baseline first gives that claim a differential proof instead of an
    assertion. That is this workstream's own ratified bar --
    state/lessons/2026-07-29-verify-a-rewrite-differentially-not-by-running-it.yaml, universal
    scope -- and M1 was already held to it. S3B is the largest refactor in either plan still
    lacking such a proof; this ordering supplies one for free.
    (2) The **staged-per-band capture mechanism still applies, but the trigger is now wave
    order rather than a peer's settle note.** The reconciliation-close text below is correct
    and unchanged in substance: capture after M10, never after M3, because M10 touches
    `guard_plumbing_and_loops.py` a second time. What changes is only how settlement is known
    -- by this plan's own wave boundary, not by a note published to a thread. The
    key-on-chain-entry-name requirement is UNCHANGED and remains load-bearing: M1 already
    moved two guards between modules, so a module-path-keyed corpus would still go silently
    missing.
- id: S4
  title: Migrate all ~94 message sites to the record, with a named exception set
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA8, TA9. Sized by review at ~94 sites, mechanically similar, most
    one-line constructor swaps — an afternoon to a day of executor work under
    this repo's operating assumptions, NOT a quarter.
    Fan out by disjoint file scope with per-file executor dispatch and
    EM-serial commits; never one sequential grind, never a worktree per module.
    The real risk is not the swap, it is a silent downgrade of the 33 messages
    already at rung A or B — hence TA9's differential render (built by S3C),
    which is the actual work and should be scoped as such.
    Cross-band shape is SETTLED, not an open question (the Director of Engineering/eng-director
    review, F9, major, ruling with DoE authority): the type lands
    package-wide as a shared API, and each band converts its own guards. This
    follows directly from coordinator/docs/wiki/bash-guard-threat-model.md
    § Two bands, and the shared-helper seam between them ("Editing a shared
    resolver is editing both bands. Check the effect on the other one and say
    what you found -- in the commit.") and § Dividing the work between two
    sessions ("Ownership runs by guard, end to end"), both already-ratified
    doctrine -- it does not need PM reconciliation to become correct. Follow
    the shared-helper rule's own instruction: disclose the cross-band effect
    of landing the type in the commit that lands it.
    The confinement-band conversion (this band's own block_* guard modules
    are DIFFERENT from the confinement band's) is a FIRST-WAVE, concretely
    specified ask carried into the PM's stated reconciliation step, not a
    post-hoc flag: name the AlternativeRecord type and its import path, the
    constructor invariant, the specific confinement-band guard modules, and
    the differential-render obligation (TA9-shape) those conversions
    inherit, so the confinement-band session can accept, decline, or
    counter-propose in one round-trip. Coordinator-content-repo's standing direct-commit
    grant into claude-klabauter would technically permit converting the
    confinement band's guards unilaterally; do NOT use it that way here --
    the by-guard cut is PM-ratified and end-to-end ownership includes the
    message, so unilateral conversion would be the split-a-guard-down-the-
    middle shape that doctrine kills by name. What happens if the peer band
    declines is an open dependency, surfaced to the PM separately, not
    resolved here.

    <!-- Reconciliation response (state/2026-07-29-typed-alternative-record-
    band-reconciliation-response.md §§ 2, 7), 2026-07-29: both mixed-module
    questions below are resolved, not open. commit_tripwires.py: agreed as a
    function-list split, no objection from either side -- check_machine_path_leak
    is the peer session's, check_schema_version_bump and check_bin_sh_polyglot
    are this plan's. dispatch_checks.py: the peer is closing it structurally
    rather than by sub-file agreement -- extracting the two guards that make
    the file mixed (check_offer_git_c -> guard_offer_git_c.py,
    check_head_tail_plumbing_rewrite -> guard_head_tail_rewrite.py; pure move,
    behaviour unchanged, importers repointed) is already in flight under the
    merged plan's row M1. That extraction removes the two worst cases but does
    not by itself empty the advisory tail, so a bare filename handoff would
    still collide on the remainder. This session's chosen answer: the DURABLE
    option, not the interim one -- extend the same extraction to the rest of
    the advisory tail so ownership of every function in the file is answerable
    by filename, per the peer's own recommendation. Reason: this plan is not
    execution-authorized (no schedule pressure that would justify taking the
    function-list interim instead), and the extraction is the discharge-test
    answer named in this plan's own north star -- an artifact rather than two
    sessions remembering where a line was. The function-list interim (this
    plan owns the confinement functions plus the extracted pair; the peer
    owns the named advisory tail functions) remains available if this plan's
    schedule changes and needs to start before the fuller extraction lands. -->
    Mixed-file constraint (band enumeration, 2026-07-29) -- RESOLVED BY
    EXTRACTION, not an open sub-file agreement (see note above): two modules
    were MIXED at function level, not file level, so a filename handoff did
    not apply to them as originally written -- the threat-model wiki already
    names this as a known weakness of its own cut ("it is file-level
    everywhere except the one module holding both bands, where the
    advisory/rewrite family is a contiguous tail block but a single rewrite
    guard sits orphaned in the middle of the confinement run").
    `dispatch_checks.py`: 8 confinement functions, then `check_offer_git_c`
    (the orphaned advisory/rewrite guard the wiki names), then a contiguous
    advisory tail block of 9 -- being closed by extending the peer session's
    in-flight extraction to the full tail, per the note above.
    `commit_tripwires.py`: `check_machine_path_leak` is confinement (peer
    session's); `check_schema_version_bump` and `check_bin_sh_polyglot` are
    advisory/warn-only (this plan's) -- agreed as a function-list split.

    <!-- SUPERSEDED READING -- reconciliation response (state/2026-07-29-
    typed-alternative-record-band-reconciliation-response.md § 1), 2026-07-29.
    What we read (kept verbatim below): guard_grep_via_bash.py,
    guard_multiprobe_banner.py, and guard_plumbing_and_loops.py are
    CLASS = "hard-deny" / fail_closed = True and genuinely deny on Windows,
    so under the wiki's binary band criterion they read as confinement, even
    though dispatch.py's CONFINEMENT_HARD_DENIES comment excludes them on
    narrower adversarial-evasion grounds -- two different operative senses of
    "confinement" (band cut vs. one ordering test) coexisting.
    The peer session verified this evidence and the wiki-vs-ordering-test
    distinction as CORRECT at source. But the question does not resolve in
    either direction -- it DISSOLVES: the merged plan's row M1 splits the
    chain into THREE bands, not two -- confinement-deny, advisory-rewrite,
    and platform-conditioned-deny -- with an explicit `band` enum field per
    chain entry. A two-band model keyed on fail_closed cannot express "hard-
    deny that must run AFTER the rewrites", which is what these three modules
    actually are. Promoting them into the confinement band was tried and
    EMPIRICALLY REVERTED -- it inverted their behaviour on both platforms.
    That empirical revert, not a re-reading of the evidence, is the
    substantive reason the two-band answer below was wrong.
    All three modules are the entire membership of the new third band and are
    owned end-to-end by the peer session (merged plan rows M3, M10). No
    conversion-ownership question remains for them from this plan's side.
    Recorded with the supersession rather than deleted, so a later reader
    does not re-derive the two-band question from the evidence alone. -->
    Superseded reading (band enumeration, 2026-07-29), kept for record:
    `guard_grep_via_bash.py`, `guard_multiprobe_banner.py`, and
    `guard_plumbing_and_loops.py` are `CLASS = "hard-deny"` /
    `fail_closed = True` and genuinely deny on Windows, satisfying the
    wiki's "may this run at all" test -- so under the wiki's band criterion
    they ARE confinement, even though `dispatch.py` carries an inline
    comment that they are "Deliberately NOT added to
    `CONFINEMENT_HARD_DENIES`" in `test_hard_denies_precede_rewrites.py`.
    That set is scoped narrower, to guards whose whole purpose is confining
    a caller against adversarial evasion (identity gates, git-history
    protection, the subagent-commit ban); these three are machine-load
    guards with no adversarial-evasion shape, per the comment's own
    reasoning. Two different operative senses of "confinement" coexist: the
    wiki's binary band definition, which governs the BAND CUT, and this
    narrower engineering-internal set, which governs one ORDERING TEST. They
    answer different questions, and the exclusion is about adversarial-
    evasion scope rather than band membership. This reading is SUPERSEDED --
    see the note above -- these three modules are their own third band
    (platform-conditioned-deny), owned by the peer session, not conversion-
    owned by this plan.

    CONSOLIDATION: **scope amended -- this row now covers the whole package, including
    the three modules its own SUPERSEDED READING block below excludes as peer-owned.**
    `guard_grep_via_bash.py`, `guard_multiprobe_banner.py` and `guard_plumbing_and_loops.py`
    (the third band, `platform-conditioned-deny`) were excluded on ownership grounds only --
    "owned end-to-end by the peer session (merged plan rows M3, M10). No conversion-ownership
    question remains for them from this plan's side." One session now owns them, so they enter
    this row's population. The absorbed M15 (above) is the record of what that conversion
    must satisfy; read M15's body as part of this row's instructions.
    This is NOT an aggregate scope increase: M15 already carried these modules under the
    merged plan's authorized scope. The work moves rows; it does not appear.
    Two things in this row's body are now MOOT, and are kept rather than deleted so a later
    reader does not re-derive them:
      - The whole cross-band coordination paragraph -- "a FIRST-WAVE, concretely specified ask
        carried into the PM's stated reconciliation step ... so the confinement-band session
        can accept, decline, or counter-propose in one round-trip," and "What happens if the
        peer band declines is an open dependency." There is no counterparty; nothing is asked,
        nothing can be declined, and that open dependency is closed by consolidation.
      - The prohibition "coordinator-content-repo's standing direct-commit grant into claude-klabauter would
        technically permit converting the confinement band's guards unilaterally; do NOT use
        it that way here." the Director of Engineering's ruling was against splitting one guard's ownership down the
        middle -- end-to-end by-guard ownership includes the message. With a single owner there
        is no split to make, so the ruling is satisfied rather than overridden. The grant is
        still the authority under which these commits land, exactly as it was for waves 1-2.
      - `commit_tripwires.py`'s function-list split and `dispatch_checks.py`'s
        extraction-based resolution were both mixed-file OWNERSHIP answers. Ownership is no
        longer contested; both files are converted whole by this row.
    The row's real risk is unchanged and is the reason S3C exists: a silent downgrade of the
    33 messages already at rung A or B.
- id: S5
  title: Compute rung from the record and delete the prose extractor
  change_kind: script-edit
  surface: coordinator_core/bash_guards/
  queue_scope: project
  deferred: false
  body: |
    Covers TA3, TA4, TA11. Depends on S4 being complete for the population
    being graded — a half-migrated package needs both paths, which the
    anti-scope forbids as a steady state but is unavoidable transiently.
    <!-- Reconciliation response (state/2026-07-29-typed-alternative-record-
    band-reconciliation-response.md § 4), 2026-07-29: the completion
    condition below is CONCRETE, not the vague "depends on both bands
    converting" it replaces -- two artifact-tied conditions, granted by the
    peer session. Deliberately not a date, and deliberately not "when the
    peer session says it is done". -->
    Completion condition (concrete, per reconciliation response § 4,
    superseding the prior honest-but-vague "depends on both bands
    converting"): the prose extractor is deleted when BOTH hold. (a) Both
    bands' guards construct records -- the peer session signals its half of
    this with a NAMED TEST asserting that no guard module in its ownership
    constructs a message envelope by the prose path (a test, not a report,
    so the signal cannot rot). (b) The differential-render baseline (this
    plan's TA9 / S3C) is green across both bands' message branches -- the
    peer session's branches must be in this plan's TA9/S3C trigger corpus;
    treat their absence from it as a blocker on S5, not as coverage.
    <!-- Reconciliation reply § 4, 2026-07-29: timing qualification on
    condition (b) -- the blocker itself is unchanged and stands exactly as
    agreed above. The peer session's branches must enter the S3C corpus
    AFTER their promotions (M3/M7/M10, see the sequencing note below) have
    settled, not before. Capturing them earlier would satisfy this
    condition's letter -- their branches would be "in the corpus" -- while
    making TA9's signal worthless: the baseline would freeze pre-promotion
    text, and the peer's later conversion would then show their own intended
    promotions as diffs indistinguishable from conversion regressions. See
    S3C for the staged-per-band capture mechanism this condition depends
    on. -->
    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md § 2), 2026-07-29: precision on the settle point named
    "M3/M7/M10" above. -->
    **The settle point is `M10`, specifically -- not `M3`.** The peer's three promoting rows do
    not land together: `M3` (`guard_plumbing_and_loops.py`) is in flight now (wave 1 remainder);
    `M7` (`guard_head_tail_rewrite.py`) is wave 2; `M10` (`guard_grep_via_bash.py`,
    `guard_multiprobe_banner.py`, and `guard_plumbing_and_loops.py` a SECOND time) is wave 3.
    Because `M10` touches `guard_plumbing_and_loops.py` again, capturing after `M3` would baseline
    a half-promoted message and then diff a fully-promoted one against it -- worse than capturing
    before `M3` at all. Settlement is not inferred from a module being touched; it is signalled by
    a one-line note the peer publishes to the reconciliation thread when `M10` lands.
    <!-- Reconciliation close (archive/memos/2026-07-29-typed-alternative-record-band-
    reconciliation-close.md § 4), 2026-07-29: provenance of the peer's M15 commitment once
    their plan closes. -->
    **The peer's conversion-to-records row (`M15`) is deferred, named-reason, and its plan closes
    without it.** `M15` cannot start until this plan's `S1` lands, and `S1` is gated on a PM
    authorization neither session controls -- so the peer's merged plan defers `M15` with that
    named reason and closes out without it, deliberately, so their close-out is not held hostage
    to this plan's gate and their own waves are not misreported as unfinished. The commitment does
    not lapse with that close-out: it is recorded in the peer's plan task spine as a deferred row
    naming `S1` as its unblock condition, and in the reconciliation thread
    (archive/memos/2026-07-29-typed-alternative-record-band-reconciliation-close.md § 4; the
    four-document thread was archived on settlement -- input, response, reply, close) -- so a closed
    peer plan is not misread as a withdrawn commitment. This plan's extractor-deletion condition
    (both halves, above) is unchanged by the peer's close-out.
    Asymmetry priced in by the peer session: condition (a) cannot start on
    their side until this plan's S1 pins the import path and field set --
    until then they have nothing to convert to. So S5's critical path runs
    through S1, and the sooner S1's signature lands the sooner the peer's
    half of condition (a) can start.
    <!-- Reconciliation response § 5, 2026-07-29: sequencing correction that
    moves this row later. The merged plan's waves 2-3 are ALREADY rewriting
    message text in the exact modules this plan wants converted (merged-plan
    rows M3 guard_plumbing_and_loops.py, M7 the extracted head/tail rewrite,
    and M10 the ladder promotions across guard_grep_via_bash.py,
    guard_multiprobe_banner.py, guard_plumbing_and_loops.py). Those rows are
    PM-ratified and in flight. If this plan's type landed mid-flight, the
    peer session would be converting messages it is simultaneously
    rewriting -- the same message authored twice. Agreed shape: the peer
    session's promotions land against PROSE in their current waves, and their
    conversion to records is a FOLLOW-ON row, opened once S1 pins the
    signature -- so the peer's half of condition (a) completes AFTER their
    current waves, not during them. This is the same shape this plan's own
    Out-of-scope section already prescribes from the other direction: a
    promotion lands on top of the record where the seam has reached a guard,
    against prose where it has not, because that is cheaper than blocking the
    ladder on a plan still being written. Known schedule dependency, not an
    open one. -->
    Delete the extractor rather than retaining it behind a flag. Retention is
    how the false-positive class survives.
    Key rung by (message, caller class) per TA4 — the same text is a real offer
    to the EM and a dead end to a confined subagent, and a single-keyed grade
    cannot express that.
    Handle T12 explicitly: guard_inprocess_search returns DENY as its success
    shape, so grade on record content, never verdict class, or the
    best-behaved guard in the package reports as a hard refusal.

    CONSOLIDATION: the **completion condition is unchanged in substance and simpler in
    mechanism.** Condition (a) -- "both bands' guards construct records" -- was to be signalled
    by the peer with "a NAMED TEST asserting that no guard module in its ownership constructs
    a message envelope by the prose path (a test, not a report, so the signal cannot rot)."
    The named-test requirement STANDS and is the right artifact; what changes is that it is
    one test over every guard in the package, authored once by this session, rather than two
    tests scoped by ownership. Condition (b) -- differential-render baseline green across all
    message branches -- is unchanged.
    The M15 provenance block below (the peer's deferred row, its unblock condition, its
    survival past a plan close-out) is now historical: M15 is un-deferred and absorbed into
    S4. Read it as the record of why the two plans were sequenced as they were, not as a live
    dependency. Likewise "the sooner S1's signature lands the sooner the peer's half of
    condition (a) can start" -- the critical path still runs through S1, but there is no
    second party waiting on it.
    Everything else binds unchanged, and these are the parts that matter: delete the extractor
    rather than retaining it behind a flag; key rung by (message, caller class) per TA4; and
    handle T12 explicitly, because `guard_inprocess_search` returns DENY as its SUCCESS shape,
    so grading on verdict class would report the best-behaved guard in the package as a hard
    refusal.
- id: S6
  title: Record the seam and the audience rule in the shared threat-model wiki
  change_kind: doc-edit
  surface: coordinator/docs/wiki/bash-guard-threat-model.md
  queue_scope: project
  deferred: false
  body: |
    The wiki is the surface both sessions read, and the audience rule is a
    doctrine change with reach beyond this package — it says what a guard may
    offer an IC versus an EM.
    State the reasoning, not just the rule: why two gate designs failed, why
    the medium rather than the content was the problem, and the general test
    (a gate is judged by which shipping rows it fails on landing day).
    Framing (the Director of Engineering/eng-director review, F11, minor): do not write the audience
    rule up as free-standing new doctrine. § Two bands, and the shared-helper
    seam between them already carries the rules this plan's cross-band shape
    depends on, and its own closing paragraph already anticipates this exact
    seam ("a message-composition seam where a guard supplies a typed
    alternative rather than free prose makes a rung-C message unwritable").
    Amend that existing closing paragraph IN PLACE, promoting its prediction
    to a description of what shipped, and state the audience rule as an
    extension of the shared-helper rule rather than a new section. One page,
    one argument.
    Follow-on check (the Director of Engineering/eng-director review, F8, major): record whether the
    DoE-side hook scripts (coordinator/hooks/scripts/) can import from
    claude-klabauter at hook runtime. If they can, they are a named follow-on
    population for the record type; if they cannot, that is a fleet-shaped
    gap worth stating in this same wiki edit rather than discovering later.
    Percolation check before writing: this wiki page must stand alone for a
    reader who has the plugin and none of our substrate. No bare decision-record
    ids, no dates-as-changelog, no citations to non-seed pages.

    Follow-on note (band enumeration, 2026-07-29): the wiki's own band
    criterion (§ Two bands) and the narrower engineering-internal
    `CONFINEMENT_HARD_DENIES` set (`test_hard_denies_precede_rewrites.py`)
    answer different questions -- the wiki cut is binary band membership,
    the narrower set is scoped to guards whose purpose is confining a
    caller against adversarial evasion -- but nothing on the page states
    that distinction, and resolving it for S4's three ambiguous modules
    (`guard_grep_via_bash.py`, `guard_multiprobe_banner.py`,
    `guard_plumbing_and_loops.py`) cost a source-level round-trip that a
    one- or two-sentence addition to § Two bands would have made
    unnecessary. Worth closing in the doctrine rather than re-deriving.
```
