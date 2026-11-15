"""A Windows checkout (core.autocrlf=true, no .gitattributes) holds every vendored JSON as CRLF
over an LF blob. The drift verdict compares line-ending-normalised text, so such a copy is never
drift -- neither against DoE directly nor through the publish-scrub mirror_blob path."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.frontmatter import schema_validate as sv

_LF = '{\n  "title": "x",\n  "type": "object"\n}\n'


def _crlf_copy(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "x.schema.json"
    path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    return path


def test_a_crlf_working_copy_of_the_doe_blob_is_not_drift(tmp_path):
    path = _crlf_copy(tmp_path, _LF)
    assert sv._drift_verdict(path, tmp_path, "HEAD", _LF) is None


def test_a_crlf_doe_blob_against_an_lf_copy_is_not_drift(tmp_path):
    path = tmp_path / "x.schema.json"
    path.write_bytes(_LF.encode("utf-8"))
    assert sv._drift_verdict(path, tmp_path, "HEAD", _LF.replace("\n", "\r\n")) is None


def test_a_crlf_scrubbed_mirror_matches_its_recorded_mirror_blob(tmp_path):
    scrubbed = _LF.replace('"x"', '"y"')
    path = _crlf_copy(tmp_path, scrubbed)
    verdict = sv._drift_verdict(
        path, tmp_path, "HEAD", _LF,
        base_content=_LF, base_blob=sv._git_blob_sha(_LF), mirror_blob=sv._git_blob_sha(scrubbed),
    )
    assert verdict is None
