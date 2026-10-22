# plan_merge fixtures

Regression fixture for `plan.merge`: the two plans merged by hand at coordinator-content-repo commit
`bea0332877c3504a917e0633dd41e3bde8b4f172`, plus the delta that reproduces that merge.

Extraction (run in a coordinator-content-repo clone; refs here are coordinator-content-repo refs, not claude-klabauter refs):

    git show bea0332877:coordinator/schemas/plan.schema.json
    git show bea0332877:coordinator/schemas/plan-tasks.schema.json

The two source plans and the hand-merged consolidation come from the same ref. Each copy is
byte-identical to its blob.

| File | Role |
|---|---|
| `merged-execution-shape.md`, `typed-alternative-record-seam.md` | Source plans (14 and 9 rows). The first keeps its spine under `## Open for the PM ...`, so the delta gives `spine_heading`. |
| `worked-example.delta.json` | Delta: 15 row entries (14 amended, 1 new), 4 declared drops, 4 new sections. |
| `expected-consolidated.md` | Reference for partition, row-id order, amended values and declared drops. Not a byte oracle: the op's layout differs and the prototype inserted hand-written interstitial prose, deliberately not reproduced. |
| `schemas/` | plan 1.4.0 and plan-tasks 1.1.1, the versions the merge was validated against. Passed as `schema_dir`. The live vendored plan-tasks is newer (3.18.0 when captured); `test_live_vendored_schema_is_exercised_not_skipped` runs against it and records its version. |

Expected counts: 23 source rows = 9 untouched + 14 amended + 0 unaccounted; 1 new; 24 out; 4 dropped.

The 4 declared drops include 2 `(preamble)` drops the prototype made silently.

Delta transcription choices:
- Row-level `deferred` is carried as `fields.deferred`, because `rows[].deferred: true` maps to `disposition: deferred` and would clobber M14's `wont_do`.
- `status: draft` (the delta schema refuses `approved`); `predecessor_handoff` is a string (the plan schema refuses null).
- Drop headings keep the `## ` prefix; `source` is the source path.

Test map (`../test_plan_merge.py`): `TestWorkedExample` serves the regression and fidelity
criteria (partition, counts, six checks, CRLF equivalence, live-schema drift); `TestRefusals`
serves one case per refusal code; `TestOutputGuards` serves OUTPUT_IS_SOURCE, OUTPUT_EXISTS and
the no-file-on-refusal criterion.
