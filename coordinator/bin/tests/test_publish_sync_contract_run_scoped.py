"""coordinator/bin/tests/test_publish_sync_contract_run_scoped.py — regression
test for the AC15 gate's blast radius.

`check_publish_sync_contract` runs once, pre-loop, over the publish-mode table.
AC7 keyed its loop on "has an entry point" rather than `is_mirror_like` so that
`repo-cut` could not go permanently unchecked. Correct as far as it went, but
the table-wide form refused the WHOLE run over an entry point the run's rows
would never call: when claude-klabauter landed `sync_repo_cut`, coordinator-claude's percolate
root — which carries its own pre-fourth-mode `setup/publish_sync.py` override
(§ `_resolve_publish_sync_module_path`) and whose five rows are all
mirror/flat-mirror — lost every one of those rows to a mode it does not use.
Reported in cross-repo/inbox/2026-08-10-coordinator-claude-em-config-class-landed-
fleet-reach-is-your-publish.md; filed as state/bug-backlog/2026-08-10-a-
percolate-root-holding-a-stale-publish-4c2e91a7d33b.yaml.

The fix scopes the gate to the modes the run's rows actually dispatch. The
obligation this file pins is two-sided, and the second side is the one that
matters: narrowing a fail-closed gate must not blunt it. So the stale-module
fixture here is checked BOTH ways — passing for a run that cannot reach the
missing entry point, still refusing for one that can, and still refusing under
the conservative `modes_in_run=None` fallback.

Per state/audits/2026-08-03-percolate-table-shaped-test-blind-spot.md, these
drive the REAL `check_publish_sync_contract` against a module built from the
REAL `publish_sync` entry points, never a table-vs-table comparison.

Run: python -m pytest coordinator/bin/tests/test_publish_sync_contract_run_scoped.py -q
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parents[1]
_COORDINATOR_LIB = _BIN_DIR.parent / "lib"
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate import publish_sync  # noqa: E402


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_sync_contract_run_scoped_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_STALE_MODULE_PATH = Path("setup/publish_sync.py")


@pytest.fixture
def stale_override():
    """A percolate root's own `publish_sync.py` from before `repo-cut` landed:
    real `sync_mirror`/`sync_flat_mirror`/`load_ignore`, no `sync_repo_cut`.
    Entry points are the REAL ones so the four-part bind check runs for real on
    the modes that ARE in scope — a stub would pass the presence check and then
    tell us nothing about the binding."""
    module = types.ModuleType("stale_publish_sync")
    module.sync_mirror = publish_sync.sync_mirror
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    module.load_ignore = publish_sync.load_ignore
    return module


def _check(module, modes):
    publish.check_publish_sync_contract(
        module, _STALE_MODULE_PATH, "native", modes_in_run=modes
    )


def test_stale_override_no_longer_refuses_a_run_it_cannot_affect(stale_override):
    """coordinator-claude's actual run: every row mirror/flat-mirror, both entry points present
    and binding. The absent `sync_repo_cut` is unreachable from these rows, so
    the gate must not veto them."""
    _check(stale_override, frozenset({"mirror", "flat-mirror"}))


def test_stale_override_still_refuses_a_run_that_reaches_the_missing_entry_point(
    stale_override,
):
    """The half that must NOT regress: once a `repo-cut` row is in the run, the
    same stale module is refused exactly as before — AC7's intent intact."""
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(stale_override, frozenset({"mirror", "repo-cut"}))
    message = excinfo.value.message
    assert "sync_repo_cut" in message
    assert "native" in message
    assert str(_STALE_MODULE_PATH) in message


def test_unscoped_call_still_checks_the_whole_table(stale_override):
    """`modes_in_run=None` is the conservative fallback taken when the run's
    rows cannot be parsed into known modes — it must keep checking everything,
    since that is exactly when we do not know what will be dispatched."""
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(stale_override, None)
    assert "sync_repo_cut" in excinfo.value.message


def test_scoping_does_not_excuse_a_mode_that_is_in_the_run():
    """Narrowing must not become a way to skip a check for a mode present in the
    run: a module missing `sync_mirror` is refused by a mirror run even though
    `modes_in_run` is at its narrowest."""
    module = types.ModuleType("missing_sync_mirror")
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    module.load_ignore = publish_sync.load_ignore
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(module, frozenset({"mirror"}))
    assert "sync_mirror" in excinfo.value.message


def test_load_ignore_is_checked_regardless_of_which_modes_are_in_the_run():
    """`load_ignore` is called by `dispatch_mirror_like` itself, not by any one
    mode's entry point, so mode-scoping must leave its check untouched."""
    module = types.ModuleType("missing_load_ignore")
    module.sync_mirror = publish_sync.sync_mirror
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(module, frozenset({"mirror", "flat-mirror"}))
    assert "load_ignore" in excinfo.value.message


def test_zero_pipe_row_collapses_modes_in_run_to_none():
    """Review: coordinator:code-reviewer P2 — a row with no `|` at all must
    force the conservative check-the-whole-table fallback (`None`), not be
    silently dropped from the set-builder while sibling rows narrow the
    scope regardless."""
    rows = ["mirror-target|mirror|src|dst", "unparseable-row-no-pipe"]
    assert publish._modes_in_run_from_rows(rows) is None


def test_rows_that_all_parse_still_narrow_the_scope():
    """Sanity check for the sibling behaviour the fix must not disturb: when
    every row parses and every mode is known, the scope still narrows."""
    rows = ["a|mirror|src|dst", "b|flat-mirror|src|dst"]
    assert publish._modes_in_run_from_rows(rows) == frozenset({"mirror", "flat-mirror"})


def test_a_complete_module_passes_every_scoping():
    """The real engine module satisfies the gate under every scoping, including
    the repo-cut run and the unscoped fallback."""
    for modes in (
        frozenset({"mirror", "flat-mirror"}),
        frozenset({"mirror", "flat-mirror", "repo-cut"}),
        frozenset({"manifest"}),
        None,
    ):
        _check(publish_sync, modes)
