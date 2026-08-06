"""
coordinator_core.ops.tests.test_workflow_contract

Unit tests for the Workflow-script pattern-contract SSOT
(coordinator_core/ops/_workflow_contract.py) — the shared scrubber, meta
extraction + pure-literal check, forbidden-globals (ERROR), phase-mismatch
set-diff on both surfaces (WARN), barrier-vs-pipeline heuristic (WARN), and
model-default heuristic (WARN).

Coverage:
    (a) scrub — strings/templates/comments masked, line count preserved
    (b) scrub — escaped delimiters do not prematurely close a span
    (c) extract_meta_block — well-formed + malformed (missing/unterminated)
    (d) check_meta_pure_literal — each impure shape flagged
    (e) check_meta_required_fields — missing field flagged
    (f) check_forbidden_globals — each forbidden global flagged, scrubbed text
    (g) F1 fixture — "Date.now()" inside a template literal does NOT trip
        the forbidden-global check
    (h) phase-mismatch — both phase()-call and agent-options phase: surfaces
    (i) barrier-vs-pipeline heuristic — flags the shape, spares a legit
        agent-mediated barrier
    (j) model-default heuristic — flags model-less agent(), spares an
        explicit model: call
    (k) run_checks — end-to-end severity map (ERROR only impure-meta +
        forbidden-globals)

Spec backlink: docs/plans/2026-07-12-workflow-skeleton-stamper-claude-klabauter-engine.md § C1
"""

from __future__ import annotations

from coordinator_core.ops._workflow_contract import (
    Severity,
    agent_options_phase_titles,
    check_barrier_vs_pipeline,
    check_forbidden_globals,
    check_meta_pure_literal,
    check_meta_required_fields,
    check_model_default,
    check_phase_mismatch,
    extract_meta_block,
    meta_phase_titles,
    phase_titles,
    run_checks,
    scrub,
)


# ---------------------------------------------------------------------------
# scrub()
# ---------------------------------------------------------------------------


def test_scrub_masks_single_quote_string_contents():
    src = "const x = 'Date.now() lives here';"
    out = scrub(src)
    assert "Date.now()" not in out
    assert out.startswith("const x = '")
    assert out.endswith("';")


def test_scrub_masks_double_quote_string_contents():
    src = 'const x = "Math.random() lives here";'
    out = scrub(src)
    assert "Math.random()" not in out


def test_scrub_masks_template_literal_contents_including_interpolation():
    src = "const x = `hello ${Date.now()} world`;"
    out = scrub(src)
    assert "Date.now()" not in out
    assert "${" not in out or out.count("${") == 0


def test_scrub_masks_line_comment_contents():
    src = "// Date.now() is forbidden\nconst x = 1;"
    out = scrub(src)
    assert "Date.now()" not in out
    assert "const x = 1;" in out


def test_scrub_masks_block_comment_contents():
    src = "/* Math.random() note */\nconst y = 2;"
    out = scrub(src)
    assert "Math.random()" not in out
    assert "const y = 2;" in out


def test_scrub_preserves_line_count():
    src = "line1\n'a string\nspanning\nlines'\nline5\n// comment\nline7"
    out = scrub(src)
    assert out.count("\n") == src.count("\n")


def test_scrub_preserves_non_string_code_verbatim():
    src = "await parallel(items.map(x => agent(x)));"
    out = scrub(src)
    assert out == src


def test_scrub_honors_escaped_quote_inside_string():
    src = r"const x = 'it\'s Date.now() inside';"
    out = scrub(src)
    # the escaped quote must not end the string early — Date.now() stays masked
    assert "Date.now()" not in out
    # and the trailing `;` (real code after the string closes) survives
    assert out.endswith("';")


def test_scrub_handles_backslash_before_backtick_in_template():
    src = "const x = `a \\` b Date.now() c`;"
    out = scrub(src)
    assert "Date.now()" not in out


# ---------------------------------------------------------------------------
# extract_meta_block()
# ---------------------------------------------------------------------------


def test_extract_meta_block_well_formed():
    src = "export const meta = { name: 'x', description: 'y' };\nphase('p');"
    block = extract_meta_block(src)
    assert block is not None
    assert block.startswith("{")
    assert block.endswith("}")
    assert "name" in block


def test_extract_meta_block_missing_returns_none():
    src = "const notMeta = { name: 'x' };"
    assert extract_meta_block(src) is None


