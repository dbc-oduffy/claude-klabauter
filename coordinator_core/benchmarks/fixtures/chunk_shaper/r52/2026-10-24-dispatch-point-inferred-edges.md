---
title: "Dispatch-point inferred edges: hookspec references reach hookimpls"
status: executing
plan_id: "pln-dispatch-point-inferred-edges-cd0a45"
---

## Tasks

```yaml plan-tasks
- id: C1
  title: "Dispatch-point interface: detector registry, edge dataclass, pluggy detector"
  change_kind: code-edit
  surface: example_retrieval_repo_core/dispatch_points.py
  writes:
    - example_retrieval_repo_core/dispatch_points.py
    - tests/test_dispatch_points.py
  reads_at_head:
    - example_retrieval_repo_mcp/graph/schema.py
    - example_retrieval_repo_core/addon_hookspecs_stable.py
    - example_retrieval_repo_priming/producers/reference_edge_producer.py
    - example_retrieval_repo_core/symbol_extract/__init__.py
    - example_retrieval_repo_core/symbol_extract/extract.py
  queue_scope: project
  disposition: open
  traces_to_brief: "a reference to a dispatch point implies the things that"
  body: |
    The wave-0 interface chunk every other row builds on. Ships, with no DB and no tool
    involvement:

      - `DispatchPointKind` / `DispatchPointDetector` protocol: given a parsed Python file
        and its tree-sitter root, yield `DeclaredDispatchPoint` (the spec side) and
        `DispatchImplementation` (the impl side), each carrying file, line, symbol name and
        the detector id that found it.
      - `InferredDispatchEdge` dataclass: spec-side name, impl-side name, `detector_id`,
        `basis` (what was matched: `decorator+name`, `name-only`), and `confidence` drawn
        from the EXISTING `CONFIDENCE_TIERS` vocabulary in `example_retrieval_repo_mcp/graph/schema.py`
        (line 468: `{STATIC, INFERRED, NAMING, EXPLICIT}` — UPPERCASE, imported, never
        re-spelled locally) — `INFERRED` when both sides carry a resolved hookspec/hookimpl
        decorator, `NAMING` when the pairing rests on the name alone. A constructor given a
        tier outside `{INFERRED, NAMING}` raises — including `STATIC`/`EXPLICIT`, the two
        tiers that WOULD read as compiler-grade. (`deterministic` is deliberately NOT
        mentioned: it belongs to `CONFIDENCE_LEVELS`, not to this vocabulary, so a test
        asserting it raises would assert nothing.)
      - **`DISPATCH_KIND_TO_TIER` / `TIER_TO_DISPATCH_KIND`, the kind<->tier mapping, live
        HERE and nowhere else.** C2 persists the tier in the `kind` column (two spellings,
        see C2), so every reader has to turn `dispatch_impl` back into `INFERRED` and
        `dispatch_impl_naming` back into `NAMING` — C4 for its response field, C5 for its
        per-hop confidence and its `CONFIDENCE_LEVELS` crossing, C7 for its assertions. Three
        private copies of that mapping is three places it can drift, and the mapping is the
        disclosure this plan exists to guarantee. C1 exports both directions and the pair of
        kind strings as named constants; no other chunk spells either string literally.
      - `DISPATCH_POINT_DETECTORS`, the registry, and `register_detector()`. The pluggy
        detector is the one registered member: it recognises decorators whose attribute or
        call target ends in `hookspec` / `hookimpl` (covering `@hookspec`,
        `@hookimpl`, `@SOMEMARKER.hookimpl`, and a marker bound from
        `HookspecMarker`/`HookimplMarker`), and pairs spec to impl by function name.
        The `example_retrieval_repo_*` hook-name convention may raise a pair's `basis` detail but is
        NEVER required for a pair to exist (see § Anti-scope).

    **Where the parse tree comes from is DECIDED here, not assumed.** `symbol_extract`'s
    frozen public surface (`__init__.py:83` `__all__`) exports NO parser and NO parse-tree
    root — the only tree-sitter access is `extract.py`'s private `_get_python_parser` and the
    deliberately carved-out `_walk` helpers (`__init__.py:29-32`, mirroring DR-PRAG-045).
    Importing a private of a PACKAGED frozen leaf is not available to us, so this module
    builds its own cached `Parser` from `tree_sitter_python` + `tree_sitter` — the same two
    imports `_get_python_parser` makes, already dependencies — and degrades the same way:
    grammar unavailable means a logged warning and ZERO detected dispatch points, never a
    raise, so C2's producer reports an empty-but-`ok` result rather than failing an index.
    The detector takes `(path, source_bytes, root_node)` so C2 owns the file walk and C1
    stays I/O-free, and it reaches a function's decorators through the node's
    `decorated_definition` PARENT: tree-sitter nests `function_definition` inside it, which
    is both why a decorated hookimpl already has a `symbols` row (AP8) and why that row's
    line span excludes the decorator.

    No I/O, no sqlite, no MCP imports — this module must be importable by both the priming
    and the tools packages without pulling either in. Tests cover: each decorator spelling,
    a name-only pair landing on `NAMING`, a `STATIC`/`EXPLICIT` attempt raising, registry
    enumeration, and a non-pluggy decorated function producing nothing.

- id: C2
  title: "dispatch_point_producer: write kind='dispatch_impl' rows into structural references"
  change_kind: code-edit
  surface: example_retrieval_repo_priming/producers/dispatch_point_producer.py
  writes:
    - example_retrieval_repo_priming/producers/dispatch_point_producer.py
    - example_retrieval_repo_priming/manifest.yaml
    - example_retrieval_repo_priming/producer_fingerprint.py
    - tests/test_dispatch_point_producer.py
  depends_on:
    - chunk: C1
      gate_kind: output-consumption-runtime
      note: "calls DISPATCH_POINT_DETECTORS and emits C1's InferredDispatchEdge at run time"
  queue_scope: project
  disposition: open
  traces_to_brief: "Teach the graph that a reference to a dispatch point implies the things that"
  body: |
    A new full-snapshot producer, modelled line-for-line on `reference_edge_producer.py`'s
    shape: `PRODUCER_ID = "dispatch_point_producer"`, a `run()` returning `ProducerResult`
    with the same failure vocabulary (`failed` + `error_text` + `remediation` when the
    `symbols` table is absent), `enable_structural_foreign_keys`, WAL/busy-timeout calls,
    and `path_norm` relativization of `output_path`.

    Writes into the EXISTING `references` table — no DDL beyond the idempotent
    `ensure_references_role_authority_producer_columns(conn)` call the sibling producer
    already makes (`reference_edge_producer.py:1037`; it is `ALTER TABLE ... ADD COLUMN`
    guarded by a column-presence check, not a `lite_schema_version` bump) — with
    `kind='dispatch_impl'`, **`authority='project_treesitter'`**,
    `producer='dispatch_point_producer'`, `symbol_id` = the **hookspec** symbol's `symbols.id`,
    `context_id` = the **hookimpl** symbol's `symbols.id` (both ends keyed on `symbols.id` per
    DR-2041; a pair whose either end has no `symbols` row is counted, not written).

    **The orientation is load-bearing and is this way round for a reason — do not flip it.**
    `fetch_symbol_references` resolves the QUERIED name to `symbol_id` and returns the joined
    `context_id` rows as the answer: `FROM "references" r LEFT JOIN symbols ctx ON
    ctx.id = r.context_id WHERE r.symbol_id IN (...)` (`symbol_references.py:469-470` and
    `:999-1002`), disclosed as "Resolved symbol_id -> context_id edges" (`:1228`). The exit
    criterion asks the question AT THE HOOKSPEC and wants the impl back, so the hookspec must
    be `symbol_id` and the impl must be `context_id`. The reverse orientation produces rows
    that no `project_referencers` call on a hookspec can ever match, and a C4 test that inserts
    its own rows would pass against it. This also matches the sibling's semantics — the
    referencing SITE is `context_id` (`reference_edge_producer.py:933-941, 998-1009`) and the
    hookimpl is the site that answers the spec.

    **One direction only, never a materialized inverse.**
    `docs/wiki/peer-repo-comparisons/peer-code-intelligence-non-goals.md:74` records that
    materialized inverse edges were withdrawn; the reverse question is served by the index on
    the reverse column, not by a second row. C5 traverses this same spec->impl orientation.

    **`authority` is `project_treesitter`, never `project`.** `reference_edge_producer.py`'s
    module docstring (lines 48-52) states `'project'` "is reserved for a compiler-grade
    producer" precisely so a reader can tell a heuristic row from a clang row; a tree-sitter
    dispatch inference is the heuristic case this reservation exists for.

    **The confidence carrier is DECIDED here, not left to the implementer.** `references` has
    no confidence column (`symbol_id, context_id, kind, file, line` +
    `role_mask, authority, producer, branch, git_sha`) and none of the nullable columns can
    carry a tier. Per the cq-05 precedent the same docstring records — "the confidence/tier
    decision cq-05 requires is answered by EXTENDING the existing `authority` vocabulary" —
    the tier is carried in `kind`: `kind='dispatch_impl'` for `INFERRED` and
    `kind='dispatch_impl_naming'` for `NAMING`, two values, no new column and no migration.
    Both spellings and the kind<->tier mapping are C1's exported constants, not literals
    re-typed here or in C4/C5/C7; a chunk that invents a third carrier breaks the parallel
    rows that read it.

    Stamps a `producer_ledger` row so `_producer_substrate.py` can disclose it.

    Registration, both enumerations of the producer set:
      - `manifest.yaml`: new `- id: dispatch_point_producer` entry,
        `output_path_from: paths.structural_index_project`,
        `runner: producers.dispatch_point_producer`, `scope: project`,
        `expected_min_records: 0`, `requires_capabilities: []`, `invalidates_consumers: []`
        (both present on every sibling row — manifest.yaml:213-224 — and a manifest row
        missing either fails load), `depends_on` the same structural/scip producers
        `reference_edge_producer` lists, `project_band_extensions` deliberately absent
        (writes no `symbols` row — the `doc_links_producer` / `reference_edge_producer`
        precedent, with the same note).
      - `producer_fingerprint.py`: a scope row with `suffixes: PYTHON_EXTENSIONS` and the
        prune-omit treatment its walk actually needs.

    Tests build a tiny on-disk structural DB under `tmp_path`, run the producer over a
    two-file Python tree, and assert the emitted rows, the ledger row, the zero-pair case
    (clean `ok`, not a failure), and the missing-`symbols`-table failure shape.

- id: C3
  title: "Pluggy dispatch fixture project (hookspec, hookimpl, call site)"
  change_kind: test-edit
  surface: tests/fixtures/pluggy_dispatch
  writes:
    - tests/fixtures/pluggy_dispatch/hookspecs.py
    - tests/fixtures/pluggy_dispatch/impl_plugin.py
    - tests/fixtures/pluggy_dispatch/host.py
    - tests/fixtures/pluggy_dispatch/README.md
  queue_scope: project
  disposition: open
  traces_to_brief: "a small fixture project with a hookspec and a"
  body: |
    The exit criterion's fixture, and the only chunk with no predecessor. Three small
    Python files beside the other `tests/fixtures/` projects: a `hookspecs.py` declaring
    one `@hookspec` function (named with this repo's `example_retrieval_repo_*` convention) plus a
    second spec named WITHOUT it, so the generality anti-scope is testable; an
    `impl_plugin.py` with `@hookimpl` implementations of both and one decorated function
    matching no spec; and a `host.py` that registers the plugin and calls the hook — the
    dispatch site the PM's question is asked at. `README.md` — the convention every other
    documented fixture here uses (`tests/fixtures/clang_minimal/README.md`,
    `chunker_regression/README.md`, `scip_python_nested/README.md`), not a bespoke
    `FIXTURE-*.md` spelling — states, in two lines, what each file is for and which
    assertion depends on it, so a later edit cannot silently break a test's premise.

    Every path inside the fixture is relative and forward-slash; nothing references the
    repo root. Tests that index it copy it under `tmp_path` rather than indexing in place.

- id: C4
  title: "Read side: project_referencers surfaces dispatch_impl edges with their confidence tier"
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/symbol_references.py
  writes:
    - example_retrieval_repo_mcp/tools/symbol_references.py
    - tests/test_symbol_references_dispatch_impl.py
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: "needs C1's confidence-tier and kind vocabulary to name the edge kind it reads"
  queue_scope: project
  disposition: open
  traces_to_brief: "`project_referencers` and `project_trace` on the dispatch site return"
  body: |
    `project_referencers` on a non-asset-shaped name already routes through
    `fetch_symbol_references` (the `else` arm at `live.py:3012-3024`, reached because the
    backend is gated on `is_ue_asset_shaped and _is_ue_project` at `live.py:2996` — so a
    plain symbol name reaches the structural lane on a UE project too, not only on a
    non-UE one), so this is a read-side change in `symbol_references.py` — NO new door
    lane, and the `/Game/` asset path is not touched. Work:

      - Add `dispatch_impl` and `dispatch_impl_naming` (C2's two tier spellings) to the
        module's edge-kind vocabulary, `_KIND_TO_PRODUCER`, and to its
        `edge_kinds_in_response` disclosure, attributed to `dispatch_point_producer`
        exactly as `use`/`type_ref` are attributed today.
      - Add `dispatch_point_producer` to the producer ids this module reads substrate for,
        so an unrun producer is disclosed as an empty SUBSTRATE and not as an empty
        FINDING. The five response states, including `verdict='missing_index'`, keep their
        current meanings; a missing `dispatch_point_producer` with a populated
        `references` table is a caveat, never a verdict change.
      - Carry each returned dispatch edge's confidence tier into the response and into the
        hint text, in the module's existing heuristic-not-compiler-grade register.

    **This row alone does NOT make the live door answer.** `fetch_symbol_references`'s
    `producer_ids=_REFERENCES_PRODUCERS` default is the TEST default — "the real server
    passes an explicit `producer_ids=` at registration ... `.host_producer_ids`"
    (`symbol_references.py` ~line 673). Until C6 adds `dispatch_point_producer` to
    `project_referencers`' registry `host_producer_ids`, a direct-call test here passes
    while the production entry point returns nothing. C4 and C6 stay parallel (disjoint
    files), but C7 is the only row that proves the wire path, and neither C4 nor C6 may be
    closed as "referencers works" on its own evidence.

    Tests insert `dispatch_impl` / `dispatch_impl_naming` rows directly (the
    `tests/test_symbol_references.py` `_insert_reference` pattern) so this row needs neither
    C2 nor C3 to be verifiable, and assert the tier reaches the response, the producer
    appears in `producers_available`, and a dispatch edge never reads as `STATIC`/`EXPLICIT`.

    **Insert them in C2's orientation — `symbol_id` = hookspec, `context_id` = hookimpl — and
    query on the hookspec name.** A hand-inserted row in the other orientation passes a
    direct-call test and cannot be reached by any real `project_referencers` question, which
    is the one way this row can go green against an unusable schema. Add an explicit
    assertion that the queried name is the SPEC and the returned context is the IMPL.

- id: C5
  title: "project_trace start_layer='code': traverse structural symbols/references incl. dispatch edges"
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/live.py
  writes:
    - example_retrieval_repo_mcp/tools/dispatch_trace.py
    - example_retrieval_repo_mcp/tools/live.py
    - tests/test_dispatch_point_trace_lane.py
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: "needs C1's edge/confidence vocabulary to shape the traversal's disclosure"
  queue_scope: project
  disposition: open
  traces_to_brief: "`project_referencers` and `project_trace` on the dispatch site return"
  body: |
    The genuinely new ground. `project_trace` today admits five UE-shaped start layers
    (`live.py:1049`) and, on a project with no `.uproject`, short-circuits to an empty
    answer before `_sql_project_trace` runs (`live.py:3810`) — so a Python hookspec cannot
    be named at all.

      - New `example_retrieval_repo_mcp/tools/dispatch_trace.py`: a BFS over
        `structural_project.sqlite3`'s `symbols` + `references` (all kinds, dispatch edges
        included), with the same `direction` / `max_depth` / per-edge confidence semantics
        the cross-layer traversal already exposes. **The payload is EDGE-LIST-shaped, not
        node/edge-shaped** (AP9): `project_trace`'s `data` is `{edges, start, start_layer}`
        (`live.py:3812`) with no `nodes` key, and the post-processing at `live.py:3874-3890`
        filters and strips `data["edges"]` BY NAME. The code lane returns exactly that key
        with per-edge fields of the same names the existing edges carry, or its hops are
        silently dropped by code that never sees them — which is also why no envelope test
        has to change. Each hop carries its edge `kind` and confidence tier; a dispatch hop
        is visibly `inferred`. If a per-edge field the existing strippers assume turns out
        to be absent from a structural row, the lane synthesises it rather than renaming the
        key, and C5 records which fields it synthesised.
      - `live.py`: admit `code` as a start layer and route it to the new module BEFORE the
        no-UE short-circuit and before `_sql_project_trace`'s `valid_layers` check — the
        five existing layers keep their exact current behaviour, including the
        short-circuit.
      - **`confidence_min` must be handled explicitly or the exit criterion fails.** Its
        signature default is `"deterministic"` (`live.py:3802`) over `CONFIDENCE_LEVELS`
        (`live.py:700`: deterministic 3 / heuristic 2 / inferred 1), so applying the
        existing filter unchanged to the code lane drops EVERY dispatch hop on a default
        `project_trace(start=..., start_layer='code')` call — the PM's own done-condition
        asked at the default. Decision: on `start_layer='code'` an UNPASSED `confidence_min`
        resolves to `'inferred'` (the lane's floor), a caller-PASSED value is honoured
        verbatim, and the response's `parameters_not_applied`/hint discloses which of the
        two happened. A `confidence_min='deterministic'` the caller actually passed still
        filters the dispatch hops out, which is the point of the tier. The mapping
        `CONFIDENCE_TIERS.INFERRED/NAMING -> CONFIDENCE_LEVELS 'inferred'` is written once,
        in this module, and is the only place the two vocabularies meet.
      - Preserve the existing substrate/`parameters_not_applied` disclosure discipline on
        the new lane, reading `dispatch_point_producer` + the structural producers through
        `_producer_substrate.py`.

    Tests drive the lane against a `tmp_path` structural DB with hand-inserted rows, and
    assert: the code lane returns the impl from a hookspec start WITH NO `confidence_min`
    PASSED (the default-resolution decision above); an explicitly passed `confidence_min=
    'deterministic'` drops the dispatch hop and says so in the disclosure; an unknown start
    layer still errors as today; and the UE layers' short-circuit answer is byte-identical
    to baseline.

- id: C6
  title: "Advertise the lane: registry routing, producer deps, instructions metadata"
  change_kind: code-edit
  surface: example_retrieval_repo_mcp/tools/registry.py
  writes:
    - example_retrieval_repo_mcp/tools/registry.py
    - tests/test_dispatch_point_routing.py
  depends_on:
    - chunk: C1
      gate_kind: epistemic-premise
      note: "needs the detector/edge vocabulary to word the routing note and producer ids"
  queue_scope: project
  disposition: open
  traces_to_brief: "the implementations that will actually run"
  body: |
    The capability's reachable entry — without this row the lane exists and no caller is
    told (§ Anti-scope). `registry.py` carries the per-tool `ProducerDependency` and the
    routing notes that `example_retrieval_repo_instructions()` GENERATES its tables from, so this is
    the nav entry for a tool-shaped product.

      - `project_trace`'s `ProducerDependency.host(...)` gains `dispatch_point_producer`
        and the structural producers the code lane reads, and its routing note states that
        `start_layer='code'` traverses the structural reference graph including inferred
        dispatch-point edges — and that those edges are inferred.
      - `project_referencers`' entry is extended, not re-pointed: its asset lane stays
        `ProducerDependency.addon_gated("example-retrieval-repo-ue-addon", "asset_registry")`, with
        the code lane's host producers declared alongside per the already-resolved
        made-to-answer disposition, which is also what puts `dispatch_point_producer` into
        the `host_producer_ids` the server passes as `fetch_symbol_references`'
        `producer_ids=` — without it C4's read-side change is unreachable from the door
        (see C4), so the two rows are jointly necessary and neither is sufficient.
      - Negative-spec comment at both sites: the UE asset lane's addon gating is not
        weakened by the code lane's presence.

    Tests assert the generated routing/instructions text names the `code` start layer and
    the word inferred, and that every producer id declared here resolves to a real
    `producer_ledger` write site (the existing discipline the surrounding comments cite).

- id: C7
  title: "End-to-end fixture proof, wiki page, and DELIVERY.md"
  change_kind: code-edit
  surface: tests/test_dispatch_point_end_to_end.py
  writes:
    - tests/test_dispatch_point_end_to_end.py
    - docs/wiki/dispatch-point-inference/dispatch-point-inference.md
    - docs/wiki/DIRECTORY_GUIDE.md
    - docs/wiki/mcp-tools/envelope-verdict-contract.md
    - DELIVERY.md
  depends_on:
    - chunk: C2
      gate_kind: output-consumption-runtime
      note: "indexes the fixture with the real producer and reads the rows it wrote"
    - chunk: C3
      gate_kind: output-consumption-runtime
      note: "the fixture project under test"
    - chunk: C4
      gate_kind: output-consumption-runtime
      note: "asserts the referencers answer this chunk produces"
    - chunk: C5
      gate_kind: output-consumption-runtime
      note: "asserts the trace answer this chunk produces"
    - chunk: C6
      gate_kind: output-consumption-runtime
      note: "asserts the advertised routing text names the lane"
  queue_scope: project
  disposition: open
  traces_to_brief: "It is done when, on a small fixture project with a hookspec and a"
  body: |
    The prime exit criterion's falsifier, plus the PM's named deliverable.

      - `tests/test_dispatch_point_end_to_end.py`: copy `tests/fixtures/pluggy_dispatch/`
        under `tmp_path`, run the structural producers plus `dispatch_point_producer`, then
        call `project_referencers` on the hookspec name and `project_trace` with
        `start_layer='code'` on the same name, passing no `confidence_min`. Assert the
        hookimpl appears in BOTH, each dispatch edge carries `INFERRED` (or `NAMING`) and
        is reached through the real tool entry points (not `fetch_symbol_references`
        directly), neither reads `STATIC`/`EXPLICIT`, the
        non-conventionally-named hookspec also resolves (generality), and the decorated
        function matching no spec yields no edge. Plus the no-regression arm: the UE asset
        lane and the five UE trace layers answer exactly as at baseline.
      - `docs/wiki/dispatch-point-inference/dispatch-point-inference.md`: what a dispatch
        point is here, how to register a second detector, why the edge is inferred and
        never deterministic, and why it is NOT in `graph.db`. Indexed from
        `docs/wiki/DIRECTORY_GUIDE.md` — an unindexed wiki page is not findable.
      - `docs/wiki/mcp-tools/envelope-verdict-contract.md:35` currently documents
        `project_trace`/`project_referencers` **on a non-UE project** as the worked example of
        `inert-by-design`. This plan makes that row false for the code lane, and a stale
        contract row reads as settled behaviour (the exact failure CLAUDE.md § "What this file
        is" names). Narrow the row to the UE-shaped lanes and name the code lane as the
        answering path; do NOT delete the row, and do not weaken the
        `not-applicable-on-this-install` row beneath it. If
        `tests/test_envelope_verdict_contract.py` asserts that row's text, update the
        assertion with the row and say so in close-out.
      - `DELIVERY.md` at the repo root, with the PM's four named sections: what was built;
        the product judgement (persist-in-structural-references over a graph.db edge_type
        over read-time-only, pluggy-detector-behind-a-general-seam, what was left out); how
        it fits (v4 freeze, DR-2041 keying, CONFIDENCE_TIERS, manifest registration, the
        heuristic-not-compiler-grade register, and the resolved `project_referencers`
        disposition); and what is next plus the least-confident part.
```
