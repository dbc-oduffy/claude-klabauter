"""
bin.tests.test_claude_klabauter_doctor_eol_rider_absent_producer — producer/consumer contract
between the two eol doctor probes and the op that used to produce the sentinels they
read.

`claude-klabauter.eol.census` and `claude-klabauter.eol.audit_producers` each read a per-machine sentinel
written by a cadence rider that was a leg of the `session.boot_sweep` composite. That
module was DELETED at 2e7eff5c1; the op id survives, repointed at
`coordinator_core.ops.session.boot_backstop`, which deliberately carries none of the
composite's cadence riders. So the sentinel has NO writer on any machine, and the two
readings the probes used to offer for an absent sentinel — "the rider has not run yet,
boot a session" and "its host op is suspended, wait for reinstatement" — are both false
causes: neither a boot nor a reinstatement produces a sentinel.

The defect this file pins is narrower and sharper than the wording. Both probes reached
their host-suspension arm through a try-block importing the deleted module's cadence
constant, whose `except` detail named only the FIRST import in the block
(`coordinator_core.lifecycle.git_common_dir`, which imports fine). Every run therefore
reported "the engine tree is not importable" — pointing at `claude-klabauter.core.import`, which
PASSES in the same run — for a failure that was actually "this module was deleted."

Spec backlink: docs/plans/2026-08-22-the-boot-backstop-asks-git-nothing.md § C3, which
routes both riders to a ceremony gate with their windows preserved (1h/24h).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Optional

import pytest

_REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
_BIN_PROBE = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"
_DELETED_COMPOSITE = _REPO_ROOT / "coordinator_core" / "ops" / "session" / "boot_sweep.py"
_SUCCESSOR = _REPO_ROOT / "coordinator_core" / "ops" / "session" / "boot_backstop.py"


def _load_probe_module() -> Optional[ModuleType]:
    """Import bin/claude-klabauter-doctor-probe.py as a fresh module via importlib.

    Registered in sys.modules before exec so dataclass annotation resolution
    (sys.modules[cls.__module__]) finds a valid namespace on Python 3.14+.
    """
    if not _BIN_PROBE.exists():
        return None
    key = "claude_klabauter_doctor_probe_eol_rider_absent_producer_unit"
    spec = importlib.util.spec_from_file_location(key, _BIN_PROBE)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        sys.modules.pop(key, None)
        return None
    return mod


def _require_module() -> ModuleType:
    mod = _load_probe_module()
    if mod is None:
        pytest.skip("bin/claude-klabauter-doctor-probe.py not on disk or not importable")
    return mod  # type: ignore[return-value]


@pytest.fixture
def empty_common_dir(tmp_path, monkeypatch):
    """Point the probes' `git_common_dir` at an empty directory — no sentinel."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    import coordinator_core.lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "git_common_dir", lambda *_a, **_k: tmp_path)
    return tmp_path


@pytest.mark.parametrize(
    "runner_name, probe_op",
    [
        ("_run_probe_eol_census", "eol.census"),
        ("_run_probe_eol_audit_producers", "eol.audit_producers"),
    ],
)
def test_sentinel_absent_names_the_deleted_producer(
    runner_name, probe_op, empty_common_dir
):
    mod = _require_module()

    result = getattr(mod, runner_name)(_REPO_ROOT)
    remediation = result.remediation or ""

    assert result.skipped is True
    assert mod._EOL_RIDER_PRODUCER_DELETED_AT in result.detail
    assert "nothing writes one" in result.detail
    # The two retired false causes, and the misattributed import cause that sent
    # operators to a probe which passes.
    assert "has not run yet" not in result.detail
    assert "not importable" not in result.detail
    assert "Boot a session" not in remediation
    assert "reinstated" not in remediation
    # The one route that does yield a current verdict, plus where the sentinel
    # comes back from.
    assert probe_op in remediation
    assert mod._EOL_RIDER_REBUILD_PLAN in remediation


@pytest.mark.parametrize(
    "runner_name", ["_run_probe_eol_census", "_run_probe_eol_audit_producers"]
)
def test_probe_does_not_import_the_deleted_composite(runner_name):
    """The probes must not reach for `ops.session.boot_sweep` again.

    Importing a deleted module inside a multi-import try-block is what produced the
    false cause; the cadence windows now live on the probe as module constants.
    """
    mod = _require_module()
    source = _BIN_PROBE.read_text(encoding="utf-8")

    assert "from coordinator_core.ops.session.boot_sweep import" not in source
    assert "import coordinator_core.ops.session.boot_sweep" not in source


def test_cadence_windows_are_local_and_usable_as_a_stale_threshold():
    """The staleness arm multiplies these; a missing or zero window silently
    disables it rather than failing loudly."""
    mod = _require_module()

    for name in (
        "_EOL_CENSUS_CADENCE_WINDOW_SECONDS",
        "_EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS",
    ):
        window = getattr(mod, name)
        assert isinstance(window, (int, float))
        assert window > 0

    # The values the deleted composite carried at 2e7eff5c1.
    assert mod._EOL_CENSUS_CADENCE_WINDOW_SECONDS == 60 * 60.0
    assert mod._EOL_AUDIT_PRODUCERS_CADENCE_WINDOW_SECONDS == 24 * 60 * 60.0
    assert mod._EOL_STALE_WINDOW_MULTIPLE > 1


def test_no_producer_has_reappeared():
    """RETURNS-WHEN tripwire for the absent-producer wording.

    Two file reads, not a corpus walk: the composite's deletion and the successor's
    silence are the whole claim. If C3 lands and gives either rider a cadence host,
    this fails — repoint the probes' detail/remediation and their cadence-window
    constants at that host in the same change.
    """
    mod = _require_module()

    assert not _DELETED_COMPOSITE.exists()
    if _SUCCESSOR.exists():
        successor_source = _SUCCESSOR.read_text(encoding="utf-8")
        assert mod._EOL_CENSUS_SENTINEL_NAME not in successor_source
        assert mod._EOL_AUDIT_PRODUCERS_SENTINEL_NAME not in successor_source
