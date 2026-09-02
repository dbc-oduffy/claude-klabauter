"""
Tests for coordinator_core.ops.extract_cited_sidecars.

Spec: state/dispatch-briefs/2026-09-02-state-keeps-the-work-not-the-machinery/C4.md
"""
from __future__ import annotations

import os
import subprocess

import pytest

from coordinator_core.ops.extract_cited_sidecars import (
    main,
    run_extraction,
    scan,
)

# Spawns a real external process (git ls-files); runs at cadence gates.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _write(root: str, rel: str, content: str) -> None:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(content)


def test_uuid_cited_and_on_disk_is_captured(tmp_path):
    root = str(tmp_path)
    uid = "abcd1234-1111-2222-3333-444455556666"
    _write(root, "state/subagent-share/" + uid + "/a.md", "sidecar content\n")
    _write(
        root,
        "archive/bug-backlog/foo.yaml",
        f"cited: state/subagent-share/{uid}\n",
    )
    uuid_citations, sha_citations = scan(root)
    assert uid in uuid_citations
    assert uuid_citations[uid] == ["archive/bug-backlog/foo.yaml"]


def test_uuid_cited_but_not_on_disk_is_excluded(tmp_path):
    root = str(tmp_path)
    uid = "deadbeef-1111-2222-3333-444455556666"
    _write(
        root,
        "archive/bug-backlog/foo.yaml",
        f"cited: state/subagent-share/{uid}\n",
    )
    uuid_citations, _sha = scan(root)
    assert uid not in uuid_citations


def test_citations_inside_state_are_excluded_from_walk(tmp_path):
    root = str(tmp_path)
    uid = "11112222-1111-2222-3333-444455556666"
    _write(root, "state/subagent-share/" + uid + "/a.md", "sidecar\n")
    _write(
        root,
        "state/other-record.yaml",
        f"cited: state/subagent-share/{uid}\n",
    )
    uuid_citations, _sha = scan(root)
    assert uid not in uuid_citations


def test_sha_shaped_token_is_captured_raw(tmp_path):
    root = str(tmp_path)
    sha = "a" * 40
    _write(root, "docs/notes.md", f"see commit {sha} for details\n")
    _uuid, sha_citations = scan(root)
    assert "docs/notes.md" in sha_citations
    assert sha in sha_citations["docs/notes.md"]


def test_sha_scan_does_not_require_git_commit_shape(tmp_path):
    # Raw-set contract: any 40-hex token is captured, not just real commits.
    root = str(tmp_path)
    fake_sha = "f" * 40
    _write(root, "docs/notes.md", f"fixture hash {fake_sha}\n")
    _uuid, sha_citations = scan(root)
    assert fake_sha in sha_citations["docs/notes.md"]


def test_one_git_subprocess_regardless_of_uuid_count(tmp_path, monkeypatch):
    root = str(tmp_path)
    for i in range(5):
        uid = f"{i:08d}-1111-2222-3333-444455556666"
        _write(root, "state/subagent-share/" + uid + "/a.md", "x\n")
        _write(root, f"archive/bug-backlog/f{i}.yaml", f"cited: state/subagent-share/{uid}\n")

    calls = []
    real_run = subprocess.run

    def _spy(*args, **kwargs):
        calls.append(args)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _spy)
    scan(root)
    assert len(calls) == 1


def test_main_writes_both_audit_files(tmp_path):
    root = str(tmp_path)
    uid = "22223333-1111-2222-3333-444455556666"
    _write(root, "state/subagent-share/" + uid + "/a.md", "x\n")
    _write(root, "archive/bug-backlog/f.yaml", f"cited: state/subagent-share/{uid}\n")
    sha = "b" * 40
    _write(root, "docs/notes.md", f"commit {sha}\n")

    rc = main(["--root", root])
    assert rc == 0

    uuid_out = os.path.join(root, "state", "audits", "2026-09-02-cited-subagent-share-sidecars.md")
    sha_out = os.path.join(root, "state", "audits", "2026-09-02-cited-commit-shas.md")
    assert os.path.isfile(uuid_out)
    assert os.path.isfile(sha_out)

    uuid_text = open(uuid_out, encoding="utf-8").read()
    assert uid in uuid_text
    assert "archive/bug-backlog/f.yaml" in uuid_text
    assert "a.md" in uuid_text

    sha_text = open(sha_out, encoding="utf-8").read()
    assert sha in sha_text
    assert "docs/notes.md" in sha_text


def test_run_extraction_no_citations_still_renders(tmp_path):
    root = str(tmp_path)
    uuid_text, sha_text = run_extraction(root)
    assert "No citations found." in uuid_text
    assert "No 40-hex-shaped tokens found." in sha_text
