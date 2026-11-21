"""A landing whose run already passed its overlay verify is not parked at `reverify_on_mismatch` by peer
churn in files the run never wrote: the verdict is keyed on the run's own blobs plus the base sha, and a
legacy verdict (no recorded components) is cleared only against the paths the run itself declared, using the
stored `measured-rev.json` it still hashes to. A change to a declared path's landed blob still mismatches
and parks."""

from __future__ import annotations


from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov

import pytest
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
SEEDS = ("t/test_x.py::test_a",)


def test_same_blob_content_reads_the_object_store_up_to_line_endings(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shas = []
    for body in (b"a\nb\n", b"a\r\nb\r\n", b"a\nc\n"):
        out = subprocess.run(["git", "-C", str(tmp_path), "hash-object", "-w", "--stdin"], input=body, capture_output=True, check=True)
        shas.append(out.stdout.decode().strip())

    assert cov._same_blob_content(tmp_path, shas[0], shas[1])
    assert not cov._same_blob_content(tmp_path, shas[0], shas[2])
    assert not cov._same_blob_content(tmp_path, shas[0], "0" * 40)
