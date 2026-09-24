"""coordinator_core.write_guards.nudge_handoff_author_lint — advisory guard.

Gives `coordinator_core.ops.handoff_author_lint` (built, registered, tested,
and until now uncalled outside its own tests and the registry tables — see
that plan's census row 1) the one caller that reaches an author without being
invoked: this guard runs on every `Write`/`Edit`/`MultiEdit` to a
`state/handoffs/*.md` path and relays SUMMARY_PLACEHOLDER, SUMMARY_OVER_CAP
and LEDGER_ROW_UNPARSEABLE findings the moment the author writes them.

`AC_NO_CHECKBOXES` is filtered OUT by `code`:
`coordinator_core.write_guards.nudge_handoff_ac_shape` (priority 220) already
delivers that finding, mirroring the completeness gate's own branch order
(silent on `kind: session-handoff`; silent on an edit that leaves an
already-prose section unchanged). Emitting it a second time here, with a
different silence rule, would make two modules answer one question under two
names. Both of that guard's silences are correct, not gaps: (i) for
`kind: session-handoff`, leg A never counts boxes, so this filter is simply
inert for that kind, not a missed case; (ii) an edit that leaves an existing
prose AC section unchanged is 220's deliberate new-defects-only rule, which
this guard's own multiset dedupe (below) applies identically to its own three
codes.

The op's `_handler` needs `repo_root` and reads the file from DISK — but this
guard runs in PreToolUse, BEFORE the write lands, so the on-disk file is
either the OLD text or (for a brand-new handoff) absent entirely. Calling
`_handler` here would lint the wrong text or nothing. That is why the op
exposes `lint_text(text) -> list[dict]` (see its module docstring's Entry
points section) — a pure, I/O-free seam over the same three body/frontmatter
grammars `_handler` already calls, taking the POST-WRITE body this guard
constructs itself. Importing `lint_text` also means this guard never reaches
past the op registry for a private `_handler`.

BORROWED, NOT COPIED — path-candidate extraction, path-shape gating, git-root
containment and post-write body reconstruction are the exact helpers
`nudge_handoff_ac_shape.py` already owns, imported directly:
`_extract_candidates`, `_normalize_and_gate`, `_resulting_body`,
`_MAX_WHOLE_FILE_BYTES`. That sibling's own `_resulting_body` docstring
declines an unreviewed private cross-import and names a future shared
`write_guards/_post_write_body.py` extraction as the eventual fix; this
import is the reviewed one it anticipates, and it adds no fifth copy of that
logic. Extracting the shared module stays out of this plan's scope.

NEW-DEFECTS-ONLY, keyed on a MULTISET not a set: when a pre-image exists,
`lint_text(pre_image)` is counted by `(code, error)` with a `Counter`. Post
findings are then walked in order and emitted only while their key's
remaining pre-image count is zero, decrementing it otherwise. A plain `set`
would silently swallow a SECOND byte-identical unparseable ledger row added
beside an existing one (`_ledger_findings` puts the row TEXT, not its line
number, in `error`, so two identical rows share one key) — the author adds a
second copy of the same mistake and hears nothing about it. The Counter
reports it; the sibling guard's own single-defect new-defects-only rule
generalizes the same way here. A `Write` to a brand-new path has no
pre-image, so every post finding fires (nothing to subtract).

Intended behaviour on a reworded still-over-cap summary (stated so nobody
"fixes" it later): `SUMMARY_OVER_CAP`'s `error` embeds `len(value)`, so
rewording an over-cap summary to a DIFFERENT length changes the key and
re-fires — intended, since the author just touched the defective value and
left it defective. Rewording to the SAME length leaves the key unchanged and
stays silent — accepted, not a gap; no new key logic closes it.

CLASS is "advisory": the four findings this relays each have a silent, not a
failing, downstream degradation (see the op's own module docstring), and a
handoff may legitimately carry no AC section or an empty ledger — a hard-deny
here would refuse valid work and teach the next author to route around the
guard, same rationale as the sibling's own CLASS choice.

Message register (docs/wiki/guard-messaging.md § Register): relays each
finding's own `hint` verbatim — the op already writes it as "one fact, once,
plus a terse alternative" — one line per finding, prefixed by its `where`.
No added self-legitimacy, repeated claim, reassurance, apology, or override
key; there is nothing to bypass, since this guard never blocks.

Negative-spec:
  - Does NOT add a fifth grammar. All findings come from `lint_text`; nothing
    here parses a summary, a checkbox, or a ledger row itself.
  - Does NOT relay `AC_NO_CHECKBOXES` — filtered by `code` (see above); that
    finding is `nudge_handoff_ac_shape`'s to deliver.
  - Does NOT block the write under any circumstance — `check()` returns
    either `None` or an `additionalContext` envelope, never a
    `permissionDecision`.
  - Does NOT fail closed on any error — the whole body is wrapped in
    `try/except Exception: return None`, matching the sibling.
  - Does NOT re-fire an already-present defect on an unrelated edit — the
    multiset dedupe above.
  - Does NOT spawn a subprocess. No `subprocess` import in this module.

Spec backlink: docs/plans/2026-09-11-wire-the-authoring-surface-lint-and-clos.md
(chunk C1)
"""