def test_extract_meta_block_unterminated_returns_none():
    src = "export const meta = { name: 'x', description: 'y'"
    assert extract_meta_block(src) is None


def test_extract_meta_block_nested_braces_balanced():
    src = "export const meta = { name: 'x', description: 'y', phases: ['a', 'b'] };"
    block = extract_meta_block(src)
    assert block is not None
    assert block.count("{") == block.count("}")


# ---------------------------------------------------------------------------
# check_meta_pure_literal()
# ---------------------------------------------------------------------------


def test_pure_literal_flags_interpolation():
    block = extract_meta_block("export const meta = { name: `${x}`, description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert any(f.code == "meta-impure-interpolation" for f in findings)
    assert all(f.severity == Severity.ERROR for f in findings)


def test_pure_literal_flags_backtick():
    block = extract_meta_block("export const meta = { name: `plain`, description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert any(f.code == "meta-impure-template-literal" for f in findings)


def test_pure_literal_flags_spread():
    block = extract_meta_block("export const meta = { ...base, description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert any(f.code == "meta-impure-spread" for f in findings)


def test_pure_literal_flags_call_shape():
    block = extract_meta_block("export const meta = { name: computeName(), description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert any(f.code == "meta-impure-call" for f in findings)


def test_pure_literal_flags_bare_identifier_value():
    block = extract_meta_block("export const meta = { name: someVar, description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert any(f.code == "meta-impure-bare-identifier" for f in findings)


def test_pure_literal_allows_js_literal_keywords_as_values():
    block = extract_meta_block(
        "export const meta = { name: 'x', description: 'y', model: undefined };"
    )
    findings = check_meta_pure_literal(block)
    assert not any(f.code == "meta-impure-bare-identifier" for f in findings)


def test_pure_literal_clean_meta_has_no_findings():
    block = extract_meta_block(
        "export const meta = { name: 'x', description: 'y', phases: ['a', 'b'] };"
    )
    findings = check_meta_pure_literal(block)
    assert findings == []


def test_pure_literal_allows_nan_and_infinity_as_bare_identifier_values():
    # Review: code-reviewer (Finding 5, nit) — NaN/Infinity are JS literal
    # values, not variable references; must not be flagged as bare identifiers.
    block = extract_meta_block(
        "export const meta = { name: 'x', description: 'y', retries: Infinity, delta: NaN };"
    )
    findings = check_meta_pure_literal(block)
    assert not any(f.code == "meta-impure-bare-identifier" for f in findings)


def test_pure_literal_messages_are_actionable():
    block = extract_meta_block("export const meta = { name: `x`, description: 'y' };")
    findings = check_meta_pure_literal(block)
    assert findings
    for f in findings:
        assert "literal" in f.message


# ---------------------------------------------------------------------------
# check_meta_required_fields()
# ---------------------------------------------------------------------------


def test_required_fields_flags_missing_description():
    block = extract_meta_block("export const meta = { name: 'x' };")
    findings = check_meta_required_fields(block)
    assert any(f.code == "meta-missing-description" for f in findings)
    assert all(f.severity == Severity.ERROR for f in findings)


def test_required_fields_flags_missing_name():
    block = extract_meta_block("export const meta = { description: 'y' };")
    findings = check_meta_required_fields(block)
    assert any(f.code == "meta-missing-name" for f in findings)


def test_required_fields_clean_when_both_present():
    block = extract_meta_block("export const meta = { name: 'x', description: 'y' };")
    findings = check_meta_required_fields(block)
    assert findings == []


# ---------------------------------------------------------------------------
# check_forbidden_globals() — ERROR tier, scrubbed text
# ---------------------------------------------------------------------------


def test_forbidden_global_date_now_flagged():
    scrubbed = scrub("const t = Date.now();")
    findings = check_forbidden_globals(scrubbed)
    assert any(f.code == "forbidden-global-date-now" for f in findings)
    assert all(f.severity == Severity.ERROR for f in findings)


def test_forbidden_global_math_random_flagged():
    scrubbed = scrub("const r = Math.random();")
    findings = check_forbidden_globals(scrubbed)
    assert any(f.code == "forbidden-global-math-random" for f in findings)


def test_forbidden_global_argless_new_date_flagged():
    scrubbed = scrub("const d = new Date();")
    findings = check_forbidden_globals(scrubbed)
    assert any(f.code == "forbidden-global-new-date" for f in findings)


def test_forbidden_global_new_date_with_arg_is_fine():
    scrubbed = scrub("const d = new Date(someTimestamp);")
    findings = check_forbidden_globals(scrubbed)
    assert not any(f.code == "forbidden-global-new-date" for f in findings)


def test_forbidden_global_messages_are_actionable():
    scrubbed = scrub("const t = Date.now();")
    findings = check_forbidden_globals(scrubbed)
    assert findings
    for f in findings:
        assert "Date.now()" in f.message or "new Date" in f.message


def test_forbidden_global_f1_fixture_template_literal_text_not_flagged():
    """F1: an agent() prompt template literal that literally CONTAINS the
    text "Date.now()" must NOT trigger the forbidden-global check — the
    scrubber masks string/template-literal contents before the regex runs."""
    src = (
        "await agent(`Explain why Date.now() is forbidden in this codebase`, "
        "{model: 'sonnet'});"
    )
    scrubbed = scrub(src)
    findings = check_forbidden_globals(scrubbed)
    assert findings == []


def test_forbidden_global_f1_fixture_in_comment_not_flagged():
    src = "// note: do not use Date.now() here\nconst x = 1;"
    scrubbed = scrub(src)
    findings = check_forbidden_globals(scrubbed)
    assert findings == []


def test_forbidden_global_real_call_outside_string_still_flagged_alongside_masked_mention():
    src = "// avoid Date.now()\nconst t = Date.now();"
    scrubbed = scrub(src)
    findings = check_forbidden_globals(scrubbed)
    assert len(findings) == 1
    assert findings[0].code == "forbidden-global-date-now"


def test_forbidden_global_date_now_spares_namespaced_property_access():
    # Review: code-reviewer (Finding 4, nit) — myObj.Date.now() is a property
    # access on a caller-defined object, not the global Date.now(); the
    # negative lookbehind (?<!\.) must spare it.
    scrubbed = scrub("const t = myObj.Date.now();")
    findings = check_forbidden_globals(scrubbed)
    assert not any(f.code == "forbidden-global-date-now" for f in findings)


def test_forbidden_global_math_random_spares_namespaced_property_access():
    scrubbed = scrub("const r = ctx.Math.random();")
    findings = check_forbidden_globals(scrubbed)
    assert not any(f.code == "forbidden-global-math-random" for f in findings)


def test_forbidden_global_date_now_still_flags_unqualified_global():
    scrubbed = scrub("const t = Date.now();")
    findings = check_forbidden_globals(scrubbed)
    assert any(f.code == "forbidden-global-date-now" for f in findings)


def test_forbidden_global_math_random_still_flags_unqualified_global():
    scrubbed = scrub("const r = Math.random();")
    findings = check_forbidden_globals(scrubbed)
    assert any(f.code == "forbidden-global-math-random" for f in findings)


# ---------------------------------------------------------------------------
# phase-mismatch — both surfaces, WARN only
# ---------------------------------------------------------------------------


def test_phase_titles_extracts_phase_call_sites():
    src = "phase('Scout'); phase('Analyze');"
    assert phase_titles(src) == {"Scout", "Analyze"}


def test_agent_options_phase_titles_extracts_options_object_field():
    src = "await agent('x', {phase: 'Fan-out', model: 'sonnet'});"
    assert agent_options_phase_titles(src) == {"Fan-out"}


def test_meta_phase_titles_extracts_declared_phases():
    block = extract_meta_block(
        "export const meta = { name: 'x', description: 'y', phases: ['Scout', 'Analyze'] };"
    )
    assert meta_phase_titles(block) == {"Scout", "Analyze"}


def test_meta_phase_titles_empty_when_no_phases_array():
    block = extract_meta_block("export const meta = { name: 'x', description: 'y' };")
    assert meta_phase_titles(block) == set()


def test_phase_call_mismatch_is_warn_not_error():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "phase('Scout');\n"
        "phase('Unlisted');\n"
    )
    block = extract_meta_block(src)
    findings = check_phase_mismatch(src, block)
    unlisted = [f for f in findings if f.code == "phase-call-title-mismatch"]
    assert len(unlisted) == 1
    assert unlisted[0].severity == Severity.WARN
    assert "Unlisted" in unlisted[0].message


def test_agent_options_phase_mismatch_is_warn_not_error():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "await agent('x', {phase: 'GhostPhase', model: 'sonnet'});\n"
    )
    block = extract_meta_block(src)
    findings = check_phase_mismatch(src, block)
    ghost = [f for f in findings if f.code == "agent-options-phase-title-mismatch"]
    assert len(ghost) == 1
    assert ghost[0].severity == Severity.WARN
    assert "GhostPhase" in ghost[0].message


def test_phase_mismatch_both_surfaces_independently_checked():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "phase('Unlisted1');\n"
        "await agent('x', {phase: 'Unlisted2', model: 'sonnet'});\n"
    )
    block = extract_meta_block(src)
    findings = check_phase_mismatch(src, block)
    codes = {f.code for f in findings}
    assert "phase-call-title-mismatch" in codes
    assert "agent-options-phase-title-mismatch" in codes
    assert all(f.severity == Severity.WARN for f in findings)


def test_phase_mismatch_clean_when_all_declared():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "phase('Scout');\n"
        "await agent('x', {phase: 'Scout', model: 'sonnet'});\n"
    )
    block = extract_meta_block(src)
    findings = check_phase_mismatch(src, block)
    assert findings == []


# ---------------------------------------------------------------------------
# barrier-vs-pipeline heuristic — WARN only, never hard-fails a legit barrier
# ---------------------------------------------------------------------------


def test_barrier_vs_pipeline_flags_double_parallel_with_no_agent_between():
    src = (
        "const a = await parallel(items.map(x => thunk(x)));\n"
        "const deduped = dedupe(a);\n"
        "const b = await parallel(deduped.map(x => thunk2(x)));\n"
    )
    findings = check_barrier_vs_pipeline(scrub(src))
    assert len(findings) == 1
    assert findings[0].code == "barrier-vs-pipeline"
    assert findings[0].severity == Severity.WARN
    assert "pipeline()" in findings[0].message


def test_barrier_vs_pipeline_spares_legit_barrier_with_agent_between():
    src = (
        "const a = await parallel(items.map(x => agent(x, {model: 'sonnet'})));\n"
        "const deduped = dedupe(a);\n"
        "const b = await parallel(deduped.map(x => agent(x, {model: 'sonnet'})));\n"
    )
    findings = check_barrier_vs_pipeline(scrub(src))
    assert findings == []


def test_barrier_vs_pipeline_single_parallel_call_not_flagged():
    src = "const a = await parallel(items.map(x => thunk(x)));\n"
    findings = check_barrier_vs_pipeline(scrub(src))
    assert findings == []


def test_barrier_vs_pipeline_never_error():
    src = (
        "const a = await parallel(items.map(x => thunk(x)));\n"
        "const b = await parallel(items.map(x => thunk2(x)));\n"
    )
    findings = check_barrier_vs_pipeline(scrub(src))
    assert all(f.severity == Severity.WARN for f in findings)


# ---------------------------------------------------------------------------
# model-default heuristic — WARN only
# ---------------------------------------------------------------------------


def test_model_default_flags_agent_call_without_model():
    src = "await agent('do the thing');"
    findings = check_model_default(scrub(src))
    assert len(findings) == 1
    assert findings[0].code == "agent-model-default"
    assert findings[0].severity == Severity.WARN
    assert "model:'sonnet'" in findings[0].message or "model: 'sonnet'" in findings[0].message


def test_model_default_spares_agent_call_with_explicit_model():
    src = "await agent('do the thing', {model: 'sonnet'});"
    findings = check_model_default(scrub(src))
    assert findings == []


def test_model_default_flags_each_model_less_call_independently():
    src = (
        "await agent('a');\n"
        "await agent('b', {model: 'sonnet'});\n"
        "await agent('c');\n"
    )
    findings = check_model_default(scrub(src))
    assert len(findings) == 2
    assert all(f.severity == Severity.WARN for f in findings)


def test_model_default_handles_nested_parens_in_args():
    src = "await agent(buildPrompt(x, y), {label: 'x', schema: makeSchema()});"
    findings = check_model_default(scrub(src))
    assert len(findings) == 1
    assert findings[0].code == "agent-model-default"


# ---------------------------------------------------------------------------
# run_checks() — end-to-end severity map
# ---------------------------------------------------------------------------


def test_run_checks_conformant_script_has_no_error_findings():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "phase('Scout');\n"
        "await agent('do work', {model: 'sonnet', phase: 'Scout'});\n"
    )
    findings = run_checks(src)
    assert not any(f.severity == Severity.ERROR for f in findings)


