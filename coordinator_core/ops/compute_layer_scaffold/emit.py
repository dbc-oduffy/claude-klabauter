"""coordinator_core.ops.compute_layer_scaffold.emit — the Sub-shape B module composer.

Purpose: composes the TEXT of a `<skill>_assemble` decision-object producer
skeleton from a caller-declared verb set. Emission is f-string concatenation
— the same shape as `coordinator_core.ops.workflow_scaffold::_compose_script`
— never a reusable template engine, and this module does NOT import
`workflow_scaffold`: the spike verified that module's emission is plain
concatenation, not shared machinery, so copying the shape is the intended
reuse (docs/research/spike-verdicts/2026-08-13-compute-layer-scaffolder-emits-conformant-producer.md).

The emitted module IMPORTS AND COMPOSES, never re-implements:
  - `coordinator_core.contract.decision_object.envelope.build_envelope`
  - `coordinator_core.contract.decision_object.envelope.extend_exit_codes`
  - `coordinator_core.contract.apply_base.execute_directives`

Negative-spec (AC4): the emitted exit ladder is always the explicit
`BUSINESS_FAIL=1, USAGE=2, TRANSPORT_FAIL=3` — `extend_exit_codes` anchors
ONLY `SUCCESS = 0` and RAISES on a caller-supplied `SUCCESS`
(`contract/decision_object/envelope.py::extend_exit_codes`); the emitted
`main()` therefore always returns `ExitCode.SUCCESS`, never `ExitCode.OK`
(the exact bug the spike's hand-written probe shipped, undetected by its own
generated test — see the spike verdict § finding 3).

Negative-spec (AC5): `_CLI_DISPATCH` is always emitted as a closed dict
literal over the module's own `CONSUMES_MANIFEST` — this emitter has no
code path producing `getattr`/`import_module` dispatch.

Negative-spec (AC12): this module writes TEXT only. It does not write to
disk, does not register the emitted module anywhere (no `authz/
classification.py`, no `ops/_registry_map.py`, no `bin/` trampoline) — that
is explicitly out of scope for this chunk (C4's surface).

Spec backlink: docs/plans/2026-08-13-compute-layer-scaffolder.md, chunk C1.
"""

from __future__ import annotations

import re
from typing import Sequence

_IDENT_SANITIZE_RE = re.compile(r"[^0-9A-Za-z_]")


class InvalidVerb(ValueError):
    """Raised when a declared verb cannot be turned into a valid Python
    identifier suffix for its `_dispatch_<verb>` handler name."""


def _dispatch_fn_name(verb: str) -> str:
    """Renders `verb` (e.g. "handoff.supersede_predecessor") into a valid
    `_dispatch_<...>` function name by replacing every non-identifier
    character with `_`. Raises `InvalidVerb` if `verb` is empty or the
    sanitized result collides with the bare `_dispatch_` prefix (i.e. the
    verb carried no identifier-safe characters at all)."""
    if not verb:
        raise InvalidVerb("a manifest verb must be a non-empty string")
    sanitized = _IDENT_SANITIZE_RE.sub("_", verb)
    if not sanitized or sanitized.strip("_") == "":
        raise InvalidVerb(f"verb {verb!r} sanitizes to an empty identifier")
    return f"_dispatch_{sanitized}"


def _pascal_case(skill_name: str) -> str:
    """Renders a skill name (e.g. "pickup_assemble") into PascalCase for
    the emitted exit-code enum's class name (e.g. "PickupAssembleExitCode")."""
    parts = _IDENT_SANITIZE_RE.sub("_", skill_name).split("_")
    return "".join(part.capitalize() for part in parts if part)


def _py_str_literal(value: str) -> str:
    """Render `value` as a single-quoted Python string literal, escaping
    backslashes/quotes/newlines so the emitted text stays a PURE LITERAL."""
    escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
    return f"'{escaped}'"


def _docstring_safe(value: str) -> str:
    """Escapes `value` for safe interpolation inside a `\"\"\"`-delimited
    docstring: backslashes first (else a later-inserted backslash could
    itself be escaped), then breaks up any `\"\"\"` run so it cannot
    terminate the enclosing docstring early, then neutralizes newlines so
    the header always renders as a single visual line.

    Review: coordinator:code-reviewer — caller-supplied `skill_name` reached
    `compose_producer_module`'s docstring unescaped (every sibling value
    goes through `_py_str_literal`/`_pascal_case` first), so a value
    containing `\"\"\"` could break out of the docstring and leave the
    remainder of the string as live module source — a correctness/
    robustness defect (the malformed module fails to parse, since the
    docstring is emitted ahead of the mandatory `from __future__ import
    annotations`), not a confirmed code-execution path.
    """
    escaped = value.replace("\\", "\\\\")
    escaped = escaped.replace('"""', '\\"\\"\\"')
    escaped = escaped.replace("\n", "\\n")
    return escaped


