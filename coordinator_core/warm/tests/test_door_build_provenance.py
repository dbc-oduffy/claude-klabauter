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

from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door import build_posix as door_build_posix


def test_write_provenance_records_image_sha256_matching_the_output_exe(tmp_path):
    output_exe = tmp_path / "door.exe"
    output_exe.write_bytes(b"stand-in compiled binary bytes")

    # write_provenance reads door.c/door_core.c/.h off the real module
    # location (_SOURCE / _CORE_SOURCE / _CORE_HEADER) -- those are real,
    # checked-in files, so no stand-in source is needed.
    provenance_path = door_build.write_provenance(
        output_exe, "clang", "clang", tmp_path
    )

    record = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert record["image_sha256"] == hashlib.sha256(output_exe.read_bytes()).hexdigest()
    # Existing fields stay load-bearing -- unchanged by this addition.
    assert record["door_c_sha256"] == hashlib.sha256(door_build._SOURCE.read_bytes()).hexdigest()


def test_write_provenance_posix_records_image_sha256_matching_the_output(tmp_path):
    output = tmp_path / "door"
    output.write_bytes(b"stand-in posix compiled binary bytes")

    provenance_path = door_build_posix.write_provenance(output, "clang", tmp_path)

    record = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert record["image_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
