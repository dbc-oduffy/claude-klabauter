"""Derived per-op contract: one record per op, read from the registration quad.

`build_contract(op)` reads OP_MODULE_MAP, authz classify(), the default timeout, the
handler/module docstring and ipc._PARAMS_SCHEMA. Descriptive only: nothing here
feeds dispatch.

Traps: an unclassified op raises KeyError (never defaulted); the handler is read
from `_REGISTRY` directly, not `get_op_handler`, so a suspended op's contract is
byte-identical to its live one. `timeout_s` is the unclamped default, never
`COORDINATOR_DISPATCH_TIMEOUT_SECS`, so contract bytes are env-independent.
"""
from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from coordinator_core.authz.classification import OpClass, classify
from coordinator_core.ops._registry_map import OP_MODULE_MAP


@dataclass(frozen=True)
class OpContract:
    name: str
    summary: str
    op_class: OpClass
    timeout_s: float
    module: str
    params_schema: Dict[str, Any]
    description: str

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "summary": self.summary,
            "op_class": self.op_class.value,
            "timeout_s": self.timeout_s,
            "module": self.module,
            "params_schema": self.params_schema,
            "description": self.description,
        }
        return {k: d[k] for k in sorted(d)}


def describe_doc(op: str, handler: Callable) -> str:
    """Handler docstring, with the owning module docstring appended when the
    handler's own is missing, a stub, or a single line."""
    doc = inspect.getdoc(handler) or inspect.getdoc(inspect.unwrap(handler))
    if doc is None or "module docstring" in doc or "\n" not in doc.strip():
        mod = sys.modules.get(getattr(inspect.unwrap(handler), "__module__", ""))
        mod_doc = inspect.getdoc(mod) if mod is not None else None
        if mod_doc:
            doc = f"{doc}\n\n{mod_doc}" if doc else mod_doc
    return doc or f"{op}: handler has no docstring"


def _summary(description: str) -> str:
    for line in description.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _default_timeout(op: str) -> float:
    """`_timeout_for`'s branch on the unclamped defaults: never reads
    COORDINATOR_DISPATCH_TIMEOUT_SECS."""
    from coordinator_core import ipc

    if ipc.is_ceremony_method(op):
        return float(ipc.DISPATCH_TIMEOUT_SECS)
    return float(ipc._OP_TIMEOUT_OVERRIDES.get(op, ipc.DISPATCH_TIMEOUT_SECS))


def build_contract(op: str) -> OpContract:
    """Return the OpContract for *op*.

    Raises KeyError for an op absent from OP_MODULE_MAP or OP_CLASSIFICATION,
    and LookupError when the owning module does not register it.
    """
    from coordinator_core import ipc

    module = OP_MODULE_MAP[op]
    op_class = classify(op)
    importlib.import_module(module)
    handler: Optional[Callable] = ipc._REGISTRY.get(op)
    if handler is None:
        raise LookupError(f"op {op!r}: module {module!r} did not register it")
    description = describe_doc(op, handler)
    schema = ipc._PARAMS_SCHEMA.get(op)
    return OpContract(
        name=op,
        summary=_summary(description),
        op_class=op_class,
        timeout_s=_default_timeout(op),
        module=module,
        params_schema=schema if schema is not None else {"type": "object"},
        description=description,
    )
