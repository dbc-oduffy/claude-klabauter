"""The bin payload, copied through the publisher's ignore filter, carries every unimatrix-* script
that INSTALL.md names, so `find_missing_doc_refs` reports no bin/unimatrix-* pair."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

LIB = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(LIB))

from percolate.allowlist import _copytree_ignore_filter  # noqa: E402
from percolate.doc_path_refs import find_missing_doc_refs  # noqa: E402
from percolate.ignore import PercolateIgnoreMatcher  # noqa: E402

BIN_SRC = LIB.parent / "bin"


def _payload(tmp_path: Path) -> Path:
    matcher = PercolateIgnoreMatcher.from_file(BIN_SRC / ".percolate-ignore")
    payload = tmp_path / "payload"
    payload.mkdir()
    shutil.copytree(BIN_SRC, payload / "bin", ignore=_copytree_ignore_filter(matcher, BIN_SRC, "bin"))
    return payload


def _unimatrix_scripts() -> list[str]:
    return sorted(p.name for p in BIN_SRC.glob("unimatrix-*.py"))


def test_source_tree_carries_unimatrix_scripts():
    assert "unimatrix-install.py" in _unimatrix_scripts()


def test_payload_satisfies_install_md_unimatrix_refs(tmp_path):
    payload = _payload(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "INSTALL.md").write_text(
        "Run `<plugin-root>/bin/unimatrix-install.py` to wire the peers.\n", encoding="utf-8"
    )
    missing = find_missing_doc_refs(docs, payload)
    assert [m for m in missing if m[1].startswith("bin/unimatrix-")] == []


def test_every_unimatrix_script_reaches_payload(tmp_path):
    payload = _payload(tmp_path)
    names = _unimatrix_scripts()
    assert names
    assert [n for n in names if not (payload / "bin" / n).is_file()] == []


def test_absent_script_is_still_reported(tmp_path):
    payload = _payload(tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "INSTALL.md").write_text("Run `<plugin-root>/bin/unimatrix-no-such-script.py`.\n", encoding="utf-8")
    assert find_missing_doc_refs(docs, payload) == [("INSTALL.md", "bin/unimatrix-no-such-script.py")]
