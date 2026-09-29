"""test_publish_percolate_root_cli_override.py -- BV-20260927-05 fix 3.

`publish.py` used to resolve PERCOLATE_ROOT exclusively via
`coordinator_percolate_runtime_root()` (keyed off `~/.claude/.coordinator-content-root`),
ignoring a caller's own already-resolved root. `percolate-mirror.py` never
forwarded its own `--percolate-root` to the child `publish.py` subprocess,
so the child silently re-resolved a DIFFERENT root on a box whose
`.coordinator-content-root` pointer names a different tree (the live defect: a cloud
PERCOLATE_ROOT override never reached `publish.py`, which loaded
Coordinator-content-repo's `publish-targets.portable` instead of the caller's).

This covers, in-process, no subprocess spawn:
  1. `build_arg_parser()` accepts `--percolate-root`.
  2. `_resolve_percolate_root_and_rung(override=...)` short-circuits on the
     override BEFORE the native resolver rung runs at all.

Run: python -m pytest coordinator/bin/tests/test_publish_percolate_root_cli_override.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_percolate_root_override_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def test_percolate_root_flag_is_accepted():
    args = publish.build_arg_parser().parse_args(["mytarget", "--percolate-root", "/tmp/foo"])
    assert args.percolate_root == "/tmp/foo"


def test_percolate_root_flag_defaults_to_none():
    args = publish.build_arg_parser().parse_args(["mytarget"])
    assert args.percolate_root is None


def test_override_wins_before_the_native_resolver_runs(monkeypatch):
    """The override must short-circuit ahead of `_locate_cc_invoke` --
    asserting that helper is never called is the load-bearing check: a fix
    that merely preferred the override's VALUE but still ran the native
    resolver first would still cost the resolver's own side effects (and,
    on the live defect's box, its own wrong answer racing the override)."""
    called = []
    monkeypatch.setattr(publish, "_locate_cc_invoke", lambda: called.append(True))

    root, rung = publish._resolve_percolate_root_and_rung(override="/tmp/bar")

    assert root == Path("/tmp/bar")
    assert rung == "cli-override"
    assert called == []


def test_no_override_still_falls_through_to_native_resolver(monkeypatch):
    monkeypatch.setattr(publish, "_locate_cc_invoke", lambda: None)
    root, rung = publish._resolve_percolate_root_and_rung(override=None)
    assert rung != "cli-override"
