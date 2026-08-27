# `auto-reconcile-policy.yaml` grammar pin

Spec backlink: `pln-claude-klabauter-auto-reconcile-pass-off-425848` § C9, DEC-1.
Boundary authority: `/Users/example-operator/X/DoE-claude/docs/decisions/DR-047-doe-claude-klabauter-boundary-redraw-contract-vs-e.md`
("DoE owns rules, claude-klabauter owns machine").

## Ownership

This grammar is the schema/shape **claude-klabauter ships and validates against**. The policy
**data** file itself, `coordinator/auto-reconcile-policy.yaml`, is **authored and owned by
DoE** (the sibling `claude-central-em` repo) — claude-klabauter's `coordinator_core/reconcile/policy_loader.py`
reads it fresh on every call and never writes it. This mirrors the existing
`subagent-sandbox-policy.yaml` ← `coordinator_core/subagent_sandbox` precedent (DR-047
contract-vs-engine split): a threshold/data amendment on DoE's side is a policy-YAML edit,
zero claude-klabauter code change.

## Reader

`coordinator_core/reconcile/policy_loader.py:load_policy()`. Consumed by
`coordinator_core/reconcile/commit_reality.py` (C2, the DEC-1 three-signal matcher) and
`coordinator_core/reconcile/gate_eval.py` (C3, the unified gate evaluator).

## Top-level keys

| key | type | required | meaning |
|---|---|---|---|
| `three_signal` | mapping | yes | Tuning knobs for the DEC-1 three-signal shipped-ness bar (signal (a) commit-subject match, (b) named-deliverable-on-disk, (c) SHA-reachable-on-HEAD). Accepted as a mapping with OPTIONAL sub-keys (see below) — all sub-keys have code-side defaults, so an absent/empty `three_signal: {}` is valid and matches today's ratified behavior baseline. `policy_loader._validate_grammar` only checks the required top-level keys; these sub-keys are not independently type-validated by the loader, so adding/amending one is a DoE-side YAML data edit, zero claude-klabauter code change and zero re-validation. |
| `auto_ship_enabled` | boolean | no (optional) | Gates whether `handoff.reconcile_open` may actually apply an auto-ship mutation; **default `false` (fail-closed)** — absent from the file resolves to `false`. `auto_ship_enabled` is INDEPENDENT of `dry_run`: flipping `dry_run: false` alone does NOT arm auto-ship. To arm auto-ship the author must ALSO explicitly write `auto_ship_enabled: true`. |

### `three_signal` optional sub-keys (2026-07-20 claude-central-em false-positive memo, Defect 2)

| key | type | default | meaning |
|---|---|---|---|
| `subject_match_min_tokens` | int | `2` | Minimum number of DISTINCT derived noun tokens that must appear in a candidate commit's subject for signal (a) to count as a match. Raised from the original "any single token" bar — a lone incidental path-component token (e.g. `ops`) was previously sufficient to select a wholly unrelated commit. Raising this further will drop some true positives on short-title handoffs; validate against the live `check_auto_reconcile` output before tightening. |
| `subject_match_extra_stopwords` | list of strings | `["ops", "core", "config", "plans", "docs", "state", "tests", "lib", "bin", "src", "schemas", "contract"]` | Additional noun-token stopwords unioned with the matcher's built-in set — structural/path-shape vocabulary that carries near-zero signal about what a handoff's actual deliverable is. |
| `deliverable_requires_file` | boolean | `true` | When true, signal (b) requires an existing FILE (or a glob with >=1 file hit) — an existing directory alone no longer counts as "deliverable present". Setting `false` restores the pre-fix directory-tolerant behavior. |
| `mechanical_commit_denylist` | list of strings | yes | Commit-subject prefixes/tokens that must NOT count as signal-(a) evidence even when they touch a scope path — guards against a `pickup:`/`session-init`/`memo:`/`handoff.transition`-family/frontmatter-mutation commit satisfying signal (a) without representing real completed work (the Staff Engineer #2, inverse-direction guard). Ratified initial content: `pickup:`, `reclaim(docs)`, `session-init`, `memo:`, `handoff.transition`-family subjects, frontmatter-mutation subjects. |
| `cross_handoff_attribution` | boolean | yes | When `true` (the ratified default), the matcher demotes a candidate `verdict: auto-ship` to `surface` whenever >1 open handoff's `scope` pathspecs overlap the candidate commit's touched paths — the fourth DEC-1 conservatism guard (the Staff Engineer review, finding index 2). Setting `false` disables the guard (not recommended; DoE-owned toggle for future tuning). |
| `dry_run` | boolean | yes | Default policy-level dry-run flag consumed by the `handoff.reconcile_open` op (C4) — **ratified default `true`** (first live pass is observation-only; DoE flips after reading the dry-run report). Distinct from the op's own per-call `dry_run` param, which may override this at invocation time. |

## Overlay

A consuming repo may arm auto-reconcile for **itself alone** via a repo-resident overlay file,
`auto-reconcile-policy.local.yaml`, living at the CONSUMING repo's own root — never under
`coordinator/`, and never resolved from `CLAUDE_PLUGIN_ROOT` (that env var is the plugin-floor
route, not the overlay's). See DR-158 for the convention half of this design.

The overlay merges over the plugin floor **key by key** (a top-level `dict.update`, not a deep
merge) — it may restate any subset of the top-level keys above, and the floor supplies every key
the overlay does not restate.

Grammar validation (this doc's shape) applies to the **merged** result, not to the overlay in
isolation — this is what makes a partial overlay (e.g. one restating only `dry_run`) legal rather
than rejected as missing keys it never intended to supply.

`auto_ship_enabled` is expressible per-repo in an overlay, exactly as it is in the floor — no new
field, since it is already an optional validated boolean in the table above. Its absence, in the
overlay, the floor, or both, still resolves to `false`; see § Fail-closed contract below for the
default this doc does not restate here.

If the plugin floor is present but fails to read or parse, the reader does NOT silently merge the
overlay over an absent-equivalent `{}` — doing so risks a merged-and-validated "loaded" result
built from only the overlay's own keys, losing the malformed-floor signal `source` exists to
carry. This case reports `source="malformed"` with a floor-specific warning, same as any other
malformed branch (see § Fail-closed contract).

## Fail-closed contract (the reader's obligation, not a policy-file key)

The grammar pin governs the **shape** DoE authors against; the **fail-closed behavior on a
missing or malformed file** is a `policy_loader.py` reader-side obligation, not something the
YAML itself declares:

- File **absent** (the expected steady state pre-ratification) → the reader returns a
  conservative no-auto-ship policy (`dry_run: true`, `auto_ship_enabled: false`) with **no
  warning**. This is expected, not a defect — surfacing a warning every workday-start during
  the pre-ratification period would be noise.
- File **present but fails grammar validation** (missing required key, wrong type on a
  required key, invalid YAML) → the reader returns the same conservative no-auto-ship policy
  **plus a surfaced data-defect warning**. This IS a real defect DoE should hear about,
  distinct from the expected-absent case above.
- File **present and valid** → the reader returns the parsed policy dict verbatim (with
  `auto_ship_enabled` defaulted to `false` if the key is absent from the file, so an
  absent-file run and a valid-file run that simply omits the key resolve to the same
  fail-closed answer — silence never arms auto-ship), no warning.

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
  `dry_run` default is a DoE-side YAML data edit — it does NOT require a claude-klabauter code change or
  a re-read of this grammar doc, provided the shape stays within the table above.
