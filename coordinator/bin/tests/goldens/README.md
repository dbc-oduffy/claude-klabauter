# Frozen golden fixtures — DEC-3(c) fallback (DoE JS oracle capture)

> Spec backlink: `DoE-claude:pln-de-polyglot-the-coordinator-mi-119303` § DEC-3(c),
> chunk A4. Consumes A5's per-oracle map in
> `docs/architecture/migration-hitlist.md` § Depolyglot repoint surface (b).

## Why these files exist

Six `claude-klabauter` test suites skip (not fail) when `node` is absent, because they use
DoE-claude's `.js` files as *live differential oracles* — their Python ports are proven
correct by shelling out to the JS at test time and diffing outputs. Deleting the `.js`
before those suites are converted to frozen goldens silently blinds their correctness
proof (CI stays green-with-skips). DEC-3 gates DoE-side JS deletion on claude-klabauter confirming
that conversion; DEC-3(c) is the fallback that unblocks the wait — DoE captures the JS
oracles' output itself (snapshotting DoE's own JavaScript needs neither claude-klabauter's context
nor consent) and hands claude-klabauter ready-to-wire fixtures via cross-repo memo. Wiring these
into the actual pytest suites (replacing the live `node` shell-out with a load of the
frozen JSON below) remains claude-klabauter's own follow-up — not done here.

Every file below was produced by an **actual run of the real DoE `.js` oracle** against a
fixture tree reconstructed from the reference suite's own fixture-generation code (or, for
`test_verify_schema_registry_sync`, against DoE's real live `coordinator/schemas/` tree).
None of the JSON content in these files was hand-written.

## Per-golden index

| Golden file | Oracle `.js` | Command shape | Consuming claude-klabauter suite |
|---|---|---|---|
| `test_dag_js_parity.golden.json` | `coordinator/bin/lib/walk-handoff-dag.js` | `node walk-handoff-dag.js --start <path> --format {paths,json} [--edge-kinds ...] [--reverse-membership ... --live-set-json ... [--exclude ...]]` | `coordinator_core/tests/test_dag_js_parity.py` |
| `test_records_query_parity.golden.json` | `coordinator/bin/query-records.js` | `node query-records.js --type <t> --where <expr> --format {paths,json} --root <fixture_repo> --limit <n>` | `coordinator_core/ops/tests/test_records_query_parity.py` **and** `coordinator_core/text/test_refresh_queries.py`'s live-bridge leg (per migration-hitlist.md's correction: that suite's node-skip gate is really about `query-records.js`'s liveness via `REFRESH_QUERIES_QUERY_RECORDS_JS`, not `refresh-queries.js` — this same golden covers both) |
| `test_parity_handoff_ops.golden.json` | `coordinator/bin/handoff-transition.js`, `coordinator/bin/stamp-shipped-in.js`, `coordinator/bin/lint-frontmatter.js` | `node handoff-transition.js <verb> --handoff <path> [--session-id <id>] [--at <iso>]`; `node stamp-shipped-in.js --handoff <path> --sha <sha>`; `node lint-frontmatter.js --file <path> --root <repo> --json` | `coordinator_core/frontmatter/tests/test_parity_handoff_ops.py` (four of its five named oracles — see "Known gap" below for the fifth) |
| `test_parity_memo_ops.golden.json` | `coordinator/bin/memo-transition.js` | `node memo-transition.js <verb> --memo <path> [--session-id <id>] [--at <iso>] [--decision <v>] [--decision-note <t>] [--realized-by <p>] [--actioned-note <t>]` | `coordinator_core/frontmatter/tests/test_parity_memo_ops.py` |
| `test_verify_schema_registry_sync.golden.json` | `coordinator/bin/query-records.js` | `node query-records.js --type <t> --format json --limit 1` (once per DoE schema carrying `applies_to:`) | `coordinator_core/ops/test_verify_schema_registry_sync.py::test_golden_oracle_parity_against_live_doe_repo` |

## Known gap — `normalize-handoff-frontmatter.js`

`test_parity_handoff_ops.py` names `normalize-handoff-frontmatter.js` as a fifth required
oracle (its module-level existence-gate skips the whole suite if any of the five CLIs is
missing), but its actual comparison class, `TestNormalizeParity`, is **already permanently
`@pytest.mark.skip`'d** on the claude-klabauter side — the docstring there says the legacy JS-compare
path "was intentionally removed in the finish-strangler collapse (chunk C6)... there is no
JS side left to compare against." Capturing a golden for a retired comparison target would
be dead weight, not a live coverage gap — so this golden set does not include one. If that
skip is ever reversed, re-run this chunk's capture script (below) with the normalize case
added back in.

