"""The stage reply names the staged twin it wrote, so a restage after revise names the new twin."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import nested_run_contract as nrc
from coordinator_core.ops.dispatch_emit import reply_sidecar

_RUN = "run1"
_SCRIPT = f"export const meta = {{ name: 'x' }}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n"


def _restage(root, payload):
    manifest = root / "state" / "warp" / _RUN / "manifest.json"
    reply_sidecar.write_reply(root, manifest, payload)
    handle, twin = reply_sidecar.handle_and_twin(root, manifest)
    return handle, twin


def test_restage_names_the_new_twin_on_disk(tmp_path):
    script = tmp_path / "scratch" / "warp" / f"{_RUN}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text(_SCRIPT, encoding="utf-8")

    h1, t1 = _restage(tmp_path, {"rows": [{"id": "C1"}]})
    h2, t2 = _restage(tmp_path, {"rows": [{"id": "C1"}, {"id": "C2"}]})

    assert h1["sha256"] != h2["sha256"]
    assert t2 == nrc.staged_twin_rel(_RUN, h2["sha256"]) != t1
    assert (tmp_path / t2).is_file()

