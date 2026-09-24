"""
coordinator_core.hooks.tests.test_hooks_handlers_read_through_payload_of --
AST guard: every `hooks.*` handler in `coordinator_core/hooks/*.py` must read
its params argument through `coordinator_core._hook_envelope.payload_of`, in
one of the two shapes the design settled on, or this suite fails.

WHY THIS RULE IS NOT NORMALISED AT `ipc.py`'s DISPATCH CORE OR EITHER DOOR
(negative spec). Two distinct params shapes reach these handlers today: both
engine doors (`coordinator_core/warm/hook_http.py :: build_request` and
`coordinator/bin/hook-run.py`) send the wrapped shape `{"payload": <event>}`,
while the cold DoE guard chain sends the flat event directly. Normalising at
`ipc.py`'s dispatch core, or inside either door, would require picking ONE
shape and rewriting every caller on the other side to match it -- that is a
transport-contract change across a boundary this plan does not own (the cold
chain is DoE-claude's), not a handler-body fix. `payload_of`
(`coordinator_core/_hook_envelope.py`) already normalises both shapes to one
dict; the defect this guard exists to catch is a handler that skips that call
and reads its params directly, so it silently no-ops on whichever shape it
did not anticipate.

THE EXEMPTION LIST IS EMPTY BY DESIGN. A handler that never references its
params argument at all passes vacuously (it has nothing to normalise). Every
handler that DOES reference it must go through `payload_of` -- there is no
carve-out for "this one handler is fine reading params directly," and this
guard must never grow one.

WHY THIS GUARD DOES NOT IMPORT THE MODULES UNDER TEST. Importing
`coordinator_core.hooks.<module>` registers its op as an import side effect
via `@register_op`, which can mask an unconverted handler behind "well,
SOMETHING imported it during this test run" -- the same trap
`test_eager_hook_modules_covers_every_register_op.py` (this guard's in-tree
shape precedent) documents for the eager-import list. This module therefore
parses `coordinator_core/hooks/*.py` as TEXT with `ast` only -- no
`importlib`, no `__import__`, no `subprocess` -- and never imports the
package.

Spec: docs/plans/2026-09-23-hooks-handler-payload-of-audit.md (P171-C8).
"""

from __future__ import annotations

import ast
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parent.parent

#: Minimum count of hooks.* handler functions expected on the tree -- a zero
#: or short walk (e.g. a broken glob, or a directory move) must fail loudly
#: rather than silently passing with nothing checked.
_MIN_EXPECTED_HANDLER_COUNT = 84


def _is_register_op_decorator(dec: ast.expr) -> bool:
    if not isinstance(dec, ast.Call):
        return False
    callee = dec.func
    if isinstance(callee, ast.Name):
        return callee.id == "register_op"
    if isinstance(callee, ast.Attribute):
        return callee.attr == "register_op"
    return False


def _is_hooks_op_decorator(dec: ast.expr) -> bool:
    """A register_op(...) decorator whose first argument is a "hooks."-
    prefixed string literal. Non-literal (constant-reference) op names are
    treated as hooks.* too -- this guard does not need the resolved name,
    only that the call is register_op(...) on a coordinator_core/hooks/
    module, so it counts every such decorator regardless of resolvability."""
    return _is_register_op_decorator(dec)


def _register_op_handlers(tree: ast.Module) -> "list[ast.FunctionDef | ast.AsyncFunctionDef]":
    """Every module-level function decorated with register_op(...)."""
    handlers: "list[ast.FunctionDef | ast.AsyncFunctionDef]" = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_hooks_op_decorator(dec) for dec in node.decorator_list):
            handlers.append(node)
    return handlers


def _non_docstring_body(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "list[ast.stmt]":
    body = list(func.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def _first_param_name(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "str | None":
    args = func.args.args
    if not args:
        return None
    return args[0].arg


def _references_name(node: ast.AST, name: str) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == name:
            return True
    return False


def _is_payload_of_call_on(value: ast.expr, param_name: str) -> bool:
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "payload_of"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Name)
        and value.args[0].id == param_name
        and not value.keywords
    )


def _entry_shape(stmt: ast.stmt, param_name: str) -> "str | None":
    """Returns "P" if `stmt` is `<param_name> = payload_of(<param_name>)`,
    "Q" if it is `payload = payload_of(<param_name>)`, else None."""
    if not isinstance(stmt, ast.Assign):
        return None
    if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name):
        return None
    if not _is_payload_of_call_on(stmt.value, param_name):
        return None
    target_name = stmt.targets[0].id
    if target_name == param_name:
        return "P"
    if target_name == "payload":
        return "Q"
    return None


def _subscripts_payload_key(node: ast.AST, param_name: str) -> bool:
    """True if `node` contains `<param_name>["payload"]` or
    `<param_name>.get("payload", ...)` anywhere."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript):
            if isinstance(sub.value, ast.Name) and sub.value.id == param_name:
                key = sub.slice
                if isinstance(key, ast.Constant) and key.value == "payload":
                    return True
        if isinstance(sub, ast.Call):
            func = sub.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Name)
                and func.value.id == param_name
                and sub.args
                and isinstance(sub.args[0], ast.Constant)
                and sub.args[0].value == "payload"
            ):
                return True
    return False


def _uses_param_as_more_than_whole_value(node: ast.AST, param_name: str) -> bool:
    """True if `node` subscripts `param_name`, calls a method on it, or reads
    an attribute off it -- anything other than passing it as a whole value
    (a call argument, dict(param_name), an isinstance test)."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Name) and sub.value.id == param_name:
            return True
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) and sub.value.id == param_name:
            return True
    return False


