# subagent_sandbox — pinned contract

This package is claude-klabauter's engine half of DR-047's contract-vs-engine split for the surviving
subagent-sandbox provision/report seam. It formerly also carried the two-tier PreToolUse
write-confinement DENY enforcement; DR-058 removed that enforcement as friction-over-EM-intent
(the hard-deny splice was excised from `coordinator_core.write_guards.engine` in commit
0998c6a6, and the deny-only machinery — `Decision`, `evaluate`, `evaluate_payload_json`,
`to_hook_output`, `__main__.py`, and the `confined`/`exempt`/`sanctioned_dirs` policy fields —
was gutted from this package in lockstep). This note now pins only the surviving
provision-and-emit contract below.

Example-doctrine-repo (`~/.claude`, source repo `example-doctrine-repo`) owns the **policy contract**
(`coordinator/subagent-sandbox-policy.yaml`) and the **spawn-hook plumbing** that invokes this
package; claude-klabauter owns the **provisioning engine** itself. A drift on example-doctrine-repo's side of this
surviving surface is a detectable break here, not a silent divergence.

## Provision-and-emit contract

The package's live seam: a spawn-time provisioner,
`python3 -m coordinator_core.subagent_sandbox.provision_report`, that creates a per-session
run-report sidecar doc for agent/subagent types the policy has opted into `report_sidecar` and
emits the doc's path so the spawning caller can hand it to the subagent.

**stdin** — a PreToolUse-shaped JSON payload (the same shape resolve_effective_types is built to
read regardless of caller):

| Field | Notes |
|---|---|
| `agent_type` | Top-level field; primary leg of the OR-resolver eligibility check |
| `subagent_type` | Not read directly off the payload — resolved via `resolve_effective_types(payload, git_root)`, the shared OR-resolver back-pointer helper in `engine.py` (imported verbatim, never re-derived) |
| `session_id` | Required; missing/falsy → no provisioning (empty stdout) |
| `agent_id` | Consumed indirectly, via `resolve_effective_types` |
| `provision_key` | Optional; SUBSUME deterministic-path mode — see below |
| `type` | Optional; SUBSUME template-type axis — see **`--type` axis / template registry** below |
| `plan_path` | Optional; plan-derivable `report_sidecar` home for four named `subagent_type`s only — see **Plan-derivable `report_sidecar`** below |

**`session_id` is dual-purpose:** it is both (a) the path-sanitized directory leaf under
`state/subagent-share/` (unchanged, pre-existing behavior) AND, as of the `lead_session_id`
SUBSUME addition below, (b) the RAW (unsanitized) value stamped verbatim into the provisioned
doc's frontmatter as `lead_session_id` — the requesting EM/lead's session id, distinct from the
spawned agent's own `agent_id` (see § Starter-doc scaffold below).

**Eligibility + label resolution:** provisioning fires when `agent_type in policy.report_sidecar`
OR `subagent_type in policy.report_sidecar` (membership test against the same `report_sidecar`
policy list — see key below). The label used in the output filename is `agent_type` if *it* was
the matching leg, else `subagent_type` — never both, never a synthesized combination.

**stdout / exit code:**

- **Eligible, no `provision_key`** — exactly one line of JSON on stdout:
  ```json
  {"report_sidecar": "state/subagent-share/<session-id>/<label>-<8hex>.md"}
  ```
  then `exit 0`.
- **Eligible, `provision_key` present** — exactly one line of JSON on stdout:
  ```json
  {"report_sidecar": "state/subagent-share/<session-id>/<provision_key>.md"}
  ```
  then `exit 0` — see **Optional `provision_key` (deterministic path mode)** below.
- **Ineligible, or any fail-open condition** (missing `session_id`, no git root, unsanitizable
  segment, parse failure, unexpected exception) — empty stdout, `exit 0`. This module must never
  brick a spawn.

**Path template:** `state/subagent-share/<sanitized-session-id>/<sanitized-label>-<8hex-nonce>.md`
when no `provision_key` is supplied, or `state/subagent-share/<sanitized-session-id>/<sanitized-provision_key>.md`
when one is, repo-relative to the resolved git root.

