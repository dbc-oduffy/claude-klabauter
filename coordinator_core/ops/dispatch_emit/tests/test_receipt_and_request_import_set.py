"""
coordinator_core.ops.dispatch_emit.tests.test_receipt_and_request_import_set --
``completion_receipt`` and ``commit_request`` load neither ``coordinator_complete_entry`` nor
``inventory_mint`` for the helpers they once imported from them.

Negative-spec:
    - Does NOT assert modules owned by other rows' files.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[4]

_ABSENT = (
    "coordinator_core.ops.coordinator_complete_entry",
    "coordinator_core.ops.dispatch_emit.inventory_mint",
)

_PROBE = """
import json, sys
from pathlib import Path
from coordinator_core.ops.dispatch_emit import commit_request as cr
from coordinator_core.ops.dispatch_emit import completion_receipt as rc
root = Path(sys.argv[1])
(root / "a.txt").write_text("x\\topus\\ny\\tsonnet\\n", encoding="utf-8")
counted = rc._count_session_dispatches(root / "a.txt")
bare = cr._bare_path(":(literal)a[1].py")
loe = rc._actual_loe(root, "no-such-session")
print("RESULT=" + json.dumps({
    "ok": counted == (2, 1) and bare == "a[1].py" and loe is None,
    "loaded": [m for m in json.loads(sys.argv[2]) if m in sys.modules],
}))
"""


@pytest.mark.spawns_process
def test_receipt_and_request_helpers_do_not_load_the_cut_modules(tmp_path):
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, str(tmp_path), json.dumps(_ABSENT)],
        capture_output=True, text=True, timeout=120, cwd=_REPO_ROOT,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr[-800:]
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("RESULT="))
    got = json.loads(line[len("RESULT="):])
    assert got["ok"], "probe helpers returned unexpected values"
    assert got["loaded"] == []


def test_literal_pathspec_prefix_matches_its_original():
    from coordinator_core.ops.dispatch_emit import commit_request, inventory_mint

    assert commit_request._LITERAL_PATHSPEC_PREFIX == inventory_mint._LITERAL_PATHSPEC_PREFIX


@pytest.mark.parametrize("path", [
    "a.py", ":(literal)a[1].py", ":(literal)", "", ":(literal):(literal)x", "x/:(literal)y",
])
def test_strip_agrees_with_original(path):
    from coordinator_core.ops.dispatch_emit import commit_request, inventory_mint

    original = inventory_mint.strip_literal_pathspec(path)
    if original.startswith(":("):
        with pytest.raises(commit_request.MalformedCommitRequestError):
            commit_request._bare_path(path)
    else:
        assert commit_request._bare_path(path) == original


@pytest.mark.parametrize("content", [
    None, "", "\n", "a\topus\nb\tx\n", "a\tOPUS\nb", "opus-only\n\n", "x\ty\tOpus\n", "\xff".encode("latin-1"),
])
def test_count_session_dispatches_agrees_with_original(tmp_path, content):
    from coordinator_core.ops import coordinator_complete_entry as orig
    from coordinator_core.ops.dispatch_emit import completion_receipt

    f = tmp_path / "dispatched-agents.txt"
    if isinstance(content, bytes):
        f.write_bytes(content)
    elif content is not None:
        f.write_text(content, encoding="utf-8", newline="")
    assert completion_receipt._count_session_dispatches(f) == orig._count_session_dispatches(f)
