# validator-negative — known-bad fixture corpus

Spec backlink: docs/plans/2026-06-27-ccos-1-dual-context-validator.md § W0 deliverable 4

Each file here is a known-bad coordinator record that the **current** `schema.js` validator
REJECTS. The companion `expected-rejections.json` records the precise error each fixture
produces. Together they form the **AC5b oracle**: W1's rebuilt validator must reject every
fixture in this directory with a consistent error shape.

## How to run the full negative corpus

The corpus loader is `coordinator/bin/tests/test_validator_negative_corpus.py` — a Python
re-port of the retired `testNegativeCorpus(fixturesDir, schemasDir)` from example-doctrine-repo
`coordinator/bin/lib/schema.js` (deleted in `480ad8f8`, "D1: retire claude-klabauter oracle .js +
bin/lib/*.js"). It is the ONLY consumer of this directory in this repo.

```bash
# From repo root:
python3 -m pytest coordinator/bin/tests/test_validator_negative_corpus.py -q
```

<!-- Review: code-reviewer — restores a single-fixture debugging recipe;
the deleted `node -e` one-liner's equivalent is pytest's own -k selector. -->
## How to feed a single fixture to the validator

To triage one fixture in isolation (e.g. while iterating on a schema), select it by its
fixture-id stem via pytest's `-k`:

```bash
# From repo root — replace h01 with the fixture stem you're triaging:
python3 -m pytest coordinator/bin/tests/test_validator_negative_corpus.py -k h01 -v
```

It drives `expected-rejections.json` (this directory's must-reject index) against
`coordinator_core.frontmatter.schema_validate.validate()` /
`validate_memo_cross_fields()`, plus two drift guards: every indexed fixture must exist
on disk, and every fixture on disk must appear in an index (or a documented/named
exclusion) — see the module docstring for the current known-gap list (an unported
`completion-entry` schema and two orphaned `c3-*` fixtures).

## Coverage

The corpus covers:

| Category | Fixtures | Rules |
|---|---|---|
| Handoff cross-field rules | h01–h13 | H-CROSS-1 through H-CROSS-A3a-3 |
| Handoff required field / enum | h14–h15 | H-REQUIRED, H-ENUM |
| Handoff type tags | h16 | string-or-null |
| Memo cross-field rules | m01–m09 | M-CROSS-1 through M-CROSS-8 |
| Completion-entry type tags | c01–c05 | object, enum-nested, enum, list-of-string, number-or-null |

## NOT covered

- **additionalProperties / unknown fields**: the current validator is permissive on unknown
  fields — it accepts records with arbitrary extra keys. No negative fixture is provided for
  this case because the current validator does NOT reject it. W1 may add unknown-field
  rejection; if so, add a fixture and update `expected-rejections.json`.

- **iso-date type check as negative fixture**: the `created: iso-date` required field would
  trigger a type error if given a bad date, but `parseFrontmatter` may normalize some values.
  The `H-REQUIRED` fixture (h14) exercises the required-field path; a separate iso-date
  type-error fixture is deferred as low-value (the type check code is trivially correct).

- **review-trail JSON files**: `lint-frontmatter.js` skips `.json` files at line ~217. The
  `r01-diff-loc-wrong-type.json` fixture is present for direct validator testing but is NOT
  in the `expected-rejections.json` index (which targets lint-frontmatter-reachable files).
  `number` type checking IS covered by `c05` (loe.agent_dispatches uses the object-spec form).
