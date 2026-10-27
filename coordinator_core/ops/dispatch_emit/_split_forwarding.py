"""Transitional patch-forwarding shim for the dispatch_emit hotspot split.

install() makes attribute writes on an original module also reach the modules
now holding the moved code, so monkeypatch.setattr / mock.patch on the old
module keep working while importers are re-pointed. Deleted with the split.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Mapping, Sequence

_ORIGINAL_CLASS = "_split_forwarding_original_class"
_FORWARD_MAP = "_split_forwarding_map"


def _targets(module: types.ModuleType, name: str) -> list[types.ModuleType]:
    fmap = module.__dict__.get(_FORWARD_MAP, {})
    out = []
    for target_name in fmap.get(name, ()):
        target = sys.modules.get(target_name) or importlib.import_module(target_name)
        if target is not module:
            out.append(target)
    return out


class _ForwardingModule(types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        for target in _targets(self, name):
            if hasattr(target, name):
                setattr(target, name, value)

    def __delattr__(self, name: str) -> None:
        super().__delattr__(name)
        for target in _targets(self, name):
            if hasattr(target, name):
                delattr(target, name)


def install(module_name: str, forward_map: Mapping[str, Sequence[str]]) -> None:
    """Forward writes to `module_name.<name>` to every module in forward_map[name].

    forward_map must list every name a moved def reads from module globals
    (moved defs and imported helpers alike). Re-installing replaces the map.
    """
    module = sys.modules.get(module_name) or importlib.import_module(module_name)
    module.__dict__[_FORWARD_MAP] = {k: tuple(v) for k, v in forward_map.items()}
    if not isinstance(module, _ForwardingModule):
        module.__dict__[_ORIGINAL_CLASS] = type(module)
        module.__class__ = _ForwardingModule


def uninstall(module_name: str) -> None:
    """Restore the module's original class and drop the forward map."""
    module = sys.modules.get(module_name)
    if module is None or not isinstance(module, _ForwardingModule):
        return
    module.__class__ = module.__dict__.pop(_ORIGINAL_CLASS, types.ModuleType)
    module.__dict__.pop(_FORWARD_MAP, None)
