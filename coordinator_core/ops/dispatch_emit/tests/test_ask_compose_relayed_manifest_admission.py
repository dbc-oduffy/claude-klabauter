"""A relayed stage manifest is adopted only when whole; a lossy relay halts with a named reason."""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose import _compose

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ROW = {
    "id": "C1", "brief_path": "b/C1.md", "agent_type": "coordinator:executor", "model": "opus",
    "writes": ["a.py"], "wave": 0, "deps": [],
}
_WHOLE = {"rows": [_ROW], "review_declared_paths": ["a.py"], "test_targets": ["t.py"]}
_JOURNAL_RELAY = {"rows": ["TC(w0)", "TS(w0)", "CH", "AL (w3)"]}


def _gap(manifest) -> str | None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not on PATH")
    js = (
        ask_compose._ADMIT_MANIFEST_JS
        + f"\nlet _stagedManifest = null;\nprocess.stdout.write(JSON.stringify(_wholeManifestGap({json.dumps(manifest)})));\n"
    )
    return json.loads(subprocess.run([node, "-e", js], capture_output=True, text=True, check=True).stdout)


def test_the_on_disk_manifest_shape_is_admitted():
    assert _gap(_WHOLE) is None
    assert _gap({**_WHOLE, "gated": [{"id": "G1"}], "owed": [{"id": "O1"}]}) is None


def test_the_journal_relay_with_string_rows_is_refused():
    assert _gap(_JOURNAL_RELAY) == "review_declared_paths"
    assert _gap({**_JOURNAL_RELAY, "review_declared_paths": [], "test_targets": []}) == "rows[0]"


def test_a_whole_reply_missing_only_test_targets_is_refused():
    assert _gap({k: v for k, v in _WHOLE.items() if k != "test_targets"}) == "test_targets"


def test_the_c268bb_row_shape_is_refused():
    thin = {**_WHOLE, "rows": [{"id": "C1", "wave": 0, "deps": []}]}
    assert _gap(thin) == "rows[0].brief_path"


@pytest.mark.parametrize(
    ("patch", "field"),
    [
        ({"error": "x"}, "error: x"),
        ({"gated": [{"reason": "r"}]}, "gated[0].id"),
        ({"owed": [{"id": ""}]}, "owed[0].id"),
        ({"rows": [{**_ROW, "wave": "0"}]}, "rows[0].wave"),
        ({"rows": [{**_ROW, "writes": None}]}, "rows[0].writes"),
    ],
)
def test_each_pinned_field_is_checked(patch, field):
    assert _gap({**_WHOLE, **patch}) == field


def test_no_adoption_site_adopts_on_a_rows_array_alone():
    script = _compose()
    assert not re.search(r"_stagedManifest\s*=\s*(?!null)[^;]*(?:readyOpReply|\.manifest)", script.replace(
        "_stagedManifest = m;", ""))
    assert "Array.isArray(_ready.readyOpReply?.rows)" not in script
    for site in ("_ready.readyOpReply", "_reready.readyOpReply", "_spineReview.manifest",
                 "_revised.manifest", "_authored.manifest"):
        assert f"_adoptManifest({site})" in script, site


def test_a_refused_relay_halts_with_the_named_reason_before_execute():
    script = _compose()
    assert "_manifest = { error: 'relayed stage manifest is not whole: ' + _refusedField }" in script
    assert "reason: _manifest.error" in script
    assert script.index("relayed stage manifest is not whole") < script.index("phase(" + repr(ask_compose._emit._EXECUTE_PHASE_TITLE))


def test_incomplete_chunk_pushes_need_no_own_guard_and_result_rows_are_strings():
    script = _compose()
    assert "_incompleteChunks.push(g.id)" in script and "_incompleteChunks.push(o.id)" in script
    assert "rows: (_manifest?.rows ?? []).map((r) => r.id)" in script
    assert "String(r.id)" not in script
