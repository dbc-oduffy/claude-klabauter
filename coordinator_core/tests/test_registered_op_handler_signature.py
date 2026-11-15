"""Every registered op handler accepts dispatch's (params, repo_root=...) call.

Trap: `@register_op` binds to whatever `def` follows it. Inserting a helper between
the decorator and the handler silently registers the helper; the op then raises
TypeError on every call while every unit test of the handler still passes.
"""

from __future__ import annotations

import inspect

from coordinator_core.ipc import _REGISTRY
from coordinator_core.tests.test_dispatch_message import _import_all_ops_tree_modules


def _accepts_dispatch_call(fn) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return True  # uninspectable builtin/partial: nothing to assert
    try:
        sig.bind({}, repo_root=None)
    except TypeError:
        return False
    return True


def test_every_registered_op_handler_takes_params_and_repo_root():
    _import_all_ops_tree_modules()
    bad = sorted(f"{name} -> {getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', fn)}"
                 for name, fn in _REGISTRY.items() if not _accepts_dispatch_call(fn))
    assert not bad, "op handler cannot take (params, repo_root=): " + "; ".join(bad)


def test_a_helper_wedged_under_the_decorator_is_caught():
    def _old_handoff_ids(rels, worktree):
        return []

    assert not _accepts_dispatch_call(_old_handoff_ids)
