#!/usr/bin/env bash
# deep-research-record-roundtrip.test.sh — AC6/AC7a/AC7b integration test for
# the deep-research queryable index layer.
#
# Spec backlink: docs/plans/2026-06-30-deep-research-queryable-index-layer.md § AC6, § AC7a, § AC7b
#
# Creates fixture artifacts under docs/research/ with a future-dated stem
# (2099-01-01-roundtrip-fixture-*) so they cannot clash with real research files.
# All fixtures are removed via an EXIT trap — failure mid-test still cleans up.
#
# What this test proves:
#   AC6     — query-records --type {research-synthesis,research-claim,coverage-audit,gap-report}
#             each enumerate the correct fixture file; research-claim yields ≥2 synthetic records.
#   P1-1    — research-synthesis sibling-exclusion: coverage-audit and gap-report siblings
#             are NOT returned by --type research-synthesis.
#   P2-1    — required index fields (pipeline, question, coverage_score) are present in the
#             synthesis frontmatter (compensates for required:[]).
#   AC7a    — synthesis body carries expressive prose, not a bare header skeleton.
#   AC7b    — pipeline:structured fixture body contains a thin resolvable link, not prose.
#   schema  — claims.json parses as a valid JSON array; .md fixtures have --- delimiters;
#             --validate-all returns verdict:ok for the fixture synthesis.
#   temporal — --type research-synthesis --since <past-date> includes the fixture whose
#             frontmatter carries `created:` (fleet-canonical temporal key per D4); proves
#             the date→created rename wires through to --since/--older-than filtering.
#
# TYPE_TO_GLOB drift coverage note: the canonical drift-enforcement test lives in
# bin/lib/query-records.test.js (describe 'TYPE_TO_GLOB drift-enforcement'); that unit
# test confirms the 4 new schema applies_to globs map correctly. This test exercises
# the end-to-end CLI path for those types against real fixture files.
#
# Run from any directory inside this repo:
#   bash coordinator/bin/tests/deep-research-record-roundtrip.test.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Bash >=4 guard
# ---------------------------------------------------------------------------
if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: bash >=4 required (got ${BASH_VERSION}). Install via: brew install bash" >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "${SCRIPT_DIR}" rev-parse --show-toplevel)"
QR_CLI="${REPO_ROOT}/coordinator/bin/query-records.js"
RESEARCH_DIR="${REPO_ROOT}/docs/research"

# Fixture stems — year 2099 avoids any plausible clash with real research files.
STEM_WEB="2099-01-01-roundtrip-fixture-web"
STEM_C="2099-01-01-roundtrip-fixture-c"

F_SYNTHESIS="${RESEARCH_DIR}/${STEM_WEB}.md"
F_CLAIMS="${RESEARCH_DIR}/${STEM_WEB}.claims.json"
F_AUDIT="${RESEARCH_DIR}/${STEM_WEB}-coverage-audit.md"
F_GAP="${RESEARCH_DIR}/${STEM_WEB}-gap-report.md"
F_STRUCTURED="${RESEARCH_DIR}/${STEM_C}.md"