def test_run_checks_impure_meta_is_error():
    src = "export const meta = { name: someVar, description: 'y' };\n"
    findings = run_checks(src)
    errors = [f for f in findings if f.severity == Severity.ERROR]
    assert any(f.code == "meta-impure-bare-identifier" for f in errors)


def test_run_checks_forbidden_global_is_error():
    src = (
        "export const meta = { name: 'x', description: 'y' };\n"
        "const t = Date.now();\n"
    )
    findings = run_checks(src)
    errors = [f for f in findings if f.severity == Severity.ERROR]
    assert any(f.code == "forbidden-global-date-now" for f in errors)


def test_run_checks_missing_meta_block_is_error():
    src = "phase('Scout');\nconst x = 1;\n"
    findings = run_checks(src)
    assert any(f.code == "meta-missing" and f.severity == Severity.ERROR for f in findings)


def test_run_checks_only_impure_meta_and_forbidden_globals_are_error_tier():
    """AC1/severity-map: ERROR must contain ONLY impure-meta and
    forbidden-globals codes — everything else (phase mismatch, barrier,
    model-default) is WARN."""
    src = (
        "export const meta = { name: someVar, description: 'y', phases: ['Scout'] };\n"
        "phase('Unlisted');\n"
        "await agent('a');\n"
        "const t = Date.now();\n"
        "const a = await parallel(items.map(x => thunk(x)));\n"
        "const b = await parallel(items.map(x => thunk2(x)));\n"
    )
    findings = run_checks(src)
    error_codes = {f.code for f in findings if f.severity == Severity.ERROR}
    allowed_error_prefixes = ("meta-impure", "meta-missing", "forbidden-global")
    for code in error_codes:
        assert code.startswith(allowed_error_prefixes), code

    warn_codes = {f.code for f in findings if f.severity == Severity.WARN}
    assert "phase-call-title-mismatch" in warn_codes
    assert "agent-model-default" in warn_codes
    assert "barrier-vs-pipeline" in warn_codes


