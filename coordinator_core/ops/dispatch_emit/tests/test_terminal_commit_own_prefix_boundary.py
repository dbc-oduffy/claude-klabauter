"""`_own_prefix_files` trusts only claimed files under the chunk's OWN prefix.

Prefix matching must stop at a path-component boundary: a claimed sibling
directory sharing only a leading string with the prefix is not under it.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import PREFIX_CLAIM_LABEL, ChunkCommit


def _own_files(tmp_path: Path, prefix: str, claims: list) -> list:
    report = tmp_path / "report.md"
    report.write_text(
        "\n".join(f"{PREFIX_CLAIM_LABEL} {claim}" for claim in claims) + "\n", encoding="utf-8"
    )
    chunk = ChunkCommit(id="c1", title="t", prefixes=(prefix,), report="report.md")
    return terminal_commit._own_prefix_files(tmp_path, chunk, {})


def test_slash_terminated_prefix_excludes_a_leading_string_sibling(tmp_path: Path) -> None:
    kept = _own_files(tmp_path, "src/foo/", ["src/foo/a.py", "src/foobar/x.py"])
    assert kept == ["src/foo/a.py"]


def test_bare_prefix_excludes_a_leading_string_sibling(tmp_path: Path) -> None:
    kept = _own_files(tmp_path, "src/foo", ["src/foo/a.py", "src/foobar/x.py"])
    assert kept == ["src/foo/a.py"]


def test_a_claim_equal_to_the_prefix_is_kept(tmp_path: Path) -> None:
    assert _own_files(tmp_path, "src/foo", ["src/foo"]) == ["src/foo"]
