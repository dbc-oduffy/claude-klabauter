"""`set --concern` / `unset --concern` edit one key in place: every table and comment the
concern file's co-writer owns (example-retrieval-repo-cli's top-level scalars, [env], [provenance], [bin])
stays byte-identical. The example-retrieval-repo serve reads only data["env"]; nesting [env] under
[example_retrieval_repo.env] unset its band env and took every engine band down."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_IMPL = Path(__file__).resolve().parents[2] / "templates" / "bin" / "_machine_local.py"

_REGISTRY = "schema = 1\nconcerns = ['project_rag']\n"

# The example-retrieval-repo-cli layout, as written on the box before the 2026-11-15 nesting.
_COWRITTEN = """\
schema = 1
generated_at = "2026-10-30T00:00:00Z"
# band bulk output, off E for disk
bulk_output_root = "D:/bulk"

[env]
# Managed by `example-retrieval-repo-cli wire`. Do not hand-edit [env]/[provenance].
EXAMPLE_RETRIEVAL_REPO_STRUCTURAL_INDEX = "D:/idx/structural"
EXAMPLE_RETRIEVAL_REPO_UE_ADDON_SERVED_VERSIONS = "5.8"

[provenance]
# Per-key provenance for conflict diagnosis.
EXAMPLE_RETRIEVAL_REPO_STRUCTURAL_INDEX = {source = "addon", at = "2026-10-30"}
EXAMPLE_RETRIEVAL_REPO_UE_ADDON_SERVED_VERSIONS = {source = "addon", at = "2026-10-30"}

[bin]
example_retrieval_repo_cli = "C:/bin/example-retrieval-repo-cli.exe"
recorded_at = "2026-10-30T00:00:00Z"
"""


def _run(reg: Path, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, MACHINE_LOCAL_REGISTRY_DIR=str(reg))
    return subprocess.run(
        [sys.executable, str(_IMPL), *argv], env=env, capture_output=True, text=True, timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _seed(reg: Path, newline: str) -> Path:
    (reg / "registry.toml").write_text(_REGISTRY, encoding="utf-8")
    path = reg / "example_retrieval_repo.local.toml"
    path.write_bytes(_COWRITTEN.replace("\n", newline).encode("utf-8"))
    return path


def _sections(raw: bytes) -> dict:
    """Header -> its exact bytes (header line through the line before the next header)."""
    out, header, chunk = {}, "", []
    for line in raw.splitlines(keepends=True):
        if line.lstrip().startswith(b"["):
            out[header] = b"".join(chunk)
            header, chunk = line.strip().decode(), []
        chunk.append(line)
    out[header] = b"".join(chunk)
    return out


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_set_concern_leaves_every_cowriter_table_byte_identical(tmp_path, newline):
    path = _seed(tmp_path, newline)
    before = _sections(path.read_bytes())

    proc = _run(tmp_path, "set", "--concern", "project_rag", "example_retrieval_repo.python", "C:/py/python.exe")

    assert proc.returncode == 0, proc.stderr
    after = _sections(path.read_bytes())
    for header in ("", "[env]", "[provenance]"):
        assert after[header] == before[header], header
    # [bin] was the last table; the appended key table follows it, so compare its lines.
    assert after["[bin]"].rstrip() == before["[bin]"].rstrip()
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["env"]["EXAMPLE_RETRIEVAL_REPO_STRUCTURAL_INDEX"] == "D:/idx/structural"
    assert "env" not in parsed["project_rag"]
    assert parsed["project_rag"]["python"] == "C:/py/python.exe"
    assert _run(tmp_path, "get", "example_retrieval_repo.python").stdout.strip() == "C:/py/python.exe"
    if newline == "\r\n":
        assert b"\n" not in path.read_bytes().replace(b"\r\n", b"")


def test_set_concern_twice_edits_the_key_in_place(tmp_path):
    path = _seed(tmp_path, "\n")
    assert _run(tmp_path, "set", "--concern", "project_rag", "example_retrieval_repo.python", "C:/a").returncode == 0
    once = path.read_bytes()

    assert _run(tmp_path, "set", "--concern", "project_rag", "example_retrieval_repo.python", "C:/b").returncode == 0

    twice = path.read_bytes()
    assert twice.count(b"python = ") == 1
    assert once.replace(b"C:/a", b"C:/b").split(b"written_at")[0] == twice.split(b"written_at")[0]


def test_set_concern_on_a_cowriter_top_level_key_edits_it_where_it_lives(tmp_path):
    path = _seed(tmp_path, "\n")
    before = _sections(path.read_bytes())

    proc = _run(tmp_path, "set", "--concern", "project_rag", "example_retrieval_repo.bulk_output_root", "E:/bulk")

    assert proc.returncode == 0, proc.stderr
    after = _sections(path.read_bytes())
    assert after[""] == before[""].replace(b'"D:/bulk"', b"'E:/bulk'")
    assert after["[env]"] == before["[env]"]
    assert "project_rag" not in tomllib.loads(path.read_text(encoding="utf-8"))


def test_unset_concern_leaves_every_cowriter_table_byte_identical(tmp_path):
    path = _seed(tmp_path, "\n")
    assert _run(tmp_path, "set", "--concern", "project_rag", "example_retrieval_repo.python", "C:/a").returncode == 0
    before = _sections(path.read_bytes())

    proc = _run(tmp_path, "unset", "--concern", "project_rag", "example_retrieval_repo.python")

    assert proc.returncode == 0, proc.stderr
    after = _sections(path.read_bytes())
    for header in ("", "[env]", "[provenance]", "[bin]"):
        assert after[header] == before[header], header
    assert "[provenance.python]" not in after
    assert _run(tmp_path, "has", "example_retrieval_repo.python").returncode == 1