def check_handler(func: "ast.FunctionDef | ast.AsyncFunctionDef") -> "str | None":
    """The three-clause rule from the plan's § Design, over one parsed
    handler function. Returns a violation reason string, or None if clean
    (including the vacuous case: the handler never references its first
    parameter at all)."""
    param_name = _first_param_name(func)
    if param_name is None:
        return None

    stmts = _non_docstring_body(func)
    if not any(_references_name(stmt, param_name) for stmt in stmts):
        return None  # vacuous pass: never touches params

    if not stmts or _entry_shape(stmts[0], param_name) is None:
        return (
            f"first non-docstring statement is not `{param_name} = payload_of({param_name})` "
            f"or `payload = payload_of({param_name})`"
        )
    form = _entry_shape(stmts[0], param_name)

    for stmt in stmts:
        if _subscripts_payload_key(stmt, param_name):
            return f'subscripts or .get()s `{param_name}["payload"]` instead of relying on payload_of'

    if form == "Q":
        for stmt in stmts[1:]:
            if _uses_param_as_more_than_whole_value(stmt, param_name):
                return (
                    f"form-Q handler reads `{param_name}` as more than a whole value "
                    f"after the entry statement (subscript or attribute access)"
                )

    return None


def _all_violations() -> "list[tuple[str, str, str]]":
    """(module_stem, function_name, reason) for every violating handler
    across coordinator_core/hooks/*.py."""
    violations: "list[tuple[str, str, str]]" = []
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for func in _register_op_handlers(tree):
            reason = check_handler(func)
            if reason is not None:
                violations.append((path.stem, func.name, reason))
    return violations


def _all_handlers_count() -> int:
    count = 0
    for path in sorted(_HOOKS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count += len(_register_op_handlers(tree))
    return count


def test_at_least_84_registered_handlers_are_found() -> None:
    count = _all_handlers_count()
    assert count >= _MIN_EXPECTED_HANDLER_COUNT, (
        f"only found {count} register_op-decorated hooks.* handler function(s) under "
        f"{_HOOKS_DIR} -- expected at least {_MIN_EXPECTED_HANDLER_COUNT}. A short walk "
        "here (broken glob, moved directory) would otherwise pass this suite vacuously."
    )


def test_no_handler_reads_params_without_payload_of() -> None:
    violations = _all_violations()
    assert not violations, (
        "The following hooks.* handler(s) reference their params argument without "
        "reading it through coordinator_core._hook_envelope.payload_of first (or read "
        "past the params[\"payload\"]/params.get(\"payload\") shape it replaces): "
        + ", ".join(f"{mod}.{fn} ({reason})" for mod, fn, reason in violations)
    )


# ---------------------------------------------------------------------------
# Self-tests: the checker over synthetic source strings, per the plan's
# assertion (3).
# ---------------------------------------------------------------------------


def _handler_from_source(source: str) -> "ast.FunctionDef | ast.AsyncFunctionDef":
    tree = ast.parse(source)
    handlers = _register_op_handlers(tree)
    assert len(handlers) == 1, f"expected exactly one register_op handler in synthetic source, got {len(handlers)}"
    return handlers[0]


def test_flat_read_with_no_entry_statement_is_flagged() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_no_entry")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    tool_name = params.get("tool_name")
    return {"tool_name": tool_name}
'''
    )
    assert check_handler(func) is not None


def test_subscripting_payload_key_is_flagged() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_payload_subscript")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    event = params["payload"]
    return {"event": event}
'''
    )
    assert check_handler(func) is not None


def test_form_q_handler_reading_params_later_is_flagged() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_form_q_late_read")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    payload = payload_of(params)
    session_id = params.get("session_id")
    return {"payload": payload, "session_id": session_id}
'''
    )
    assert check_handler(func) is not None


def test_clean_form_p_handler_passes() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_form_p")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    params = payload_of(params)
    tool_name = params.get("tool_name")
    return {"tool_name": tool_name}
'''
    )
    assert check_handler(func) is None


def test_clean_form_q_handler_passes() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_form_q")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    payload = payload_of(params)
    leg_params = dict(params)
    return {"payload": payload, "leg_params": leg_params}
'''
    )
    assert check_handler(func) is None


def test_handler_never_touching_params_passes_vacuously() -> None:
    func = _handler_from_source(
        '''
@register_op("hooks.synthetic_untouched")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    return {}
'''
    )
    assert check_handler(func) is None


def test_missing_from_eager_style_reverting_form_p_entry_fails_and_names_module() -> None:
    """Spot-check per the plan's Acceptance: reverting one converted form-P
    entry line makes the guard fail and name that module/function -- proven
    here over a synthetic module rather than mutating a real one."""
    tree = ast.parse(
        '''
@register_op("hooks.reverted_form_p")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    tool_name = params.get("tool_name")
    return {"tool_name": tool_name}
'''
    )
    handlers = _register_op_handlers(tree)
    assert len(handlers) == 1
    reason = check_handler(handlers[0])
    assert reason is not None
    assert handlers[0].name == "_handler"


def test_missing_from_eager_style_reverting_form_q_entry_fails_and_names_module() -> None:
    """Spot-check per the plan's Acceptance: reverting one converted form-Q
    entry line (here, a later attribute read past the entry) makes the guard
    fail -- proven over a synthetic module."""
    tree = ast.parse(
        '''
@register_op("hooks.reverted_form_q")
async def _handler(params: dict, repo_root=None) -> dict:
    """Docstring."""
    payload = payload_of(params)
    extra = params.get("session_id")
    return {"payload": payload, "extra": extra}
'''
    )
    handlers = _register_op_handlers(tree)
    assert len(handlers) == 1
    reason = check_handler(handlers[0])
    assert reason is not None
    assert handlers[0].name == "_handler"
