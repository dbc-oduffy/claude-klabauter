"""halted_record_commit.py imports from land_run_halt only names land_run_halt defines at HEAD.

A name defined only in an uncommitted land_run_halt.py hunk imports fine in the live tree and fails
`ImportError` in a clean export of the landed candidate.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halted_record_commit


_REPO = Path(__file__).resolve().parents[4]
_MODULE = "coordinator_core.ops.dispatch_emit.land_run_halt"


def test_unexecuted_withholds_is_the_halt_commits_own_definition():
    """The halt commit's reader is `land_run_halt`'s, imported (not redefined) so there is one definition."""
    assert halted_record_commit.unexecuted_withholds.__module__ == _MODULE
