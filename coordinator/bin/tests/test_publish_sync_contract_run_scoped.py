
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
    _check(stale_override, frozenset({"mirror", "flat-mirror"}))


def test_stale_override_still_refuses_a_run_that_reaches_the_missing_entry_point(
    stale_override,
):
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(stale_override, frozenset({"mirror", "repo-cut"}))
    message = excinfo.value.message
    assert "sync_repo_cut" in message
    assert "native" in message
    assert str(_STALE_MODULE_PATH) in message


def test_unscoped_call_still_checks_the_whole_table(stale_override):
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(stale_override, None)
    assert "sync_repo_cut" in excinfo.value.message


def test_scoping_does_not_excuse_a_mode_that_is_in_the_run():
    module = types.ModuleType("missing_sync_mirror")
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    module.load_ignore = publish_sync.load_ignore
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(module, frozenset({"mirror"}))
    assert "sync_mirror" in excinfo.value.message


def test_load_ignore_is_checked_regardless_of_which_modes_are_in_the_run():
    module = types.ModuleType("missing_load_ignore")
    module.sync_mirror = publish_sync.sync_mirror
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(module, frozenset({"mirror", "flat-mirror"}))
    assert "load_ignore" in excinfo.value.message


def test_zero_pipe_row_collapses_modes_in_run_to_none():
    rows = ["mirror-target|mirror|src|dst", "unparseable-row-no-pipe"]
    assert publish._modes_in_run_from_rows(rows) is None


def test_rows_that_all_parse_still_narrow_the_scope():
    rows = ["a|mirror|src|dst", "b|flat-mirror|src|dst"]
    assert publish._modes_in_run_from_rows(rows) == frozenset({"mirror", "flat-mirror"})


def test_a_complete_module_passes_every_scoping():
    for modes in (
        frozenset({"mirror", "flat-mirror"}),
        frozenset({"mirror", "flat-mirror", "repo-cut"}),
        frozenset({"manifest"}),
        None,
    ):
        _check(publish_sync, modes)


def test_refusal_names_the_engine_module_to_diff_against(stale_override):
    """A refusal against an OVERRIDE must name the engine module the reader has
    to diff, not just the offending path. doe-claude-em, 2026-08-26: an AC15
    refusal over `sweep_top_level_orphans` gave them the kwarg and the rung --
    enough for a five-minute diagnosis -- but not the reference path, which
    they then had to find by hand before they could port the missing bodies."""
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        _check(stale_override, frozenset({"mirror", "repo-cut"}))
    assert str(publish._ENGINE_PUBLISH_SYNC_PATH) in excinfo.value.message


def test_refusal_against_the_engine_module_does_not_name_it_twice():
    module = types.ModuleType("engine_missing_sync_mirror")
    module.sync_flat_mirror = publish_sync.sync_flat_mirror
    module.load_ignore = publish_sync.load_ignore
    with pytest.raises(publish.PublishSyncContractError) as excinfo:
        publish.check_publish_sync_contract(
            module,
            publish._ENGINE_PUBLISH_SYNC_PATH,
            "native",
            modes_in_run=frozenset({"mirror"}),
        )
    assert "Expected (engine)" not in excinfo.value.message
