"""coordinator_core.hooks.guard_python_syntax_on_write — PreToolUse
(Write|Edit|MultiEdit) hard-deny op: denies a write that would leave
unparseable Python on disk under `coordinator_core/`.

Arrival note (W4-C7, docs/plans/2026-09-18-doe-holds-no-scripts.md): ported
from DoE-claude `coordinator/hooks/scripts/guard-python-syntax-on-write.py`.
Two shape changes, both forced by DR-047 (claude-klabauter owns guard logic, DoE owns
plumbing) plus this row's own op contract, neither a behaviour change:

  (a) `_SCOPE_DIR` moves from DoE's `"coordinator"` (that repo's own live,
      no-build-step plugin tree) to this repo's own equivalent hazard
      surface, `"coordinator_core"` — the pure-Python, no-build-step,
      warm-engine-loaded tree this project's own CLAUDE.md names ("No build
      step, no lint, no CI"). A `.py` write elsewhere in this repo is not a
      live hook/op body and carries none of the fleet-wide blast radius
      this guard exists for, same reasoning as the source, different tree.
  (b) `reconstruct_after` now comes from `coordinator_core.write_guards.
      _sentinel_write_guard` (the consolidated home this repo's own docstring
      there names THIS guard as one of the six DoE callers it anticipates)
      rather than a local hand-copy of `_reconstruct_after` — DoE's version
      duplicated an idiom six of its guards each hand-copied; claude-klabauter already
      has the one consolidated body. `extract_target_path` comes from the
      sibling `coordinator_core.hooks.support.sentinel_write_guard`.
      Composing the deny reason still goes through `coordinator_core.hooks.
      support.message_envelope.compose`/`render` (already landed, W4-C3) —
      but this op returns the envelope dict directly via `_hook_envelope.
      deny` rather than calling `message_envelope.emit()`'s stdout-writing
      `CHANNEL_DENY` path, which is shaped for the old stdin/stdout hook
      entrypoint this row's body explicitly says NOT to add
      ("add no per-hook coordinator/bin shim").

Why this exists (unchanged from the source): this tree's hooks/ops are
interpreted source with no build step, served live by the warm engine. A
`SyntaxError` written into one does not fail a test run later — it lands in
every concurrent session at once, on the next fire, with that op or hook
failing open (or the whole process failing to import it). This guard is the
mechanism for "parse it before it lands", not a remembered step in a brief.

Parseability ONLY — a deliberately narrow bar. `compile()` in "exec" mode,
nothing more. Not lint, not import resolution, and emphatically not
executing or importing the module: a guard with side effects at the write
seam is a worse failure than the one it prevents. A file that parses but is
semantically wrong is out of scope by design.

Deny, not advise — no before/after diff. Unparseable is unparseable
regardless of what was there before.

Fail-open (returns `no_advisory()`, i.e. ALLOW), in order: `tool_name` not
in the guarded set; no target path in `tool_input`; target not a `.py` file
under `coordinator_core`; on-disk read failure for an existing file;
ambiguous before/after reconstruction (`reconstruct_after` returned None);
after-text that compiles. A guard that cannot compute its own input has no
basis to deny.

Deny message: carries the `SyntaxError`'s line number, message, and the
offending line's text — deliberately not routed through a wiki anchor. The
reader is mid-repair on a file they are looking at; the line number IS the
remedy.

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C7
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core._hook_envelope import deny
from coordinator_core.hooks._envelope import no_advisory
from coordinator_core.hooks.support.message_envelope import compose, render
from coordinator_core.hooks.support.sentinel_write_guard import extract_target_path
from coordinator_core.ipc import register_op
from coordinator_core.write_guards._sentinel_write_guard import reconstruct_after

_GUARDED_TOOLS = ("Write", "Edit", "MultiEdit")

#: Only this repo's own live, no-build-step engine tree. See module
#: docstring (a).
_SCOPE_DIR = "coordinator_core"


def is_in_scope(target: Path) -> bool:
    """A `.py` file under this repo's `coordinator_core/` tree."""
    if target.suffix != ".py":
        return False
    return _SCOPE_DIR in target.parts


def _excerpt(text: str, lineno: "int | None") -> str:
    if not isinstance(lineno, int) or lineno < 1:
        return ""
    lines = text.split("\n")
    if lineno > len(lines):
        return ""
    return " ".join(lines[lineno - 1].split())[:120]


def _deny_reason(target: str, exc: SyntaxError, after: str) -> str:
    """The prose diagnosis (the only part `message_envelope.CEILING` counts),
    kept plain-string-returning for the character-cap measurement harness."""
    where = f"line {exc.lineno}" if exc.lineno else "an unknown line"
    excerpt = _excerpt(after, exc.lineno)
    tail = f": {excerpt}" if excerpt else ""
    return f"python syntax: unparseable at {where} -- {exc.msg}{tail}. Breaks every concurrent session on this live tree."


@register_op("hooks.guard_python_syntax_on_write")
async def _handler(params: dict, repo_root=None) -> dict:
    """PreToolUse(Write|Edit|MultiEdit) op: deny a write that leaves
    unparseable Python on disk under `coordinator_core/`.

    `params` is the flat PreToolUse payload (`tool_name`, `tool_input` as a
    dict, …) per this package's payload-dict-in / hook-response-out
    contract.
    """
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

    if not is_in_scope(target):
        return no_advisory()

    try:
        before = target.read_text(encoding="utf-8", errors="replace") if target.is_file() else ""
    except Exception:
        return no_advisory()

    after = reconstruct_after(params.get("tool_name", ""), tool_input, before)
    if after is None:
        return no_advisory()

    try:
        compile(after, str(target), "exec")
    except SyntaxError as exc:
        reason = render(compose(_deny_reason(target_raw, exc, after)))
        return deny("PreToolUse", reason)
    except Exception:
        return no_advisory()

    return no_advisory()
