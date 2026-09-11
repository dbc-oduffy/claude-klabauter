"""
coordinator_core.ops.workflow_bind — JSON-RPC "workflow.bind_args" operation.

Purpose: turn a PARAMETERIZED fleet Workflow `.mjs` script — one whose only
variable input is the harness-injected `args` global — into a STANDALONE,
fireable script with that input bound as a literal. Returns the composed
script TEXT; it does not write to disk, matching `workflow.scaffold`'s
contract (the caller — a DoE veneer, this repo's op-invoke CLI via stdout
redirect, or an EM — places the file). Scope "none" / COMPUTE_ONLY.

WHY THIS EXISTS. Two emitters already ship for the two shapes we had:
`workflow.scaffold` composes a skeleton from nothing, and `dispatch.emit`
composes a whole script from a plan spine's waves. Neither covers the third
and most common shape — a workflow script that is already written, correct,
and reviewed, whose every run differs only in its `args`. For those, the
"emitter" was the EM: it hand-typed the args object into the `Workflow` tool
call. That is the shape this op removes.

A hand-typed args object fails in three ways an emitted one cannot:

  1. **Silent field omission.** `plan-blitz.mjs`'s own args contract carries a
     standing note that `executionOpen` reached a live wave missing, because a
     caller copied an example that omitted it. Every omission there is silent
     by construction — the wave still reports the batons, under a different
     key. A caller that DERIVES the field from the frozen gate report cannot
     omit it; a caller that types it can.
  2. **No archived record of what was fired.** An args object that exists only
     inside a tool call is not on disk, so a run cannot be reproduced,
     re-fired, or diffed against the next one. An emitted script is an
     artifact that archives with the trail — the property `dispatch.emit`'s
     own consumers already rely on.
  3. **A tool-call payload that scales with the wave.** Eight batons of
     frontmatter inlined into one call is a large, unreviewable literal, and
     the size is load-bearing on nothing.

HOW THE BINDING WORKS, and why it is a prepend rather than a rewrite. A fleet
Workflow script must OPEN with `export const meta = {...}` as a pure literal —
the contract checker reads that block, and only that block, to learn the run's
phases. So the bound copy is composed as:

    <the source script's own meta block, verbatim>
    const args = <the caller's args, as a JSON literal>
    <the rest of the source script, verbatim>

Nothing in the body is rewritten, reordered, or re-derived. The source script
keeps sole authorship of its own logic and its own phase declarations; this op
contributes exactly one statement. A `const args` at the top of the wrapped
body shadows the harness-injected global of the same name for the whole
script, which is what makes the emitted copy standalone.

REFUSALS, all loud. This op refuses rather than emitting a script that would
fail somewhere less legible than here:

  - no `export const meta = {` opening the source (not a fleet Workflow
    script, or one whose meta is computed — the contract forbids that anyway);
  - a source that already declares `args` at top level (`const`/`let`/`var`),
    because the bound declaration would be a duplicate-declaration SyntaxError
    at fire time, reported as a broken vehicle rather than a mis-bind;
  - `args` that is not a JSON object, or that does not round-trip through
    `json.dumps` (a value the emitted literal could not carry faithfully).

A refusal names the source path and the reason. An emitted script that is
wrong in the body is not this op's failure mode to catch — `workflow.validate`
is the contract checker, and the veneer runs it over the emitted file.

Wire params:
    script_path (str, required) — path to the parameterized `.mjs` source.
                                   Read as UTF-8; must exist.
    args (dict, required)       — the args object to bind. JSON-serializable.

Reply fields:
    script (str)      — the composed standalone script text.
    source_path (str) — the resolved source path the text was composed from.
    bound_keys (list) — sorted top-level keys of the bound args, so a caller
                        can assert what it bound without re-parsing the text.

Spec backlink: DoE-claude `coordinator/skills/plan-blitz/SKILL.md` § Fire the
wave, whose veneer `skills/plan-blitz/emit-wave-fire.py` is the first consumer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op

_META_OPEN = "export const meta = {"

# Top-level `args` declarations in the SOURCE. Matched at column zero only:
# an `args` bound inside a function is an ordinary parameter and shadows
# nothing this op writes, so refusing on it would refuse correct scripts.
_ARGS_DECLS = ("const args", "let args", "var args")


def _meta_block_end(lines: list[str], open_index: int) -> Optional[int]:
    """Index of the line closing the meta literal, or None if it never closes.

    The block is closed by a `}` at column zero — the same shape the contract
    checker and every fleet script already use. Brace counting is deliberately
    NOT used: the meta literal contains description and detail strings full of
    braces in prose, and a counter over those is the kind of parser that is
    right until one phase detail mentions a template literal.
    """
    for i in range(open_index + 1, len(lines)):
        if lines[i].startswith("}"):
            return i
    return None


def bind_args(source: str, args: dict, source_path: str = "<source>") -> str:
    """Compose the standalone script text. Pure; raises ValueError on refusal."""
    lines = source.splitlines()

    open_index = None
    for i, line in enumerate(lines):
        if line.startswith(_META_OPEN):
            open_index = i
            break
    if open_index is None:
        raise ValueError(
            f"workflow.bind_args: {source_path} does not open a "
            f"`{_META_OPEN}...}}` block at column zero — it is not a fleet "
            "Workflow script, or its meta is computed rather than a pure "
            "literal, which the workflow contract already forbids."
        )

    close_index = _meta_block_end(lines, open_index)
    if close_index is None:
        raise ValueError(
            f"workflow.bind_args: {source_path} opens a meta block at line "
            f"{open_index + 1} that is never closed by a `}}` at column zero."
        )

    for i, line in enumerate(lines):
        if any(line.startswith(decl) for decl in _ARGS_DECLS):
            raise ValueError(
                f"workflow.bind_args: {source_path} already declares `args` at "
                f"top level (line {i + 1}: {line.strip()!r}). Binding would emit "
                "a duplicate declaration, which fails at fire time as a "
                "SyntaxError and reads as a broken vehicle rather than a "
                "mis-bind. Bind a script that consumes the injected `args` "
                "global instead."
            )

    if not isinstance(args, dict):
        raise ValueError(
            f"workflow.bind_args: args must be a JSON object, got "
            f"{type(args).__name__}."
        )
    try:
        literal = json.dumps(args, indent=2, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"workflow.bind_args: args is not JSON-serializable ({exc}). The "
            "emitted literal could not carry it faithfully."
        ) from exc

    head = "\n".join(lines[: close_index + 1])
    tail = "\n".join(lines[close_index + 1 :])

    binding = (
        "\n"
        "// ---------------------------------------------------------------------------\n"
        "// EMITTED, NOT HAND-WRITTEN. This file is a bound copy of\n"
        f"//   {source_path}\n"
        "// composed by `workflow.bind_args`. The body below is that script verbatim;\n"
        "// the only addition is the `args` literal, which shadows the harness-injected\n"
        "// global of the same name and makes this copy standalone — fire it with\n"
        "// `Workflow({scriptPath: <this file>})` and NO args.\n"
        "//\n"
        "// Edit the SOURCE and re-emit. An edit here is lost on the next emit, and a\n"
        "// bound copy that has drifted from its source is the one artifact a reader\n"
        "// will trust and should not.\n"
        "// ---------------------------------------------------------------------------\n"
        f"const args = {literal}\n"
    )

    return f"{head}\n{binding}{tail}\n" if tail else f"{head}\n{binding}"


@register_op("workflow.bind_args")
def _workflow_bind_args(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "workflow.bind_args" handler. See module docstring."""
    script_path = params.get("script_path")
    if not script_path:
        raise ValueError("workflow.bind_args requires param: script_path")
    if "args" not in params:
        raise ValueError("workflow.bind_args requires param: args")

    path = Path(script_path)
    if not path.is_absolute() and repo_root is not None:
        path = Path(repo_root) / path
    if not path.is_file():
        raise ValueError(
            f"workflow.bind_args: no script at {path} — the source must exist "
            "on disk. A missing source reads as 'the vehicle does not exist' "
            "when it is nearly always an unresolved plugin root."
        )

    source = path.read_text(encoding="utf-8")
    args = params.get("args")
    script = bind_args(source, args, source_path=str(path))

    return {
        "script": script,
        "source_path": str(path),
        "bound_keys": sorted(args.keys()),
    }
