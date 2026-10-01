"""
coordinator_core.ops.dispatch_emit.wake_digest — the wake-digest contract.

Purpose: the single loader/generator for `coordinator_core/contract/wake-digest.schema.json`
(§ Design D1, docs/plans/2026-09-27-emitter-dag-terminal-commit-wake-digest.md). Everything
that reads or emits the schema goes through this module — no caller re-parses the JSON file
or hand-writes a `$defs` literal.

Pure module: `load_schema` does exactly one file read (cached); `completion_return_js` and
`stage_schema_literal` do no I/O of their own; `validate_digest` lazily imports `jsonschema`
so importing this module never pulls in a validator dependency for callers that only need
the JS generator (the emitted Workflow script itself never imports Python).

completion_return_js's field table is declarative and covers exactly the schema's required
properties, recursively (AC13) — `_required_paths` walks the schema itself so the table can
never silently drift from the contract it renders. Every string-typed leaf is wrapped in
`_cap(expr, n)` with `n` read from that leaf's own `maxLength`, so a cap here can never
diverge from the schema's cap.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Optional

_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "contract" / "wake-digest.schema.json"

# The script-level arrays and flag C12 declares; completion_return_js references only
# these (plus stage-result bindings and emitter literals) — never an executor's own reply.
RUNTIME_VARS = (
    "_incompleteChunks",
    "_unansweredBriefs",
    "_stoppedBy",
    "_notStarted",
    "_halted",
    "_verifications",
)

# An observation that says the criterion is not met, or that what matched was the
# baseline (pre-change) state. A JS regex literal, rendered into the emitted script.
_CRITERION_CONTRADICTION_RE_JS = (
    r"/\bnot (?:yet )?met\b|\bbaseline (?:still )?match(?:es|ed)\b|\bmatch(?:es|ed)? (?:the )?baseline\b/i"
)

_CAP_HELPER_JS = (
    "function _cap(s, n) { "
    "if (s === null || s === undefined) return null; "
    "s = String(s); "
    "return s.length > n ? s.slice(0, n) : s; "
    "}"
)


@functools.cache
def load_schema() -> dict:
    """The wake-digest schema, read from disk exactly once per process."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def stage_schema_literal(name: str) -> str:
    """One of the schema's own `$defs` entries, rendered as a JS object literal.

    Used as an `agent()` call's `schema:` value so the test-runner and falsifier stages
    are pinned to exactly the shapes this contract defines — never a hand-restated copy.
    """
    schema = load_schema()
    defs = schema.get("$defs", {})
    if name not in defs:
        raise KeyError(f"wake-digest schema has no $defs entry {name!r}")
    return json.dumps(defs[name], sort_keys=True)


def _resolve_ref(root: dict, node: dict) -> dict:
    ref = node.get("$ref")
    if not ref:
        return node
    if not ref.startswith("#/"):
        raise ValueError(f"wake-digest schema carries an unsupported $ref {ref!r}")
    target = root
    for part in ref[2:].split("/"):
        target = target[part]
    return target


def _object_candidates(root: dict, node: dict) -> list:
    """The object-shaped schema(s) a (possibly nullable / $ref'd) node resolves to."""
    node = _resolve_ref(root, node)
    if "anyOf" in node:
        out = []
        for cand in node["anyOf"]:
            cand = _resolve_ref(root, cand)
            if cand.get("type") == "null":
                continue
            out.extend(_object_candidates(root, cand))
        return out
    if "properties" in node:
        return [node]
    return []


def _required_paths_for_node(root: dict, node: dict, prefix: str) -> list:
    node = _resolve_ref(root, node)
    if node.get("type") == "array":
        items = node.get("items", {})
        return _required_paths_for_node(root, items, f"{prefix}[]")
    paths: list = []
    for obj in _object_candidates(root, node):
        for name in obj.get("required", []):
            full = f"{prefix}.{name}"
            paths.append(full)
            paths.extend(_required_paths_for_node(root, obj["properties"][name], full))
    return paths


def _required_paths(schema: dict) -> list:
    """Every required property path in the schema, recursively, dot-joined with
    `[]` marking an array's items. Declaration order, depth-first — matches the
    order `completion_return_js`'s field table is written in.
    """
    paths: list = []
    for name in schema.get("required", []):
        paths.append(name)
        paths.extend(_required_paths_for_node(schema, schema["properties"][name], name))
    return paths


