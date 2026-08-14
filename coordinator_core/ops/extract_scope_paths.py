"""
coordinator_core.ops.extract_scope_paths — handoff `scope:` frontmatter path extractor.

Purpose: extracts the `scope:` path list from a handoff's YAML frontmatter, one
path per line, stripping the leading two-space `- ` list-item prefix. Fails
loud (exit 1) when the block is absent or empty so callers (handoff/pickup
Step 1 preflights) get a clear diagnostic rather than silently proceeding
with zero paths. `_extract_scope_paths` also accepts a `key` override
(default `"scope"`) so a caller needing the same top-level list-block scan
over a differently-named key (e.g. `completeness_checklist:`) reuses this
scanner instead of a second copy — AC16 (`coordinator/docs/wiki/computed-skills.md`
§ "Consumes manifest") names this module as the MUST-consume capability
powering `preflight.dirty_paths`; the `key` param is what lets
`coordinator_core.pickup_assemble` also consume it for
`preflight.completeness_items[]` without forking the scan.

Exit codes:
  0 — scope: block found, non-empty, paths printed to stdout
  1 — scope: block missing or empty (stderr diagnostic, no stdout)
  2 — usage error: missing arg or handoff file not found (stderr diagnostic)

Port of: extract-scope-paths.sh (DoE 894d4bc6, 2026-07-22) — the bash
oracle this module was ported from has since been retired (debash campaign);
nothing compares this module's output against a live shell implementation
any more. The 8 fixtures in `test_extract_scope_paths.py` (lifted from that
oracle's own bats suite) are the operative invariant now, not a byte-parity
claim against something that no longer runs.
Spec backlink: docs/plans/2026-06-30-session-terminator-mechanism-unification.md C4

Negative-spec:
    - Does NOT do a full YAML parse — reuses the same line-scan stop
      conditions as the bash awk idiom: a `  - ` list-item line while inside
      the scope block is emitted; a `---` line (frontmatter close) OR a line
      starting with a lowercase ASCII letter at column 0 (next top-level
      frontmatter key) both terminate the scope block. This mirrors the
      review-fixed awk (`found && /^---/{exit}` added before the
      `found && /^[a-z]/{exit}` guard) so a scope: block that is the LAST
      frontmatter field does not leak body `  - ` bullets into the output.
    - stderr diagnostics keep the literal `extract-scope-paths.sh: ` /
      `usage: extract-scope-paths.sh <handoff-file>` prefixes (NOT renamed to
      the trampoline's extension-less name) — carried over verbatim from the
      retired bash oracle's own bats suite (same debash-campaign retirement
      noted above), which is where these exact prefix strings originated,
      not a live constraint asserted against a file that no longer exists.
    - No bash-version guard (bash>=4 check) — that check is bash-runtime-only
      and has no Python analog; this module is invoked directly, not via a
      bash reimplementation.
    - Does NOT handle a trailing inline `# comment` after a captured item
      (e.g. `  - "value"  # note`) — `unquote_yaml_scalar` only strips a
      matched quote pair at the very start/end of the stripped string, so a
      trailing comment defeats the pair match and the item is returned raw
      (quotes, comment and all), with no diagnostic. Inherited from the
      `scope:` path this was ported from, which never handled inline
      comments either — not a regression from the `key` generalization.
      (Review: code-reviewer — Finding 4.)
    - The CLI surface (`main`) stays `scope`-only by design — `key` is an
      in-process API surface for `coordinator_core.pickup_assemble`, not
      exposed as a CLI flag. (Review: code-reviewer — Finding 5.)
"""

from __future__ import annotations

import os
import re
import sys
from typing import List, Optional

from coordinator_core.frontmatter.primitives import unquote_yaml_scalar

_PROG = "extract-scope-paths.sh"  # literal program-name prefix — see negative-spec


def _extract_scope_paths(text: str, key: str = "scope") -> List[str]:
    """Return the `key:` block's `  - <value>` entries, prefix stripped,
    trailing whitespace stripped, then unquoted (`unquote_yaml_scalar` — a
    no-op on `scope:`'s already-bare paths *in practice today*, so the
    default `key="scope"` path is behaviourally unchanged from before this
    param existed, modulo trailing whitespace now being trimmed there too —
    strictly more correct for a bare path. This is not a structural
    guarantee: it holds only because nothing currently writes `scope:` list
    items through `serialize_yaml_scalar`. A future writer that quoted a
    scope path containing a YAML structural character would have its quotes
    silently stripped here where the old code returned them literally —
    Review: code-reviewer — Finding 1, softened from an unqualified "no-op"
    claim so a future reader doesn't treat it as enforced by this module.).

    Stop-condition shape this was originally ported FROM (the bash oracle
    below is retired — this is a historical porting note, not a live
    byte-identity constraint anything still checks; `key` substituted for
    the literal `scope`):
    `/^scope:/{found=1; next} found && /^  - /{print substr($0, 5)}
    found && /^---/{exit} found && /^[a-z]/{exit}`.
    The whitespace-strip-before-unquote above is a deliberate divergence
    from that original (which only sliced the prefix, no strip) — not an
    accident to reconcile back.
    """
    paths: List[str] = []
    found = False
    prefix = f"{key}:"
    for line in text.splitlines():
        if not found:
            if line.startswith(prefix):
                found = True
            continue
        if line.startswith("  - "):
            paths.append(unquote_yaml_scalar(line[4:].strip()))
            continue
        if line.startswith("---"):
            break
        if re.match(r"^[a-z]", line):
            # Review: code-reviewer — ASCII-only `[a-z]` match, not
            # str.islower() (which is True for non-ASCII lowercase code
            # points like 'ñ'), to match the bash oracle's ASCII-only
            # `/^[a-z]/` and dirty_tree_gate.py's twin stop condition.
            break
    return paths


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) < 1:
        print(f"usage: {_PROG} <handoff-file>", file=sys.stderr)
        return 2

    handoff = args[0]

    if not os.path.isfile(handoff):
        print(f"{_PROG.replace('.sh', '')}: file not found: {handoff}", file=sys.stderr)
        return 2

    try:
        with open(handoff, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"{_PROG.replace('.sh', '')}: file not found: {handoff} ({exc})", file=sys.stderr)
        return 2

    scope = _extract_scope_paths(text)

    if not scope:
        print(
            f"{_PROG.replace('.sh', '')}: scope: block missing or empty in {handoff}",
            file=sys.stderr,
        )
        return 1

    for path in scope:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