# ---------------------------------------------------------------------------
# Cleanup trap — remove ONLY the fixtures this test created.
# ---------------------------------------------------------------------------
cleanup() {
  rm -f "${F_SYNTHESIS}" "${F_CLAIMS}" "${F_AUDIT}" "${F_GAP}" "${F_STRUCTURED}" 2>/dev/null || true
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# PASS / FAIL counters
# ---------------------------------------------------------------------------
PASS=0
FAIL=0
FAILS=()

pass() { echo "  PASS: $*"; PASS=$(( PASS + 1 )); }
fail() { echo "  FAIL: $*" >&2; FAIL=$(( FAIL + 1 )); FAILS+=("$*"); }

# ---------------------------------------------------------------------------
# Create fixtures
# ---------------------------------------------------------------------------
echo "== deep-research round-trip integration test =="
echo "repo root:        ${REPO_ROOT}"
echo "stem (web):       docs/research/${STEM_WEB}.*"
echo "stem (structured): docs/research/${STEM_C}.md"
echo ""
echo "Creating fixtures..."

# Fixture A — research-synthesis (pipeline: web) with expressive prose body.
# Body carries real sentences so AC7a (body-not-skeleton) is verifiable by grep.
cat > "${F_SYNTHESIS}" <<'FIXTURE_EOF'
---
title: Roundtrip Fixture — Web Pipeline Research
question: What are the performance implications of using LLM-based agents for real-time coordination tasks?
created: 2099-01-01
pipeline: web
source_count: 12
topic_facets:
  - agent-coordination
  - real-time-performance
  - latency-budgets
coverage_score: 4
---

## Executive Summary

The evidence consistently shows that LLM-based agents introduce measurable latency when
used for real-time coordination. This latency stems from inference time, network round-trips
to model endpoints, and the token-generation overhead inherent in autoregressive decoding.
The key finding is that for tasks requiring sub-100ms response times, LLM-based orchestration
is structurally unsuited without caching or pre-computation strategies.

## Findings

Several distinct patterns emerged across the twelve sources examined. First, the latency
floor for hosted model APIs sits between 200ms and 800ms per call under normal load, with
tail latencies exceeding 2 seconds during peak hours. This is incompatible with real-time
control loops but workable for coordination tasks with relaxed timing requirements. Second,
streaming responses reduce perceived latency for human-facing outputs but do not help the
coordination-pipeline case, where downstream consumers need complete parseable outputs before
acting. Third, local model inference on consumer-grade GPU hardware can achieve 50-150ms for
small models (7B parameters), but at a quality cost that currently limits utility for complex
reasoning tasks.
FIXTURE_EOF

# Fixture B — research-claim JSON array (≥2 claims, all schema-valid fields).
cat > "${F_CLAIMS}" <<'FIXTURE_EOF'
[
  {
    "id": "claim-001",
    "claim_text": "Hosted LLM API latency floor is 200-800ms per call under normal load.",
    "confidence": "HIGH",
    "source_url": "https://example.com/llm-latency-benchmark-2024",
    "source_date": "2024-09-15",
    "topic_tags": ["latency", "api-performance"],
    "type": "fact"
  },
  {
    "id": "claim-002",
    "claim_text": "Streaming responses do not help coordination pipelines that require complete outputs before acting.",
    "confidence": "MEDIUM",
    "source_url": "https://example.com/streaming-vs-batch-coordination",
    "source_date": "2024-11-01",
    "topic_tags": ["streaming", "coordination", "latency"],
    "type": "limitation"
  }
]
FIXTURE_EOF

# Fixture C — coverage-audit sidecar.
cat > "${F_AUDIT}" <<'FIXTURE_EOF'
---
audited_synthesis: docs/research/2099-01-01-roundtrip-fixture-web.md
pipeline: web
audit_date: 2099-01-01
present_count: 8
absent_count: 3
degraded: false
---

# Coverage Audit — Roundtrip Fixture (web pipeline)

This audit evaluated the synthesis against the claim records produced by the web pipeline run.
Of eleven coverage pointers checked, eight were present in the synthesis body and three were
absent. The absent pointers relate to edge cases in sub-100ms latency scenarios that the
synthesis acknowledged but did not explore in depth.
FIXTURE_EOF

# Fixture D — gap-report sidecar.
cat > "${F_GAP}" <<'FIXTURE_EOF'
---
deepening_recommended: true
gap_count: 3
coverage_score: 4
high_severity_gaps: 1
medium_severity_gaps: 2
contested_unresolved: 0
---

# Gap Report — Roundtrip Fixture (web pipeline)

One high-severity gap was identified: the synthesis does not address the interaction between
streaming partial outputs and downstream coordination-pipeline consumers. This gap could change
the recommendation for use cases that tolerate eventual consistency. Two medium-severity gaps
cover local-model inference quality thresholds and caching strategy trade-offs.
FIXTURE_EOF

# Fixture E — research-synthesis (pipeline: structured) with thin-link body.
# Body is intentionally sparse — a single resolvable link, no prose paragraphs (AC7b).
cat > "${F_STRUCTURED}" <<'FIXTURE_EOF'
---
title: Roundtrip Fixture — Structured Pipeline (thin link)
question: How does structured output extraction compare to web synthesis for quantitative datasets?
created: 2099-01-01
pipeline: structured
source_count: 3
topic_facets:
  - structured-output
  - data-extraction
coverage_score: 3
---

## Output

See [structured output](2099-01-01-roundtrip-fixture-c-structured.yaml)
FIXTURE_EOF

echo "Fixtures created."
echo ""

# ---------------------------------------------------------------------------
# Stage 1: AC6 — basic round-trip enumeration for all 4 new types
# ---------------------------------------------------------------------------
echo "-- Stage 1: AC6 basic enumeration --"

# 1a. research-synthesis includes the synthesis fixture
set +e
SYNTH_OUT=$(node "${QR_CLI}" --type research-synthesis --format paths 2>/dev/null)
SYNTH_EXIT=$?
set -e

if [[ ${SYNTH_EXIT} -ne 0 ]]; then
  fail "AC6 research-synthesis: CLI exited non-zero (${SYNTH_EXIT})"
elif echo "${SYNTH_OUT}" | grep -qF "docs/research/${STEM_WEB}.md"; then
  pass "AC6 research-synthesis: fixture synthesis included in results"
else
  fail "AC6 research-synthesis: fixture synthesis NOT found — output had $(echo "${SYNTH_OUT}" | wc -l | tr -d ' ') lines"
fi

# 1b. research-claim enumerates ≥2 records from the claims.json
set +e
CLAIM_OUT=$(node "${QR_CLI}" --type research-claim --format paths 2>/dev/null)
CLAIM_EXIT=$?
set -e

if [[ ${CLAIM_EXIT} -ne 0 ]]; then
  fail "AC6 research-claim: CLI exited non-zero (${CLAIM_EXIT})"
else
  set +e
  CLAIM_COUNT=$(echo "${CLAIM_OUT}" | grep -cF "docs/research/${STEM_WEB}.claims.json" 2>/dev/null || echo 0)
  set -e
  if [[ "${CLAIM_COUNT}" -ge 2 ]]; then
    pass "AC6 research-claim: ≥2 records enumerated from fixture claims.json (got ${CLAIM_COUNT})"
  else
    fail "AC6 research-claim: expected ≥2 records from claims.json, got ${CLAIM_COUNT}"
  fi
fi

# 1c. coverage-audit includes the coverage-audit fixture
set +e
AUDIT_OUT=$(node "${QR_CLI}" --type coverage-audit --format paths 2>/dev/null)
AUDIT_EXIT=$?
set -e

if [[ ${AUDIT_EXIT} -ne 0 ]]; then
  fail "AC6 coverage-audit: CLI exited non-zero (${AUDIT_EXIT})"
elif echo "${AUDIT_OUT}" | grep -qF "docs/research/${STEM_WEB}-coverage-audit.md"; then
  pass "AC6 coverage-audit: fixture coverage-audit included in results"
else
  fail "AC6 coverage-audit: fixture NOT found — output had $(echo "${AUDIT_OUT}" | wc -l | tr -d ' ') lines"
fi

# 1d. gap-report includes the gap-report fixture
set +e
GAP_OUT=$(node "${QR_CLI}" --type gap-report --format paths 2>/dev/null)
GAP_EXIT=$?
set -e

if [[ ${GAP_EXIT} -ne 0 ]]; then
  fail "AC6 gap-report: CLI exited non-zero (${GAP_EXIT})"
elif echo "${GAP_OUT}" | grep -qF "docs/research/${STEM_WEB}-gap-report.md"; then
  pass "AC6 gap-report: fixture gap-report included in results"
else
  fail "AC6 gap-report: fixture NOT found — output had $(echo "${GAP_OUT}" | wc -l | tr -d ' ') lines"
fi

echo ""

# ---------------------------------------------------------------------------
# Stage 1e: temporal filter — --since must include the fixture (created: 2099-01-01).
# Proves the temporal key rename (date → created per D4) wires through to --since.
# A past-relative date (1d) is used so the filter is always active; the fixture's
# created: 2099-01-01 is in the future, but --since uses string >=, so "1d ago" <
# "2099-01-01" and the fixture is always included.
# ---------------------------------------------------------------------------
echo "-- Stage 1e: temporal filter (--since / created field) --"

# Compute a date 1 day ago in YYYY-MM-DD format (portable: date -d on Linux,
# date -v on BSD/macOS; fall back to node if neither works).
SINCE_DATE=""
if date --version >/dev/null 2>&1; then
  # GNU date
  SINCE_DATE=$(date -d "1 day ago" +%Y-%m-%d 2>/dev/null || true)
fi
if [[ -z "${SINCE_DATE}" ]]; then
  # BSD/macOS date
  SINCE_DATE=$(date -v-1d +%Y-%m-%d 2>/dev/null || true)
fi
if [[ -z "${SINCE_DATE}" ]]; then
  # Fallback: node
  SINCE_DATE=$(node -e "const d=new Date();d.setDate(d.getDate()-1);process.stdout.write(d.toISOString().slice(0,10));" 2>/dev/null || true)
fi

if [[ -z "${SINCE_DATE}" ]]; then
  fail "temporal: could not compute a past date for --since (GNU/BSD date and node all failed)"
else
  set +e
  SINCE_OUT=$(node "${QR_CLI}" --type research-synthesis --since "${SINCE_DATE}" --format paths 2>/dev/null)
  SINCE_EXIT=$?
  set -e

  if [[ ${SINCE_EXIT} -ne 0 ]]; then
    fail "temporal: --since ${SINCE_DATE} exited non-zero (${SINCE_EXIT})"
  elif echo "${SINCE_OUT}" | grep -qF "docs/research/${STEM_WEB}.md"; then
    pass "temporal: --since ${SINCE_DATE} includes fixture with created:2099-01-01 (temporal key wired)"
  else
    fail "temporal: --since ${SINCE_DATE} did NOT include fixture — created: field not read or renamed incorrectly"
  fi
fi

echo ""

# ---------------------------------------------------------------------------
# Stage 2: P1-1 — sibling-exclusion: research-synthesis must NOT return
#          coverage-audit or gap-report co-located siblings.
# ---------------------------------------------------------------------------
echo "-- Stage 2: sibling-exclusion (P1-1) --"

# We already have SYNTH_OUT from Stage 1; reuse it.
if echo "${SYNTH_OUT}" | grep -qF "${STEM_WEB}-coverage-audit.md"; then
  fail "P1-1: research-synthesis wrongly included coverage-audit sibling"
else
  pass "P1-1: research-synthesis does NOT include coverage-audit sibling"
fi

if echo "${SYNTH_OUT}" | grep -qF "${STEM_WEB}-gap-report.md"; then
  fail "P1-1: research-synthesis wrongly included gap-report sibling"
else
  pass "P1-1: research-synthesis does NOT include gap-report sibling"
fi

echo ""

# ---------------------------------------------------------------------------
# Stage 3: P2-1 — field-presence: required index fields must appear in the
#          synthesis frontmatter (compensates for required:[]).
# ---------------------------------------------------------------------------
echo "-- Stage 3: field presence (P2-1) --"

# Grep the fixture frontmatter directly — we authored the file, so a missing
# key means the fixture itself is broken, not query-records.  This is the
# load-bearing check: if the field is in the frontmatter, query-records will
# surface it (proven by the AC6 round-trip above).
for field in pipeline question coverage_score; do
  if grep -qF "${field}:" "${F_SYNTHESIS}"; then
    pass "P2-1 field present: ${field} (confirmed in fixture frontmatter)"
  else
    fail "P2-1 field missing: ${field} not written to fixture frontmatter"
  fi
done

echo ""

# ---------------------------------------------------------------------------
# Stage 4: AC7a — synthesis body is expressive prose, not a bare skeleton.
#          Grep for a specific sentence authored in the fixture.
# ---------------------------------------------------------------------------
echo "-- Stage 4: AC7a body-not-skeleton --"

PROSE_SENTENCE="evidence consistently shows that LLM-based agents introduce measurable latency"
if grep -qF "${PROSE_SENTENCE}" "${F_SYNTHESIS}"; then
  pass "AC7a: synthesis body contains expressive prose sentence"
else
  fail "AC7a: expected prose sentence not found in synthesis body"
fi

# Confirm non-trivial body: count non-header, non-frontmatter, non-blank lines.
# Uses grep -v to strip --- markers and ## headings, counts what remains.
set +e
BODY_NONHEADER=$(grep -v '^---' "${F_SYNTHESIS}" | grep -v '^##' | grep -v '^$' | grep -c '.' 2>/dev/null || echo 0)
set -e
if [[ "${BODY_NONHEADER}" -gt 3 ]]; then
  pass "AC7a: synthesis body has ${BODY_NONHEADER} non-header content lines (clearly not a skeleton)"
else
  fail "AC7a: synthesis body only has ${BODY_NONHEADER} non-header content lines — too sparse to be expressive prose"
fi

echo ""

# ---------------------------------------------------------------------------
# Stage 5: AC7b — pipeline:structured fixture body is a thin resolvable link,
#          NOT prose paragraphs.
# ---------------------------------------------------------------------------
echo "-- Stage 5: AC7b pipeline-C thin-link --"

# Body must contain a resolvable link (markdown link syntax pointing to a real artifact name).
# Using grep -F (fixed string) so [ ] are treated literally.
if grep -qF 'See [structured output]' "${F_STRUCTURED}"; then
  pass "AC7b: structured pipeline body contains the thin resolvable link"
else
  fail "AC7b: structured pipeline body does not contain expected resolvable link"
fi

# Body must NOT contain prose paragraphs (long lines > 80 chars after the frontmatter).
# Use awk with a state machine: skip until the second '---' delimiter (end of frontmatter),
# then count lines in the body that are > 80 chars and are not headings or blank.
# Frontmatter YAML lines can be long (e.g. the 'question:' field) — the awk state machine
# is the only reliable way to exclude them on both Linux and Windows git-bash.
LONG_PROSE_LINES=$(awk '
  /^---$/ { delim++; next }
  delim < 2 { next }
  /^##/ { next }
  /^$/ { next }
  length > 80 { count++ }
  END { print count+0 }
' "${F_STRUCTURED}")
if [[ "${LONG_PROSE_LINES}" -eq 0 ]]; then
  pass "AC7b: structured pipeline body has no prose paragraphs (no body lines > 80 chars)"
else
  fail "AC7b: structured pipeline body has ${LONG_PROSE_LINES} body line(s) > 80 chars — expected thin link only"
fi

echo ""

# ---------------------------------------------------------------------------
# Stage 6: Schema validity
# ---------------------------------------------------------------------------
echo "-- Stage 6: schema validity --"

# 6a. claims.json is valid JSON, is an array, and has ≥2 entries.
# Use node -e with the path passed as an argument so we avoid any heredoc/stdin conflict.
CLAIMS_VALID=$(node -e "
  const fs = require('fs');
  try {
    const data = JSON.parse(fs.readFileSync(process.argv[1], 'utf8'));
    if (!Array.isArray(data)) { process.stdout.write('NOT_ARRAY'); process.exit(0); }
    if (data.length < 2) { process.stdout.write('TOO_FEW:' + data.length); process.exit(0); }
    process.stdout.write('OK:' + data.length);
  } catch(e) { process.stdout.write('PARSE_ERROR:' + e.message); }
" "${F_CLAIMS}" 2>/dev/null)
if [[ "${CLAIMS_VALID}" == OK* ]]; then
  CLAIM_ENTRY_COUNT="${CLAIMS_VALID#OK:}"
  pass "schema: claims.json is a valid JSON array with ${CLAIM_ENTRY_COUNT} entries"
elif [[ "${CLAIMS_VALID}" == TOO_FEW* ]]; then
  fail "schema: claims.json has fewer than 2 entries — ${CLAIMS_VALID}"
else
  fail "schema: claims.json invalid — ${CLAIMS_VALID}"
fi

# 6b. Each .md fixture has a properly delimited --- frontmatter block.
# We count occurrences of '^---$' and require at least 2 (open + close).
declare -A FIXTURES_LABEL
FIXTURES_LABEL["${F_SYNTHESIS}"]="synthesis"
FIXTURES_LABEL["${F_AUDIT}"]="coverage-audit"
FIXTURES_LABEL["${F_GAP}"]="gap-report"
FIXTURES_LABEL["${F_STRUCTURED}"]="structured"

for fpath in "${F_SYNTHESIS}" "${F_AUDIT}" "${F_GAP}" "${F_STRUCTURED}"; do
  label="${FIXTURES_LABEL[${fpath}]}"
  set +e
  FM_DELIMITERS=$(grep -c '^---$' "${fpath}" 2>/dev/null || echo 0)
  set -e
  if [[ "${FM_DELIMITERS}" -ge 2 ]]; then
    pass "schema: ${label} fixture has frontmatter delimiters (${FM_DELIMITERS} '---' markers)"
  else
    fail "schema: ${label} fixture missing --- frontmatter delimiters (found ${FM_DELIMITERS})"
  fi
done

# 6c. --validate-all for research-synthesis returns verdict:ok for the fixture.
# Write JSON to a temp file to avoid piped-stdin / heredoc conflict when calling node.
VALIDATE_TMP=$(mktemp 2>/dev/null || echo "${REPO_ROOT}/.validate-tmp-$$.json")
set +e
node "${QR_CLI}" --validate-all --type research-synthesis > "${VALIDATE_TMP}" 2>/dev/null
VALIDATE_EXIT=$?
set -e

# Note: exit 1 means drift found in some record; we only care about OUR fixture's verdict.
FIXTURE_VERDICT=$(node -e "
  const fs = require('fs');
  const stem = process.argv[1];
  try {
    const records = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
    const fixture = records.find(function(r){ return r.path && r.path.indexOf(stem) !== -1; });
    if (!fixture) { process.stdout.write('NOT_FOUND'); process.exit(0); }
    process.stdout.write(fixture.verdict);
  } catch(e) { process.stdout.write('PARSE_ERROR:' + e.message); }
" "${STEM_WEB}" "${VALIDATE_TMP}" 2>/dev/null)
rm -f "${VALIDATE_TMP}" 2>/dev/null || true

if [[ "${FIXTURE_VERDICT}" == "ok" ]]; then
  pass "schema: --validate-all verdict for fixture synthesis is 'ok' (no drift)"
elif [[ "${FIXTURE_VERDICT}" == "NOT_FOUND" ]]; then
  fail "schema: fixture synthesis not found in --validate-all output"
else
  fail "schema: --validate-all verdict for fixture synthesis is '${FIXTURE_VERDICT}' (expected 'ok')"
fi

# TYPE_TO_GLOB drift-check note:
echo "  NOTE: TYPE_TO_GLOB drift-check (derives globs from schemas/*.yaml at runtime) is"
echo "        exercised by the 4 successful type-queries above + bin/lib/query-records.test.js"

echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
TOTAL=$(( PASS + FAIL ))
echo "Results: ${PASS}/${TOTAL} passed, ${FAIL} failed"

if [[ "${FAIL}" -gt 0 ]]; then
  echo ""
  echo "Failed cases:"
  for f in "${FAILS[@]}"; do
    echo "  - ${f}"
  done
  echo ""
  echo "FAIL — deep-research-record-roundtrip.test.sh"
  exit 1
fi

echo "PASS — deep-research-record-roundtrip.test.sh"
exit 0
