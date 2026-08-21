"""test_publish_main_copy_reads_shadow — asserts AC1's core claim directly:
`run_pre_sync_gates` sets `GateResult.source_dir` to the materialized
committed-ref SHADOW, never to `target.source_dir`, for the MAIN (un-
allowlisted) copy.

C6 (state/dispatch-briefs/2026-08-21-the-payload-proves-itself-before-it-
overwrites-the-engine/C6.md): the round pin is guarded four ways
(`test_publish_round_pin_and_identity_attribution.py`) and the inject leg
twice (`test_publish_inject_uses_round_pin.py`), but no test asserted this
property on its own terms — that a target with NO declared allowlist still
gets repointed at its shadow before `run_pre_sync_gates` returns, per
`GateResult`'s own docstring (docs/plans/2026-08-04-publish-from-a-committed-
ref.md C1b).

Honest framing: no such guard would have caught the 2026-08-21 outage —
nothing here reads the working tree. This guards a correct property against
future regression; it does not stand in for the incident fix (C1/C2 of this
same chunk set).

STUB-ONLY: every gate and materialize call this exercises is monkeypatched,
so this spawns no git and no subprocess.

Run: python -m pytest coordinator/bin/tests/test_publish_main_copy_reads_shadow.py -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_main_copy_shadow_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _stub_out_unrelated_gates(monkeypatch, publish):
    monkeypatch.setattr(publish, "check_live_install_clobber", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_dirty_tree", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_marketplace_version_regression", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_version_consistency", lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)


def test_main_copy_gate_result_source_dir_is_the_shadow_not_raw_source(monkeypatch, tmp_path):
    """The un-allowlisted (MAIN) row's `GateResult.source_dir` is the
    materialized shadow path — never `target.source_dir` itself."""
    raw_source = tmp_path / "raw-source-checkout"
    raw_source.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    shadow_path = tmp_path / "shadow-extracted-tree"
    shadow_path.mkdir()

    target = publish.ResolvedTarget(
        name="main-copy-target",
        mode="mirror",
        source_dir=raw_source,
        dest_dir=dest_dir,
    )

    _stub_out_unrelated_gates(monkeypatch, publish)
    monkeypatch.setattr(
        publish,
        "_round_pin_source_sha",
        lambda root, pinned_shas, *, out=None, late=False: "PINNED_SHA",
    )
    monkeypatch.setattr(
        publish,
        "_git_materialize_ref",
        lambda root, ref="HEAD": shadow_path,
    )

    result = publish.run_pre_sync_gates(
        target,
        setup_dir=tmp_path,
        identity_file_exists=False,
        identity=None,
        totals=publish.RunTotals(),
        dry_run=True,
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert result.proceed is True
    assert result.source_dir == shadow_path, (
        f"MAIN copy's GateResult.source_dir must be the shadow ({shadow_path}), "
        f"got {result.source_dir!r} instead"
    )
    assert result.source_dir != raw_source, (
        "MAIN copy's GateResult.source_dir must NEVER be target.source_dir "
        "itself — that is exactly the un-materialized raw checkout this "
        "property guards against."
    )
