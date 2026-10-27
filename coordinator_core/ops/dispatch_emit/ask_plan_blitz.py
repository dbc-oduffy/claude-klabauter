"""DoE plan-blitz single mode as an in-script stage of the warp --ask script.

wrap_stage turns workflows/plan-blitz.mjs into one async function `planBlitz(args)` and returns
its phase titles; the caller merges them into the ask script's own meta and invokes the function.
DoE's file is read, never written.
"""

from __future__ import annotations

import re
from pathlib import Path

from coordinator_core.ops.dispatch_emit import prompt_fragments as _prompt_fragments

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


def strip_comment_lines(js: str) -> str:
    """``js`` without the lines holding only a ``//`` comment.

    Strings, templates, regexes and block comments are untouched.

    Trap: regex-vs-division is decided from the previous significant character, as in
    ``prompt_fragments._scan_literals``; a regex literal that defeats it can hide a quote and
    derail the scan, so the only deletions are whole comment lines the scan reached in code mode.
    """
    n, i, prev = len(js), 0, ""
    stack: list = []  # "T" = inside a template body; an int = brace depth inside a `${ }`
    cuts: list[tuple[int, int]] = []
    while i < n:
        c = js[i]
        if stack and stack[-1] == "T":
            if c == "\\":
                i += 2
            elif c == "`":
                stack.pop()
                prev, i = "`", i + 1
            elif js.startswith("${", i):
                stack.append(0)
                prev, i = "{", i + 2
            else:
                i += 1
        elif c in "'\"":
            j = i + 1
            while j < n and js[j] != c and js[j] != "\n":
                j += 2 if js[j] == "\\" else 1
            prev, i = c, j + 1
        elif c == "`":
            stack.append("T")
            i += 1
        elif c == "/" and js.startswith("//", i):
            eol = js.find("\n", i)
            eol = n if eol < 0 else eol
            ls = js.rfind("\n", 0, i) + 1
            if not js[ls:i].strip():
                cuts.append((ls, min(eol + 1, n)))
            i = eol
        elif c == "/" and js.startswith("/*", i):
            k = js.find("*/", i + 2)
            i = n if k < 0 else k + 2
        elif c == "/" and (prev == "" or prev in _prompt_fragments._PREV_OPENERS or js[max(0, i - 8) : i].rstrip().endswith(("return", "typeof"))):
            j, in_cls = i + 1, False
            while j < n and (in_cls or js[j] != "/") and js[j] != "\n":
                if js[j] == "\\":
                    j += 1
                elif js[j] == "[":
                    in_cls = True
                elif js[j] == "]":
                    in_cls = False
                j += 1
            prev, i = "/", j + 1
        else:
            if c == "{" and stack:
                stack[-1] += 1
            elif c == "}" and stack:
                if stack[-1] == 0:
                    stack.pop()
                else:
                    stack[-1] -= 1
            if not c.isspace():
                prev = c
            i += 1
    out, last = [], 0
    for a, z in cuts:
        out.append(js[last:a])
        last = z
    out.append(js[last:])
    return "".join(out)
