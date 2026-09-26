
from __future__ import annotations


def field(params: dict, key: str) -> str:
    """Return the string value of params[key], or "" when absent, None, or empty.

    mcp_tool forwards ONLY declared input fields; an unforwarded or unresolvable
    field arrives as "" — treat "" as ABSENT, never as a real value.

    JSON booleans are normalized to 'true'/'false' (lowercase) to match hook payload
    string conventions. Python's str(True)='True' would silently break comparisons
    such as ``run_in_background == "true"``.

    Args:
        params: the raw params dict from the JSON-RPC request.
        key:    the field name to retrieve.

    Returns:
        str value, or "" if the key is missing, None, or already "".
    """
    value = params.get(key, "")
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def present(params: dict, key: str) -> bool:
    """Return True iff params[key] resolves to a non-empty string value.

    Equivalent to ``bool(field(params, key))`` — a readable guard expression
    for conditional logic in hook handlers.

    Args:
        params: the raw params dict from the JSON-RPC request.
        key:    the field name to test.

    Returns:
        True if the field is present and non-empty; False otherwise.
    """
    return bool(field(params, key))
