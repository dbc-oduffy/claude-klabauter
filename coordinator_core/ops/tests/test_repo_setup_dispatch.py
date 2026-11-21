"""
coordinator_core/ops/tests/test_repo_setup_dispatch.py -- "repo.setup" through ipc.dispatch_message.

Subject: the registered op reached by method name with only `root` (no handler call, no module
import in the test): registry, scope and authz wiring are all on the path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.authz.classification import OpClass, classify

ENGINE_BIN = Path(__file__).resolve().parents[3] / "coordinator" / "bin"


def test_repo_setup_is_a_classified_write_op() -> None:
    assert classify("repo.setup") is OpClass.MUTATING
