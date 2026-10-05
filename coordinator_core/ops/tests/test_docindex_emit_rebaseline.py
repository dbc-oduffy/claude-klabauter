import asyncio
import subprocess

from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.docindex.render import OPEN_SENTINEL
from coordinator_core.ops.docindex_emit import _docindex_emit
import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_INDEX = (
    "---\nindex_source_dir: entries/\nentry_kind: wiki-entry\n"
    "entry_fields:\n  - {field: system, label: System}\n---\n\n# Index\n\n"
    f"{OPEN_SENTINEL}\n| System |\n|---|\n| typed by hand |\n"
    "<!-- /GENERATED docindex sha256:" + "0" * 64 + " -->\n"
)


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, **no_console_creationflags())
    (tmp_path / "entries").mkdir()
    (tmp_path / "entries" / "alpha.md").write_text("---\nsystem: alpha\n---\n# Alpha\n", encoding="utf-8")
    (tmp_path / "index.md").write_text(_INDEX, encoding="utf-8")
    return tmp_path


def _emit(root, **params):
    out = _docindex_emit({"target_root": str(root), "index_path": "index.md", **params}, repo_root=None)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


def test_a_hand_edit_refusal_names_the_repair_and_write_leaves_it(tmp_path):
    root = _repo(tmp_path)
    (result,) = _emit(root, write=True)["results"]
    assert result["hand_edit"] and not result["written"]
    assert "--rebaseline" in result["repair"]
    assert "typed by hand" in (root / "index.md").read_text(encoding="utf-8")


def test_rebaseline_rewrites_the_region_from_source(tmp_path):
    root = _repo(tmp_path)
    (result,) = _emit(root, rebaseline=True)["results"]
    assert result["written"] and result["rebaselined"]
    text = (root / "index.md").read_text(encoding="utf-8")
    assert "typed by hand" not in text and "alpha" in text
    (again,) = _emit(root)["results"]
    assert not again["hand_edit"]
