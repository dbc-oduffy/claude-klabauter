"""
coordinator_core.merge_assemble.tests.test_producer_root_is_engine_not_target_repo
— regression guard for the defect three consumer repos reported on
2026-09-02 (cross-repo/inbox/2026-09-02-doe-claude-em-*,
-example-retrieval-repo-em-*, -example-retrieval-repo-ue-addon-em-*).

`_dispatch_in_process` resolved its producer as
`resolve_cli_script_root(repo_root) / "<script>.py"`, i.e.
`<TARGET REPO>/coordinator/bin/<script>.py`. `coordinator/bin/` is
ENGINE-PROVISIONED and absent from every consumer repo, so
`merge_assemble.apply` aborted with `UnrecognizedDirective` at its first
in-process directive (`d1`) in every repo that is not the engine checkout
— leaving `d1`-`d6` (tag cut, coverage gate, PR body, illegal-path scan,
portability sweep) unreachable outside `claude-klabauter` itself.

What let it through: the pre-existing tests ran with `repo_root` equal to
the engine root, where the two roots coincide and the wrong join is
indistinguishable from the right one. Every assertion here therefore uses
a `repo_root` that is DELIBERATELY NOT the engine root.

NEGATIVE SPEC: this file asserts only WHERE a producer is resolved FROM.
It does not exercise any producer's behaviour, does not assert that
`repo_root` reaches the CLI (that is each handler's own arg-threading,
covered by its docstring and `test_apply_op_dispatch`), and does not
spawn anything.

No process spawn, no git — fast tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ceremony_common.cli_dispatch import resolve_cli_script_root
from coordinator_core.merge_assemble import apply as ma_apply

_ENGINE_ROOT = Path(ma_apply.__file__).resolve().parents[2]


def test_script_root_is_the_engine_clone_and_takes_no_repo_root():
    assert resolve_cli_script_root() == _ENGINE_ROOT / "coordinator" / "bin"
    with pytest.raises(TypeError):
        resolve_cli_script_root(Path("X:/some-consumer-repo"))  # type: ignore[call-arg]


def test_both_dispatch_paths_share_one_engine_anchored_bin_dir():
    assert ma_apply._BIN_DIR == resolve_cli_script_root()


def test_in_process_dispatch_never_looks_under_a_consumer_repo(tmp_path: Path):
    """`tmp_path` stands in for a consumer repo: no `coordinator/bin/` at
    all, exactly as in example-retrieval-repo, example-retrieval-repo-ue-addon, DoE-claude and
    example-game-workbench-repo. The producer name below does not exist in the
    engine either, so the resolution still misses — but the miss must name
    the ENGINE path, not the consumer one. Under the defect this asserted
    path was `tmp_path/coordinator/bin/...`."""
    assert not (tmp_path / "coordinator" / "bin").exists()

    with pytest.raises(ma_apply.UnrecognizedDirective) as exc:
        ma_apply._dispatch_in_process("some-cli", "no-such-producer", [])

    assert str(_ENGINE_ROOT / "coordinator" / "bin" / "no-such-producer.py") in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_converged_handlers_resolve_producers_present_in_the_engine(tmp_path: Path):
    """The three verbs C2 converted must be resolvable while running a
    ceremony against a repo that is not the engine. `portability-sweep` is
    excluded: it has no producer on any box (see
    `_dispatch_portability_sweep`'s docstring), so its absence is the
    designed `already_satisfied` path, not this defect."""
    for script_name in ("merge-recovery-and-tag-cut", "check-no-illegal-paths"):
        resolved = ma_apply._BIN_DIR / f"{script_name}.py"
        assert resolved.is_file(), f"{script_name} missing from the engine's coordinator/bin"
        assert tmp_path not in resolved.parents