def test_run_checks_f1_fixture_end_to_end_no_error():
    """F1 end-to-end: a conformant script whose agent() prompt body contains
    the literal text "Date.now()" must validate WITHOUT an ERROR."""
    src = (
        "export const meta = { name: 'x', description: 'y', phases: ['Scout'] };\n"
        "phase('Scout');\n"
        "await agent(`Explain why Date.now() is forbidden here`, "
        "{model: 'sonnet', phase: 'Scout'});\n"
    )
    findings = run_checks(src)
    assert not any(f.severity == Severity.ERROR for f in findings)


def test_run_checks_meta_description_with_call_shape_text_is_not_error():
    """Review: code-reviewer (Finding 1, P0) — a conformant meta.description
    string VALUE containing a call-shape token / parenthetical (e.g. "them
    (top 10)", "pipeline()") must NOT false-positive as meta-impure-call;
    run_checks must feed check_meta_pure_literal/check_meta_required_fields
    the SCRUBBED meta block, not raw text (Finding 3's regression fixture)."""
    src = (
        "export const meta = {\n"
        "  name: 'flaky-finder',\n"
        "  description: 'Find flaky tests and rank them (top 10) — we use "
        "pipeline() by default',\n"
        "  phases: [{ title: 'Scan' }],\n"
        "};\n"
        "phase('Scan');\n"
        "await agent('do work', {model: 'sonnet', phase: 'Scan'});\n"
    )
    result = run_checks(src)
    errors = [f for f in result if f.severity == Severity.ERROR]
    assert errors == [], errors


def test_run_checks_genuinely_impure_meta_still_caught_after_scrub_fix():
    """Companion to the above: the scrub-based fix must not blind the check
    to REAL impurity (code, not string content) inside the meta block."""
    src = (
        "export const meta = {\n"
        "  name: 'x',\n"
        "  description: computeDescription(),\n"
        "};\n"
    )
    findings = run_checks(src)
    errors = [f for f in findings if f.severity == Severity.ERROR]
    assert any(f.code == "meta-impure-call" for f in errors)
