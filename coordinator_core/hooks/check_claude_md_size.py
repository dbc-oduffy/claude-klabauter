"""coordinator_core.hooks.check_claude_md_size — PreToolUse
(Write|Edit|MultiEdit) op: the residual advisory legs of the CLAUDE.md
budget gate -- the C7 admission-gate ledger check (deny) and the byte-size
soft-warn (advisory-only).

Arrival note (W4-C5, `docs/plans/2026-09-18-doe-holds-no-scripts.md`): ported
from DoE-claude `coordinator/hooks/scripts/check-claude-md-size.py`. That
source's own docstring names a 2026-07-29 EM-ratified decision
(`docs/plans/2026-07-29-hook-fan-in-write-path.md` § C8, also documented on
`coordinator_core.write_guards.check_claude_md_size`'s own module docstring)
that the C7 admission-gate ledger check and the soft-warn leg would stay
DoE-resident PERMANENTLY -- porting them was judged to invert the
coordinator-claude-depends-on-claude-klabauter direction the tri-plane split
establishes, since the check needed to resolve a DoE repo root and read
DoE-resident working data at hook time.

This row's own body, re-authored against the W4-C1 verdict, revisits that
call: command/native-door reach through `hook-run` resolves the concern the
2026-07-29 decision was actually about (a live cross-repo import at hook
time) without inverting the dependency direction -- the payload's own `cwd`
resolves the session's repo, and doctrine assets resolve through the plugin
root, never a hardcoded DoE path. The HARD_LIMIT_BYTES leg already ported
separately (`coordinator_core.write_guards.check_claude_md_size`, a
different op) is UNCHANGED and untouched by this module -- this hook never
duplicates that deny. Its own scope is exactly the source's residual
responsibility after that 2026-07-29 fold: the C7 admission-gate ledger
check (deny) and the SOFT_LIMIT_BYTES warning (advisory, not a silent
stderr-only warning as the DoE source's non-command-door incarnation was --
this door surfaces it to the model directly).

Shape change: stdin/stdout JSON + `sys.exit(code)` control flow is replaced
with the payload-dict-in/response-out `register_op` contract, same as every
sibling guard this row lands. The pure `evaluate()` core returns
`(Message, channel)` or `None` (silent allow), built from
`coordinator_core.hooks.claude_md_ledger` (already landed, this same chunk)
for the admission-gate predicate and `coordinator_core.claude_md_budget`
(already landed, the claude-klabauter-owned SSOT) for the size discriminant and
thresholds -- no local fallback copy of either: unlike the DoE source (which
degrades to a local approximation when the claude-klabauter engine is unresolvable
because IT runs outside this engine's own process), this module IS the
engine -- `coordinator_core.claude_md_budget` and
`coordinator_core.hooks.claude_md_ledger` are always importable siblings,
never optional.

Simulation of the pending edit reuses
`coordinator_core.write_guards._sentinel_write_guard.reconstruct_after` and
`coordinator_core.hooks.support.sentinel_write_guard.extract_target_path` --
the same consolidated primitives `guard_doctrine_surface_ratio` and
`guard_doctrine_changelog_prose` already use for the identical
Write/Edit/MultiEdit-replay idiom -- rather than a third hand-copy of the
DoE source's own local `simulate()`.

Token estimate: `coordinator_core.ops.measure_token_envelope.estimate_tokens`,
imported directly (in-process; no `_arm_lazy_ops`/engine-root-probe
indirection -- that machinery existed only because the DoE source ran
OUTSIDE this engine's own process and had to resolve+place it on `sys.path`
first).

Two independent predicates, both may fire on the same payload -- the C7
admission-gate deny takes precedence (matches the source's own exit-2-before-
exit-1 ordering: `main()` returns on the admission-deny branch before ever
reaching the soft-warn check):
  1. C7 admission gate -- ANY `claude_md_ledger.GOVERNED_AUTHORING_SURFACES`
     target (resolved via `claude_md_ledger.resolve_governed_surface`) whose
     edit grows a heading section without a ledger row backing the growth:
     deny, reason text from `admission_check_for_surface`'s own refusal
     message. A read failure on an EXISTING target, or a `LedgerError`
     raised while consulting the ledger, also denies (fail LOUD, never a
     silent skip) -- matches the source's exit-2 read-failure/LedgerError
     legs.
  2. Byte-size soft-warn -- a `claude_md_budget.is_governed_claude_md`
     target (the narrower SIZE-governed set: `~/.claude/CLAUDE.md` or a
     dev-repo-sentinel-marked `<dev-repo>/coordinator/CLAUDE.md`) whose
     simulated post-edit size exceeds `SOFT_LIMIT_BYTES`: advisory, naming
     the size (and token estimate, when resolvable) against both
     thresholds. The HARD_LIMIT_BYTES leg is NOT this module's job (see
     arrival note above) -- this leg only ever reaches the soft path.

Fail-open (returns `None`), in order: `tool_name` not in the guarded set; no
target path in `tool_input`; neither predicate's governed-surface check
matches; simulation (`reconstruct_after`) cannot reconstruct the after-state.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C5
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core import claude_md_budget
from coordinator_core._hook_envelope import payload_of
from coordinator_core.hooks._envelope import allow_advisory, deny, no_advisory
from coordinator_core.hooks.claude_md_ledger import (
    LedgerError,
    admission_check_for_surface,
    resolve_governed_surface,
)
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

_RULE_ANCHOR = "the CLAUDE.md admission gate and byte-size budget"

_CHANNEL_ADVISORY = "advisory"
_CHANNEL_DENY = "deny"


def _estimate_tokens(text: str):
    try:
        from coordinator_core.ops.measure_token_envelope import estimate_tokens

        return estimate_tokens(text)
    except Exception:
        return None


def _token_note(text: str) -> str:
    tokens = _estimate_tokens(text)
    return f", ~{tokens} tokens (estimate)" if tokens is not None else ""


def _read_failure_message(target: str, exc: BaseException):
    return compose(
        f"could not read existing {target}: {exc}. Not silently skipping the "
        "C7 admission check.",
        anchor=_RULE_ANCHOR,
    )


def _ledger_error_message(exc: BaseException):
    return compose(str(exc), anchor=_RULE_ANCHOR)


def _admission_denied_message(target: str, size: int, token_note: str, refusal_message: str):
    return compose(refusal_message, anchor=_RULE_ANCHOR)


def _soft_warn_message(target: str, size: int, token_note: str, soft: int, hard: int):
    return compose(
        f"{size}b (soft {soft}, hard {hard}) -- approaching perf threshold.",
        anchor=_RULE_ANCHOR,
    )


def evaluate(payload: dict):
    """Pure core: given a parsed PreToolUse payload, returns the
    `(Message, channel)` pair to build a verdict from, or `None` for a
    silent no-op. Separable from the `register_op` handler for the same
    reason every sibling guard this row lands keeps the split -- tests can
    drive it with synthesized payloads directly."""
    if not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name", "")
    if tool_name not in _GUARDED_TOOLS:
        return None

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    target_raw = extract_target_path(tool_input)
    if not target_raw:
        return None

    try:
        target = Path(target_raw).resolve()
    except Exception:
        return None

    size_governed = claude_md_budget.is_governed_claude_md(str(target))
    admission_surface = resolve_governed_surface(target, _repo_root_for(target))
    if not size_governed and admission_surface is None:
        return None

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return None

    after = reconstruct_after(tool_name, tool_input, before)
    if after is None:
        return None

    size = len(after.encode("utf-8"))
    token_note = _token_note(after)

    if admission_surface is not None:
        if target.is_file():
            try:
                old_content = target.read_text(encoding="utf-8")
            except Exception as exc:
                return _read_failure_message(target_raw, exc), _CHANNEL_DENY
        else:
            old_content = ""

        try:
            allowed, refusal_message = admission_check_for_surface(
                admission_surface, old_content, after, _repo_root_for(target)
            )
        except LedgerError as exc:
            return _ledger_error_message(exc), _CHANNEL_DENY

        if not allowed:
            return (
                _admission_denied_message(target_raw, size, token_note, refusal_message),
                _CHANNEL_DENY,
            )

    if size_governed and size > claude_md_budget.SOFT_LIMIT_BYTES:
        return (
            _soft_warn_message(
                target_raw, size, token_note, claude_md_budget.SOFT_LIMIT_BYTES, claude_md_budget.HARD_LIMIT_BYTES
            ),
            _CHANNEL_ADVISORY,
        )

    return None


def _repo_root_for(target: Path) -> Path:
    """Walk up from `target` to the nearest `.git`-bearing ancestor, or fall
    back to `target`'s parent when none is found. `resolve_governed_surface`/
    `admission_check_for_surface` only use this to join a per-surface ledger
    path, so an unresolvable repo root degrades to "no ledger found" rather
    than raising -- consistent with this module's own fail-open contract."""
    for candidate in (target, *target.parents):
        if (candidate / ".git").exists():
            return candidate
    return target.parent


@register_op("hooks.check_claude_md_size")
def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: C7 admission-gate deny plus the
    byte-size soft-warn -- the residual advisory legs of the CLAUDE.md
    budget gate (the HARD_LIMIT_BYTES deny is a separate, already-landed
    write-guards op)."""
    params = payload_of(params)
    result = evaluate(params)
    if result is None:
        return no_advisory()

    message, channel = result
    if channel == _CHANNEL_DENY:
        return deny("PreToolUse", render(message))
    return allow_advisory("PreToolUse", render(message))
