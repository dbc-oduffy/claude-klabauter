"""coordinator_core.ops.tests.test_grind_ops — scoped tests for the
closed queue-grind op list (C9): `lessons.extract`, `lessons.verify_extraction`
and `doctrine.surface_split_regenerate`.

Covers:
    - every name in the vocabulary's `SOURCE_OPS | VERIFY_OPS | REGENERATE_OPS`
      resolves to a registered op (`_registry_map.OP_MODULE_MAP` +
      `coordinator_core.ipc.resolves`);
    - `lessons.verify_extraction` honours the exit/JSON contract on a pass
      fixture and a fail fixture (`{ok, failing_ids}`);
    - no op spawns a subprocess (`subprocess.run`/`Popen` patched to raise).

Async invocation follows the house convention (test_cutover_advance.py):
plain sync test functions wrapping the async handler in `asyncio.run(...)`.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § C9
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from coordinator_core.contract.grind_vocab import (
    REGENERATE_OPS,
    SOURCE_OPS,
    VERIFY_OPS,
)
from coordinator_core.ops import grind_ops
from coordinator_core.ops._registry_map import OP_MODULE_MAP


def _no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args, **kwargs):
        raise AssertionError("grind_ops must never spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def test_vocab_ops_all_registered():
    for name in SOURCE_OPS | VERIFY_OPS | REGENERATE_OPS:
        assert name in OP_MODULE_MAP, f"{name} is not registered in OP_MODULE_MAP"
        assert OP_MODULE_MAP[name] == "coordinator_core.ops.grind_ops"


def _write_lesson(lessons_dir: Path, name: str, *, title: str, body: str, created: str) -> None:
    lessons_dir.mkdir(parents=True, exist_ok=True)
    (lessons_dir / name).write_text(
        f"created: {created}\nscope: universal\ntitle: {title}\nbody: {body}\n",
        encoding="utf-8",
    )


def test_lessons_extract_no_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _no_spawn(monkeypatch)
    lessons_dir = tmp_path / "state" / "lessons"
    _write_lesson(
        lessons_dir,
        "2026-01-01-first.yaml",
        title="First lesson title text",
        body="First lesson body",
        created="2026-01-01",
    )

    result = asyncio.run(
        grind_ops._lessons_extract(
            {"lessons_dir": str(lessons_dir), "shortname": "proj"}, tmp_path
        )
    )
    assert result["exit_code"] == 0
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["id"] == "proj-L1"
    assert record["title"] == "First lesson title text"


def test_lessons_verify_extraction_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _no_spawn(monkeypatch)
    lessons_dir = tmp_path / "state" / "lessons"
    _write_lesson(
        lessons_dir,
        "2026-01-01-first.yaml",
        title="First lesson title text",
        body="First lesson body",
        created="2026-01-01",
    )
    extraction = asyncio.run(
        grind_ops._lessons_extract(
            {"lessons_dir": str(lessons_dir), "shortname": "proj"}, tmp_path
        )
    )
    manifest_path = tmp_path / "extraction.json"
    import json

    manifest_path.write_text(
        json.dumps({"records": extraction["records"]}), encoding="utf-8"
    )

    passing_records = [
        {
            "id": "proj-L1",
            "source": extraction["records"][0]["source"],
            "summary": "First lesson title text",
        }
    ]
    result = asyncio.run(
        grind_ops._lessons_verify_extraction(
            {"manifest": str(manifest_path), "records": passing_records}, tmp_path
        )
    )
    assert result == {"ok": True, "failing_ids": []}


def test_lessons_verify_extraction_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _no_spawn(monkeypatch)
    lessons_dir = tmp_path / "state" / "lessons"
    _write_lesson(
        lessons_dir,
        "2026-01-01-first.yaml",
        title="First lesson title text",
        body="First lesson body",
        created="2026-01-01",
    )
    extraction = asyncio.run(
        grind_ops._lessons_extract(
            {"lessons_dir": str(lessons_dir), "shortname": "proj"}, tmp_path
        )
    )
    manifest_path = tmp_path / "extraction.json"
    import json

    manifest_path.write_text(
        json.dumps({"records": extraction["records"]}), encoding="utf-8"
    )

    failing_records = [
        {
            "id": "proj-L99",
            "source": "state/lessons/2026-01-01-first.yaml:99",
            "summary": "Fabricated entry that does not exist",
        }
    ]
    result = asyncio.run(
        grind_ops._lessons_verify_extraction(
            {"manifest": str(manifest_path), "records": failing_records}, tmp_path
        )
    )
    assert result["ok"] is False
    assert result["failing_ids"] == ["proj-L99"]


def test_lessons_verify_extraction_missing_manifest_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_spawn(monkeypatch)
    with pytest.raises(grind_ops.VerifyRefusalError):
        asyncio.run(
            grind_ops._lessons_verify_extraction(
                {"manifest": str(tmp_path / "never-existed.json"), "records": []},
                tmp_path,
            )
        )


def test_lessons_verify_extraction_bad_input_exit_is_a_refusal_not_a_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_spawn(monkeypatch)
    # An empty extraction DIRECTORY with no `*-extracted-full.{yaml,json}`
    # inside is `verify()`'s own exit-2 bad-input case (§ module docstring
    # `_discover_extractions`/"no extractions found") -- distinct from a
    # grounding failure (exit 1), which this adapter must never conflate
    # with `ok=False`.
    empty_extraction_dir = tmp_path / "extractions"
    empty_extraction_dir.mkdir()
    with pytest.raises(grind_ops.VerifyRefusalError):
        asyncio.run(
            grind_ops._lessons_verify_extraction(
                {
                    "manifest": str(empty_extraction_dir),
                    "records": [{"id": "proj-L1", "source": "", "summary": "x"}],
                },
                tmp_path,
            )
        )


def test_doctrine_surface_split_regenerate_no_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_spawn(monkeypatch)
    from coordinator_core.ops.grind_ops import _load

    split_module = _load("generate-doctrine-surface-split")

    split_dir = tmp_path / "some-doc"
    source_text = "# Some Doc\n\n## Section One\n\nBody one.\n\n## Section Two\n\nBody two.\n"
    build = split_module.build_split("some-doc", source_text, "some-doc")
    files = split_module.render_files(build)
    split_dir.mkdir(parents=True)
    for filename, content in files.items():
        (split_dir / filename).write_text(content, encoding="utf-8", newline="\n")
    (split_dir / split_module.PREAMBLE_BASENAME).write_text(
        build["preamble"], encoding="utf-8", newline="\n"
    )

    result = asyncio.run(
        grind_ops._doctrine_surface_split_regenerate(
            {"split_dir": str(split_dir), "allow_dirty": True}, tmp_path
        )
    )
    assert result == {"exit_code": 0}

    # Idempotent: a second regenerate against the now-refreshed README.md
    # is a no-drift no-op under check_mode.
    result = asyncio.run(
        grind_ops._doctrine_surface_split_regenerate(
            {"split_dir": str(split_dir), "check_mode": True}, tmp_path
        )
    )
    assert result == {"exit_code": 0}


def test_doctrine_surface_split_regenerate_default_path_spawns_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The default call (both `check_mode` and `allow_dirty` omitted) is the
    ONE branch that is not spawn-free: `regenerate_split_dir()` ->
    `dirty_bodies()` -> `git_native._git(["status", "--porcelain", ...])`,
    a real `subprocess.run`. Review-confirmed load-bearing (§ module
    docstring negative-spec) -- this pins the count at exactly 1 rather than
    asserting zero, so a regression either direction (a second spawn, or the
    dirty-check silently dropped) fails this test."""
    from coordinator_core.ops.grind_ops import _load

    split_module = _load("generate-doctrine-surface-split")

    split_dir = tmp_path / "some-doc"
    source_text = "# Some Doc\n\n## Section One\n\nBody one.\n\n## Section Two\n\nBody two.\n"
    build = split_module.build_split("some-doc", source_text, "some-doc")
    files = split_module.render_files(build)
    split_dir.mkdir(parents=True)
    for filename, content in files.items():
        (split_dir / filename).write_text(content, encoding="utf-8", newline="\n")
    (split_dir / split_module.PREAMBLE_BASENAME).write_text(
        build["preamble"], encoding="utf-8", newline="\n"
    )

    spawn_count = 0
    real_run = subprocess.run

    def _counting_run(*args, **kwargs):
        nonlocal spawn_count
        spawn_count += 1
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _counting_run)

    result = asyncio.run(
        grind_ops._doctrine_surface_split_regenerate(
            {"split_dir": str(split_dir)}, tmp_path
        )
    )
    assert result == {"exit_code": 0}
    assert spawn_count == 1
