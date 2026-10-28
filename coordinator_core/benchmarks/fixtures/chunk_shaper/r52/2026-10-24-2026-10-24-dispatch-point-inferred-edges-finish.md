---
title: "Finish the dispatch-point inferred-edge work: green the oracle, clear the collateral"
status: executing
plan_id: "pln-finish-the-dispatch-point-infe-2c5cdf"
---

## Tasks

```yaml plan-tasks
- id: F1
  title: "Same-name hookspec/hookimpl resolution: query the dispatch rows of EVERY candidate symbol"
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/symbol_references.py
  writes:
    - example_retrieval_repo_mcp/tools/symbol_references.py
    - tests/test_symbol_references_dispatch_impl.py
  reads_at_head:
    - example_retrieval_repo_core/dispatch_points.py
    - example_retrieval_repo_core/structural.py
    - example_retrieval_repo_priming/producers/dispatch_point_producer.py
    - tests/test_symbol_references.py
    - tests/test_symbol_authority_union.py
    - tests/test_symbol_edge_reachability_invariant.py
  queue_scope: project
  disposition: open
  traces_to_brief: "`project_referencers` and `project_trace` on the dispatch site return"
  body: |
    The wave-0 interface row: it fixes the exit criterion's actual blocker and fixes the
    resolution CONTRACT that F2 and F7 then read. Nothing else may change this file.

    **The defect.** A pluggy hookimpl carries its hookspec's name, so `symbols` holds two
    `function` rows for the queried name. `resolve_authority_candidates(rows, mode="union")`
    (`:1014`) disambiguates across authority bands, never across files: with `ORDER BY id DESC`
    it resolves to the LAST-indexed file (the impl), so the edge query runs
    `WHERE r.symbol_id IN (9)` while the only dispatch row is keyed `symbol_id=2` (the spec).
    Verified at HEAD by direct call: `verdict='ok'`, `references: []`,
    `ambiguous: true`, `candidate_symbols` listing both `impl_plugin.py:9` and `hookspecs.py:9`,
    and `producers_available` already naming `dispatch_point_producer`.

    **The fix, scoped to dispatch kinds only.** Keep `resolved_ids` and every existing edge
    kind's answer exactly as today. When the name resolved ambiguously (`resolution["ambiguous"]`
    and `file is None`), run the SAME edge query a second time over the candidate ids that
    `resolved_ids` excluded, and keep only rows whose `kind` is in
    `DISPATCH_KIND_TO_TIER` (C1's exported mapping — do not re-spell the strings). Merge them
    into `references`, de-duplicated.

    **The second query carries the SAME scope as the first — this is not optional.** The primary
    edge query is not just `WHERE r.symbol_id IN (...)`: when `branch_scope is not None` and the
    branch column exists it appends `AND (ctx.id IS NULL OR <branch clause>)` and feeds the
    `excluded_rows` / `excluded_branches` disclosure counters (`:1046-1065`). A second read that
    omits that fragment would return dispatch edges from branches the caller's scope excludes —
    a scope leak, and one that also goes unreported because the two exclusion counts are computed
    only over `resolved_ids`. Reuse the same `_ctx_frag`/`_ctx_params` for the candidate query and
    extend the same counters over the candidate ids, so an out-of-scope dispatch row is excluded
    AND counted exactly as an out-of-scope ordinary row is. Also keep the
    `_PYTHON_IMPORT_ONLY_KINDS` filter semantics unchanged — dispatch kinds are not in that set,
    so the filter is a no-op here and must not be relaxed to make the merge simpler.

    Rationale for the narrowness: five call sites share
    `_REFERENCES_PRODUCERS` (§ census — among them live.py:1782/:3062, whose `missing_index` gate
    must not count the dispatch producer), and `tests/test_symbol_authority_union.py` plus
    `tests/test_symbol_edge_reachability_invariant.py` pin today's candidate collapse, so widening
    `resolved_ids` for all kinds changes answers this plan has no
    mandate to change (brightline (a)). A dispatch point and its implementation sharing a name is
    not ambiguity to be resolved away — it is the dispatch relation itself.

    **The prior decision this sits beside.** `resolve_authority_candidates`' collapse rule comes
    from `docs/plans/2026-08-20-graph-symbol-resolution-truth.md` C1/C2, whose invariant
    (`tests/test_symbol_edge_reachability_invariant.py`, stated in full in that file) widens
    reachability across authority rows *sharing a resolved FILE* and deliberately stops there.
    F1 does not reverse it: that invariant governs `calls` edges reached through
    `project_symbol_callers`, while this row adds a second `references` read for dispatch kinds
    only and leaves `resolved_ids` untouched. Read that docstring before editing, and do NOT edit
    `example_retrieval_repo_core/structural.py` — the helper is shared with `symbol_callers.py:462` and
    `coverage_lookup.py:286`, and `tests/acl/test_authority.py` names it as an authority surface.

    Disclosure, all of it preserved: `ambiguous` stays `true` and `candidate_symbols` stays as
    is; the hint keeps saying which file it answered for AND gains the fact that dispatch-point
    edges were collected across all same-named candidates; the existing dispatch hint
    (`:1130-1141`) still names the tier. `_core_substrate` and the five response states are NOT
    touched — the dispatch producer must stay out of the `missing_index` decision (§ Anti-scope).

    Also in this file, because it is one file's type debt and splitting it would collide:
      - The `_make_not_indexed_envelope(_ctx)` calls inside `project_symbol_references` (the
        `_suppressed` arm and the `not_indexed_reason` arm — around `:1413`/`:1414`; the brief's
        `:1409` is the `if symbol_name and _engine_structural_path:` condition, NOT a call, so
        edit by symbol and not by that line number) pass `ProjectContext | None` where
        `ProjectContext` is required, because `_suppressed` is a local bool Pyright cannot narrow
        through. Narrow with an explicit `_ctx is not None` guard; do not widen the callee's
        signature.
      - `project_symbol_references._serves_global_content = True` (`:1429`; the brief's `:1425` is
        an argument line of the preceding `fetch_symbol_references(...)` call) assigns onto a
        `FunctionType`: keep the marker (it is read elsewhere) and silence the diagnostic at the
        assignment, e.g. by setting it through a typed helper or an explicit cast; do not delete
        the marker.
      - `:915` is reported unreachable, and it must NOT be deleted. It is the
        `if not isinstance(symbol_name, str): return input_invalid(...)` guard, unreachable only
        because Pyright trusts the `symbol_name: str | None` annotation — but this value arrives
        from an MCP client over JSON, so a non-string genuinely reaches it at runtime, and the
        guard is what turns that into the `input_invalid` response state rather than a crash or a
        silently wrong query. Clear the diagnostic honestly instead: widen the parameter to
        reflect what the door can actually receive (or cast at the check), keep the guard and its
        behaviour, and add a test passing a non-string `symbol_name` and asserting
        `input_invalid`. "Zero cost is not a reason to keep code" does not apply to a validated
        boundary — deleting it would be a regression under the PM's "no regressions elsewhere".

    Tests (`tests/test_symbol_references_dispatch_impl.py`, extending the file the halted run
    already added): insert rows in C2's orientation — `symbol_id` = spec, `context_id` = impl —
    with BOTH a spec and an impl `symbols` row under the SAME name in different files, query on
    the name with no `file=`, and assert the impl comes back, the tier reaches the response,
    `ambiguous` is still disclosed, and a non-dispatch `kind` in the excluded candidate is NOT
    pulled in (the narrowness assertion). Add two more: a dispatch row whose context symbol is on
    an out-of-scope branch is absent from `references` AND counted in the exclusion disclosure
    under a `branch_scope` (the scope-leak assertion for the paragraph above), and a non-string
    `symbol_name` still answers `input_invalid`. Keep the existing assertions in the file green.

- id: F2
  title: "Carry the inferred disclosure through _adapt_symbol_references_for_referencers"
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/live.py
  writes:
    - example_retrieval_repo_mcp/tools/live.py
  depends_on:
    - chunk: F1
      gate_kind: epistemic-premise
      note: "needs F1's decision about which fields a dispatch reference row carries into the response"
  queue_scope: project
  disposition: open
  traces_to_brief: "the implementations that will actually run"
  body: |
    `_adapt_symbol_references_for_referencers` (`live.py:1737-1760`) rebuilds every row into a
    fixed shape (`asset_path`, `ref_type`, `authority`, `feature_level`) and drops the
    `confidence` / `inferred` fields F1's rows carry, so the end-to-end assertion
    `edge["confidence"] in {"INFERRED","NAMING"}` fails with a `KeyError` the moment F1 lands.
    Carry both fields through when present, and only when present — an ordinary reference row
    must not grow a `confidence: null` key, which would read as an unknown tier rather than as
    "not a dispatch edge".

    **Which branch.** The comprehension has TWO arms — the `r.get("degraded")` arm (no
    `asset_path`, by design: the context row is dangling) and the resolved arm. The oracle
    asserts `edge["asset_path"] == "impl_plugin.py"`, so the resolved arm is the one that must
    carry the tier; add the fields to BOTH arms when present rather than only one, so a degraded
    dispatch row is not silently stripped of its disclosure, and do not give the degraded arm an
    `asset_path` it does not have (`tests/test_symbol_references.py`'s
    `test_resolved_and_degraded_edges_reported_separately` pins that split).

    No other change to this file. The five UE trace layers, the `/Game/` asset lane, the no-UE
    short-circuit, the `confidence_min` sentinel and the code lane's routing all stay byte-for-byte
    as the halted run left them. In particular: do NOT touch `_cpp_symbol_graph_db_fallback` or
    `_get_graph_db` — whether that lane is even broken is F7's measurement (§ Problem item 5), and
    editing it before that measurement is how a pre-existing environmental red becomes a real
    behaviour change.

    Verification: `tests/test_project_trace_parameter_honesty.py`, `tests/test_trace.py`,
    `tests/test_non_ue_asset_layer_absence.py` and `tests/test_envelope_verdict_contract.py` stay
    green and unchanged in meaning.

- id: F3
  title: "Trace lane upstream: the test asks for one hop with max_depth=0 — fix the test, not the depth semantics"
  change_kind: test-edit
  surface: tests/test_dispatch_point_trace_lane.py
  writes:
    - tests/test_dispatch_point_trace_lane.py
    # Declared for the contingency named in the body ONLY: if the lane genuinely cannot emit
    # (spec, impl) at max_depth=1, the fix lands here. No other row writes this file in any wave.
    - example_retrieval_repo_mcp/tools/dispatch_trace.py
  queue_scope: project
  disposition: open
  traces_to_brief: "`project_referencers` and `project_trace` on the dispatch site return"
  body: |
    `test_upstream_from_impl_reaches_spec` passes `max_depth=0` and expects one hop.
    `dispatch_trace.py:210` stops expanding at `if depth >= max_depth: continue`, so `max_depth`
    is a HOP COUNT — the same reading the five UE layers use and the same reading this file's own
    `test_downstream_only_does_not_walk_back_to_callers_of_the_spec` (`max_depth=1`) and the other
    11 passing tests already rely on. **Which side is wrong, and why:** the test. Redefining
    `max_depth=0` as one hop would make `project_trace`'s depth parameter mean something different
    on the code lane than on every other lane, in a door whose parameter honesty is pinned by
    `tests/test_project_trace_parameter_honesty.py`.

    Change `max_depth=0` to `max_depth=1` and nothing else. The orientation this test aims at is
    already correct and must stay asserted: upstream from the impl takes `node_col=r.context_id` /
    `other_col=r.symbol_id` (`dispatch_trace.py:171-189`) and emits `(spec, impl)` — the single
    stored orientation read in reverse, never a materialized inverse row. Add one assertion that
    `max_depth=0` returns NO edges, so the semantics this row decided are pinned rather than
    merely assumed, and leave a comment naming this decision at the call site.

    If the executor finds the lane genuinely cannot emit `(spec, impl)` at `max_depth=1`, that is
    a lane defect to fix in `example_retrieval_repo_mcp/tools/dispatch_trace.py` and a note in close-out —
    not a reason to re-open the depth semantics.

- id: F4
  title: "ACL tripwire: classify both container-open-primitive sites it names"
  change_kind: test-edit
  surface: tests/acl/test_read_surface_completeness.py
  writes:
    - tests/acl/test_read_surface_completeness.py
  queue_scope: project
  disposition: open
  traces_to_brief: "with no regressions elsewhere"
  authorization: {source: em-inferred, "on": "2026-10-24", rationale: "The PM's 'no regressions elsewhere' covers the new file's own site; the second site (nav_layer.py) is one adjacent dict entry without which the same fail-closed tripwire stays red, so leaving it would hand back a red suite."}
  body: |
    `test_container_open_primitive_sites_are_all_classified` is the disk-derived, fail-closed
    backstop: any file under the scan roots containing `sqlite3.connect(` (or the other
    primitives) with no `_CONTAINER_SITE_CLASSIFICATION` entry fails it, and the test's own
    message states the remedy — add an entry. It names two files; each carries exactly one
    occurrence of one primitive.

      - `example_retrieval_repo_mcp/tools/dispatch_trace.py:118` — ours, added by the halted run. It opens
        `structural_project.sqlite3` read-only through `sqlite_uri(...)` for the code lane's BFS.
        Classify it against the category its sibling read surfaces use for the same act (read the
        existing entries and the category definitions in this file and pick the one whose stated
        claim is TRUE of this site — do not invent a category, and do not pick
        `container_gate_site` unless this file literally calls
        `assert_container_open_permitted(`, which a sibling test re-verifies off disk).
      - `example_retrieval_repo_core/repo_bundle/nav_layer.py` — **pre-existing and not ours.** It is not in
        the landed diff (`git status` does not list it), and neither is this test; both are
        present at the pre-diff tree `3e5b7e2` (`git ls-tree 3e5b7e2` lists
        `example_retrieval_repo_core/repo_bundle/nav_layer.py` and
        `tests/acl/test_read_surface_completeness.py`), so the tripwire was already red for that
        site before any of this work — verified here, not deferred to F7. Classify it too — one entry, janitorial — and say in close-out that it was
        pre-existing, so nobody later reads it as collateral from this work.

    **`tests/acl/test_dispatch_query_modules_no_container_open.py` does NOT apply here — settled,
    not left to the executor.** Its stated rule (read at HEAD) scopes to modules hosting a
    function registered in `example_retrieval_repo_core.workstate_store.read_api._DISPATCH`, and its
    primitives are `get_backend`, `resolve_band_reader`, `deposit_vault_chunks` and
    `principal_scoped_bands` — not `sqlite3.connect(`.
    `example_retrieval_repo_mcp/tools/dispatch_trace.py` registers nothing in that table and calls none of
    the four, so the name collision on the word "dispatch" is coincidental. Read it once to
    confirm the rule has not changed, then classify; if its scope HAS widened to this module,
    that is a genuine design question about where the code lane's DB open belongs, and the row
    surfaces it with the test's own stated rule quoted rather than classifying around it.
    `tests/acl/test_assert_container_open_permitted_gate.py` is the companion gate test; run the
    whole `tests/acl/` directory, not the one named test.

    Do not add a scan exclusion, and do not weaken `_CONTAINER_OPEN_PRIMITIVES` or the scan roots:
    `test_container_site_classification_has_no_stale_entries` and
    `test_container_grain_gate_sites_call_assert_container_open_permitted_directly` both ride on
    this dict being honest. Run the whole `tests/acl/` directory, not just the one test.

- id: F5
  title: "Routing-set assertion: the registered set is right, the test's equality is wrong"
  change_kind: test-edit
  surface: tests/test_dispatch_point_routing.py
  writes:
    - tests/test_dispatch_point_routing.py
  queue_scope: project
  disposition: open
  traces_to_brief: "with no regressions elsewhere"
  body: |
    `test_referencers_code_lane_is_runtime_producer_set` asserts
    `set(prod.code_lane_host_producer_ids) == set(_REFERENCES_PRODUCERS)`. The registry declares
    `("reference_edge_producer", "structural_index", "dispatch_point_producer")`
    (`registry.py:1322-1326`); the module holds `_REFERENCES_PRODUCERS` at the two base ids with
    the reason written beside it — `live._content_agnostic_dependencies` shares that default, its
    `missing_index` gate must not count the dispatch producer, and `fetch_symbol_references`
    appends `_DISPATCH_PRODUCER_ID` to its own substrate read regardless
    (`symbol_references.py:928-931`, `_core_substrate`).

    **Which side is wrong, and why:** the test. Adding the dispatch producer to
    `_REFERENCES_PRODUCERS` to satisfy an equality would hand a default shared by five call sites
    (§ census — the 13 is a grep-line count, not a call-site count) a
    third producer purely to make an assertion true, and would reach the verdict gate the
    dispatch producer is deliberately kept out of — the "a verdict must not become `missing_index`
    because a new dispatch lane found nothing" rule. The invariant actually worth pinning is
    no-drift between the declared set and the runtime set, which is
    `set(code_lane) == set(_REFERENCES_PRODUCERS) | {_DISPATCH_PRODUCER_ID}`. Rewrite the
    assertion to that and comment the reason at the assertion so the next reader does not
    "simplify" it back. Import the id rather than re-spelling the string — and note the id
    currently exists as TWO literals, `dispatch_trace.DISPATCH_PRODUCER_ID` (public, what
    `live.py:65` imports) and `symbol_references._DISPATCH_PRODUCER_ID` (`:595`). Import the
    public `example_retrieval_repo_mcp.tools.dispatch_trace.DISPATCH_PRODUCER_ID`, since that is the one the
    runtime lane reads, and record the duplicate-literal drift risk in close-out as a knowingly
    left defect — collapsing it is a shared-file change this plan has no mandate for.

    Also in this file: the `.producer on None` diagnostics at `:29/35/43/50/74/75` come from
    `routing_for()` being typed `-> ... | None`. Resolve them the way that keeps the test honest
    — a helper that fetches a routing entry and fails the test with the tool's name if it is
    absent — never a blanket `# type: ignore` and never by loosening the registry's signature.
    Keep `test_generated_instructions_name_code_layer_and_inferred` and
    `test_declared_producer_ids_resolve_to_a_real_write_site` exactly as they are: they are the
    capability's nav-entry assertions (§ Anti-scope).

- id: F6
  title: "Clear the priming-side type and export debt the landed diff left"
  change_kind: code-edit
  surface: example_retrieval_repo_priming/producer_fingerprint.py
  writes:
    - example_retrieval_repo_priming/producer_fingerprint.py
  queue_scope: project
  disposition: open
  traces_to_brief: "Teach the graph that a reference to a dispatch point implies the things that"
  authorization: {source: em-inferred, "on": "2026-10-24", rationale: "Type/lint debt inside the landed diff is part of finishing it (R1 'finish it'), not new scope; the brief enumerates these exact diagnostics and nothing downstream reads them for a decision."}
  body: |
    Debt only — no behaviour change, and the dispatch producer's registration is already correct
    (it runs and writes rows; see § Problem).

      - `producer_fingerprint.py:406/409` — `frozenset[str] | None` vs `set[str]` mismatch on the
        new scope row. Fix the annotation/construction so the declared type matches what is
        actually passed; do not cast away the `None` arm without proving it cannot occur on this
        path.
      - `producer_fingerprint.py` — `_get_source_root_for_producer`, `_last_ok_fingerprint` and
        `_write_ledger` reported unused. For EACH, establish whether it is genuinely dead or
        reached dynamically (grep the whole repo, including `example_retrieval_repo_scripts`, before
        deciding). Delete the genuinely dead ones; for any reached dynamically, leave it and name
        the call site in a comment. Do not delete a function this file's own tests exercise —
        `tests/test_producer_fingerprint_band_sensitivity_tripwire.py` is the one that exercises
        this module's fingerprint scope, so run it before and after any deletion. (The brief
        names a plain `test_producer_fingerprint` test file for this module; no such file exists
        in this tree — the band-sensitivity tripwire above is the one that guards it.)
      - `example_retrieval_repo_priming/producers/__init__.py` stays UNTOUCHED, and this row does not
        declare it. It is empty, so
        `from example_retrieval_repo_priming.producers import dispatch_point_producer` already works at
        runtime (the producer executes and writes rows in this author's probe); only Pyright's
        unknown-import-symbol diagnostic fires, at `tests/test_dispatch_point_producer.py:11` and
        `tests/test_dispatch_point_end_to_end.py:18`. No sibling producer is re-exported there and
        the file is in the import path of most of the suite, so adding a re-export for one
        producer out of 26 would be a new convention — and a shared-file write — not a debt fix.
        Record that disposition in close-out as a knowingly-left diagnostic.
      - `tests/fixtures/pluggy_dispatch/host.py:6` 'impl_plugin' unresolved import is a fixture
        artifact: the fixture is a standalone project copied under `tmp_path`, so a sibling-module
        import is correct there and the diagnostic is the analyser reading it as repo code. Leave
        the fixture unchanged (this row does NOT write it) and record that disposition.

- id: F7
  title: "Green the oracle end-to-end, settle the C++ fallback question by measurement, update DELIVERY.md"
  change_kind: code-edit
  surface: tests/test_dispatch_point_end_to_end.py
  writes:
    - tests/test_dispatch_point_end_to_end.py
    - DELIVERY.md
    # Declared for step 2's GREEN-at-baseline contingency ONLY: if the C++ lite fallback red IS a
    # regression from this diff, the fix lands in live.py and F7 is the row that owns it. F7 runs
    # strictly after F2 (its only other writer), so this adds no concurrent-write hazard; if the
    # measurement shows pre-existing, this file is not touched at all.
    - example_retrieval_repo_mcp/tools/live.py
  depends_on:
    - chunk: F1
      gate_kind: output-consumption-runtime
      note: "asserts the referencers answer F1 makes possible"
    - chunk: F2
      gate_kind: output-consumption-runtime
      note: "asserts the confidence tier F2 carries into the response"
    - chunk: F3
      gate_kind: output-consumption-runtime
      note: "shares the trace-lane depth semantics F3 decided"
  queue_scope: project
  disposition: open
  traces_to_brief: "It is done when, on a small fixture project with a hookspec and a"
  body: |
    The acceptance oracle and the PM's named deliverable.

    **1. Green the end-to-end file as written, and change it only where it asserts something
    untrue.** Run `tests/test_dispatch_point_end_to_end.py` whole. Both doors must answer on
    BOTH fixture specs — the `example_retrieval_repo_*`-conventional one and `gather_widgets`, which is the
    generality assertion — with the hookimpl in `impl_plugin.py` and a tier in
    `{INFERRED, NAMING}`, never `STATIC`/`EXPLICIT`, and with no `confidence_min` passed. The
    fixture and the producer are already correct (`pairs=2; resolved=2`), so an edit to this test
    needs the same argument F3 and F5 owe: which side is wrong and why.

    **2. Settle § Problem item 5 by measurement, before anyone edits the fallback lane.** Export
    the baseline tree to a scratch directory and run the ten failing
    `tests/test_cpp_symbol_lite_fallback.py` cases there:
    `mkdir -p scratch/baseline-check && git archive 3e5b7e2858a854abb9483f22df7790aacc2b8bbf |
    tar -x -C scratch/baseline-check` (`tar -x -C` fails if the directory does not exist), then
    run pytest with that tree as rootdir.

    **The measurement only answers the question if the ARCHIVED modules are the ones imported.**
    `pyproject.toml` sets `pythonpath = ["."]` and `--import-mode=importlib`, so invoking
    `python -m pytest scratch/baseline-check/tests/...` from the repo root imports
    `example_retrieval_repo_mcp` / `example_retrieval_repo_core` from the LIVE, diff-carrying tree — and a red/green
    result obtained that way cannot distinguish pre-existing from regression at all. Run from
    INSIDE `scratch/baseline-check` (its own `pyproject.toml` is then rootdir), and record, as part
    of the verbatim evidence, the resolved module paths:
    `python -c "import example_retrieval_repo_mcp, example_retrieval_repo_core; print(example_retrieval_repo_mcp.__file__, example_retrieval_repo_core.__file__)"`.
    If either resolves outside `scratch/baseline-check` (an editable install of the live tree will
    do exactly that), the run is not evidence — say so and report the measurement as
    UNSETTLED rather than reading its colour. The variable under test is the landed diff and
    nothing else: `tmp_path` is outside a git worktree in both runs, which is the point.

    `3e5b7e2` is the PRE-DIFF tree — it predates `d895bedf`
    and so contains none of this work, which is what makes it the right comparison; it is NOT the
    `prime_exit_criterion.baseline_ref` (`7afd26fd`, HEAD, which already carries C1 and the
    fixture). `git worktree` is banned and `git stash` would mutate a tree peers share, so the
    archive is the method. Scratch stays under `<repo>/scratch/`; the run writes nothing
    elsewhere. Record the output verbatim in
    close-out and in `DELIVERY.md`.
      - RED at baseline too -> pre-existing, environment-dependent (a `tmp_path` outside any git
        worktree has no registry-anchored project root, so `_get_graph_db` returns `None`). Report
        it with a recommendation; do NOT fix it here (§ Out of scope), and state plainly that the
        brief's "verdict flipped ok -> missing_index" framing was not what was observed.
      - GREEN at baseline -> it IS a regression from this diff. Then find it and fix it, preserving
        the five distinguishable response states and the empty-finding-vs-empty-substrate rule: a
        verdict must not become `missing_index` because a new dispatch lane found nothing. The
        diff's `live.py` hunks are the code lane, two hoisted tier helpers, the `confidence_min`
        sentinel default and description text — the sentinel subclass of `str` is the one change
        with any reach beyond the new lane, so look there first.

    **3. Re-run the whole red set plus the no-regression set** and record pass/fail per test:
    `tests/test_dispatch_points.py tests/test_dispatch_point_producer.py
    tests/test_symbol_references_dispatch_impl.py tests/test_dispatch_point_trace_lane.py
    tests/test_dispatch_point_routing.py tests/test_dispatch_point_end_to_end.py
    tests/acl/ tests/test_cpp_symbol_lite_fallback.py tests/test_symbol_references.py
    tests/test_symbol_authority_union.py tests/test_symbol_edge_reachability_invariant.py
    tests/test_symbol_callers.py
    tests/test_tool_producer_failure_disclosure.py tests/test_disclosure_reaches_every_door.py
    tests/test_producer_consumer_schema_contract.py
    tests/test_project_trace_parameter_honesty.py tests/test_registered_parameters_are_honest.py
    tests/test_envelope_verdict_contract.py tests/test_trace.py
    tests/test_non_ue_asset_layer_absence.py
    tests/test_producer_fingerprint_band_sensitivity_tripwire.py`. A changed
    assertion in any of the guard tests is a finding to surface, not a line to edit.

    **4. `DELIVERY.md` — update, never leave describing an unmet criterion.** It already exists
    from the halted run. Rewrite it to match what actually ships and passes, keeping the PM's four
    numbered sections, and add: the same-name hookspec/hookimpl resolution defect and the narrow
    fix chosen over the general one; the two test-side decisions (F3 depth semantics, F5 producer-set
    equality) with the argument for each; the two ACL classification entries and which was
    pre-existing; and the C++ lite fallback finding with its baseline evidence. Section 4's
    least-confident item should be stated honestly rather than softened — the candidate-merge
    narrowness and the fallback-lane verdict are the two live uncertainties.
```
