
from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import types

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BIN = _REPO_ROOT / "coordinator" / "bin"


def load_bin_module(script_name: str) -> types.ModuleType:
    path = _BIN / script_name
    if not path.is_file():
        raise FileNotFoundError(f"sibling CLI not found: {path}")
    mod_name = f"_oracle_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not build an import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def actions_by_dest(parser: argparse.ArgumentParser) -> dict[str, argparse.Action]:
    return {action.dest: action for action in parser._actions}


def subparser_of(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if name in action.choices:
                return action.choices[name]
    raise AssertionError(f"no subcommand {name!r} on this parser")


def accepts_multiple(action: argparse.Action) -> bool:
    """True when `action` can absorb more than one value in ONE invocation -- `nargs` of `+`,
    `*`, `REMAINDER`, or an integer above 1, or an append-style action.

    This is the single question every sibling-CLI oracle asks. An exemption saying "this CLI
    takes one record per call" is exactly the claim that this returns `False`."""
    if isinstance(action, (argparse._AppendAction, argparse._AppendConstAction)):
        return True
    nargs = action.nargs
    if nargs is None:
        return False
    if isinstance(nargs, int):
        return nargs > 1
    return nargs in {"+", "*", argparse.REMAINDER}
