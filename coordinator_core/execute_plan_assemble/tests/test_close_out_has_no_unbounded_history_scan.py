"""Tripwire: the close-out modules carry no unanchored `--reverse HEAD` history walk.

The deleted base-scan read the whole of HEAD's history; a bound by commit
count would answer wrongly, so the only admissible forms are anchored ranges.
"""

from pathlib import Path

import coordinator_core.execute_plan_assemble.close_out_and_stamp as coas
import coordinator_core.ops.cascade_baton_rows as cbr


def test_no_reverse_history_scan_in_close_out_machinery():
    for mod in (coas, cbr):
        text = Path(mod.__file__).read_text(encoding="utf-8")
        assert '"--reverse"' not in text and "'--reverse'" not in text, mod.__name__
        assert not hasattr(mod, "_first_deliverable_commit_range_base"), mod.__name__
        assert not hasattr(mod, "_chunk_evidence_log_range"), mod.__name__
