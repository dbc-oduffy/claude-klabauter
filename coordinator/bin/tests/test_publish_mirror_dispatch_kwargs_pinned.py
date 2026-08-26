"""coordinator/bin/tests/test_publish_mirror_dispatch_kwargs_pinned.py —
pins `dispatch_mirror_like`'s real call-site kwargs for the `mirror` mode
against `percolate.publish_sync.sync_mirror`'s ACTUAL signature, derived by
introspection (never a hand-copied literal list) — the same pattern
`check_publish_sync_contract`/`test_publish_modes.py` already use to drive
real entry points instead of comparing one table to another (state/audits/
2026-08-03-percolate-table-shaped-test-blind-spot.md).

Incident this closes the general class of (not the specific field break):
a percolate-root override's own `publish_sync.py` (e.g. DoE-claude's
`setup/publish_sync.py`) can drift out of contract with OUR `sync_mirror`
signature the moment we add a keyword argument here, and that only
surfaced in the field as a FATAL at mirror dispatch:

    FATAL: .../setup/publish_sync.py (resolved via rung 'native') defines
    'sync_mirror' but its signature does not accept the mirror-dispatch
    keyword arguments [...]: got an unexpected keyword argument
    'sweep_top_level_orphans'.

That fail-closed at dispatch time is correct behaviour and not the bug.
The bug is that nothing on OUR side flags a kwarg addition until a real
publish round runs against a stale override. `check_publish_sync_contract`
validates an ARBITRARY module's `sync_mirror` against the mirror
descriptor's declared `bind_kwargs` (`percolate/publish_modes.py`) — but
`bind_kwargs` is itself a hand-maintained literal dict, and
`bind_partial` only checks that a SUBSET binds; it does not notice a
kwarg present on the real signature but absent from `bind_kwargs`. This
file is the missing pin: it derives the expected optional-keyword set from
`sync_mirror`'s real signature via `inspect`, and fails loudly, naming the
added/removed kwarg, if `dispatch_mirror_like`'s actual call-site set
(observed by capturing a real dispatch call, not read from source text)
drifts away from it.

`changed_paths` is deliberately excluded from the pinned set: it is not
part of the `mirror_kwargs`/`bind_kwargs` contract at all — it is passed
by `dispatch_mirror_like` as a single always-literally-spelled keyword
(`changed_paths=changed_sink`) on a structurally different, presence-gated
code path (§ `dispatch_mirror_like`'s own docstring on `changed_sink`),
not a name that must be threaded through a descriptor's `bind_kwargs`/
`accepts_*` machinery the way `renamed_dir_names` and
`sweep_top_level_orphans`/`renamed_file_names` are.

WHAT THIS TEST WOULD NOT HAVE CAUGHT: the field break was in DoE-claude's
`setup/publish_sync.py`, a sibling repo's file this repo cannot import or
assert against (§ dispatch brief). No test living here can pin a file that
does not exist in this tree. What this test pins is OUR side of that
contract — the set `dispatch_mirror_like` actually passes for the
`mirror` mode — so that a future kwarg addition here is caught at test
time, in this repo, before it ships as a new unpinned requirement for
every consumer's override to discover for itself in production.

Run: python -m pytest coordinator/bin/tests/test_publish_mirror_dispatch_kwargs_pinned.py -q
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parents[1]
_COORDINATOR_LIB = _BIN_DIR.parent / "lib"
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_modes  # noqa: E402
from percolate import publish_sync  # noqa: E402


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_mirror_dispatch_kwargs_pinned_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

# The one name deliberately excluded from the pin -- see module docstring.
_STRUCTURALLY_SEPARATE_KWARGS = frozenset({"changed_paths"})


def _expected_mirror_kwargs_from_real_signature() -> frozenset[str]:
    """Introspection-derived expected set, never a hand-copied literal:
    every keyword-only parameter `percolate.publish_sync.sync_mirror`
    actually accepts, minus the structurally-separate `changed_paths`
    leg."""
    sig = inspect.signature(publish_sync.sync_mirror)
    keyword_only = {
        p.name
        for p in sig.parameters.values()
        if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    return frozenset(keyword_only - _STRUCTURALLY_SEPARATE_KWARGS)


def test_mirror_bind_kwargs_table_matches_the_real_sync_mirror_signature():
    """`percolate/publish_modes.py`'s `_MIRROR_DESCRIPTOR.bind_kwargs` is the
    exact set `check_publish_sync_contract` binds any loaded module's
    `sync_mirror` against. `bind_partial` only proves a SUBSET binds --
    it stays green even if the real signature grows a kwarg `bind_kwargs`
    never learned about, which is exactly how a consumer override can pass
    its own bind check while still being missing a kwarg nobody pinned.
    This assertion is the other half: the declared set must equal the real
    one, not merely bind against it."""
    descriptor = publish_modes.descriptor_for("mirror")
    assert descriptor is not None
    declared = frozenset(descriptor.bind_kwargs)
    expected = _expected_mirror_kwargs_from_real_signature()
    missing_from_table = expected - declared
    extra_in_table = declared - expected
    assert not missing_from_table and not extra_in_table, (
        "percolate/publish_modes.py's mirror bind_kwargs has drifted from "
        "percolate/publish_sync.py::sync_mirror's real signature -- "
        f"missing from bind_kwargs: {sorted(missing_from_table) or 'none'}; "
        f"stale/extra in bind_kwargs: {sorted(extra_in_table) or 'none'}. "
        "Update _MIRROR_DESCRIPTOR.bind_kwargs (and dispatch_mirror_like's "
        "own kwarg-building branches in publish.py) to match, or every "
        "consumer override's check_publish_sync_contract pass becomes a "
        "false green."
    )


def test_dispatch_mirror_like_call_site_passes_exactly_the_pinned_set(tmp_path):
    """Drives the REAL `dispatch_mirror_like` call path (not a table read):
    captures the actual kwargs a `mirror`-mode dispatch passes to
    `sync_mirror`, with every `accepts_*` flag armed so the call site's
    full potential kwarg set is exercised, and pins it against the
    introspection-derived expected set."""
    captured: dict[str, object] = {}

    def _capturing_sync_mirror(src_dir, dst_dir, ignore, dry_run, **kwargs):
        captured.update(kwargs)
        return (0, 0)

    stub_module = types.ModuleType("capturing_publish_sync")
    stub_module.sync_mirror = _capturing_sync_mirror
    stub_module.load_ignore = publish_sync.load_ignore

    src_dir = tmp_path / "src"
    dst_dir = tmp_path / "dst"
    src_dir.mkdir()
    dst_dir.mkdir()

    target = publish.ResolvedTarget(
        name="pin-probe",
        mode="mirror",
        source_dir=src_dir,
        dest_dir=dst_dir,
    )
    totals = publish.RunTotals()

    publish.dispatch_mirror_like(
        stub_module,
        target,
        src_dir,
        totals,
        dry_run=True,
        renamed_dir_names=frozenset({"renamed"}),
        sweep_top_level_orphans=True,
        renamed_file_names=frozenset({"renamed.txt"}),
    )

    actual = frozenset(captured) - _STRUCTURALLY_SEPARATE_KWARGS
    expected = _expected_mirror_kwargs_from_real_signature()
    added = actual - expected
    dropped = expected - actual
    assert not added and not dropped, (
        "dispatch_mirror_like's real mirror-mode call site has drifted from "
        f"sync_mirror's real signature -- passed but not on the real "
        f"signature: {sorted(added) or 'none'}; on the real signature but "
        f"never passed: {sorted(dropped) or 'none'}. A consumer override's "
        "sync_mirror must accept exactly this set."
    )
