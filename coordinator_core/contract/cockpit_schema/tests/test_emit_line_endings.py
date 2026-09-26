from __future__ import annotations

from coordinator_core.contract.cockpit_schema import ENTITY_SCHEMAS
from coordinator_core.contract.cockpit_schema.emit_schema import emit_schemas


def test_emitted_schema_files_are_lf_only(tmp_path):
    out_dir = tmp_path / "emit"
    emit_schemas(ENTITY_SCHEMAS, out_dir=out_dir)

    emitted = sorted(out_dir.glob("*.json"))
    assert emitted, "emit produced no files — registry or out_dir resolution regression"

    crlf = [p.name for p in emitted if b"\r" in p.read_bytes()]
    assert crlf == [], (
        "emitted schema files contain carriage returns — the emitter must pin "
        'newline="\\n" on every write. CRLF here dirties DoE\'s worktree on every '
        "Windows regen and blocks regen-cockpit-schema.py --advance-ref.\n"
        f"Offending files: {', '.join(crlf)}"
    )