## Reproduction — how each golden was generated

Every golden was produced by a one-shot Python capture script (fixture-build +
`subprocess.run(["node", ...])` against the real oracle, `text=True`, no shell). The
scripts themselves were scratch tooling, not committed — the commands below are the
canonical reproduction record (re-run any of them to regenerate the corresponding file
byte-for-byte, modulo entropy fields like `deliverable_id` suffixes and tmp-path names,
which are the reason each golden's JSON is a structured `{returncode, stdout, stderr, ...}`
record rather than a raw diff-target, and why path-shaped substrings are normalized to
`<TMP>` / `<FIXTURE_ROOT>` placeholders before being written):

- **`test_dag_js_parity`**: reconstructs the suite's `chain_tree` / `diamond_tree` /
  `cycle_tree` / `missing_link_tree` fixtures (`_write_handoff` helper, `test_dag_js_parity.py:101-157`)
  under a temp dir, then runs `node coordinator/bin/lib/walk-handoff-dag.js` with the same
  `--start`/`--format`/`--edge-kinds`/`--reverse-membership`/`--live-set-json`/`--exclude`
  argument combinations the suite's test methods use (`test_dag_js_parity.py:166-247`).
- **`test_records_query_parity`**: reconstructs the suite's session-scoped `fixture_repo`
  tree byte-for-byte (`test_records_query_parity.py:230-385` — handoffs, archived handoffs,
  51 implemented + 3 other-status plans, 2 sidecars, cross-repo memos incl. the
  memo-shape-guard adversarial cases), then runs `node coordinator/bin/query-records.js`
  over the representative `--type`/`--where`/`--format`/`--limit` combinations the suite's
  parity assertions exercise (`_assert_paths_parity` / `_assert_json_parity` call sites,
  `test_records_query_parity.py:596-1101`).
- **`test_parity_handoff_ops`**: reconstructs `_BASIC_FIXTURE` / `_PREAMBLE_FIXTURE` /
  `_OVERCAP_FIXTURE` / `_STAMP_BASE_FIXTURE` (`test_parity_handoff_ops.py:139-223,697-711`)
  and the four `TestHandoffPhaseCrossFieldParity` fm dicts
  (`test_parity_handoff_ops.py:1319-1391`), then runs `node coordinator/bin/handoff-transition.js`
  (consume/supersede/ship, incl. overcap-rejection and empty/whitespace-session-id-rejection
  cases), `node coordinator/bin/stamp-shipped-in.js` (the four SHA-quoting variants:
  `#`-in-SHA, all-numeric, scientific-notation, normal-hex, plus preamble preservation), and
  `node coordinator/bin/lint-frontmatter.js --file ... --json` (the four handoff_phase
  cross-field cases).
- **`test_parity_memo_ops`**: reconstructs `_FRESH_MEMO` / `_IN_PROGRESS_MEMO` /
  `_IN_PROGRESS_OTHER_MEMO` / `_IN_PROGRESS_NO_PICKED_UP_BY_MEMO` / `_ACTIONED_MEMO` /
  `_PREAMBLE_MEMO` / `_OVERCAP_MEMO` / `_INVALID_KIND_MEMO` / `_CENTRAL_ONLY_MISSING_TO_MEMO`
  / `_PRE_CUTOFF_OVERCAP_MEMO` (`test_parity_memo_ops.py:113-269`), then runs
  `node coordinator/bin/memo-transition.js` claim/action/release with the representative
  argument sets the suite's `TestClaimParity` / `TestActionParity` / `TestReleaseParity` /
  rejection-arm classes use (`test_parity_memo_ops.py:383-1391`).
- **`test_verify_schema_registry_sync`**: re-derives the schema-name → `--type` mapping the
  claude-klabauter module uses (`_schema_to_query_type` / `_extract_applies_to`, reproduced read-only
  in the capture script rather than imported, to avoid a cross-repo coupling), then runs
  `node coordinator/bin/query-records.js --type <t> --format json --limit 1` once per DoE
  schema file (`coordinator/schemas/*.schema.json`) carrying an `applies_to:` field. The
  captured result (`bug-backlog`, `debt-backlog`, `improvement-queue` NOT recognised) matches
  the claude-klabauter suite's own documented expectation of this exact pre-existing drift.

All captures were run against a live `node v24.18.0` on the author's machine, 2026-07-21,
against DoE-claude HEAD at commit range starting `b22f543b`.
