"""step_zero_emit — the "Step Zero" install-prereq NDJSON emitter.

The shared, ratified "Step Zero" install-prereq NDJSON emitter — the single
source of truth for the per-probe NDJSON line shape that coordinator emits
and that sibling repos (example-game-repo, example-cockpit-repo) conform their own
emitters against. This module holds ONLY the emitter + JSON-escape
primitives; it contains NO probe logic (probe logic lives in
`coordinator_core.install.prereq_probe`). It NEVER mutates the machine —
pure string transforms only.

Port of: step_zero_emit.sh (coordinator-claude 290997c7, 2026-07-22) — a byte-parity
Python-native implementation for Python-side Step Zero consumers.

Spec backlink: docs/wiki/step-zero-emitter-contract.md (canonical contract)
  + docs/plans/2026-06-22-step-zero-emitter-contract-lib.md
Conformance fixture: coordinator/tests/fixtures/step-zero-conformance.json
  (the NORMATIVE authority — base64-encoded expected bytes; this module's
  test replays that fixture verbatim).

Contract — one compact NDJSON line per probe (no trailing whitespace beyond
the single terminating \\n):
  {"name":"<probe>","status":"<pass|fail|warn|inconclusive>","severity":"<hard|semi-hard|advisory>","detail":"<short>","remediation":"<one-line|empty>"}

Two enums (orthogonal, non-multiplexed — a `warn` may be `hard` OR `advisory`):
  status   in {pass, fail, warn, inconclusive}   -- the verdict
  severity in {hard, semi-hard, advisory}         -- the gate weight
    hard      -- blocks the preflight exit; no operator escape path
    semi-hard -- blocks the preflight exit like hard, but is escapable via a
                 probe-specific override flag (operator consciously proceeds)
    advisory  -- surfaced to the operator but does not stop the gate
`inconclusive` is first-class: a probe that genuinely cannot determine state
returns "inconclusive", never a false "pass"/"fail".

Negative-spec: enum validation is the CALLER's responsibility, mirroring the
bash original -- json_escape()/emit_line() escape and format but do NOT
reject out-of-enum status/severity values. Do not add validation here; a
validating wrapper belongs at the probe layer (parity with the bash
docstring's own note on `_co_pp_emit`).
"""
from __future__ import annotations

# Five escapes, applied in this exact order (order is normative -- backslash
# MUST be first to avoid double-escaping the substitutions that follow).
# Mirrors _co_pp_json_escape in the bash original byte-for-byte:
#   1. \  backslash       -> \\
#   2. "  double-quote    -> \"
#   3. \r carriage-return -> \r
#   4. \n line-feed       -> \n
#   5. \t tab             -> \t
# NOT escaped (intentional boundary, matches bash): other C0 control chars,
# NUL, and non-ASCII pass through raw. clone_auth captures multiline git
# stderr, so \r/\n/\t are load-bearing (real Windows git stderr carries CRLF).
_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\r", "\\r"),
    ("\n", "\\n"),
    ("\t", "\\t"),
)


def json_escape(value: str) -> str:
    """Escape a string for safe embedding in a hand-rolled JSON value.

    Byte-parity port of _co_pp_json_escape() -- applies the five escapes
    above IN ORDER. No json.dumps() shortcut: json.dumps would also escape
    other control chars and non-ASCII (via \\uXXXX), which the bash original
    deliberately does not -- using it here would silently widen the escape
    set and break byte-parity with the conformance fixture.
    """
    result = value
    for target, replacement in _ESCAPES:
        result = result.replace(target, replacement)
    return result


def emit_line(name: str, status: str, severity: str, detail: str, remediation: str) -> str:
    """Build one compact NDJSON line for a probe result (byte-parity port of _co_pp_emit).

    All string fields are escaped via json_escape() before embedding. Returns
    the line WITH its trailing newline (matches the bash `printf '...\\n'`
    original) so callers can write it directly to a stream.
    """
    return (
        '{"name":"%s","status":"%s","severity":"%s","detail":"%s","remediation":"%s"}\n'
        % (
            json_escape(name),
            json_escape(status),
            json_escape(severity),
            json_escape(detail),
            json_escape(remediation),
        )
    )


def emit(name: str, status: str, severity: str, detail: str, remediation: str, *, stream=None) -> None:
    """Emit one NDJSON probe-result line to a stream (default: stdout).

    Thin wrapper over emit_line() for callers that want the bash-sourced
    _co_pp_emit's side-effecting-print behavior rather than a returned string.
    """
    import sys

    target = stream if stream is not None else sys.stdout
    target.write(emit_line(name, status, severity, detail, remediation))