**Optional `provision_key` (deterministic path mode):** the stdin payload may carry an optional
top-level `provision_key` string. When present and truthy, the engine sanitizes it with the SAME
single-segment sanitizer used on `session_id`/label (whitelist `[A-Za-z0-9._-]`, then reject
`""`/`"."`/`".."`; a reject aborts provisioning — empty stdout, fail-open, same as any other
unsanitizable segment) and uses the sanitized result as the path leaf, in place of the
`<label>-<8hex-nonce>` leaf, giving a deterministic, caller-addressable path instead of a nonced
one. The doc is still opened via `open(path, "x")` first; on `FileExistsError` this mode does
**not** retry with a fresh nonce (there is no nonce to retry) — it treats the collision as an
**idempotent re-open**: the existing doc is left completely unchanged and its path is returned
exactly as if the create had succeeded. This is what a chunk re-dispatch wants — re-provisioning
against the same `provision_key` must hand back the SAME doc with its accumulated content intact,
never silently truncate or overwrite it. When `provision_key` is absent or falsy, behavior is
unchanged from the nonce-leaf path above.

**Grammar pin (claude-klabauter-owned) — `provision_key` is a single flat segment:** `provision_key` MUST be
one `[A-Za-z0-9._-]` token, flat under the session directory — the sanitizer drops `/` (and `\`)
silently rather than treating it as a sub-path separator, so a caller addressing something
inherently hierarchical (e.g. a `<plan-slug>/<chunk-id>` pair) MUST pre-flatten it into a single
token (e.g. join the components on `.`) *before* passing it as `provision_key`. This flattening is
the caller's responsibility, not this module's — the module only ever sanitizes and writes one
segment, it does not construct nested directories from a compound key. As with the
`report_sidecar` policy key below: **example-doctrine-repo owns the VALUES/call-sites** (which callers pass a
`provision_key`, and what they derive it from); **claude-klabauter owns this GRAMMAR** (single-segment
shape, the sanitizer, the idempotent-reopen semantics, the path template).

**Starter-doc scaffold (superset frontmatter + body):** on a fresh create (either path mode), the
engine writes a starter doc with this exact frontmatter field set, in this order. This exact shape
— frontmatter plus the three body sections below — is also the **frozen legacy run-report shape**:
the byte-for-byte output produced when the stdin payload carries **no `type` key at all** (see
**`--type` axis / template registry** below for what changes when `type` IS present):

```yaml
---
status: open
agent_type: <agent_type>
spawned_at: <ISO-8601 UTC timestamp>
lead_session_id: <requesting EM/lead session_id, raw (unsanitized), or literal null>
divergence:
  diverged: false
commits: []
dispatch_feed: null  # forward-declared, INERT until pcli-04 emitter
---
```

followed by three body sections, in this order:

```markdown
## Run notes

## Observations

## Exit interview

- What did you have to work out that the brief could have told you?

- What did you grep, read, or probe that turned out to be a dead end — and what were you actually looking for?

- Where did your tool access, permissions, or output contract fight you? What was missing that isn't deliberately withheld from this role — a guard denial is not a gap.

- Anything you wanted to say and had nowhere to put?
```

`Run notes` and `Observations` are empty, free-form scratchpad sections; `Exit interview` is
pre-populated with four fixed questions verbatim at provision time — the agent answers under each
question, it does not invent or reorder the prompts.

`status`/`agent_type`/`spawned_at`/`divergence` are the original narrow field set; `commits: []`,
`dispatch_feed: null`, and `lead_session_id` are SUBSUME additions that make this scaffold a
**superset** of what the flight-recorder and example-doctrine-repo's spawn-hook migration expect to find on a
freshly-provisioned doc — `commits` is a plain accumulator list (empty at spawn time, appended to
over the agent's lifetime), `dispatch_feed` is forward-declared but **inert**: the engine never
writes to it past this `null` placeholder until the pcli-04 emitter lands, at which point it
becomes a live field, not a schema change, and `lead_session_id` is the raw (unsanitized)
`session_id` value off the stdin payload — the REQUESTING EM/lead's session id, stamped verbatim
(falls back to the literal `null` only when the underlying value is absent, which in practice
never happens via `_provision` since `session_id` is required for eligibility in the first
place). `lead_session_id` is a **distinct identity from `agent_id`**: `agent_id` (consumed via
`resolve_effective_types`, never itself written into frontmatter) identifies the SPAWNED agent;
`lead_session_id` identifies who dispatched it — the two must never be conflated by a downstream
reader. **example-doctrine-repo owns the authoritative run-report doc-schema doctrine** (what each field means, how
`divergence`/`commits`/`dispatch_feed`/`lead_session_id` get consumed downstream); **the engine
only writes these starter placeholders conforming to that doctrine** — it does not interpret or
validate them past writing the literal scaffold above. This whole contract (provision_key grammar
+ this superset scaffold) is the wire-for-wire target example-doctrine-repo hard-binds their spawn-hook +
flight-recorder migration to — a field rename, reorder, or removal here is exactly as much
breaking drift as a `report_sidecar` policy-key change (see § Drift detection below).
`divergence` is object-typed (`{diverged: <bool>, summary?, detail?}`, `diverged` required) per
Example-doctrine-repo's `run-report.schema.json` — the block-style `divergence:\n  diverged: false` placeholder
asserts "no divergence yet"; an empty array would fail example-doctrine-repo's object-schema validation, so do not
revert it to a list. **Emit block style, never flow style (`{diverged: false}` on one line)** —
`coordinator_core.frontmatter.schema_validate.parse_yaml` (this repo's restricted YAML parser,
which is what actually gates schema validation downstream) does not support flow-style mappings
and parses one as a raw string instead of a dict, silently tripping the object-shaped check on
every provisioned sidecar (see `cross-repo/inbox/2026-07-25-example-doctrine-repo-em-provision-report-
divergence-flow-style.md`).

**Forward-binding constraint on the pcli-04 emitter — commit-phase pathspec provenance.** When the
emitter lands and `dispatch_feed` goes live, an emitted Workflow MAY interleave
`coordinator:git-commit-agent` phases between executor waves: example-doctrine-repo took the "not yet dispatchable"
banner down 2026-08-12 (DR-153, example-doctrine-repo `79be06759`), discharging SC-DR-021's consumer-repo
condition, and their `execute-plan` RACI now names the EM Accountable for every commit with the
Responsible keystroke delegable. **Every emitted commit phase's pathspec MUST carry real
provenance** — the preceding wave's executor-reported touched-file set, or the chunk's
`surface:`/`writes:` list off the plan spine (`plan-tasks.schema.json` ≥ 1.7.0). Never a tree
survey, never an invented set. This binds harder here than in a consumer repo because SC-DR-021
population (c) is unchanged: **a path written by a raw Bash heredoc carries no session claim and is
denied at runtime.** An emitted commit phase whose pathspec covers engine-authored state will be
refused, correctly. The executor-wave case works only because executors author via `Write`/`Edit`.
Source: `cross-repo/inbox/2026-08-12-example-doctrine-repo-em-emitter-emits-commit-phases.md`.

**`--type` axis / template registry (SUBSUME):** the CLI grows an optional `--type` argument
(`choices=["run-report", "review-findings", "assessment", "staff-eng-review"]`, `default:
run-report`), and `_provision()`'s stdin payload grows a matching optional top-level `type`
string field read the same way as `provision_key`. The **frontmatter field set is unchanged
across every type** — same seven fields, same order, as the scaffold above; only the **body**
varies by type. Every template body ends with the same universal `## Exit interview` section
(unchanged, four fixed questions, verbatim — see scaffold above) regardless of type.

| `type` | Body shape |
|---|---|
| *(absent)* | **Frozen legacy shape** — `## Run notes` / `## Observations` / `## Exit interview`, byte-for-byte identical to the pre-SUBSUME scaffold above. This is the path a payload with no `type` key takes — chiefly example-doctrine-repo's `fan-out-dispatch.py`, which calls `_provision()` directly (not the CLI's `main()`) and predates this axis entirely. |
| `run-report` (explicit, or the CLI's own `--type` default) | Legacy body **plus** a `## Divergence from plan` body section (a prose companion to the existing `divergence` frontmatter field — no new frontmatter) and a `## Completion` section carrying a single markdown checkbox line as a grep-able completion marker (the frontmatter `status:` field remains the authoritative status; the checkbox is a human-facing marker layered on top, not a schema change). |
| `review-findings` | `## Findings` — one entry per finding, each with a severity, an accepted/rejected/deferred disposition, and a rationale. |
| `assessment` | `## Questions` — one entry per question, each a Q/A pair. |
| `staff-eng-review` | `## Verdict` then `## Rationale`, each its own section. |

**Precedence:** when the stdin payload already carries its own `type` field, that value wins —
the CLI's `--type` default (`run-report`) is only injected into the payload by `main()` when the
payload doesn't already set one. A direct `_provision()` caller (like `fan-out-dispatch.py`)
that never sets `type` gets the frozen legacy shape, never the enhanced `run-report` template —
this is the load-bearing back-compat guarantee this axis was built against: **a payload with no
`type` key must produce output byte-for-byte identical to what this module emitted before this
axis existed.** An unrecognized `type` string (a value outside the four above) fails open into
the `run-report` template rather than raising, consistent with this module's fail-open posture
everywhere else.

**example-doctrine-repo owns the VALUES** (which callers pass which `type`, and what each template's prose
questions mean downstream); **claude-klabauter owns the GRAMMAR** (the argument name, the choices set, the
per-type body shape, and the absent-key/legacy-shape back-compat guarantee) — the same example-doctrine-repo/claude-klabauter
split as every other axis in this contract. A `type`-shaped change on either side (new type
added, a template's body reshaped, the back-compat guarantee weakened) is exactly as much
breaking drift as a `report_sidecar` policy-key change (see § Drift detection below).

**Sanitization rule (single-segment, applied independently to `session_id`, to the effective
label, and — when present — to `provision_key`):**

1. **Whitelist** `[A-Za-z0-9._-]` — every other character (including `/` and `\`) is *dropped*,
   never escaped or percent-encoded.
2. **Reject** the whitelisted result if it is exactly `""`, `"."`, or `".."` — the whitelist
   alone preserves dots, so a bare traversal segment must be caught explicitly *after*
   whitelisting, not folded into the character class. A reject on either segment aborts
   provisioning (empty stdout).

This sanitizer checks a single path *segment* in isolation and never calls `Path.resolve()` — a
narrower, independent scope from the resolver helpers in `engine.py`.

**Nonce rule (no-`provision_key` path only):** an 8-hex-character `secrets.token_hex(4)` suffix
disambiguates concurrent provisions under the same session+label. The doc is opened with Python's
`"x"` mode (O_EXCL — create-or-fail, never overwrite); on a `FileExistsError` collision the
provisioner draws exactly one fresh nonce and retries once, uncushioned thereafter. This rule does
not apply when `provision_key` is supplied — that path has no nonce and instead idempotently
re-opens on collision, per **Optional `provision_key` (deterministic path mode)** above.

**`report_sidecar` policy key:** a top-level list in the policy YAML — `list[str]` of
`agent_type`/`subagent_type` values, exact-string-match membership, no globbing. **example-doctrine-repo owns the
VALUES** (which agent/subagent types opt into a report sidecar); **claude-klabauter owns the GRAMMAR** (the
key name, the match semantics, the path template, and the sanitizer) — the same split as the rest
of this contract. (DR-058 removed this package's other policy fields — `confined`, `exempt`,
`sanctioned_dirs` — along with the PreToolUse DENY enforcement that consumed them;
`report_sidecar` is the sole surviving policy key. `load_policy` reads `report_sidecar` off the
same YAML file regardless of whether the removed keys are still present in it — unknown/surplus
keys are silently ignored, never a parse failure.)

**Rationale — O_EXCL, not `locked_write.locked_rmw`:** the provision doc is created via a bare
`open(path, "x")` (O_EXCL create-once-or-skip), never via `locked_write.locked_rmw`.
Provisioning is a first-create with clash-detection on a brand-new per-invocation filename (the
nonce), not a read-modify-write of an existing shared file — there is no prior state to read,
lock, and rewrite, so `locked_rmw`'s flock-guarded RMW semantics do not apply here; O_EXCL's
create-or-fail atomicity is the correct (and cheaper) primitive for this shape.

**This whole package is ONE pinned contract surface.** § Drift detection below covers this
provision-and-emit contract in full. A `report_sidecar`-shaped change on example-doctrine-repo's side (key rename,
value-grammar change, emit-shape change) is a breaking drift. The SUBSUME additions above — the
optional `provision_key` grammar (single-segment shape, sanitizer reuse, idempotent-reopen
semantics), the superset starter-doc field set (`commits`/`dispatch_feed`/`lead_session_id`
alongside the original `status`/`agent_type`/`spawned_at`/`divergence`), and the `--type` axis / template
registry (the choices set, the per-type body shapes, and the absent-key/legacy-shape back-compat
guarantee) — are first-class members of this same pinned surface, not a separate or looser
contract: a change to any of them is exactly as much breaking drift as a `report_sidecar`-shaped
change.

## Plan-derivable `report_sidecar` for five named emitters (SUBSUME)

Additive refinement of the **provision-and-emit contract** above (canonical spec
`state/subagent-share/conductor/seam-adjudication.md` § 2.7, example-doctrine-repo, absorbed from G2's
D0/Z2). This is **not** part of the `contract_blocks` injection seam below — it changes the
*value* an already-eligible `subagent_type` resolves for `report_sidecar`, not the grammar of
a new key. It is documented here because it lands in the same engine function and collides in
the same file/function neighbourhood, per that spec's own framing.

**Scope — exactly five `subagent_type` values, and only when `plan_path` is present:**

| `subagent_type` | lens suffix |
|---|---|
| `coordinator:prior-art-checker` | `prior-art-check` |
| `coordinator:plan-coverage-checker` | `plan-coverage-check` |
| `coordinator:external-pattern-checker` | `external-pattern` |
| `coordinator:docs-checker` | `docs-check` |
| `coordinator:plan-reviewer` | `plan-review-check` |

**Membership test — plan-scoped-durable, not "does it review".** The discriminator is whose
identity the emitted finding-set carries. Every value above emits findings keyed to the PLAN and
consumed against that plan by `review-integrator`, routinely in a later session — for which a
session-keyed home is precisely where the next reader will not look. The Opus reviewer PERSONAS
are excluded because their output is a session judgment on work in flight, keyed to the session
that asked — not because "review" appears in the role. `coordinator:plan-reviewer` (example-doctrine-repo DR-133,
the M-tier rung between the mechanical pre-flights and the personas) sits on the durable side and
was added 2026-08-06 on that reading; see `cross-repo/archive/2026-08-05-example-doctrine-repo-em-plan-reviewer-lens-registration.md`
(that `coordinator:plan-coverage-checker` already resolves to a `review-findings`-shaped
`report_type_map` entry — the same template family `plan-review-check` joins — is the load-bearing
precedent, pinned in-repo by `_G2_LENS_EXPECTED_TEMPLATE_TYPES` in
`coordinator_core/subagent_sandbox/tests/test_g2_c13_lens_template_fit.py:71-77`).

**Lens spelling — `plan-review-check`, deliberately not `plan-review`.** The sender proposed the
bare `plan-review`; claude-klabauter owns the lens spelling (see the example-doctrine-repo/claude-klabauter split below) and narrowed it
on two grounds. First, `plan-review` is ALREADY a live `kind:` value on a different artifact class
— persona plan reviews written as `<stem>.review.md`, enumerated in
`coordinator_core/ops/docgen/templates/review.json` — so the bare token would name two disjoint
sets depending on whether a reader keys off filename suffix or off frontmatter `kind:`. Second,
`-check` is the established suffix for every checker-tier emitter in this map, and example-doctrine-repo's own sizing
ladder places `plan-reviewer` on the checker side of it (XS/S grab-n-go → **M plan-reviewer** →
L/XL named persona). The rename costs the sender nothing: the dispatching skill consumes the
RETURNED `report_sidecar` path and never re-derives the formula.

For one of these five `subagent_type`/`agent_type` values (matched against the SAME
`effective_label` the ordinary eligibility check already resolves — `agent_type` if it was the
matching `report_sidecar` leg, else `subagent_type`), when the stdin payload ALSO carries a
non-empty `plan_path` string, `_provision` writes the sidecar to the deterministic path

```
state/plan-sidecars/<plan-stem>.<lens>.md
```

instead of the ordinary session-keyed `state/subagent-share/<session_id>/` home, and emits that
path as `report_sidecar` exactly as normal — no third stdout key, no change to the emit shape.
`<plan-stem>` is `Path(plan_path).stem` (directory components are discarded by construction, so
a traversal-laden `plan_path` cannot smuggle a directory separator through this leg) passed
through the SAME single-segment sanitizer as `session_id`/label/`provision_key` (see §
Sanitization rule above). If the sanitized stem rejects to `None` (e.g. `plan_path` reduces to
`".."`), this leg is skipped entirely and provisioning falls through to the ordinary
session-keyed path — a malformed `plan_path` degrades gracefully, it never drops the sidecar.

**Every other case is completely unaffected:** a `subagent_type` outside the five above ignores
`plan_path` even if a caller sends one; and these same five agents' OTHER dispatch shapes that
carry no `plan_path` fall through to the ordinary session-keyed path unchanged. `plan_path` is
never required for eligibility; its absence is simply "use the existing home."

**This is the general plan-less rule, not one named dispatch's exception.** ANY dispatch of these
five emitters that has no governing plan on disk takes the session-keyed home — there is no
requirement that a plan exist, and no failure mode when one does not. Two families of caller hit
this, and both are correct by construction:

- `docs-checker`'s code-review dispatch, which reviews a diff rather than a plan (see D4 in the
  absorbed G2 plan).
- Run-scoped callers whose only identity is a synthetic label — e.g. Example-doctrine-repo's `/bug-sweep` Track C
  (`{run-id}-{chunk-name}`) and Phase 3.5 (`{run-id}-postfix`). These labels are scratch-directory
  names; they are NOT passed as `plan_path` and MUST NOT be. A caller that substituted such a
  label into `plan_path` would sanitize cleanly and land a run-scoped file in
  `state/plan-sidecars/`, which is an unreaped-by-design archive class — the wrong home for
  ephemeral, run-scoped output. The absence of a stem is the correct signal here, not a gap to
  fill.

<!-- The preceding block was widened 2026-07-25 after example-doctrine-repo-em read the older wording — which
     illustrated the plan-less case with docs-checker's code-review dispatch alone — as leaving
     plan-less provisioning an unstated case, and asked whether their two /bug-sweep sites were
     silently broken. They are not. The rule was always general; only the illustration was narrow.
     Reply memo: example-doctrine-repo cross-repo/inbox/2026-07-25-claude-klabauter-em-planless-dispatch-sidecar-answer.md -->

Regression coverage for the plan-less fallback:
`coordinator_core/subagent_sandbox/tests/test_provision_report.py`
`test_docs_checker_without_plan_path_keeps_session_keyed_home`.

**Collision handling — idempotent re-open, not a nonce retry.** Like the `provision_key`
deterministic-path mode above, this path has no nonce to fall back on: the doc is opened via
`open(path, "x")`; on `FileExistsError` the engine treats it as an idempotent re-open — the
existing doc is left completely unchanged and its path is returned exactly as if the create had
succeeded. Archiving a STALE prior sidecar (rename-don't-delete, feeding the
false-positive-arbitration feedback loop these agents' own doctrine describes) stays an
agent-side concern — this engine leg never renames or deletes an existing plan-sidecar file.

**example-doctrine-repo owns the VALUES** (which dispatching skill/command passes `plan_path`, and what it
derives it from — the skill/command consumes the RETURNED `report_sidecar` path, it never
re-derives the `<plan-stem>.<lens>.md` formula itself); **claude-klabauter owns the GRAMMAR** (the
`plan_path` key name, the five-emitter/lens map, the stem-derivation + sanitization rule, the
`state/plan-sidecars/` path template, and the idempotent-reopen semantics) — the same example-doctrine-repo/claude-klabauter
split as every other axis in this contract. A change to either side (a sixth emitter added, the
lens spelling changed, the path template moved) is exactly as much breaking drift as a
`report_sidecar`-shaped change (see § Drift detection below).

## `contract_blocks` / `injected_prompt_blocks` — dispatch-time prompt-block injection (SUBSUME)

Additive second seam alongside the provision-and-emit contract above, per the canonical
spec (`state/subagent-share/conductor/seam-adjudication.md` § 2.3, example-doctrine-repo). Where
`report_sidecar` provisions a per-session doc, this seam assembles a **pre-resolved chunk
of dispatch-prompt text** the example-doctrine-repo spawn-hook appends verbatim to a subagent's brief — the
engine-side collapse of what used to be N independently-pasted, sentinel-synced copies of
the same block across N agent `.md` files.

**Input axis.** The stdin payload the hook already feeds this module grows an optional
top-level `contract_blocks` key: a JSON list of `coordinator/snippets/<name>.md` block
names, in emission order. **example-doctrine-repo resolves this list**, example-doctrine-repo-side, from
`subagent-sandbox-policy.yaml`'s `contract_blocks:` map — this module never re-reads that
policy file itself, only the already-resolved list the caller supplies on the payload.
Absent or empty → no assembly attempted, no output key emitted.

**Output.** A second, additive stdout key alongside `report_sidecar`:

```json
{"report_sidecar": "<path>", "injected_prompt_blocks": "<assembled text>"}
```

`injected_prompt_blocks` is always a single pre-joined **string**, never a list — the example-doctrine-repo
hook stays a dumb transport, appending it verbatim rather than iterating or reformatting
it. Blocks are joined in input order with exactly `"\n\n"`; the engine adds no wrapper,
header, or delimiter text of its own around any individual block.

**`header_style`-aware extraction.** Each block's body is resolved from its canonical
`coordinator/snippets/<name>.md` source under `git_root`, using the SAME `header_style`
metadata `snippets/registry.toml` already carries for `verify-snippet-sync` (this module
reads it via the shared `coordinator_core.snippet_sync.registry` reader — it does not
duplicate a TOML parser):

| `header_style` | Extraction |
|---|---|
| `sentinel-embedded` | `coordinator_core.frontmatter.sentinel_blocks.extract_block` on the file's own internal BEGIN/END markers (`entry["sentinel_begin"]`/`entry["sentinel_end"]`) |
| `fixed-2-line` | whole file body minus its leading 2 header-comment lines |
| `fixed-2-line-strip-end-sentinel` | as `fixed-2-line`, additionally dropping any line equal to `entry["sentinel_end"]` |
| `comment-block` | whole file body minus its leading `<!-- -->` comment block (and the blank line separating it from the body) |

A uniform sentinel-only extraction is wrong here — it silently returns nothing for every
`fixed-2-line` / `comment-block` block (`reviewer-calibration`, `guard-encounter-preamble`,
`quota-self-detect-preamble`, and the two scan-discovered preambles). An unrecognized
`header_style` value is an assembly failure (see degraded-mode below), never a best-effort
guess.

**Closed placeholder set.** After extraction, each block body may reference exactly three
`{{…}}` placeholders — no others, no conditional logic, no expression language:

| Placeholder | Resolved from |
|---|---|
| `{{kind}}` | the same `type`/`--type` value this contract already resolves elsewhere (`TEMPLATE_TYPES[0]`, i.e. `"run-report"`, when absent) |
| `{{sidecar_path}}` | the `report_sidecar` value computed in the SAME provisioning call (empty string when no sidecar was provisioned) |
| `{{subagent_type}}` | the effective subagent type resolved via `resolve_effective_types` for the same payload |

Resolution happens engine-side, after extraction, before concatenation. An unresolved
`{{foo}}` (any placeholder name outside this set) is an assembly failure — it must never
leak literal braces into an assembled dispatch prompt.

**Degraded-mode contract — all-or-nothing, independent of `report_sidecar`.**

1. If *any* named block fails to load, fails to resolve a known `header_style`, fails
   extraction, or has an unresolvable placeholder, the engine omits `injected_prompt_blocks`
   from stdout **entirely** rather than emitting a partial assembly — a consumer cannot tell
   which half of a partial contract it's missing, so a partial contract is worse than none.
   Diagnostics go to stderr only.
2. Contract-block assembly and `report_sidecar` provisioning are **independent legs**:
   failure in either must never suppress the other, and neither ever blocks or denies a
   spawn. `main()` isolates the assembly call in its own `try`/`except` for exactly this
   reason — an unexpected exception in this leg degrades to "no key emitted", never a
   nonzero exit or a lost `report_sidecar` offer.

**Consumer-agnostic engine.** This module carries no persona name, no plan-pipeline agent
name, no hardcoded block-name list, and no baked template-type literal — block names arrive
on the input axis, bodies come from disk, and the `{{kind}}` fallback reuses this module's
pre-existing generic `TEMPLATE_TYPES[0]` CLI default rather than any consumer-specific
value. A caller-family name (a reviewer-persona slug, an emitter subagent_type) must never
appear in this module's source.

**example-doctrine-repo owns the VALUES** (which `subagent_type`s carry a `contract_blocks` entry, which
block names, in what order); **claude-klabauter owns the GRAMMAR** (the stdin key name, the
`header_style` dialect table, the placeholder set, the join rule, the all-or-nothing
degraded-mode contract) — the same split as every other axis in this contract. This
grammar is pinned in lockstep with example-doctrine-repo's `coordinator/subagent-sandbox-policy.yaml`
`contract_blocks:` key comment — a change on either side without a matching change on the
other is exactly as much breaking drift as a `report_sidecar`-shaped change (see § Drift
detection below).

### Axis inventory (machine-checked)

`test_provision_report_contract_axis_conformance.py` parses the list below (exact literal
form: `- <kind>: `<name>`` per line, `<kind>` one of `stdin`/`cli`/`stdout`) and asserts
equality, **in both directions**, against every payload key `provision_report.py` actually
reads, every non-plumbing CLI flag it accepts, and every key it writes to stdout. Adding an
axis to the code without a matching row here (or vice versa) fails that test — this is what
makes the example-doctrine-repo/claude-klabauter grammar coupling in this file discharge-by-test rather than
discharge-by-remembering. `--policy`/`--cwd` are deliberately excluded: pure invocation
plumbing (where to find the policy file / what cwd to resolve the git root from), not part
of the example-doctrine-repo<->claude-klabauter wire grammar this file pins.

- stdin: `agent_type`
- stdin: `subagent_type`
- stdin: `agent_id`
- stdin: `session_id`
- stdin: `provision_key`
- stdin: `type`
- stdin: `contract_blocks`
- stdin: `plan_path`
- cli: `type`
- stdout: `report_sidecar`
- stdout: `injected_prompt_blocks`

## `bash_policy` policy key — declaration only, no enforcement leg (SUBSUME)

Additive third policy field alongside `report_sidecar` above, read by the same
`load_policy` loader in `engine.py`. **This is a DECLARATION path only** — `Policy.bash_policy`
is loaded and exposed; this package makes no ALLOW/DENY decision from it and never re-splices
into `write_guards.engine` (that splice was DELETED by DR-058, not disabled — see module
docstring negative-spec). The consumer of this data is `coordinator_core.bash_guards`, wired by
a separate change; `load_policy` and `Policy` know nothing about that consumer.

**Key name and location:** `bash_policy`, a top-level key in the policy YAML, sibling of
`report_sidecar` / `contract_blocks` (documented above).

**Shape:** a mapping whose keys are EXACT `subagent_type` strings (same key style as
`report_sidecar`'s membership values — exact match, no globbing) and whose values are mappings
describing that agent type's allowed Bash surface. **Bash only — no write/edit leg, ever**; a
`bash_policy` row must never be read as authorizing anything outside Bash-tool scope.

**Fail-open semantics (loader only):** lookup-miss, an absent `bash_policy` key, a non-dict
policy file, or a malformed value must never raise and must never block a spawn — same posture
as every other field this loader reads. Concretely: an absent key or non-dict top-level value
resolves to `{}`; a per-key value that is not itself a mapping is dropped from the resolved
`bash_policy` dict rather than failing the whole load (a sibling well-formed row still resolves).
This mirrors `report_sidecar`'s existing wrong-typed-value handling — the load never raises and
never yields anything less permissive than "no policy for that agent type," which downstream is
authorization-neutral at this layer (the loader makes no ALLOW/DENY decision).

**Generality:** a second (or Nth) `subagent_type` row is a plain additional map entry — no code
change, no new key, no schema bump.

**example-doctrine-repo owns the VALUES** (which `subagent_type`s carry a `bash_policy` row, and what each row's
allowed-surface shape means to the consumer that reads it); **claude-klabauter owns the GRAMMAR** (the key
name, the exact-match subagent_type keying, the Bash-only scope, and this loader's fail-open
contract) — the same example-doctrine-repo/claude-klabauter split as every other axis in this contract. A `bash_policy`-shaped
change on either side (key rename, value-grammar change, fail-open weakened) is exactly as much
breaking drift as a `report_sidecar`-shaped change (see § Drift detection below).

## Drift detection

This is a **pinned** contract, not a living spec re-derived per session. If example-doctrine-repo's
`subagent-sandbox-policy.yaml` comment header changes the `report_sidecar` key name or its value
grammar, or the provision-and-emit stdin/stdout shape above changes — that is a **breaking drift**
against this note — the engine must be updated in lockstep, not silently left to fail-open past
the new shape. Treat any such change surfaced via cross-repo memo as a required update to this
file plus `engine.py`/`provision_report.py`, not an optional follow-up.
