"""
coordinator_core.merge_assemble.tests.test_in_process_producer_is_engine_anchored
— regression cover for the consumer-root probe.

Purpose: `_dispatch_in_process` must locate its producer in the ENGINE
checkout that ships it, never under the target repo the ceremony operates
on. The three in-process verbs (`merge-recovery-and-tag-cut`,
`portability-sweep`, `check-no-illegal-paths`) are claude-klabauter-shipped and exist
in no consumer repo, so a `repo_root`-anchored join made every ceremony run
outside claude-klabauter itself halt at `d1` with "no producer at
<consumer repo>/coordinator/bin/merge-recovery-and-tag-cut.py". Reported
from example-cockpit-repo 2026-09-01; the join it came from is gravestoned in
`ceremony_common/cli_dispatch.py`.

Negative spec: this file asserts nothing about WHICH producers are
installed (`portability-sweep.py` has no producer on this box and
`build_directives` marks its directive `already_satisfied` upstream), only
about the directory the lookup is rooted in.

No process spawn, no git — fast tier.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.merge_assemble import apply as merge_apply
from coordinator_core.contract.apply_base import UnrecognizedDirective


def test_producer_resolves_under_the_engine_root_not_the_target_repo(tmp_path: Path):
    """A repo root that is not the engine root must not become a producer root."""
    consumer_repo = tmp_path / "consumer-repo"
    (consumer_repo / "coordinator" / "bin").mkdir(parents=True)

    with pytest.raises(UnrecognizedDirective) as excinfo:
        merge_apply._dispatch_in_process(
            "a-cli", "a-producer-that-does-not-exist", [], consumer_repo
        )

    probed = str(excinfo.value)
    assert str(consumer_repo) not in probed, (
        "the producer was probed under the target repo root; it must be probed "
        f"under the engine's own coordinator/bin — got: {probed}"
    )
    assert str(merge_apply._BIN_DIR) in probed


def test_shipped_producer_is_found_from_an_unrelated_repo_root(tmp_path: Path):
    """`merge-recovery-and-tag-cut.py` ships with the engine, so the existence
    check clears even when `repo_root` is an unrelated directory — the failure
    mode the cockpit report hit."""
    script = merge_apply._BIN_DIR / "merge-recovery-and-tag-cut.py"
    assert script.is_file(), f"engine producer missing: {script}"

    # Reaching the script's OWN argparse error (exit 2, surfaced as
    # `RuntimeError` by `_dispatch_result`'s contract) is the proof: the
    # producer was located and invoked. The absent-producer branch would have
    # raised `UnrecognizedDirective` before `load_cli_module` was ever called.
    with pytest.raises(RuntimeError) as excinfo:
        merge_apply._dispatch_in_process(
            "merge-recovery-and-tag-cut",
            "merge-recovery-and-tag-cut",
            [],
            tmp_path,
        )
    assert not isinstance(excinfo.value, UnrecognizedDirective)
    assert "no producer at" not in str(excinfo.value)
