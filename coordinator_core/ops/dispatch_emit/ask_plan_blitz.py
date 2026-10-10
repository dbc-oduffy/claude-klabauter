"""DoE plan-blitz single mode as an in-script stage of the warp --ask script.

wrap_stage turns workflows/plan-blitz.mjs into one async function `planBlitz(args)` and returns
its phase titles; the caller merges them into the ask script's own meta and invokes the function.
DoE's file is read, never written.
"""

from __future__ import annotations

import re
from pathlib import Path

STAGE_FN = "planBlitz"
_META_OPEN = re.compile(r"^export const meta\s*=\s*", re.MULTILINE)
_TITLE = re.compile(r"\btitle:\s*(?:'((?:[^'\\]|\\.)*)'|\"((?:[^\"\\]|\\.)*)\")")
_QUOTES = "'\"`"
_TOP_AGENT_DECL = re.compile(
    r"^(?:export\s+)?(?:const|let|var|(?:async\s+)?function\*?)\s+agent\b", re.MULTILINE
)


class AskPlanBlitzRefused(ValueError):
    """plan-blitz.mjs could not be resolved or does not have the expected shape."""


def _meta_span(text: str) -> tuple[int, int]:
    """(start, end) of the `export const meta = {...}` statement; end is past the closing brace."""
    m = _META_OPEN.search(text)
    if m is None or text[m.end():m.end() + 1] != "{":
        raise AskPlanBlitzRefused("plan-blitz.mjs carries no `export const meta = {` block")
    depth, i, quote = 0, m.end(), None
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = None
        elif c in _QUOTES:
            quote = c
        elif text.startswith("//", i):
            i = text.find("\n", i)
            if i < 0:
                break
        elif text.startswith("/*", i):
            i = text.find("*/", i)
            if i < 0:
                break
            i += 1
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return m.start(), i + 1
        i += 1
    raise AskPlanBlitzRefused("plan-blitz.mjs `meta` block is unterminated")


def wrap_stage(
    plan_blitz_text: str, *, agent_prefix_var: str | None = None
) -> tuple[str, list[str]]:
    """(JS function text, phase titles): `async function planBlitz(args)` over the file's body.

    The meta export is removed (its phases are returned for the caller's merged meta); the rest
    of the file becomes the function body, so its `args` read binds to the parameter and its
    final `return` is legal.

    With `agent_prefix_var`, the function opens with `const agent = (p, o) => _askAgent(<var> +
    p, o);`, shadowing the runtime `agent` for the whole body. Precondition: the caller declares
    `_askAgent = agent` at script level. Refuses when the body declares a top-level `agent`.
    """
    start, end = _meta_span(plan_blitz_text)
    phases = [a or b for a, b in _TITLE.findall(plan_blitz_text[start:end])]
    if not phases:
        raise AskPlanBlitzRefused("plan-blitz.mjs meta declares no phase titles")
    body = plan_blitz_text[:start] + plan_blitz_text[end:]
    shadow = ""
    if agent_prefix_var is not None:
        if _TOP_AGENT_DECL.search(body):
            raise AskPlanBlitzRefused("plan-blitz.mjs declares a top-level `agent`; the brief shadow would collide")
        shadow = f"const agent = (p, o) => _askAgent({agent_prefix_var} + p, o);\n"
    return f"async function {STAGE_FN}(args) {{\n{shadow}{body}\n}}\n", phases


def load_plan_blitz_text(plugin_root: str | None = None) -> str:
    """Read workflows/plan-blitz.mjs under the resolved plugin root, or refuse by name."""
    if plugin_root is None:
        from coordinator_core.warm.caller_context import resolve_caller_context

        plugin_root = resolve_caller_context().plugin_root
    if not plugin_root:
        raise AskPlanBlitzRefused("plugin root did not resolve; plan-blitz.mjs is unreachable")
    source = Path(plugin_root) / "workflows" / "plan-blitz.mjs"
    if not source.is_file():
        raise AskPlanBlitzRefused(f"no plan-blitz.mjs at {source}")
    return source.read_text(encoding="utf-8")
