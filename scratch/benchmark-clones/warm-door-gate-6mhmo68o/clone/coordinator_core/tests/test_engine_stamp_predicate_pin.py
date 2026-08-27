"""coordinator_core.tests.test_engine_stamp_predicate_pin

Pin test: SIX independent copies of "is this root a stamped engine build"
(or its raw-bytes equivalent) now exist on this box, each duplicated
deliberately for its own named reason (the shared implementation is either
too expensive to import on that copy's hot path, or genuinely unreachable
from that copy's install-time/standalone context) --

  1. coordinator_core.warm.engine_root.is_engine_root -- the original,
     shared definition (built on coordinator_core.warm.skew.ENGINE_STAMP_FILENAME).
  2. coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::_is_stamped_engine_root
     -- standalone, installed bare into <settings-home>/bin/; cannot import
     coordinator_core at all.
  3. coordinator_core.ipc._is_dispatch_engine_stamped -- inline in the
     dispatch chokepoint (state/handoffs/2026-08-21_103635_reaching-the-
     warm-engine.md; import cost measured at +21ms, see that function's own
     docstring).
  4. bin/claude-klabauter-doctor-probe.py::_engine_root_is_stamped -- stdlib-only at
     module scope by design, grading a tree it may not be able to import.
  5. coordinator/bin/gen-launcher-shim.py::_engine_stamp_bytes -- hyphenated
     filename with no ordinary `import` form; read side of the C15 dispatch-
     root launcher cache.
  6. coordinator_core.install.substrate._dispatch_engine_stamp_bytes -- the
     write side of the same C15 cache; declines to import #5 (its actual
     sync partner) for the same hyphenated-filename reason, so it re-derives
     the tuple by hand instead of importing coordinator_core.warm.skew (which
     would leave it out of sync with #5, the module it must byte-for-byte
     agree with).

Six copies of a correctness predicate with nothing asserting they agree is
exactly the defect class this box hit twice already (publish.py's unscoped
stamp write; _ENGINE_TOUCHING_PATHS needing a pin -- see
coordinator/bin/tests/test_publish_engine_stamp.py::
test_publisher_and_skew_agree_on_engine_touching_paths, the pattern this
test follows). A silent divergence here does not fail loudly: it lets one
copy classify a root as stamped while another refuses it, invisibly, since
each stays green on its own tests. #5/#6's cache round-trip is separately
exercised by test_gen_launcher_shim_dispatch_bake.py, but that test does not
pin either against the canonical `coordinator_core.warm.skew` value -- this
test is the one place all six are compared side by side.

Negative-spec: does NOT assert the six *source files* share one literal
constant -- #2/#4/#5/#6 are constrained (by import-independence or measured
process-time cost) from importing `coordinator_core.warm.skew` directly, so
a single shared constant is not available to all six. This test instead
pins their *behavior* against a real stamp file, which is the property that
actually matters and the one a hand-edited constant could silently break.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

import coordinator_core.install.substrate as substrate
import coordinator_core.ipc as ipc
from coordinator_core.engine_root import _load_shim
from coordinator_core.warm.engine_root import is_engine_root

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOCTOR_PROBE_PATH = _REPO_ROOT / "bin" / "claude-klabauter-doctor-probe.py"
_GEN_LAUNCHER_SHIM_PATH = _REPO_ROOT / "coordinator" / "bin" / "gen-launcher-shim.py"

_doctor_probe_module: ModuleType | None = None
_gen_launcher_shim_module: ModuleType | None = None


def _load_doctor_probe() -> ModuleType:
    """Load `bin/claude-klabauter-doctor-probe.py` by path (never `import` -- the
    on-disk filename is not a valid module identifier), memoized module-scope."""
    global _doctor_probe_module
    if _doctor_probe_module is not None:
        return _doctor_probe_module
    spec = importlib.util.spec_from_file_location(
        "_claude_klabauter_doctor_probe_stamp_pin", _DOCTOR_PROBE_PATH
    )
    if spec is None or spec.loader is None:
        pytest.skip(f"could not load {_DOCTOR_PROBE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _doctor_probe_module = module
    return module


def _load_gen_launcher_shim() -> ModuleType:
    """Load `coordinator/bin/gen-launcher-shim.py` by path -- same hyphenated-
    filename constraint as `_load_doctor_probe` above, memoized module-scope."""
    global _gen_launcher_shim_module
    if _gen_launcher_shim_module is not None:
        return _gen_launcher_shim_module
    spec = importlib.util.spec_from_file_location(
        "_gen_launcher_shim_stamp_pin", _GEN_LAUNCHER_SHIM_PATH
    )
    if spec is None or spec.loader is None:
        pytest.skip(f"could not load {_GEN_LAUNCHER_SHIM_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    _gen_launcher_shim_module = module
    return module


def _all_six_verdicts(monkeypatch, root: Path) -> tuple:
    monkeypatch.setattr(ipc, "_DISPATCH_ENGINE_ROOT", root)
    ipc._reset_engine_stamped_verdict_for_test()

    resolve_claude_klabauter = _load_shim()
    doctor_probe = _load_doctor_probe()
    gen_launcher_shim = _load_gen_launcher_shim()

    return (
        is_engine_root(root),
        resolve_claude_klabauter._is_stamped_engine_root(root),
        ipc._is_dispatch_engine_stamped(),
        doctor_probe._engine_root_is_stamped(root),
        len(gen_launcher_shim._engine_stamp_bytes(root)) > 0,
        len(substrate._dispatch_engine_stamp_bytes(root)) > 0,
    )


#: Declaration for the register-aging sweep (C5,
#: `docs/plans/2026-08-26-every-register-either-derives-or-fails-on-its-dead-rows.md`):
#: every row of `_VERDICT_LABELS` names a symbol living inside a parent module, not a
#: whole module -- most rows as a dotted path (e.g. `warm.engine_root.is_engine_root`),
#: two (`claude-klabauter-doctor-probe.*`, `gen-launcher-shim.*`) as a hyphenated script filename
#: loaded via a bespoke loader rather than `import`. Both shapes still resolve because
#: resolution is suffix-path string matching, not import-legality validation.
_VERDICT_LABELS__SUBJECT_CLASS = "symbol"

_VERDICT_LABELS = (
    "warm.engine_root.is_engine_root",
    "_resolve_claude_klabauter._is_stamped_engine_root",
    "ipc._is_dispatch_engine_stamped",
    "claude-klabauter-doctor-probe._engine_root_is_stamped",
    "gen-launcher-shim._engine_stamp_bytes",
    "substrate._dispatch_engine_stamp_bytes",
)


def _format_disagreement(verdicts: tuple) -> str:
    pairs = ", ".join(f"{label}={v!r}" for label, v in zip(_VERDICT_LABELS, verdicts))
    return f"the six stamp-predicate copies disagree: {pairs}"


@pytest.mark.parametrize(
    "make_stamp",
    [
        pytest.param(lambda p: p.write_text("sha:deadbeef\n", encoding="utf-8"), id="valid_stamp"),
        pytest.param(lambda p: p.write_text("", encoding="utf-8"), id="empty_stamp"),
        pytest.param(None, id="missing_stamp"),
    ],
)
def test_all_six_stamp_predicates_agree(monkeypatch, tmp_path, make_stamp):
    coordinator_core_dir = tmp_path / "coordinator_core"
    coordinator_core_dir.mkdir()
    stamp_path = coordinator_core_dir / "_engine_stamp"
    if make_stamp is not None:
        make_stamp(stamp_path)

    verdicts = _all_six_verdicts(monkeypatch, tmp_path)
    assert len(set(verdicts)) == 1, _format_disagreement(verdicts)


def test_all_six_agree_root_has_no_coordinator_core_dir_at_all(monkeypatch, tmp_path):
    """Distinct from the empty/missing-file cases above: the CONTAINING
    directory itself is absent, not just the stamp file -- a broken or
    half-installed checkout."""
    verdicts = _all_six_verdicts(monkeypatch, tmp_path)
    assert len(set(verdicts)) == 1, _format_disagreement(verdicts)
