"""`write_provenance` (both builders) must record `image_sha256` -- the
sha256 of the binary it sits beside -- not just its inputs.

Spec backlink: state/bug-backlog/2026-08-30-installed-door-provenance-sidecar-describes-a-binary-that-is-not-there.yaml

Neither test shells out to a compiler: a fake output file stands in for a
real build, since `write_provenance` only ever reads bytes off disk, never
invokes the compiler itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door import build_posix as door_build_posix


def test_write_provenance_records_image_sha256_matching_the_output_exe(tmp_path):
    output_exe = tmp_path / "door.exe"
    output_exe.write_bytes(b"stand-in compiled binary bytes")
    source_path = tmp_path / "door.c"
    source_path.write_text("// stand-in source\n", encoding="utf-8")

    # write_provenance also reads door_core.c/.h off the real module
    # location (_CORE_SOURCE / _CORE_HEADER) -- those are real, checked-in
    # files, so no additional stand-in is needed for them.
    provenance_path = door_build.write_provenance(
        output_exe, source_path, "clang", "clang", tmp_path
    )

    record = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert record["image_sha256"] == hashlib.sha256(output_exe.read_bytes()).hexdigest()
    # Existing fields stay load-bearing -- unchanged by this addition.
    assert record["door_c_sha256"] == hashlib.sha256(source_path.read_bytes()).hexdigest()


def test_write_provenance_posix_records_image_sha256_matching_the_output(tmp_path):
    output = tmp_path / "door"
    output.write_bytes(b"stand-in posix compiled binary bytes")

    provenance_path = door_build_posix.write_provenance(output, "clang", tmp_path)

    record = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert record["image_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