from __future__ import annotations

import collections
from pathlib import Path
from typing import Any, Dict, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._repo_root import resolve_repo_root
from coordinator_core.write_guards.nudge_handoff_ac_shape import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_candidates,
    _normalize_and_gate,
    _resulting_body,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
PRIORITY = 225  # advisory band; next free slot after nudge_dangling_sizing_citation (224)

#: Finding codes this guard relays. AC_NO_CHECKBOXES is deliberately absent
#: — see module docstring.
_RELAYED_CODES = frozenset(
    {"SUMMARY_PLACEHOLDER", "SUMMARY_OVER_CAP", "LEDGER_ROW_UNPARSEABLE"}
)


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        cwd = payload.get("cwd") or None
        base_dir = Path(cwd) if cwd else Path.cwd()
        git_root = resolve_repo_root(cwd)
        allowed_roots = [Path(git_root)] if git_root else []

        matched: Optional[Path] = None
        for cand in candidates:
            cn = _normalize_and_gate(cand)
            if cn is None:
                continue
            candidate_path = Path(cn) if Path(cn).is_absolute() else (base_dir / cn)
            if allowed_roots:
                contained = contained_path(candidate_path, allowed_roots)
                if contained is None:
                    continue
                candidate_path = contained
            matched = candidate_path
            break

        if matched is None:
            return None

        try:
            if matched.stat().st_size > _MAX_WHOLE_FILE_BYTES:
                pre_image = None
            else:
                pre_image = matched.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pre_image = None

        body = _resulting_body(tool_name, tool_input, pre_image)
        if body is None:
            return None

        # Deferred import: paid only after a candidate has already matched
        # the path-shape regex (and, when a git root resolved, passed
        # containment). `handoff_author_lint` pulls in `yaml`, `ipc` and
        # `ops.fleet._common` transitively, and `engine.py`'s
        # `_discover_guards()` imports every guard module fresh on every
        # PreToolUse call with no memoization — an eager top-level import
        # would tax every unrelated write in the repo, not just candidates
        # actually in scope.
        from coordinator_core.ops.handoff_author_lint import lint_text

        post_findings = [f for f in lint_text(body) if f["code"] in _RELAYED_CODES]
        if not post_findings:
            return None

        if pre_image is not None:
            pre_findings = [
                f for f in lint_text(pre_image) if f["code"] in _RELAYED_CODES
            ]
            remaining = collections.Counter(
                (f["code"], f["error"]) for f in pre_findings
            )
            new_findings = []
            for finding in post_findings:
                key = (finding["code"], finding["error"])
                if remaining.get(key, 0) > 0:
                    remaining[key] -= 1
                    continue
                new_findings.append(finding)
            post_findings = new_findings

        if not post_findings:
            return None

        lines = [f"{f['where']}: {f['hint']}" for f in post_findings]
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": "\n".join(lines),
            }
        }
    except Exception:
        return None
