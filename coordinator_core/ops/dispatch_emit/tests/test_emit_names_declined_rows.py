"""The queue emit receipt names every declined row with its reason, and stderr carries one line each."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import queue_emit

from .conftest import REVIEW_KW
from .test_queue_emit import _FIXTURE_PROFILE_DIR, _setup_repo, _write_row
from .test_queue_select import _lesson_gate


def test_receipt_and_stderr_name_a_declined_stem_mismatched_titleless_row(tmp_path, monkeypatch, capsys):
    repo_root, queue_dir, run_dir = _setup_repo(tmp_path, n_rows=0)
    _write_row(queue_dir / "file-stem.yaml", id="a-different-id", status="open", created="2026-09-21", severity="P0")
    _write_row(queue_dir / "ok.yaml", id="ok", status="open", created="2026-09-21", title="t", body="b",
               scope="project", from_repo="x", severity="P0")
    monkeypatch.setattr(queue_emit, "load_row_schema_gate", lambda profile, root: _lesson_gate())

    emission = queue_emit.emit_queue_script(
        "fixture", "standard", None,
        queue=[queue_dir], profile_dir=_FIXTURE_PROFILE_DIR, repo_root=repo_root, run_dir=run_dir,
        session_id="sess1", **REVIEW_KW,
    )

    declined = emission.receipt_extras["declined"]
    (entry,) = declined
    assert entry["id"] in {"file-stem", "a-different-id"}
    assert entry["reason"].startswith("admission:") and entry["path"].endswith("file-stem.yaml")
    assert f"declined {entry['id']}" in capsys.readouterr().err
