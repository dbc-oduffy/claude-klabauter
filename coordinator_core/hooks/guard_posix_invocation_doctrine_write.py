"""coordinator_core.hooks.guard_posix_invocation_doctrine_write — PreToolUse
(Write|Edit|MultiEdit) advisory op: WARNS, never blocks, when a write
introduces the retired POSIX-only coordinator-CLI invocation shape into a
doctrine surface.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-posix-invocation-doctrine-
write.py`. That script ran as an in-process guard body enrolled into a
second, doctrine-plane-resident guard registry
(`_guard_runner.REAL_GUARD_REGISTRY`), fired only via
`preuse-write-dispatch.py`'s own dispatch and NEVER given a standalone
hooks.json registration of its own (its own top-of-file comment: a
standalone registration would fire it twice). None of that plumbing applies
here: this lands as its own `hooks.<name>` op per this row's body, called
directly by whatever door dials `hooks.guard_posix_invocation_doctrine_write`
— there is no second in-process registry to enroll into or double-fire
against.

Why this exists (unchanged from the source): `resolve-coordinator-bin.md`
rung 0 ranks a PowerShell host's Shape W (the `.cmd` sibling via the call
operator) above Shape A/B's `${VAR:-default}` POSIX shell expansion for
every invocation — rungs 1-3 are POSIX-shell fences unrunnable on a
PowerShell-only host without spawning a bash first. This guard is the
write-time, warn-only half of keeping that shape from recurring across the
doctrine-plane's `coordinator/skills/`, `coordinator/commands/`, and
`coordinator/docs/wiki/` trees; the BIG-RED hard-fail half is a doctrine-
plane test, out of scope for this op.

PM ruling, verbatim (2026-08-18, carried over unchanged): "not blocking on
write, it can be warn on write, because otherwise we waste tokens. this
should be triggered as a BIG RED in tests though." This op therefore always
returns an `allow_advisory` envelope (never `deny`) when it fires, and
`no_advisory()` otherwise — no deny path exists in this module at all.

Detection: delegates entirely to `coordinator_core.hooks.
posix_invocation_detect.find_posix_forwarder_invocations` — the one shared
predicate this guard and the doctrine-plane's own hard-fail test both import,
so a hook and a test can never disagree about what counts as a violation
(see that module's own docstring).

Scoping: fires only on `new_hits(before, after)` — hits present in `after`
that were not already present in `before` (a multiset/Counter difference
over matched text), never on pre-existing debt sitting untouched in a file
being edited for an unrelated reason. `GUARDED_TREES` is an unresolved
path-substring check against the payload's own target path text (forward-
slash-normalized) — it names the doctrine-plane's own tree layout
(`coordinator/skills/`, `coordinator/commands/`, `coordinator/docs/wiki/`),
which stays a literal substring match regardless of which repo the target
path resolves inside: this op's target path is whatever the calling
session's payload names, not this engine's own tree.

Fail-open (returns `no_advisory()`), in order: `tool_name` not in the
guarded set; no target path in `tool_input`; target not under one of the
three guarded trees; on-disk read failure for an existing file;
unreconstructable before/after; zero new hits.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.posix_invocation_detect import find_posix_forwarder_invocations
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: The three trees AC5 names -- forward-slash-only; `is_in_scope` normalizes
#: the target path before checking. Doctrine-plane tree names, unresolved.
GUARDED_TREES = (
    "coordinator/skills/",
    "coordinator/commands/",
    "coordinator/docs/wiki/",
)

#: Where the remedy actually lives -- named directly in the advisory text
#: too (not only via this anchor).
_RULE_ANCHOR = "coordinator/snippets/resolve-coordinator-bin.md (rung 0 / Shape W)"


def is_in_scope(target_path: str) -> bool:
    """True if `target_path` (a raw, possibly backslash-separated payload
    string) falls under one of `GUARDED_TREES`."""
    if not target_path:
        return False
    normalized = target_path.replace("\\", "/")
    return any(tree in normalized for tree in GUARDED_TREES)


def new_hits(before: str, after: str) -> list:
    """`find_posix_forwarder_invocations(after)` hits whose matched text was
    not already present the same number of times in `before`."""
    before_counts = Counter(h.text for h in find_posix_forwarder_invocations(before))
    after_hits = find_posix_forwarder_invocations(after)
    result = []
    seen: "Counter[str]" = Counter()
    for hit in after_hits:
        seen[hit.text] += 1
        if seen[hit.text] > before_counts[hit.text]:
            result.append(hit)
    return result


def _advisory_reason(target: str, hits: list) -> str:
    clis = sorted({h.cli for h in hits})
    shown = clis[:2]
    clis_text = ", ".join(shown)
    if len(clis) > len(shown):
        clis_text += f", +{len(clis) - len(shown)} more"
    name = Path(target).name
    return (
        f"{name} adds a POSIX-only `${{VAR:-default}}` invocation "
        f"reaching {clis_text} -- unrunnable on PowerShell-only. Use rung "
        "0 / Shape W instead."
    )


@register_op("hooks.guard_posix_invocation_doctrine_write")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: advise (never deny) when a write
    introduces a new POSIX-only coordinator-CLI invocation into a doctrine
    surface tree."""
    if params.get("tool_name", "") not in _GUARDED_TOOLS:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return no_advisory()

    if not is_in_scope(target_raw):
        return no_advisory()

    try:
        target = Path(target_raw).resolve()
    except Exception:
        return no_advisory()

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return no_advisory()

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    hits = new_hits(before, after)
    if not hits:
        return no_advisory()

    context = render(
        compose(_advisory_reason(target_raw, hits), anchor=_RULE_ANCHOR),
        env=params.get("env"),
    )
    return allow_advisory("PreToolUse", context)
