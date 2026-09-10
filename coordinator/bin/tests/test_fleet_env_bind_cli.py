"""tests/test_fleet_env_bind_cli.py — the sibling-facing binding CLI's contract.

WHY THIS CLI EXISTS, since the test is the durable record of it.
`docs/reference/fleet-shared-environment-contract.md` § The sibling `.pth`
binding contract instructs a sibling-repo maintainer to call
`coordinator_core.install.fleet_env.register_sibling_binding(...)`. That
instruction was unreachable: a sibling repo cannot import `coordinator_core`
(the tri-plane boundary puts the engine in claude-klabauter's plane), so every consumer
the contract addressed was told to make a call it had no way to make.
`coordinator/bin/fleet-env-bind.py` is the reachable surface, exactly as
`fleet-env.py get` already is for the other half of the same contract.

ZERO SPAWNS, deliberately. `main()` is driven in-process rather than through
`subprocess.run`, so this file costs no process creation — CLAUDE.md's
brightline makes process count a measured axis, and a CLI test that spawns per
case is how a suite acquires them by the dozen. Driving `main()` also tests the
exit-code contract at the only place it is actually defined.

STATE ISOLATION. Every case points `COORDINATOR_SETTINGS_HOME` at a `tmp_path`,
so the binding registry under test is a throwaway file and never the developer's
real machine-local registry. A test that writes a real binding onto the box it
runs on is a test that changes the machine to observe it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "fleet-env-bind.py"

_USAGE_FAIL = 2
_UNRESOLVABLE = 3
_FLAGGED = 4


@pytest.fixture()
def cli():
    """The script loaded as a module — its filename carries a hyphen, so it is
    not importable by name and `spec_from_file_location` is the only route."""
    spec = importlib.util.spec_from_file_location("_fleet_env_bind_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def settings_home(monkeypatch, tmp_path):
    """A throwaway settings home, so the binding registry this file writes is
    never the real one."""
    home = tmp_path / "settings-home"
    (home / "machine-local").mkdir(parents=True)
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


def _registry(settings_home: Path) -> list:
    path = settings_home / "machine-local" / "fleet-env-bindings.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["bindings"]


# ---------------------------------------------------------------------------
# register — the call the contract names, now reachable
# ---------------------------------------------------------------------------


def test_register_persists_a_binding(cli, settings_home, tmp_path):
    sibling_root = tmp_path / "example-retrieval-repo"
    sibling_root.mkdir()

    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(sibling_root)]) == 0
    assert _registry(settings_home) == [
        {"repo": "market_intel", "sibling": "example_retrieval_repo", "path": str(sibling_root)}
    ]


def test_register_succeeds_before_the_environment_exists(cli, settings_home, tmp_path):
    """THE PROPERTY THAT MAKES INSTALL ORDER INDEPENDENT of the fleet's
    provisioning order, and the reason a consumer may call this unconditionally.
    No environment is provisioned anywhere in this test; the entry still lands,
    and `_replay_sibling_bindings` writes its `.pth` on the next rebuild."""
    sibling_root = tmp_path / "example-retrieval-repo"
    sibling_root.mkdir()

    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(sibling_root)]) == 0
    assert len(_registry(settings_home)) == 1


def test_register_refuses_a_relative_path(cli, settings_home):
    """A relative literal in a `.pth` resolves against site-packages, not the
    caller's repo root, and silently fails to import — example-market-data-repo's
    own documented failure mode, which is why the engine raises rather than
    writes. The CLI must surface that as exit 1, not a traceback."""
    assert cli.main(["register", "market_intel", "example_retrieval_repo", "../example-retrieval-repo"]) == 1
    assert _registry(settings_home) == []


def test_register_replaces_rather_than_duplicates(cli, settings_home, tmp_path):
    """Re-running an installer must not accumulate entries for the same pair."""
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()

    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(first)]) == 0
    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(second)]) == 0

    bindings = _registry(settings_home)
    assert len(bindings) == 1
    assert bindings[0]["path"] == str(second)


# ---------------------------------------------------------------------------
# deregister
# ---------------------------------------------------------------------------


def test_deregister_removes_the_entry(cli, settings_home, tmp_path):
    sibling_root = tmp_path / "example-retrieval-repo"
    sibling_root.mkdir()
    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(sibling_root)]) == 0

    assert cli.main(["deregister", "market_intel", "example_retrieval_repo"]) == 0
    assert _registry(settings_home) == []


def test_deregister_is_idempotent(cli, settings_home):
    """An uninstall path must not fail because it already ran."""
    assert cli.main(["deregister", "market_intel", "example_retrieval_repo"]) == 0


# ---------------------------------------------------------------------------
# check — where "cannot look" and "looked and found problems" must not merge
# ---------------------------------------------------------------------------


def test_check_reports_unprovisioned_as_3_not_4(cli, settings_home, tmp_path):
    """THE CASE THIS SCRIPT EXISTS TO GET RIGHT. On an unprovisioned machine
    `resolve_environment_root` does NOT raise — C5's ladder degrades to
    `<settings-home>/.fleet-env` — so `check_sibling_bindings` accurately
    reports every registered binding as `missing_pth`. Returning 4 there would
    fire on every machine mid-rollout and train callers to ignore the code.
    3 means "nothing to check against"; 4 must stay "really broken"."""
    sibling_root = tmp_path / "example-retrieval-repo"
    sibling_root.mkdir()
    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(sibling_root)]) == 0

    assert cli.main(["check"]) == _UNRESOLVABLE


def test_check_flags_a_stale_path_once_the_environment_is_provisioned(
    cli, settings_home, tmp_path
):
    """With a provisioned tree present, a registered path that no longer
    exists is a real defect and must reach exit 4 — otherwise the 3-vs-4 split
    above would have bought correctness for the rollout case by making the
    broken case unreportable."""
    _bootstrap = cli._bootstrap_engine
    _bootstrap()
    from coordinator_core.install.fleet_env import _site_packages_dir, resolve_environment_root

    env_root = resolve_environment_root()
    _site_packages_dir(env_root).mkdir(parents=True, exist_ok=True)

    gone = tmp_path / "deleted-sibling"
    gone.mkdir()
    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(gone)]) == 0
    gone.rmdir()

    assert cli.main(["check"]) == _FLAGGED


def test_check_is_clean_when_every_binding_resolves(cli, settings_home, tmp_path):
    cli._bootstrap_engine()
    from coordinator_core.install.fleet_env import _site_packages_dir, resolve_environment_root

    env_root = resolve_environment_root()
    _site_packages_dir(env_root).mkdir(parents=True, exist_ok=True)

    sibling_root = tmp_path / "example-retrieval-repo"
    sibling_root.mkdir()
    assert cli.main(["register", "market_intel", "example_retrieval_repo", str(sibling_root)]) == 0

    assert cli.main(["check"]) == 0


# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------


def test_no_subcommand_is_a_usage_error(cli):
    assert cli.main([]) == _USAGE_FAIL


def test_unknown_subcommand_is_a_usage_error(cli):
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["frobnicate"])
    assert exc_info.value.code == _USAGE_FAIL