def compose_producer_module(skill_name: str, verbs: Sequence[str]) -> str:
    """Composes the full TEXT of a Sub-shape B `<skill_name>_assemble`
    decision-object producer skeleton.

    Args:
        skill_name: the producer's skill identifier (e.g. "example_assemble"),
            used for the module docstring and the exit-code enum's class
            name.
        verbs: the declared `CONSUMES_MANIFEST` — one `_dispatch_<verb>`
            stub and one `_CLI_DISPATCH` entry is emitted per verb, in the
            order given. Raises `InvalidVerb` if a verb is empty or has no
            identifier-safe characters, and `ValueError` if `verbs` is
            empty (a producer with no consumable verbs has nothing for
            `apply()` to dispatch).

    Returns:
        The composed module TEXT (a `str`) — this function performs no
        file I/O and registers nothing; the caller decides whether/where
        to write it (AC12).
    """
    if not skill_name:
        raise ValueError("compose_producer_module requires a non-empty skill_name")
    if not verbs:
        raise ValueError("compose_producer_module requires at least one verb")

    verb_list = list(verbs)
    fn_names = [_dispatch_fn_name(v) for v in verb_list]
    exit_code_name = f"{_pascal_case(skill_name)}ExitCode"

    manifest_literal = ", ".join(_py_str_literal(v) for v in verb_list)
    if len(verb_list) == 1:
        manifest_literal += ","

    module_docstring = (
        f'"""{_docstring_safe(skill_name)} — generated Sub-shape B decision-object producer skeleton.\n\n'
        "Generated by coordinator_core.ops.compute_layer_scaffold.emit."
        "compose_producer_module. Composes rather than re-implements the\n"
        "shared envelope/exit-code/dispatch contract:\n"
        "coordinator_core.contract.decision_object.envelope.build_envelope,\n"
        "coordinator_core.contract.decision_object.envelope.extend_exit_codes,\n"
        "coordinator_core.contract.apply_base.execute_directives.\n"
        '"""\n'
    )

    header = (
        f"{module_docstring}\n"
        "from __future__ import annotations\n\n"
        "from pathlib import Path\n"
        "from typing import Any, Callable\n\n"
        "from coordinator_core.contract.decision_object.envelope import (\n"
        "    build_envelope,\n"
        "    extend_exit_codes,\n"
        ")\n"
        "from coordinator_core.contract import apply_base\n"
    )

    manifest_block = (
        "\nCONSUMES_MANIFEST: tuple[str, ...] = ("
        f"{manifest_literal}"
        ")\n"
    )

    exit_code_block = (
        f"\n{exit_code_name} = extend_exit_codes(\n"
        f"    {_py_str_literal(exit_code_name)},\n"
        "    BUSINESS_FAIL=1,\n"
        "    USAGE=2,\n"
        "    TRANSPORT_FAIL=3,\n"
        ")\n"
    )

    dispatch_stubs = "\n".join(
        (
            f"\ndef {fn_name}(args: list[str], repo_root: Path) -> dict[str, Any]:\n"
            f'    """Generated stub for verb {_py_str_literal(verb)} — replace with a real handler."""\n'
            f"    raise NotImplementedError({_py_str_literal(f'{fn_name} is a generated stub')})\n"
        )
        for verb, fn_name in zip(verb_list, fn_names)
    )

    dispatch_table_entries = "\n".join(
        f"    {_py_str_literal(verb)}: {fn_name}," for verb, fn_name in zip(verb_list, fn_names)
    )
    dispatch_table_block = (
        "\n_CLI_DISPATCH: dict[str, Callable[[list[str], Path], dict[str, Any]]] = {\n"
        f"{dispatch_table_entries}\n"
        "}\n"
    )

    brief_block = (
        "\n\ndef brief(**kwargs: Any) -> dict[str, Any]:\n"
        '    """Composes an 8-key decision-object envelope via build_envelope."""\n'
        "    return build_envelope(**kwargs)\n"
    )

    apply_block = (
        "\n\ndef apply(\n"
        "    directives: list[dict[str, Any]],\n"
        "    judgment_points: list[dict[str, Any]],\n"
        "    repo_root: Path,\n"
        "    **kwargs: Any,\n"
        ") -> tuple[int, dict[str, Any]]:\n"
        '    """Delegates to apply_base.execute_directives over the closed _CLI_DISPATCH table."""\n'
        "    return apply_base.execute_directives(\n"
        "        directives, judgment_points, repo_root, _CLI_DISPATCH, **kwargs\n"
        "    )\n"
    )

    main_block = (
        "\n\ndef main(argv: list[str]) -> int:\n"
        '    """Generated CLI entrypoint: dispatches "brief"/"apply", else USAGE."""\n'
        "    if not argv:\n"
        f"        return int({exit_code_name}.USAGE)\n"
        "    verb = argv[0]\n"
        "    if verb == 'brief':\n"
        "        brief()\n"
        f"        return int({exit_code_name}.SUCCESS)\n"
        "    if verb == 'apply':\n"
        "        exit_code, _report = apply([], [], Path.cwd())\n"
        "        if exit_code == apply_base.APPLY_EXIT_OK:\n"
        f"            return int({exit_code_name}.SUCCESS)\n"
        f"        return int({exit_code_name}.BUSINESS_FAIL)\n"
        f"    return int({exit_code_name}.USAGE)\n"
    )

    return (
        header
        + manifest_block
        + exit_code_block
        + dispatch_stubs
        + dispatch_table_block
        + brief_block
        + apply_block
        + main_block
    )