def _maxlength(schema: dict, path: str) -> int:
    node = schema
    for part in path.split("."):
        arr = part.endswith("[]")
        key = part[:-2] if arr else part
        node = _resolve_ref(schema, node)
        candidates = node.get("anyOf")
        if candidates:
            node = next(
                (c for c in (_resolve_ref(schema, c) for c in candidates) if c.get("type") != "null"),
                candidates[0],
            )
        node = node["properties"][key]
        if arr:
            node = _resolve_ref(schema, node)["items"]
    node = _resolve_ref(schema, node)
    if "anyOf" in node:
        node = next(
            (c for c in (_resolve_ref(schema, c) for c in node["anyOf"]) if c.get("type") != "null"),
            node,
        )
    if "maxLength" not in node:
        raise ValueError(f"wake-digest schema leaf {path!r} carries no maxLength to cap against")
    return node["maxLength"]


def _js_lit(value) -> str:
    """A Python literal (str/int/bool/None/list-of-str) rendered as JS source."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_js_lit(v) for v in value) + "]"
    raise TypeError(f"no JS literal rendering for {value!r}")


def completion_return_js(
    *,
    chunks,
    width: dict,
    plan_path,
    deliverable_id,
    run_base_sha,
    test_var: Optional[str],
    test_absent_status: str,
    test_absent_note,
    verification_var: str,
    skipped_rows,
    falsifier_var: Optional[str],
    review_vars: Optional[dict],
    has_commit_request: bool,
    script_path: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """The emitted script's terminal `return { ... };`, plus the `_cap` helper it uses.

    Built from one declarative field table (`_TABLE` below) mapping every schema-required
    path to a JS source expression. `_TABLE`'s key set is asserted equal to
    `_required_paths(load_schema())` on every call (AC13) — the table cannot silently
    drift from the contract it renders. Expressions reference only `RUNTIME_VARS`,
    stage-result bindings the caller passes in (`test_var`, `verification_var`,
    `falsifier_var`, `review_vars`), and emitter-computed literals (`chunks`, `width`,
    `plan_path`, ...) — never an executor's own free-text reply.
    """
    schema = load_schema()

    def review_field(name: str, default: str = "null") -> str:
        if not review_vars:
            return default
        return review_vars.get(name, default)

    prep_var = review_field("prep")
    wave_var = review_field("wave")
    delivery_var = review_field("delivery")
    integration_var = review_field("integration")
    review_status_expr = (
        f"({integration_var} ? 'integrated' : (({prep_var} || {wave_var} || {delivery_var}) ? 'unstructured' : 'not_run'))"
        if review_vars
        else "'not_run'"
    )

    test_present = test_var is not None
    tests_status_expr = (
        f"({verification_var}.some(v => v && v.status === 'fail') ? 'fail' : "
        f"({test_var} ? {test_var}.status : {_js_lit(test_absent_status)}))"
        if test_present
        else f"({verification_var}.some(v => v && v.status === 'fail') ? 'fail' : {_js_lit(test_absent_status)})"
    )

    falsifier_present = falsifier_var is not None
    # The falsifier agent self-reports `status` and has reported `met` for "the
    # observation matches the BASELINE" -- the pre-change state, i.e. NOT met --
    # while its own observation said "not yet met". `met` is the value that
    # stamps a plan implemented (terminal_commit._stamp_plan_implemented), so a
    # `met` whose observation contradicts it is demoted; the safe direction.
    criterion_status_expr = (
        f"({falsifier_var} ? (({falsifier_var}.status === 'met' && "
        + _CRITERION_CONTRADICTION_RE_JS
        + f".test({falsifier_var}.observation ?? '')) ? 'not_met' : {falsifier_var}.status) : 'not_run')"
        if falsifier_present
        else "'not_run'"
    )

    next_action_kind = "'terminal_commit'" if has_commit_request else "'none'"
    next_action_op = "'dispatch.terminal_commit'" if has_commit_request else "null"
    if has_commit_request:
        # C11's integration stage result (`_reviewIntegration`) carries no
        # `integration_stem`/`slices` of its own post-review-integrator-
        # retirement -- only `fixes_applied`. `integration_stem` is derived
        # here from the integration sidecar path the result DOES carry
        # (`sidecar_path`, basename minus `.md` -- what `review_stamp._
        # resolve_terminal_commit`'s `applies <stem>` trailer match expects,
        # § `_find_sidecar_by_stem`); `slices` comes from the prep stage's
        # own `.slices.length` (the same value `review.slices` above uses),
        # never from the integration result. Example-retrieval-repo EM memo
        # 2026-09-28-example-retrieval-repo-em-terminal-commit-inline-review-trailer-none.
        #
        # Zero-integration-stage path (2026-09-28 PM order, step b' -- no
        # `_reviewIntegration` binding exists at all): `inline_review` points
        # at the mechanical bookkeeping record instead
        # (`review_mint.wave_bookkeeping.bookkeep_wave`'s one write), named by
        # a stem the emitter already knows at compose time
        # (`review_vars["bookkeeping_stem"]`, a JS string literal) -- no
        # runtime sidecar_path derivation needed since the record's location
        # is deterministic, not agent-chosen. `slices`/`fixes` are computed
        # here from the review-wave results directly (the same source the
        # bookkeeping step itself reads), since the record does not exist
        # yet at the point this script computes its own return value.
        # The run record `dispatch.terminal_commit` writes and `review_stamp.
        # mint` reads: every stage's schema-validated RETURN, never a field an
        # agent was trusted to copy into its own sidecar frontmatter.
        record_fields_expr = ""
        if review_vars:
            test_num = lambda field: f"({test_var} ? {test_var}.{field} ?? null : null)" if test_present else "null"
            record_fields_expr = (
                ", plan_id: " + review_vars.get("plan_id_literal", "null")
                + ", wave_sidecar_paths: "
                + f"(Array.isArray({wave_var}) ? {wave_var}.map(r => r && r.sidecar_path).filter(Boolean) : [])"
                + ", prep: "
                + f"({prep_var} ? {{ run_base_sha: {prep_var}.run_base_sha ?? null, "
                + f"product_files: {prep_var}.product_files ?? null, "
                + f"foreign_claims: {prep_var}.foreign_claims ?? [], "
                + f"slice_files: ({prep_var}.slices ?? []).flatMap(s => (s && s.files) || []) }} : null)"
                + ", delivery: "
                + f"({delivery_var} ? {{ verdict: {delivery_var}.verdict ?? null, "
                + f"product_files: {delivery_var}.product_files ?? null, "
                + f"claims_unbacked: ({delivery_var}.claims_unbacked ?? []).length }} : null)"
                + f", tests: {{ status: {tests_status_expr}, run: {test_num('tests_run')}, "
                + f"failed: {test_num('tests_failed')}, sidecar: {test_num('sidecar_path')} }}"
                + ", criterion: "
                + (
                    f"({falsifier_var} ? {{ status: {criterion_status_expr}, "
                    f"observation: {falsifier_var}.observation ?? null, "
                    f"sidecar: {falsifier_var}.sidecar_path ?? null }} "
                    ": { status: 'not_run', observation: null, sidecar: null })"
                    if falsifier_present
                    else "{ status: 'not_run', observation: null, sidecar: null }"
                )
            )
        if review_vars and review_vars.get("integration"):
            # The trailer names the engine's record (`bookkeeping_stem`), not
            # the integrator's own sidecar: that sidecar's frontmatter is
            # agent-written and carried none of what `mint` reads.
            integration_stem_expr = review_vars.get("bookkeeping_stem") or (
                "(" + integration_var + "?.sidecar_path ? "
                "String(" + integration_var + ".sidecar_path).split('/').pop().replace(/\\.md$/, '') : null)"
            )
            slices_count_expr = f"({prep_var} ? {prep_var}.slices.length : null)"
            inline_review_expr = (
                "(" + integration_var + " ? { integration_stem: " + integration_stem_expr + ", "
                "slices: " + slices_count_expr + ", fixes: " + integration_var + ".fixes_applied"
                + record_fields_expr
                + ", integration: { sidecar: " + integration_var + ".sidecar_path ?? null, "
                "unresolved: " + integration_var + ".unresolved ?? [], "
                "confinement_violations: " + integration_var + ".confinement_violations ?? 0 }"
                " } : null)"
            )
        elif review_vars and review_vars.get("bookkeeping_stem"):
            # Zero-stage `inline_review` carries everything
            # `review_mint.wave_bookkeeping.bookkeep_wave` needs (PM
            # follow-up, 2026-09-28): `dispatch.terminal_commit` runs
            # `bookkeep_wave` FIRST on this path (keyed off
            # `integration_stem` being a bookkeeping stem, i.e. no matching
            # sidecar of its own), using these very params, before it builds
            # the commit -- so the record lands in the same commit as the
            # code it reviews.
            stem_lit = review_vars["bookkeeping_stem"]
            prep_stem_lit = review_vars.get("prep_label_stem_literal")
            prep_sidecar_expr = (
                f"({prep_var} && {prep_var}.share_dir ? "
                f"{prep_var}.share_dir + '/' + {prep_stem_lit} + '.md' : null)"
                if prep_stem_lit
                else "null"
            )
            inline_review_expr = (
                "(" + wave_var + " ? { integration_stem: " + stem_lit + ", "
                "slices: " + wave_var + ".length, "
                "fixes: " + wave_var + ".reduce((n, r) => n + ((r && r.applied) || 0), 0), "
                "prep_sidecar: " + prep_sidecar_expr
                + record_fields_expr
                + " } : null)"
            )
        else:
            inline_review_expr = "null"
        # `script_path`/`session_id` make the params `dispatch.terminal_commit`
        # takes verbatim; either is omitted, never emitted null, when unknown.
        # A row a halt kept from starting never landed either: terminal_commit
        # stamps every row NOT named here as coded.
        params_expr = (
            "{ incomplete_chunks: [...new Set([..." + RUNTIME_VARS[0]
            + ", ..." + RUNTIME_VARS[3] + "])], "
            + (f"script_path: {_js_lit(script_path)}, " if script_path else "")
            + (f"session_id: {_js_lit(session_id)}, " if session_id else "")
            + "inline_review: " + inline_review_expr
            + " }"
        )
    else:
        params_expr = "null"

    outcome_expr = (
        f"({RUNTIME_VARS[4]} ? 'halted' : "
        f"(({RUNTIME_VARS[0]}.length || {RUNTIME_VARS[1]}.length || {RUNTIME_VARS[3]}.length) ? 'incomplete' : 'completed'))"
    )
    completed_expr = f"({outcome_expr} === 'completed')"

    decision_required_expr = (
        f"({RUNTIME_VARS[4]} ? {RUNTIME_VARS[4]} : "
        + (
            f"({delivery_var} && {delivery_var}.verdict === 'FAIL' ? 'review delivery verdict FAIL"
            + ("; landed rows are UNCOMMITTED: next_action dispatch.terminal_commit commits them" if has_commit_request else "")
            + "' : "
            if review_vars
            else "("
        )
        + (f"({criterion_status_expr} === 'not_met' ? 'falsifier not met' : " if falsifier_present else "(")
        + f"({tests_status_expr} === 'fail' || {tests_status_expr} === 'error' ? 'tests failed' : "
        + (f"({integration_var} && {integration_var}.unresolved && {integration_var}.unresolved.length ? 'unresolved review notes' : " if review_vars else "(")
        + (f"({integration_var} && {integration_var}.rebuild_decision ? 'rebuild decision raised' : " if review_vars else "(")
        + (f"({integration_var} && {integration_var}.brief_conformance && {integration_var}.brief_conformance.unmet ? 'brief items unmet' : " if review_vars else "(")
        + f"({RUNTIME_VARS[0]}.length ? 'incomplete chunks' : "
        + f"({RUNTIME_VARS[1]}.length ? 'unanswered briefs: ' + {RUNTIME_VARS[1]}.join(', ') : null))"
        + ")))))))"
    )

    # A row an executor reported PARTIAL/BLOCKED (or answered without a status)
    # has WRITTEN files; only a row the halt kept from starting never did. Each
    # id is deduped (an unanswered row rides both arrays) and named by what
    # happened to it, never blanket `not_started`.
    deviation_ids_expr = (
        f"[...new Set([...{RUNTIME_VARS[0]}, ...{RUNTIME_VARS[1]}, ...{RUNTIME_VARS[3]}])]"
    )
    deviation_kind_expr = (
        f"({RUNTIME_VARS[3]}.includes(id) ? 'not_started' : "
        f"({RUNTIME_VARS[2]}.includes(id) ? 'stop_rule' : "
        f"({RUNTIME_VARS[1]}.includes(id) ? 'no_answer' : 'partial')))"
    )

    table = {
        "schema": "'wake-digest'",
        "version": "1",
        "plan.path": _js_lit(plan_path),
        "plan.deliverable_id": _js_lit(deliverable_id),
        "outcome": outcome_expr,
        "completed": completed_expr,
        "halted": f"({RUNTIME_VARS[4]} ? _cap({RUNTIME_VARS[4]}, {_maxlength(schema, 'halted')}) : null)",
        "chunks": _js_lit(list(chunks)),
        "criterion.status": criterion_status_expr,
        "criterion.observation": (
            f"_cap({falsifier_var}?.observation ?? null, {_maxlength(schema, 'criterion.observation')})"
            if falsifier_present
            else "null"
        ),
        "criterion.sidecar": (
            f"_cap({falsifier_var}?.sidecar_path ?? null, {_maxlength(schema, 'criterion.sidecar')})"
            if falsifier_present
            else "null"
        ),
        "tests.status": tests_status_expr,
        "tests.run": f"({test_var} ? {test_var}.tests_run : null)" if test_present else "null",
        "tests.failed": f"({test_var} ? {test_var}.tests_failed : null)" if test_present else "null",
        "tests.build_clean": f"({test_var} ? {test_var}.build_clean : null)" if test_present else "null",
        "tests.note": (
            f"({test_var} ? _cap({test_var}.summary ?? null, {_maxlength(schema, 'tests.note')}) : {_js_lit(test_absent_note)})"
            if test_present
            else _js_lit(test_absent_note)
        ),
        "tests.sidecar": (
            f"({test_var} ? _cap({test_var}.sidecar_path ?? null, {_maxlength(schema, 'tests.sidecar')}) : null)"
            if test_present
            else "null"
        ),
        "tests.per_row.verified": f"{verification_var}.length",
        "tests.per_row.passed": f"{verification_var}.filter(v => v && v.status === 'pass').length",
        "tests.per_row.failed[].chunk": f"{verification_var}.filter(v => v && v.status !== 'pass').map(v => v.chunk)[0]",
        "tests.per_row.failed[].anchor": (
            f"_cap({verification_var}.filter(v => v && v.status !== 'pass')"
            f".map(v => v.sidecar_path ?? null)[0], "
            f"{_maxlength(schema, 'tests.per_row.failed[].anchor')})"
        ),
        "tests.per_row.unstructured": f"{RUNTIME_VARS[5]}.filter(v => v === null || v === undefined).map((v, i) => i)",
        "tests.per_row.skipped": _js_lit(list(skipped_rows)),
        "review.status": review_status_expr,
        "review.slices": f"({prep_var} ? {prep_var}.slices.length : null)" if review_vars else "null",
        "review.fixes_applied": f"({integration_var} ? {integration_var}.fixes_applied : null)" if review_vars else "null",
        "review.em_may_think_differently[].line": (
            f"_cap(({integration_var}?.em_may_think_differently ?? []).map(x => x.line)[0], "
            f"{_maxlength(schema, 'review.em_may_think_differently[].line')})" if review_vars else "null"
        ),
        "review.em_may_think_differently[].anchor": (
            f"_cap(({integration_var}?.em_may_think_differently ?? []).map(x => x.anchor)[0], "
            f"{_maxlength(schema, 'review.em_may_think_differently[].anchor')})" if review_vars else "null"
        ),
        "review.unresolved[].line": (
            f"_cap(({integration_var}?.unresolved ?? []).map(x => x.line)[0], "
            f"{_maxlength(schema, 'review.unresolved[].line')})" if review_vars else "null"
        ),
        "review.unresolved[].anchor": (
            f"_cap(({integration_var}?.unresolved ?? []).map(x => x.anchor)[0], "
            f"{_maxlength(schema, 'review.unresolved[].anchor')})" if review_vars else "null"
        ),
        "review.overflow": f"({integration_var}?.overflow ?? 0)" if review_vars else "0",
        "review.brief_conformance.items": (
            f"({integration_var}?.brief_conformance?.items ?? null)" if review_vars else "null"
        ),
        "review.brief_conformance.met": (
            f"({integration_var}?.brief_conformance?.met ?? null)" if review_vars else "null"
        ),
        "review.brief_conformance.unmet": (
            f"({integration_var}?.brief_conformance?.unmet ?? null)" if review_vars else "null"
        ),
        "review.rebuild_decision.line": (
            f"_cap({integration_var}?.rebuild_decision?.line ?? null, "
            f"{_maxlength(schema, 'review.rebuild_decision.line')})" if review_vars else "null"
        ),
        "review.rebuild_decision.anchor": (
            f"_cap({integration_var}?.rebuild_decision?.anchor ?? null, "
            f"{_maxlength(schema, 'review.rebuild_decision.anchor')})" if review_vars else "null"
        ),
        "review.delivery.verdict": f"({delivery_var}?.verdict ?? 'not_run')" if review_vars else "'not_run'",
        "review.delivery.product_files": f"({delivery_var}?.product_files ?? null)" if review_vars else "null",
        "review.delivery.claims_unbacked": (
            f"({delivery_var}?.claims_unbacked?.length ?? null)" if review_vars else "null"
        ),
        "review.integration_sidecar": (
            f"_cap({integration_var}?.sidecar_path ?? null, {_maxlength(schema, 'review.integration_sidecar')})"
            if review_vars
            else "null"
        ),
        "deviations[].chunk": f"{deviation_ids_expr}[0]",
        "deviations[].kind": deviation_kind_expr,
        "deviations[].anchor": f"_cap(null, {_maxlength(schema, 'deviations[].anchor')})",
        "run_base_sha": _js_lit(run_base_sha),
        "width.rows": _js_lit(width["rows"]),
        "width.max_concurrent_rows": _js_lit(width["max_concurrent_rows"]),
        "width.critical_path_rows": _js_lit(width["critical_path_rows"]),
        "width.runtime_cap": "'min(16, CPUs-2)'",
        "width.runtime_cap_on_emitting_host": _js_lit(width["runtime_cap_on_emitting_host"]),
        "decision_required": f"_cap({decision_required_expr}, {_maxlength(schema, 'decision_required')})",
        "next_action.kind": next_action_kind,
        "next_action.op": next_action_op,
        "next_action.params": params_expr,
        "next_action.params.incomplete_chunks": _js_lit([]),
        "next_action.params.inline_review": "null",
        "next_action.params.inline_review.integration_stem": "null",
        "next_action.params.inline_review.slices": "null",
        "next_action.params.inline_review.fixes": "null",
    }

    required_list = _required_paths(schema)
    required = set(required_list)
    # A composite (object/array) path is discharged by its own leaves being present in
    # the table, not by a table entry of its own — e.g. "tests.per_row" needs no direct
    # entry once "tests.per_row.verified" etc. are covered. Fold every such ancestor out
    # of the coverage check.
    composite = {
        p for p in required_list
        if any(other != p and other.startswith(p + ".") or other.startswith(p + "[]") for other in required_list)
    }
    leaves = required - composite
    missing = leaves - set(table)
    if missing:
        raise AssertionError(f"completion_return_js field table missing schema paths: {sorted(missing)}")

    lines = [_CAP_HELPER_JS, ""]
    lines.append("return {")
    lines.append(f"  schema: {table['schema']},")
    lines.append(f"  version: {table['version']},")
    lines.append("  plan: { path: %s, deliverable_id: %s }," % (table["plan.path"], table["plan.deliverable_id"]))
    lines.append(f"  outcome: {table['outcome']},")
    lines.append(f"  completed: {table['completed']},")
    lines.append(f"  halted: {table['halted']},")
    lines.append(f"  chunks: {table['chunks']},")
    lines.append(
        "  criterion: { status: %s, observation: %s, sidecar: %s },"
        % (table["criterion.status"], table["criterion.observation"], table["criterion.sidecar"])
    )
    lines.append("  tests: {")
    lines.append(f"    status: {table['tests.status']},")
    lines.append(f"    run: {table['tests.run']},")
    lines.append(f"    failed: {table['tests.failed']},")
    lines.append(f"    build_clean: {table['tests.build_clean']},")
    lines.append(f"    note: {table['tests.note']},")
    lines.append(f"    sidecar: {table['tests.sidecar']},")
    lines.append("    per_row: {")
    lines.append(f"      verified: {table['tests.per_row.verified']},")
    lines.append(f"      passed: {table['tests.per_row.passed']},")
    lines.append(
        "      failed: (%s !== undefined ? [{ chunk: %s, anchor: %s }] : [])."
        % (table["tests.per_row.failed[].chunk"], table["tests.per_row.failed[].chunk"], table["tests.per_row.failed[].anchor"])
        + "filter(x => x.chunk !== undefined),"
    )
    lines.append(f"      unstructured: {table['tests.per_row.unstructured']},")
    lines.append(f"      skipped: {table['tests.per_row.skipped']},")
    lines.append("    },")
    lines.append("  },")
    lines.append("  review: {")
    lines.append(f"    status: {table['review.status']},")
    lines.append(f"    slices: {table['review.slices']},")
    lines.append(f"    fixes_applied: {table['review.fixes_applied']},")
    lines.append(
        "    em_may_think_differently: (%s !== undefined ? [{ line: %s, anchor: %s }] : [])."
        % (
            table["review.em_may_think_differently[].line"],
            table["review.em_may_think_differently[].line"],
            table["review.em_may_think_differently[].anchor"],
        )
        + "filter(x => x.line !== undefined && x.line !== null),"
    )
    lines.append(
        "    unresolved: (%s !== undefined ? [{ line: %s, anchor: %s }] : [])."
        % (table["review.unresolved[].line"], table["review.unresolved[].line"], table["review.unresolved[].anchor"])
        + "filter(x => x.line !== undefined && x.line !== null),"
    )
    lines.append(f"    overflow: {table['review.overflow']},")
    lines.append(
        "    brief_conformance: (%s !== null ? { items: %s, met: %s, unmet: %s } : null),"
        % (
            table["review.brief_conformance.items"],
            table["review.brief_conformance.items"],
            table["review.brief_conformance.met"],
            table["review.brief_conformance.unmet"],
        )
    )
    lines.append(
        "    rebuild_decision: (%s !== null ? { line: %s, anchor: %s } : null),"
        % (table["review.rebuild_decision.line"], table["review.rebuild_decision.line"], table["review.rebuild_decision.anchor"])
    )
    lines.append(
        "    delivery: { verdict: %s, product_files: %s, claims_unbacked: %s },"
        % (table["review.delivery.verdict"], table["review.delivery.product_files"], table["review.delivery.claims_unbacked"])
    )
    lines.append(f"    integration_sidecar: {table['review.integration_sidecar']},")
    lines.append("  },")
    lines.append(
        "  deviations: %s.map(id => ({ chunk: id, kind: %s, anchor: %s })),"
        % (
            deviation_ids_expr,
            table["deviations[].kind"],
            table["deviations[].anchor"],
        )
    )
    lines.append(f"  run_base_sha: {table['run_base_sha']},")
    lines.append(
        "  width: { rows: %s, max_concurrent_rows: %s, critical_path_rows: %s, "
        "runtime_cap: %s, runtime_cap_on_emitting_host: %s },"
        % (
            table["width.rows"],
            table["width.max_concurrent_rows"],
            table["width.critical_path_rows"],
            table["width.runtime_cap"],
            table["width.runtime_cap_on_emitting_host"],
        )
    )
    lines.append(f"  decision_required: {table['decision_required']},")
    lines.append(
        "  next_action: { kind: %s, op: %s, params: %s },"
        % (table["next_action.kind"], table["next_action.op"], table["next_action.params"])
    )
    lines.append("};")
    return "\n".join(lines)


def validate_digest(obj: dict) -> list:
    """Validate `obj` against the wake-digest schema; returns a list of error strings
    (empty when valid). Lazy `jsonschema` import — only paid by callers that validate.
    """
    import jsonschema

    validator_cls = jsonschema.validators.validator_for(load_schema())
    validator_cls.check_schema(load_schema())
    validator = validator_cls(load_schema())
    return [str(err) for err in validator.iter_errors(obj)]
