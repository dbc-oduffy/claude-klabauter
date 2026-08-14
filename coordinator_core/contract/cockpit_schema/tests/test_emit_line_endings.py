"""
test_emit_line_endings — the emitter writes LF on every platform.

Failure class guarded: `Path.write_text` without `newline=` runs in universal-
newlines mode, so a Windows emit rewrites all 31 schema files CRLF. Nothing
downstream caught it — DoE's `.gitattributes` pins `*.json eol=lf` so the
committed bytes normalise in the index, and `test_committed_emit_drift` reads
its comparands in text mode, which strips the CR before comparing. The exposure
is the *worktree*: `regen-cockpit-schema.py --advance-ref` runs the regen and
then refuses on a `git status --porcelain` that now reports 31 modified files,
with no second attempt that succeeds. Reported by doe-claude-em, 2026-08-11
(`cross-repo/archive/2026-08-11-doe-claude-em-cockpit-3-11-0-tagged-revendor-now.md`).

Negative spec: this asserts raw bytes, not decoded text — a text-mode read on
Windows would silently translate the very CRLF this test exists to catch.
"""
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
