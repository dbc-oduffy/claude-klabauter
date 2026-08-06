"""
coordinator_core.ops.check_surface_inline_budget — generalized anti-rebound
inline-mechanism budget gate (WARN) for any converted skill/agent surface.

Purpose: `check_wsc_inline_budget.py` proved the pattern for ONE surface
(workstream-complete/SKILL.md) and ONE signal (```bash fence count). This
module generalizes both axes: any file (or set of files matched by a glob)
can be held to a budget, and the count spans four inline-mechanism signals
that together approximate "creep-back" into a surface that was converted
away from inline procedure toward a named entrypoint (AC-11):

  - `bash_fence`      — an opening fenced code block tagged bash/sh/shell/
                        console/shell-session (the original wsc signal,
                        generalized from bash-only to the same tag set
                        `NO-MULTI-LINE-SHELL-FENCE` recognizes).
  - `cc_trusted`       — a `_cc_trusted=0` trusted-root-preamble initializer
                        line (anchored whole-line match, matching the
                        example-doctrine-repo `test_no_command_fences_in_doctrine.py` gate's
                        own anchoring rationale — a substring match
                        double-counts the preamble's traversal re-zero
                        line, which contains the same text).
  - `narrated_step`    — a numbered-list line ("1. ...", "Step 2: ...")
                        whose text also carries an inline single-backtick
                        code span — a cheap proxy for "a procedure narrated
                        step-by-step with commands inline" rather than
                        delegated to a named op.
  - `inline_payload`   — an inline single-backtick code span (outside any
                        fence) containing a shell chaining/substitution
                        metacharacter (`&&`, `||`, `;`, `` ` ``, `$(`) — a
                        payload that migrated out of a fence into prose
                        rather than being converted, mirroring the example-doctrine-repo
                        gate's H5 "container-swap" finding.

Note: these four signals are independent tells, not mutually exclusive
  categories — a single offending line can co-fire more than one signal
  (e.g. a fenced `_cc_trusted=0` preamble line hits both `bash_fence` and
  `cc_trusted`; a narrated numbered step with an inline shell-metacharacter
  span hits both `narrated_step` and `inline_payload`), moving the `total`
  budget by 2 or more, not 1, for that single line. This is by design
  (Finding 3, 2026-07-24 code review) — a future baseline-tuner should not
  be surprised by the multiplier.

Spec backlink: canonical-resolution-engine plan, chunk W1-A3a (2026-07-24)
  — AC-11 anti-rebound inline-mechanism budget gate.
Prior art: coordinator_core/ops/check_wsc_inline_budget.py (single-surface,
  single-signal ancestor of this module); example-doctrine-repo
  coordinator/tests/test_no_command_fences_in_doctrine.py (BLOCK-severity
  doctrine gate over the full signal space this module approximates at
  WARN-severity and reduced precision — deliberately not ported verbatim;
  see Negative-spec below).

Negative-spec:
    - This is NOT a port of the example-doctrine-repo BLOCK-severity gate. That gate is a
      ~2000-line, corpus-calibrated, multi-pass-tuned detector living in
      the doctrine repo (example-doctrine-repo) against a fixed guarded-tree scope
      (skills/commands/agents/pipelines/snippets) and is out of scope for
      this repo (claude-klabauter) to depend on or duplicate. This module is
      a lighter-weight, WARN-only, surface-agnostic heuristic — false
      positives cost nothing (WARN, non-blocking) so precision is
      deliberately traded for simplicity and cross-surface reuse.
    - Does NOT parse Markdown/CommonMark. Fence detection is the same
      line-prefix match the wsc ancestor uses (`^```(tag)`); narrated-step
      and inline-payload detection are single-line regex/substring scans,
      not AST-aware. A stray fence-looking or backtick-looking line inside
      a non-code context is still counted — a faithful continuation of the
      ancestor's own accepted imprecision, not a bug to "fix" here.
    - Never BLOCKs. `check()` returns exit code 1 on over-budget (WARN,
      non-blocking by caller convention) or 0/2 exactly as the ancestor
      does — see its own Exit codes docstring. A caller that wants BLOCK
      severity should reach for the example-doctrine-repo gate instead of tightening this
      one.
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path
from typing import List, Optional

# Generalizes the wsc ancestor's ```bash-only match to the same shell-tag
# set `NO-MULTI-LINE-SHELL-FENCE` recognizes elsewhere in the fleet
# (bash/sh/shell/console/shell-session) — a fence tagged ```python or
# ```json is not an inline-mechanism signal for this gate's purpose.
_SHELL_FENCE_RE = re.compile(
    r"^```(?:bash|sh|shell|console|shell-session)\b", re.IGNORECASE
)

# Anchored whole-line match (not substring) — mirrors the example-doctrine-repo gate's own
# `_PREAMBLE_RE` anchoring rationale: the preamble's traversal re-zero line
# also contains the literal text `_cc_trusted=0` as a substring, so a
# substring test double-counts one preamble copy as two hits.
_CC_TRUSTED_RE = re.compile(r"^[ \t]*_cc_trusted=0[ \t]*$")

# A numbered-list line ("1. ...", "12) ...", "Step 3: ...") that also
# carries an inline single-backtick code span — cheap proxy for "a
# procedure narrated step-by-step with a command inline" rather than
# delegated to a named op.
_NARRATED_STEP_RE = re.compile(
    r"^\s*(?:\d+[.)]|step\s+\d+:)\s+.*`[^`\n]+`", re.IGNORECASE
)

# An inline single-backtick span (never a fence body — callers strip fence
# lines before scanning) carrying a shell chaining/substitution
# metacharacter — the same class of tell the example-doctrine-repo gate's H5 "container-swap"
# finding names: a payload moved out of a fence into prose is still a
# payload.
_INLINE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_METACHARS = ("&&", "||", ";", "$(")


def count_inline_mechanism_signals(text: str) -> dict:
    """Per-signal counts for `text` — see module docstring for the four
    signals. Returns a dict with keys `bash_fence`, `cc_trusted`,
    `narrated_step`, `inline_payload`, and `total` (sum of the four)."""
    lines = text.splitlines()

    bash_fence = sum(1 for line in lines if _SHELL_FENCE_RE.match(line))
    cc_trusted = sum(1 for line in lines if _CC_TRUSTED_RE.match(line))
    narrated_step = sum(1 for line in lines if _NARRATED_STEP_RE.match(line))

    in_fence = False
    inline_payload = 0
    for line in lines:
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in _INLINE_SPAN_RE.finditer(line):
            span = match.group(1)
            if any(mc in span for mc in _METACHARS):
                inline_payload += 1
                break  # one hit per line is enough signal, avoid double-counting

    total = bash_fence + cc_trusted + narrated_step + inline_payload
    return {
        "bash_fence": bash_fence,
        "cc_trusted": cc_trusted,
        "narrated_step": narrated_step,
        "inline_payload": inline_payload,
        "total": total,
    }


def _resolve_paths(surface_glob: str) -> List[Path]:
    """`surface_glob` may be a single file path or a glob pattern (e.g.
    `coordinator/skills/*/SKILL.md`). Returns matching files sorted for
    deterministic output; empty list if nothing matches (caller reports a
    fatal error, mirroring the ancestor's "SKILL.md not found" case)."""
    direct = Path(surface_glob)
    if direct.is_file():
        return [direct]
    matches = sorted(Path(p) for p in glob.glob(surface_glob, recursive=True))
    return [p for p in matches if p.is_file()]


def check(surface_glob: str, baseline_path: str) -> tuple:
    """Core predicate, generalized from the wsc ancestor's `check()`.

    `surface_glob` — a file path or glob pattern; every matching file's
    inline-mechanism signals are summed into one `total` count.
    `baseline_path` — a file holding a single stored baseline integer.

    Returns (message, exit_code) — same three-way contract as the ancestor:
      0 — total within baseline (or no baseline file yet)
      1 — total exceeds baseline (WARN — non-blocking by caller convention)
      2 — fatal error (no file matched `surface_glob`)
    """
    files = _resolve_paths(surface_glob)
    if not files:
        return (f"ERROR: no file matched surface glob {surface_glob!r}", 2)

    per_file: dict = {}
    current_total = 0
    for f in files:
        signals = count_inline_mechanism_signals(f.read_text(encoding="utf-8"))
        per_file[str(f)] = signals["total"]
        current_total += signals["total"]

    baseline_file = Path(baseline_path)
    if not baseline_file.is_file():
        return (
            f"INFO: no baseline set yet — current inline-mechanism count: "
            f"{current_total} across {len(files)} file(s)",
            0,
        )

    baseline = int(baseline_file.read_text(encoding="utf-8").strip())

    if current_total > baseline:
        offenders = ", ".join(
            f"{path}={count}" for path, count in per_file.items() if count > 0
        )
        return (
            f"WARN: inline-mechanism count {current_total} exceeds baseline "
            f"{baseline} for surface {surface_glob!r} — new mechanism should "
            f"be a named entrypoint, not inline (offenders: {offenders})",
            1,
        )

    return (
        f"OK: inline-mechanism count {current_total} within baseline {baseline} "
        f"for surface {surface_glob!r}",
        0,
    )


def main(argv: List[str]) -> int:
    """CLI entry: argv[0] surface path/glob, argv[1] baseline path."""
    surface_glob: Optional[str] = argv[0] if len(argv) > 0 else None
    baseline_path: Optional[str] = argv[1] if len(argv) > 1 else None

    if surface_glob is None or baseline_path is None:
        print(
            "check_surface_inline_budget: missing required surface_glob/baseline_path arguments",
            file=sys.stderr,
        )
        return 2

    message, rc = check(surface_glob, baseline_path)
    stream = sys.stderr if rc == 2 else sys.stdout
    print(message, file=stream)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
