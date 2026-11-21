"""check_blanket_git_add matches executing command words only: heredoc payloads and quoted arguments never trigger it."""

from __future__ import annotations


import pytest

from coordinator_core.bash_guards import _dc_blanket_add

pytestmark = pytest.mark.complete


HEREDOC_BODY = "cat > f.json <<'EOF'\n{\"note\": \"it's a fixture: git add -- state\"}\nEOF\ncoordinator-invoke workflow.revise \"$(cat f.json)\""


def test_shared_splitter_drops_heredoc_body():
    segs = _dc_blanket_add._awk_quote_aware_split("cat > f <<'EOF'\nit's; git add -A\nEOF\nls")
    assert not any("git add" in s for s in segs)
