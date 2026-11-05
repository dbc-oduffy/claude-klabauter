"""review.reachability `pipeline-output` entries: a literal repo-relative path a script writes."""

from __future__ import annotations

import pytest

from coordinator_core.git.run import run_git
from coordinator_core.ops.review_reachability import _ALL_ENTRY_KINDS, _handler
from coordinator_core.ops.tests._reachability_fixture import _git, _write

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

OUT = "state/census/report.json"
PRODUCER = "tools/census.py"


def _repo(tmp_path, base_files: dict[str, str]):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _write(root, "README.md", "# repo\n")
    for rel, text in base_files.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root, run_git(["-C", str(root), "rev-parse", "HEAD"]).stdout.strip()


def _run(root, base, producer_text, **extra):
    _write(root, PRODUCER, producer_text)
    params = {"repo_root": str(root), "base_sha": base, "worktree": True, "paths": [PRODUCER], **extra}
    res = _handler(params)
    assert res["error"] is None
    return {e["ref"]: e["verdict"] for e in res["entries"] if e["kind"] == "pipeline-output"}


WRITE_TEXT = f'from pathlib import Path\n\n\ndef run():\n    Path("{OUT}").write_text("x")\n'


def test_kind_is_a_default_entry_kind():
    assert "pipeline-output" in _ALL_ENTRY_KINDS


def test_producer_without_consumer_is_unreachable(tmp_path):
    root, base = _repo(tmp_path, {"docs/notes.md": "nothing here\n"})
    assert _run(root, base, WRITE_TEXT) == {f"{PRODUCER}:{OUT}": "unreachable"}


def test_consumer_elsewhere_is_reachable(tmp_path):
    root, base = _repo(tmp_path, {"tools/read.py": f'DATA = "{OUT}"\n'})
    assert _run(root, base, WRITE_TEXT) == {f"{PRODUCER}:{OUT}": "reachable"}


def test_doc_consumer_counts(tmp_path):
    root, base = _repo(tmp_path, {"docs/guide.md": f"Read `{OUT}` after the run.\n"})
    assert _run(root, base, WRITE_TEXT) == {f"{PRODUCER}:{OUT}": "reachable"}


def test_test_file_mention_is_not_a_consumer(tmp_path):
    root, base = _repo(tmp_path, {"tests/test_census.py": f'assert "{OUT}"\n'})
    assert _run(root, base, WRITE_TEXT) == {f"{PRODUCER}:{OUT}": "unreachable"}


def test_open_write_and_append_are_producers(tmp_path):
    root, base = _repo(tmp_path, {})
    text = 'def run():\n    open("out/a.txt", "w").write("x")\n    open("out/b.txt", mode="ab").write(b"x")\n'
    assert _run(root, base, text) == {
        f"{PRODUCER}:out/a.txt": "unreachable",
        f"{PRODUCER}:out/b.txt": "unreachable",
    }


def test_json_dump_target_is_a_producer(tmp_path):
    root, base = _repo(tmp_path, {})
    text = 'import json\n\n\ndef run(d):\n    json.dump(d, open("out/c.json", "w"))\n'
    assert _run(root, base, text) == {f"{PRODUCER}:out/c.json": "unreachable"}


def test_read_mode_open_is_not_a_producer(tmp_path):
    root, base = _repo(tmp_path, {})
    assert _run(root, base, 'def run():\n    return open("out/a.txt", "r").read()\n') == {}


def test_fstring_and_variable_paths_are_undecidable_never_unreachable(tmp_path):
    root, base = _repo(tmp_path, {})
    fstr = 'from pathlib import Path\n\n\ndef run(d):\n    Path(f"out/{d}/a.json").write_text("x")\n'
    assert _run(root, base, fstr) == {f"{PRODUCER}:<dynamic path>": "undecidable"}
    var = 'def run(p):\n    p.write_text("x")\n'
    assert _run(root, base, var) == {f"{PRODUCER}:<dynamic path>": "undecidable"}


def test_non_repo_relative_literals_are_not_pipeline_outputs(tmp_path):
    root, base = _repo(tmp_path, {})
    text = 'from pathlib import Path\n\n\ndef run():\n    Path("/var/out/a.json").write_text("x")\n    Path("a.json").write_text("x")\n'
    got = _run(root, base, text)
    assert all(v == "undecidable" for v in got.values())
    assert not any(k.endswith("a.json") for k in got)


def test_same_file_second_use_and_basename_elsewhere_are_undecidable(tmp_path):
    root, base = _repo(tmp_path, {})
    again = WRITE_TEXT + f'\n\ndef load():\n    return open("{OUT}").read()\n'
    assert _run(root, base, again) == {f"{PRODUCER}:{OUT}": "undecidable"}
    root2, base2 = _repo(tmp_path / "b", {"docs/x.md": "see report.json for detail\n"})
    assert _run(root2, base2, WRITE_TEXT) == {f"{PRODUCER}:{OUT}": "undecidable"}


def test_entry_kinds_filter_excludes_the_kind(tmp_path):
    root, base = _repo(tmp_path, {})
    assert _run(root, base, WRITE_TEXT, entry_kinds=["exported-function"]) == {}
