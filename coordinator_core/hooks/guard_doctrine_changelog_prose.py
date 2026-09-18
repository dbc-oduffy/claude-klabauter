"""coordinator_core.hooks.guard_doctrine_changelog_prose — PreToolUse
(Write|Edit|MultiEdit) op: advises when a write introduces NEW
changelog-shaped prose into a doctrine surface, denies when it introduces a
changelog-shaped token into the repo-root `coordinator.local.md` config
class.

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/guard-doctrine-changelog-prose.py`.
That script ran as an in-process guard body enrolled into a second,
doctrine-plane-resident guard registry (`_guard_runner.REAL_GUARD_REGISTRY`),
fired only via `preuse-write-dispatch.py`'s own dispatch, with its own
top-of-file comment warning that a standalone hooks.json registration would
fire it twice. None of that plumbing applies here: this lands as its own
`hooks.<name>` op per this row's body, called directly by whatever door
dials `hooks.guard_doctrine_changelog_prose` — there is no second in-process
registry to enroll into or double-fire against. The stdin/stdout JSON
payload read and `_message_envelope.emit()`'s stdout-writing channel dispatch
are replaced with the payload-dict-in/response-out `register_op` contract
and direct `allow_advisory`/`deny` envelope construction, same shape as the
sibling guards this same row lands (`guard_doctrine_surface_bash_write`,
`guard_doctrine_surface_ratio`). No other shape change: the detection logic
lives entirely in `coordinator_core.hooks.doctrine_changelog_prose`
(`new_violations`, `scope_class`), already landed and unmodified by this
chunk.

Why this exists (unchanged from the source): a doctrine surface states the
rule as it stands now, present tense, addressed to the reader who has to
obey it next — it does not carry the rule's own history (ruling dates,
`DR-` supersession chains, "was P now Q", "retired on <date>",
origin-incident narration). That standing rule lives in this repo's
`CLAUDE.md` § Conventions. See `coordinator_core.hooks.doctrine_changelog_prose`'s
module docstring for the full detection-rule writeup, the two
explicitly-handled ambiguous classes, and the false-positive exemptions this
deliberately does NOT flag.

Advisory for doctrine prose, deny for config debt
----------------------------------------------------
Two classes, two verdicts (`doctrine_changelog_prose.scope_class`). The
doctrine-prose class cannot always tell a real violation from a legitimate
authority citation with certainty — the two ambiguous classes in the
detector exist precisely because some shapes are genuinely undecidable from
the text alone. A guard in that position should offer the alternative
rather than refuse the write.

The config class (a repo-root `coordinator.local.md`) is a PM-ruled HARD
DENY instead: no proximity condition, no verb requirement, and no ambiguous
tier softens it, so there is no case where advising instead of refusing is
the safer default. A line marked
`<!-- doctrine-retirement-exemption: <reason> -->` is exempt from the date
leg only — not the rot-prone-path leg, and not the bare-`DR-` leg, which a
retirement clause satisfies by citing its record path-qualified rather than
by claiming an exemption. See the detector module.

Scoping
-------
Fires only on `new_violations(before, after)` — a multiset difference
between the file's violations before and after THIS write — never on
pre-existing debt sitting untouched in a file being edited for an unrelated
reason. See `doctrine_changelog_prose.new_violations`'s docstring.

Reconstructing before/after
-----------------------------
`before` is the current on-disk content (empty string for a not-yet-existing
file); `after` is reconstructed via
`coordinator_core.write_guards._sentinel_write_guard.reconstruct_after` —
Write's `content` directly, Edit's single `old_string`/`new_string`
replacement (or all occurrences under `replace_all`), MultiEdit's sequential
application over `tool_input["edits"]`.

Fail-open (returns `no_advisory()`), in order: `tool_name` not in the
guarded set; no target path in `tool_input`; target's `scope_class()` is
`None` (wrong tree/extension, a `tests/`/`fixtures/` subdirectory, or a
`coordinator.local.md` not directly at a `.git`-rooted repo root); on-disk
read failure for an existing file; unreconstructable before/after; zero new
violations — for BOTH classes (see `doctrine_changelog_prose.new_violations`'s
docstring for why a deny still scopes to the delta, never whole-file debt).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core._hook_envelope import deny
from coordinator_core.hooks._envelope import allow_advisory, no_advisory
from coordinator_core.hooks.doctrine_changelog_prose import new_violations, scope_class
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: Where the rule this guard enforces actually lives -- not a wiki page (this
#: guard was not asked to author one), the repo's own standing convention.
_RULE_ANCHOR = 'CLAUDE.md § Conventions ("Doctrine is not changelog")'


def _advisory_reason(target: str, violations: list) -> str:
    """The prose diagnosis. High-confidence hits are named directly; an
    ambiguous-only result gets softer language ("worth a second look") since
    a bare-phrase match can't establish a real violation on its own — see
    the detector module docstring's writeup of the two ambiguous classes."""
    high = [v for v in violations if v.confidence == "high"]
    ambiguous = [v for v in violations if v.confidence == "ambiguous"]

    if high:
        kinds = sorted({v.kind for v in high})
        shown = kinds[:2]
        kinds_text = ", ".join(shown)
        if len(kinds) > len(shown):
            kinds_text += f", +{len(kinds) - len(shown)} more"
        extra = f" (+{len(ambiguous)} ambiguous)" if ambiguous else ""
        return (
            f"{target} adds {len(high)} changelog-shaped passage(s){extra} "
            f"({kinds_text}). State the rule in present tense; put history "
            "in the commit message or a decision record."
        )

    kinds = sorted({v.kind for v in ambiguous})
    kinds_text = ", ".join(kinds[:2])
    return (
        f"{target} adds {len(ambiguous)} passage(s) worth a second look "
        f"({kinds_text}) -- may be live-rule provenance, may be reversal "
        "narration."
    )


