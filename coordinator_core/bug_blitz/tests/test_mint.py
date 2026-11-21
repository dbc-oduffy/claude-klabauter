"""Tests for bug_blitz.mint: one row per file, severity, idempotence, reuse of an open tf row."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.bug_blitz.contract import FailingTest, run_tag
from coordinator_core.bug_blitz.mint import mint_tf_rows


@pytest.fixture(autouse=True)
def _no_output_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)


def _rows(root: Path) -> dict[str, dict]:
    out = {}
    for p in sorted((root / "state" / "bug-backlog").glob("*.yaml")):
        out[p.stem] = yaml.safe_load(p.read_text(encoding="utf-8"))
    return out


def _tests() -> tuple[FailingTest, FailingTest, FailingTest]:
    return (
        FailingTest(file="a/test_x.py", name="test_one", message="AssertionError: boom\nmore"),
        FailingTest(file="a/test_x.py", name="test_two", message="ValueError"),
        FailingTest(file="b/test_y.py", name="test_three", message="Segmentation fault: SIGSEGV"),
    )


def test_one_row_per_target_with_fields(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="redset.txt")
    rows = _rows(tmp_path)
    assert len(ids) == 3 and set(ids) == set(rows)
    by_title = {r["title"]: r for r in rows.values()}
    a, c = by_title["TF: a/test_x.py::test_one"], by_title["TF: b/test_y.py::test_three"]
    assert a["surface"] == "a/test_x.py" and a["severity"] == "P1" and c["severity"] == "P0"
    assert a["tags"] == ["tf", run_tag("r1")]
    assert a["evidence"] == "redset.txt"
    assert a["status"] == "open"
    assert "test_one :: AssertionError: boom" in a["body"]
    assert "more" not in a["body"] and "test_two" not in a["body"]


def _six_targets() -> tuple[FailingTest, FailingTest, FailingTest, FailingTest, FailingTest, FailingTest]:
    return (
        FailingTest(file="a/test_x.py", name="TestX::test_one", message="boom"),
        FailingTest(file="a/test_x.py", name="TestX::test_two", message="boom"),
        FailingTest(file="a/test_x.py", name="test_plain", message="boom"),
        FailingTest(file="b/test_y.py", name="TestY::test_one", message="boom"),
        FailingTest(file="b/test_y.py", name="TestY::test_two", message="boom"),
        FailingTest(file="c/test_z.py", name="test_only", message="boom"),
    )


def test_six_targets_in_three_files_mint_six_rows_and_rerun_reuses_them(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _six_targets(), target_ref="t")
    assert len(ids) == 6 == len(set(ids)) == len(_rows(tmp_path))
    before = _snapshot(tmp_path)
    assert mint_tf_rows(tmp_path, "r1", _six_targets(), target_ref="t") == ids
    assert _snapshot(tmp_path) == before


def test_duplicate_node_id_collapses_to_one_row(tmp_path: Path) -> None:
    dup = _six_targets()[:1] * 2
    assert len(mint_tf_rows(tmp_path, "r1", dup, target_ref="t")) == 1


def test_archived_row_suppresses_only_its_own_target(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _six_targets()[:2], target_ref="t")
    _archive_first_row(tmp_path, ids[0])
    again = mint_tf_rows(tmp_path, "r2", _six_targets()[:2], target_ref="t")
    assert again[0] == ids[0] and again[1] == ids[1]
    assert len(_rows(tmp_path)) == 1


def test_file_level_target_renders_as_bare_file_never_trailing_colons(tmp_path: Path) -> None:
    mint_tf_rows(tmp_path, "r1", (FailingTest(file="a/test_x.py", name=""),), target_ref="redset.txt")
    body = next(iter(_rows(tmp_path).values()))["body"]
    assert "- a/test_x.py" in body
    assert "::" not in body


def test_evidence_is_repo_relative_for_absolute_target_under_root(tmp_path: Path) -> None:
    target = tmp_path / "scratch" / "redset.txt"
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref=str(target))
    assert {r["evidence"] for r in _rows(tmp_path).values()} == {"scratch/redset.txt"}


def test_evidence_outside_root_stays_as_given(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    outside = str(tmp_path / "other" / "redset.txt")
    mint_tf_rows(root, "r1", _tests(), target_ref=outside)
    assert {r["evidence"] for r in _rows(root).values()} == {outside}


def test_rerun_same_run_is_idempotent(tmp_path: Path) -> None:
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _rows(tmp_path)
    second = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert second == first
    assert _rows(tmp_path) == before


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in (root / "state" / "bug-backlog").glob("*.yaml")}


def test_new_run_reuses_open_row_in_place(tmp_path: Path) -> None:
    first = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _snapshot(tmp_path)
    second = mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t")
    after = _snapshot(tmp_path)
    assert second == first
    assert set(after) == set(before)
    added = f"  - {run_tag('r2')}\n".encode()
    for name, data in before.items():
        assert b"system:" in after[name]
        assert after[name].replace(added, b"", 1) == data
        assert after[name].count(added) == 1


def test_same_run_recall_writes_and_declares_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    before = _snapshot(tmp_path)
    calls = []
    monkeypatch.setattr("coordinator_core.bug_blitz.mint.declare_write", calls.append)
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert _snapshot(tmp_path) == before and calls == []


def test_run_dir_manifest_marks_new_rows_created_and_reused_rows_not(tmp_path: Path) -> None:
    from coordinator_core.ops.dispatch_emit.minted_records import read

    first_dir, second_dir = tmp_path / "run1", tmp_path / "run2"
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t", run_dir=first_dir)
    paths = {f"state/bug-backlog/{i}.yaml" for i in ids}
    assert {(e["path"], e["created"]) for e in read(first_dir)} == {(p, True) for p in paths}
    mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t", run_dir=second_dir)
    assert {(e["path"], e["created"]) for e in read(second_dir)} == {(p, False) for p in paths}


def test_reused_row_is_on_the_manifest_when_its_tag_write_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.ops.dispatch_emit.minted_records import read

    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")

    def boom(*_a):
        raise OSError("disk")

    monkeypatch.setattr("coordinator_core.bug_blitz.mint._write_reuse", boom)
    run_dir = tmp_path / "run2"
    with pytest.raises(OSError):
        mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t", run_dir=run_dir)
    assert [(e["path"], e["created"]) for e in read(run_dir)] == [(f"state/bug-backlog/{ids[0]}.yaml", False)]


def test_no_run_dir_records_nothing(tmp_path: Path) -> None:
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    assert not list(tmp_path.rglob("minted.jsonl"))


def test_reuse_declares_the_edited_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    calls = []
    monkeypatch.setattr("coordinator_core.bug_blitz.mint.declare_write", calls.append)
    mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert calls == [f"state/bug-backlog/{ids[0]}.yaml"]


def test_flow_style_tags_raise_and_leave_file_unchanged(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    path = tmp_path / "state" / "bug-backlog" / f"{ids[0]}.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"tags:\n  - tf\n  - {run_tag('r1')}\n", f"tags: [tf, {run_tag('r1')}]\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")
    before = path.read_bytes()
    with pytest.raises(ValueError, match=ids[0]):
        mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert path.read_bytes() == before


def _flow_style(root: Path, row_id: str, run: str) -> None:
    path = root / "state" / "bug-backlog" / f"{row_id}.yaml"
    text = path.read_text(encoding="utf-8").replace(
        f"tags:\n  - tf\n  - {run_tag(run)}\n", f"tags: [tf, {run_tag(run)}]\n"
    )
    path.write_text(text, encoding="utf-8", newline="\n")


def _third() -> tuple[FailingTest]:
    return (FailingTest(file="c/test_z.py", name="test_four", message="boom"),)


def test_bad_second_row_aborts_before_any_write(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    _flow_style(tmp_path, ids[1], "r1")
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match=ids[1]):
        mint_tf_rows(tmp_path, "r2", _tests() + _third(), target_ref="t")
    assert _snapshot(tmp_path) == before


def test_two_flow_style_rows_one_error_names_both(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t")
    for row_id in ids:
        _flow_style(tmp_path, row_id, "r1")
    with pytest.raises(ValueError) as info:
        mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t")
    assert ids[0] in str(info.value) and ids[1] in str(info.value)


def test_write_failure_leaves_no_tmp_and_row_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    before = _snapshot(tmp_path)

    def boom(src, dst):
        raise OSError("disk")

    monkeypatch.setattr("coordinator_core.bug_blitz.mint.os.replace", boom)
    with pytest.raises(OSError):
        mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert list((tmp_path / "state" / "bug-backlog").glob("*.tmp")) == []
    assert _snapshot(tmp_path) == before


def test_reused_row_still_loads_with_both_run_tags(tmp_path: Path) -> None:
    from coordinator_core.ops.queue_family import load_family_records

    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    recs = load_family_records("bug-backlog", tmp_path, where="status=open")
    assert [r["path"].rsplit("/", 1)[-1] for r in recs] == [f"{ids[0]}.yaml"]
    tags = recs[0]["frontmatter"]["tags"]
    assert run_tag("r1") in tags and run_tag("r2") in tags


def test_closed_row_is_not_reused(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    path = tmp_path / "state" / "bug-backlog" / f"{ids[0]}.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("status: open", "status: closed"), encoding="utf-8")
    again = mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert again != ids
    assert len(_rows(tmp_path)) == 2


def _archive_first_row(root: Path, row_id: str) -> Path:
    src = root / "state" / "bug-backlog" / f"{row_id}.yaml"
    dest = root / "archive" / "bug-backlog" / "2026-11" / src.name
    dest.parent.mkdir(parents=True)
    dest.write_text(src.read_text(encoding="utf-8").replace("status: open", "status: closed-fixed"), encoding="utf-8")
    src.unlink()
    from coordinator_core.bug_blitz.archive_index import backfill

    backfill(root)
    return dest


def test_archived_tf_row_for_the_surface_is_reused_not_reminted(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    dest = _archive_first_row(tmp_path, ids[0])
    before = dest.read_bytes()
    again = mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")
    assert again == ids
    assert _rows(tmp_path) == {}
    assert dest.read_bytes() == before


def test_archived_row_on_another_surface_does_not_suppress_a_new_row(tmp_path: Path) -> None:
    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    _archive_first_row(tmp_path, ids[0])
    other = mint_tf_rows(tmp_path, "r2", _tests()[2:], target_ref="t")
    assert other != ids
    assert len(_rows(tmp_path)) == 1


def test_archive_lookup_parses_only_rows_on_the_targets_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.bug_blitz import mint as mint_mod

    prefix = "pkg/with/a/long/shared/path/prefix/tests"
    wanted = FailingTest(file=f"{prefix}/test_want.py", name="test_one", message="boom")
    archive = tmp_path / "archive" / "bug-backlog" / "2026-10"
    archive.mkdir(parents=True)

    def archived(test: FailingTest, digest: str) -> None:
        title = f"TF: {test.file}::{test.name}"
        slug = mint_mod._slug_from_title(title)
        (archive / f"2026-10-01-{slug}-{digest}.yaml").write_text(
            f'title: "{title}"\nsurface: {test.file}\ntags:\n  - tf\nstatus: closed-fixed\n', encoding="utf-8"
        )

    for i in range(30):
        archived(FailingTest(file=f"{prefix}/test_other{i}.py", name="test_one"), f"{i:012x}")
    archived(wanted, "f" * 12)
    mint_mod.archive_index.backfill(tmp_path)

    parsed: list[int] = []
    real_load = mint_mod._load_tf_candidate
    monkeypatch.setattr(mint_mod, "_load_tf_candidate", lambda text: parsed.append(1) or real_load(text))

    ids = mint_tf_rows(tmp_path, "r1", (wanted,), target_ref="t")
    assert len(ids) == 1 and ids[0].endswith("f" * 12)
    assert not _rows(tmp_path), "the archived row is reused, nothing is minted"
    assert len(parsed) <= 1, f"{len(parsed)} archived rows parsed for one target"

    fresh = FailingTest(file=f"{prefix}/test_fresh.py", name="test_one", message="boom")
    assert mint_tf_rows(tmp_path, "r2", (fresh,), target_ref="t")[0] not in ids
    assert len(_rows(tmp_path)) == 1


def test_submit_reports_missing_archive_index_without_reading_yaml(tmp_path, monkeypatch, caplog):
    import logging

    from coordinator_core.bug_blitz import mint as mint_mod

    title = "TF: pkg/test_a.py::test_one"
    month = tmp_path / "archive/bug-backlog/2026-10"
    month.mkdir(parents=True)
    row = month / f"2026-10-01-{mint_mod._slug_from_title(title)}-a.yaml"
    row.write_text(f'title: "{title}"\nsurface: pkg/test_a.py\ntags: [tf]\n', encoding="utf-8")

    def forbidden(*args, **kwargs):
        pytest.fail("submit opened an unindexed archive row")

    monkeypatch.setattr(Path, "read_text", forbidden)
    with caplog.at_level(logging.INFO, logger=mint_mod.__name__):
        assert mint_mod._archived_tf_row_ids(tmp_path, {title: "pkg/test_a.py"}) == {}
    assert "Skipped 1 unindexed bug archive rows" in caplog.text


def test_empty_tests_mints_nothing(tmp_path: Path) -> None:
    assert mint_tf_rows(tmp_path, "r1", (), target_ref="t") == []


def test_two_run_ids_minting_concurrently_yield_one_row_per_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    import time

    from coordinator_core.bug_blitz import mint as mint_mod

    real_append = mint_mod._append

    def slow_append(*args, **kwargs) -> str:
        time.sleep(0.15)
        return real_append(*args, **kwargs)

    monkeypatch.setattr(mint_mod, "_append", slow_append)
    results: dict[str, list[str]] = {}

    def run(run_id: str) -> None:
        results[run_id] = mint_tf_rows(tmp_path, run_id, _tests(), target_ref="t")

    first = threading.Thread(target=run, args=("190644Z",))
    second = threading.Thread(target=run, args=("190655Z",))
    first.start()
    time.sleep(0.05)
    second.start()
    first.join()
    second.join()

    assert results["190644Z"] == results["190655Z"]
    assert len(_rows(tmp_path)) == len(_tests())
    for row in _rows(tmp_path).values():
        assert {run_tag("190644Z"), run_tag("190655Z")} <= set(row["tags"])


def test_mint_lock_is_the_repo_queue_dir_not_the_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinator_core.bug_blitz import mint as mint_mod

    targets: list[Path] = []
    real = mint_mod.held_lock

    def spy(target, **kwargs):
        targets.append(Path(target))
        return real(target, **kwargs)

    monkeypatch.setattr(mint_mod, "held_lock", spy)
    mint_tf_rows(tmp_path, "r1", _tests(), target_ref="t", run_dir=tmp_path / "scratch" / "r1")
    mint_tf_rows(tmp_path, "r2", _tests(), target_ref="t", run_dir=tmp_path / "scratch" / "r2")
    assert targets == [tmp_path / "state" / "bug-backlog"] * 2


def test_mint_reads_only_the_queue_rows_whose_slug_matches_a_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import io

    ids = mint_tf_rows(tmp_path, "r1", _tests()[:1], target_ref="t")
    queue = tmp_path / "state" / "bug-backlog"
    for i in range(200):
        (queue / f"2026-01-01-tf-unrelated-{i}-{i:012x}.yaml").write_text(
            f'title: "TF: other/test_{i}.py::test_one"\nstatus: open\ntags:\n  - tf\n', encoding="utf-8"
        )

    reads: list[str] = []
    real_open = io.open

    def counting_open(file, mode="r", *args, **kwargs):
        if "r" in mode and str(queue) in str(file):
            reads.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    monkeypatch.setattr(io, "open", counting_open)
    again = mint_tf_rows(tmp_path, "r2", _tests()[:1], target_ref="t")

    assert again == ids
    assert len(reads) <= 2, f"{len(reads)} queue rows read for one target: {reads[:3]}"


def test_a_crlf_row_gains_the_tag_and_keeps_its_line_endings() -> None:
    from coordinator_core.bug_blitz.mint import _with_tag

    text = "id: r\r\ntags:\r\n  - tf\r\n  - bug-blitz-old\r\nstatus: open\r\n"
    out = _with_tag(text, "bug-blitz-new", "r.yaml")
    assert out == "id: r\r\ntags:\r\n  - tf\r\n  - bug-blitz-old\r\n  - bug-blitz-new\r\nstatus: open\r\n"
