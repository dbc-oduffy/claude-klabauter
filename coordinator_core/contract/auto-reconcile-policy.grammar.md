# `auto-reconcile-policy.yaml` grammar pin

Spec backlink: `docs/plans/2026-07-13-claude-klabauter-auto-reconcile-open-handoffs.md` § C9, DEC-1.
Boundary authority: `/Users/example-operator/X/example-doctrine-repo/docs/decisions/DR-047-example-doctrine-repo-claude-klabauter-boundary-redraw-contract-vs-e.md`
("example-doctrine-repo owns rules, claude-klabauter owns machine").

## Ownership

This grammar is the schema/shape **claude-klabauter ships and validates against**. The policy
**data** file itself, `coordinator/auto-reconcile-policy.yaml`, is **authored and owned by
Example-doctrine-repo** (the sibling `claude-central-em` repo) — claude-klabauter's `coordinator_core/reconcile/policy_loader.py`
reads it fresh on every call and never writes it. This mirrors the existing
`subagent-sandbox-policy.yaml` ← `coordinator_core/subagent_sandbox` precedent (DR-047
contract-vs-engine split): a threshold/data amendment on example-doctrine-repo's side is a policy-YAML edit,
zero claude-klabauter code change.

## Reader

`coordinator_core/reconcile/policy_loader.py:load_policy()`. Consumed by
`coordinator_core/reconcile/commit_reality.py` (C2, the DEC-1 three-signal matcher) and
`coordinator_core/reconcile/gate_eval.py` (C3, the unified gate evaluator).

## Top-level keys

| key | type | required | meaning |
|---|---|---|---|
| `three_signal` | mapping | yes | Tuning knobs for the DEC-1 three-signal shipped-ness bar (signal (a) commit-subject match, (b) named-deliverable-on-disk, (c) SHA-reachable-on-HEAD). Accepted as a mapping with OPTIONAL sub-keys (see below) — all sub-keys have code-side defaults, so an absent/empty `three_signal: {}` is valid and matches today's ratified behavior baseline. `policy_loader._validate_grammar` only checks the required top-level keys; these sub-keys are not independently type-validated by the loader, so adding/amending one is a example-doctrine-repo-side YAML data edit, zero claude-klabauter code change and zero re-validation. |

### `three_signal` optional sub-keys (2026-07-20 claude-central-em false-positive memo, Defect 2)

| key | type | default | meaning |
|---|---|---|---|
| `subject_match_min_tokens` | int | `2` | Minimum number of DISTINCT derived noun tokens that must appear in a candidate commit's subject for signal (a) to count as a match. Raised from the original "any single token" bar — a lone incidental path-component token (e.g. `ops`) was previously sufficient to select a wholly unrelated commit. Raising this further will drop some true positives on short-title handoffs; validate against the live `check_auto_reconcile` output before tightening. |
| `subject_match_extra_stopwords` | list of strings | `["ops", "core", "config", "plans", "docs", "state", "tests", "lib", "bin", "src", "schemas", "contract"]` | Additional noun-token stopwords unioned with the matcher's built-in set — structural/path-shape vocabulary that carries near-zero signal about what a handoff's actual deliverable is. |
| `deliverable_requires_file` | boolean | `true` | When true, signal (b) requires an existing FILE (or a glob with >=1 file hit) — an existing directory alone no longer counts as "deliverable present". Setting `false` restores the pre-fix directory-tolerant behavior. |
| `mechanical_commit_denylist` | list of strings | yes | Commit-subject prefixes/tokens that must NOT count as signal-(a) evidence even when they touch a scope path — guards against a `pickup:`/`session-init`/`memo:`/`handoff.transition`-family/frontmatter-mutation commit satisfying signal (a) without representing real completed work (the Staff Engineer #2, inverse-direction guard). Ratified initial content: `pickup:`, `reclaim(docs)`, `session-init`, `memo:`, `handoff.transition`-family subjects, frontmatter-mutation subjects. |
| `cross_handoff_attribution` | boolean | yes | When `true` (the ratified default), the matcher demotes a candidate `verdict: auto-ship` to `surface` whenever >1 open handoff's `scope` pathspecs overlap the candidate commit's touched paths — the fourth DEC-1 conservatism guard (the Staff Engineer review, finding index 2). Setting `false` disables the guard (not recommended; example-doctrine-repo-owned toggle for future tuning). |
| `dry_run` | boolean | yes | Default policy-level dry-run flag consumed by the `handoff.reconcile_open` op (C4) — **ratified default `true`** (first live pass is observation-only; example-doctrine-repo flips after reading the dry-run report). Distinct from the op's own per-call `dry_run` param, which may override this at invocation time. |

## Fail-closed contract (the reader's obligation, not a policy-file key)

The grammar pin governs the **shape** example-doctrine-repo authors against; the **fail-closed behavior on a
missing or malformed file** is a `policy_loader.py` reader-side obligation, not something the
YAML itself declares:

- File **absent** (the expected steady state pre-ratification) → the reader returns a
  conservative no-auto-ship policy (`dry_run: true`, `auto_ship_enabled: false`) with **no
  warning**. This is expected, not a defect — surfacing a warning every workday-start during
  the pre-ratification period would be noise.
- File **present but fails grammar validation** (missing required key, wrong type on a
  required key, invalid YAML) → the reader returns the same conservative no-auto-ship policy
  **plus a surfaced data-defect warning**. This IS a real defect example-doctrine-repo should hear about,
  distinct from the expected-absent case above.
- File **present and valid** → the reader returns the parsed policy dict verbatim (with
  `auto_ship_enabled` defaulted to `true` if the key is absent from the file), no warning.

## Non-normative example

```yaml
three_signal: {}
mechanical_commit_denylist:
  - "pickup:"
  - "reclaim(docs)"
  - "session-init"
  - "memo:"
  - "handoff.transition"
cross_handoff_attribution: true
dry_run: true
```

## Negative-spec

- This grammar does NOT pin the `handoff.reconcile_open` op's wire shape (params/return) —
  that lives in `coordinator_core/contract/handoff-reconcile-producer-contract.md` (C5).
- This grammar does NOT pin the `gate_eval` clear-predicate rules (all-shipped / abandoned-
  surfaces / partial-narrows / fail-loud-asymmetry) — those are C3's compute-engine contract,
  cited by the C5 producer-contract doc, not policy-YAML data.
- Amending `mechanical_commit_denylist` values, the `cross_handoff_attribution` toggle, or the
  `dry_run` default is a example-doctrine-repo-side YAML data edit — it does NOT require a claude-klabauter code change or
  a re-read of this grammar doc, provided the shape stays within the table above.