def _deny_reason(target: str, violations: list) -> str:
    """The config-class deny diagnosis -- every hit here is high-confidence
    by construction (`doctrine_changelog_prose`'s config scan has no
    ambiguous tier), so this carries no soft-language branch the sibling
    `_advisory_reason` needs."""
    kinds = sorted({v.kind for v in violations})
    shown = kinds[:2]
    kinds_text = ", ".join(shown)
    if len(kinds) > len(shown):
        kinds_text += f", +{len(kinds) - len(shown)} more"
    return (
        f"{target} adds {len(violations)} changelog-shaped config token(s) "
        f"({kinds_text}). This config file class is a hard deny: drop the "
        "date, cite a DR by repo-relative path instead of a bare id (the "
        "number alone matches two decision namespaces), or replace the "
        "rot-prone pointer with the live fact it names. A genuine retirement "
        "keeps its date with <!-- doctrine-retirement-exemption: <reason> -->."
    )


@register_op("hooks.guard_doctrine_changelog_prose")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: advise on new changelog-shaped
    doctrine prose, deny new changelog-shaped config debt."""
    if params.get("tool_name", "") not in _GUARDED_TOOLS:
        return no_advisory()

    tool_input = params.get("tool_input")
    if not isinstance(tool_input, dict):
        return no_advisory()

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return no_advisory()

    try:
        target = Path(target_raw).resolve()
    except Exception:
        return no_advisory()

    klass = scope_class(target)
    if klass is None:
        return no_advisory()

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return no_advisory()

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    is_config = klass == "config"
    is_json = target.suffix == ".json"
    new = new_violations(before, after, is_json=is_json, is_config=is_config)
    if not new:
        return no_advisory()

    if is_config:
        reason = render(_deny_message(target_raw, new))
        return deny("PreToolUse", reason)

    context = render(_advisory_message(target_raw, new))
    return allow_advisory("PreToolUse", context)


def _advisory_message(target: str, violations: list):
    return compose(_advisory_reason(target, violations), anchor=_RULE_ANCHOR)


def _deny_message(target: str, violations: list):
    return compose(_deny_reason(target, violations), anchor=_RULE_ANCHOR)
