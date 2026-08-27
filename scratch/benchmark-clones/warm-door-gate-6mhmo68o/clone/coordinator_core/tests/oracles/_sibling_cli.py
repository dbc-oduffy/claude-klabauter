"""Load a sibling CLI from `coordinator/bin/` and introspect its argparse surface.

Import-and-introspect, never spawn. Two reasons, both this repo's own rules: a new shell-out
needs a named carve-out in `docs/reference/shell-out-carve-outs.md`, and a probe that spawns N
sibling CLIs to prove they cannot be batched would itself be the amplification the gate polices.
Reading the parser is also strictly more precise than reading `--help` text.
"""

from __future__ import annotations

import argparse
import importlib.util
import pathlib
import sys
import types

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_BIN = _REPO_ROOT / "coordinator" / "bin"


def load_bin_module(script_name: str) -> types.ModuleType:
    """Import `coordinator/bin/<script_name>` under a private module name.

    The bin scripts carry hyphens, so they are not importable as packages; a path-based spec is
    the only route. Loaded under a `_oracle_` prefix so a script that shares a stem with a real
    module cannot shadow it in `sys.modules`."""
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
    """Every action on `parser`, keyed by `dest` -- subparser actions included but not
    descended into. Use `subparser_of` for a named subcommand."""
    return {action.dest: action for action in parser._actions}


def subparser_of(parser: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    """The sub-parser registered under `name`.

    Raises rather than returning `None`: an oracle whose subcommand has been renamed must FAIL,
    not silently pass over a parser it never found. A silently-skipped oracle is precisely the
    prose exemption this layer replaces."""
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
