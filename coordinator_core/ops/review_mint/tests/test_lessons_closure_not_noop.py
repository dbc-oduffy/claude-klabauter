"""A run whose committed settlements closed rows is landed with the closure count; a run that committed nothing stays a no-op."""

import json
import shutil
import subprocess

import pytest

from coordinator_core.ops.review_mint.tests.test_execute_review import _ZERO_FILE_PREP, _compose

pytestmark = pytest.mark.complete


def _run_guard(settled, rows, review_paths=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    _, phases = _compose()
    body = [
        f"  const _reviewPrep = {json.dumps(dict(_ZERO_FILE_PREP))};"
        if ln.startswith("  const _reviewPrep = await")
        else ln
        for ln in phases[0][1].splitlines()
        if not ln.lstrip().startswith("phase(")
    ]
    script = (
        "(async () => { const _out = await (async () => {\n"
        + (f"const _settled = {json.dumps(settled)};\nconst _rows = {json.dumps(rows)};\n" if settled is not None else "")
        + (f"const _reviewPaths = {json.dumps(review_paths)};\n" if review_paths is not None else "")
        + "\n".join(body)
        + "\n return {slices: _reviewPrep.slices}; })();\n console.log(JSON.stringify(_out)); })();"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return json.loads(out.stdout)


_ARCHIVE = ["state/archive/a.md"]


def _closed(n):
    ids = [f"r{i}" for i in range(n)]
    return (
        [{"row": r, "outcome": "committed"} for r in ids],
        {r: {"closeResult": {"old": "state/a.md", "new": "state/archive/a.md"}} for r in ids},
    )


def test_closure_only_run_is_landed_with_the_closure_count():
    settled, rows = _closed(6)
    result = _run_guard(settled, rows, _ARCHIVE)
    assert result["halted"] == "landed: 6 rows closed, no product file changed"
    assert "no-op" not in result["halted"]


def test_single_closure_reads_singular():
    settled, rows = _closed(1)
    assert _run_guard(settled, rows, _ARCHIVE)["halted"] == "landed: 1 row closed, no product file changed"


def test_run_that_committed_nothing_is_still_a_no_op():
    assert _run_guard([], {}, [])["halted"] == "no-op"
    assert _run_guard(None, None)["halted"] == "no-op"


def test_uncommitted_or_unclosed_settlements_do_not_count_as_closures():
    settled = [{"row": "a", "outcome": "handed-back"}, {"row": "b", "outcome": "committed"}]
    rows = {"a": {"closeResult": {"new": "x"}}, "b": {"closeResult": None}}
    assert _run_guard(settled, rows, _ARCHIVE)["halted"] == "no-op"
