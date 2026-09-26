
from __future__ import annotations

import coordinator_core.coverage as coverage


def test_commit_deliverable_id_trailers_not_reintroduced():
    assert not hasattr(coverage, "_commit_deliverable_id_trailers")
