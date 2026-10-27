"""ask.stage records write_baseline for each dispatched row's declared file writes."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, fingerprint

SIZING_REL = "state/sizings/2026-10-23-fixture.yaml"


def _sizing(repo):
    doc = {"intent": "Fix pkg/a.py and pkg/new.py.", "exit_criterion": {"statement": "ok"}, "estimate": {"tshirt": "XS"}}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_stage_records_dirty_blob_and_null_for_absent(tmp_path, monkeypatch):
    _sizing(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("dirty\n", encoding="utf-8", newline="\n")

    def boom(*a, **k):
        raise AssertionError("spawn")

    real = ask_stage._write_baseline.capture

    def guarded(root, paths):
        monkeypatch.setattr(subprocess, "Popen", boom)
        try:
            return real(root, paths)
        finally:
            monkeypatch.undo()

    monkeypatch.setattr(ask_stage._write_baseline, "capture", guarded)
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py", "pkg/new.py"])
    data = json.loads(json.dumps(m.to_json()))
    assert data["write_baseline"]["pkg/a.py"] == fingerprint(tmp_path, "pkg/a.py")
    assert data["write_baseline"]["pkg/a.py"] is not None
    assert data["write_baseline"]["pkg/new.py"] is None


def test_directories_and_globs_are_never_baselined(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("d", "d/", "g/*.py", "f.py"), 0)]
    assert set(ask_stage._stage_write_baseline(tmp_path, rows)) == {"f.py"}


def test_adopting_row_paths_are_absent(tmp_path):
    (tmp_path / "s.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [
        ManifestRow("S1", "executor", "sonnet", "b.md", ("s.py",), 0, adopts_stranded=True),
        ManifestRow("C1", "executor", "sonnet", "b.md", ("c.py",), 0),
    ]
    assert ask_stage._stage_write_baseline(tmp_path, rows) == {"c.py": None}


def _halt(tmp_path, run_id, baseline, base_sha="a" * 40):
    rec = ask_stage.record_dir(tmp_path, run_id)
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "x", "run_base_sha": base_sha}), encoding="utf-8", newline="\n"
    )
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": baseline}), encoding="utf-8", newline="\n")
    return rec


BASE = "b" * 40
EXTRA_BYTES = b"base content\n"
BASE_EXTRA = blob_sha(EXTRA_BYTES)


class _FakeGit:
    """`git cat-file --batch --filters` over a commit `BASE` whose tree holds only pkg/extra.py."""

    def __init__(self, ok=True):
        self.ok = ok
        self.calls = 0

    def __call__(self, args, *, cwd=None, input=None, **kw):
        self.calls += 1
        out = b""
        for ln in input.decode().splitlines():
            if ln == BASE:
                out += f"{BASE} commit 3\n".encode() + b"abc\n"
            elif ln.startswith(f"{BASE}:pkg/extra.py "):
                out += f"{BASE_EXTRA} blob 1\n".encode() + EXTRA_BYTES + b"\n"
            else:
                out += f"{ln.split(' ')[0]} missing\n".encode()
        return SimpleNamespace(returncode=0 if self.ok else 128, stdout_bytes=out if self.ok else b"")


@pytest.fixture
def fake_git(monkeypatch):
    fake = _FakeGit()
    monkeypatch.setattr("coordinator_core.git.run.run_git", fake)
    return fake


def test_refire_baseline_is_the_halted_run_base_not_the_live_tree(tmp_path, fake_git):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("first fire output\n", encoding="utf-8", newline="\n")
    (tmp_path / "pkg" / "extra.py").write_text("live\n", encoding="utf-8", newline="\n")
    base_a = "0" * 40
    rec = _halt(tmp_path, "r1", {"pkg/a.py": base_a, "pkg/new.py": None}, BASE)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/a.py", "pkg/new.py", "pkg/extra.py"), 0)]
    got = ask_stage._stage_write_baseline(tmp_path, rows, rec)
    assert got["pkg/a.py"] == base_a
    assert got["pkg/new.py"] is None
    assert got["pkg/extra.py"] == BASE_EXTRA != fingerprint(tmp_path, "pkg/extra.py")
    # the earlier fire's output differs from that baseline, so it counts as written
    assert ask_stage._write_baseline.unwritten(tmp_path, got, ["pkg/a.py"]) == []


def test_refire_without_a_usable_manifest_baselines_at_the_run_base(tmp_path, fake_git):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "extra.py").write_text("first fire output\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/extra.py", "pkg/new.py"), 0)]
    for i, manifest in enumerate((None, "{not json", json.dumps({"rows": []}))):
        rec = _halt(tmp_path, f"m{i}", {}, BASE)
        if manifest is None:
            (rec / "manifest.json").unlink()
        else:
            (rec / "manifest.json").write_text(manifest, encoding="utf-8")
        got = ask_stage._stage_write_baseline(tmp_path, rows, rec)
        assert got == {"pkg/extra.py": BASE_EXTRA, "pkg/new.py": None}
        # the halted fire's output is written work, not unwritten
        assert ask_stage._write_baseline.unwritten(tmp_path, got, ["pkg/extra.py"]) == []


def test_refire_whose_run_base_cannot_be_read_refuses_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr("coordinator_core.git.run.run_git", _FakeGit(ok=False))
    rec = _halt(tmp_path, "bad", {}, BASE)
    (rec / "manifest.json").unlink()
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/extra.py",), 0)]
    with pytest.raises(ask_stage.RefireBaselineError):
        ask_stage._stage_write_baseline(tmp_path, rows, rec)


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_refire_baseline_is_in_the_fingerprint_domain_under_an_eol_rule(tmp_path):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def git(*a):
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "core.autocrlf=false", *a],
            check=True,
            capture_output=True,
            creationflags=flags,
        )

    git("init", "-q")
    (tmp_path / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8", newline="\n")
    (tmp_path / "f.txt").write_text("a\nb\n", encoding="utf-8", newline="\n")
    git("add", "--", ".gitattributes", "f.txt")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        creationflags=flags,
    ).stdout.strip()
    (tmp_path / "f.txt").write_bytes(b"a\r\nb\r\n")  # the checkout-converted bytes of the committed blob
    (tmp_path / "g.txt").write_text("late\n", encoding="utf-8", newline="\n")
    rec = _halt(tmp_path, "eol", {}, base)
    (rec / "manifest.json").unlink()
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("f.txt", "g.txt"), 0)]
    got = ask_stage._stage_write_baseline(tmp_path, rows, rec)
    assert got == {"f.txt": fingerprint(tmp_path, "f.txt"), "g.txt": None}
    assert ask_stage._write_baseline.unwritten(tmp_path, got, ["f.txt", "g.txt"]) == ["f.txt"]


def test_refire_with_a_whitespace_path_refuses_before_spawning_git(tmp_path, fake_git):
    rec = _halt(tmp_path, "ws", {}, BASE)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/has space.py",), 0)]
    with pytest.raises(ask_stage.RefireBaselineError):
        ask_stage._stage_write_baseline(tmp_path, rows, rec)
    assert fake_git.calls == 0


def test_refire_with_every_path_recorded_spawns_no_git(tmp_path, fake_git):
    rec = _halt(tmp_path, "full", {"pkg/a.py": None}, BASE)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/a.py",), 0)]
    assert ask_stage._stage_write_baseline(tmp_path, rows, rec) == {"pkg/a.py": None}
    assert fake_git.calls == 0


def test_non_refire_record_still_captures_the_live_tree(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("a.py",), 0)]
    rec = ask_stage.record_dir(tmp_path, "r2")
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": {"a.py": "0" * 40}}), encoding="utf-8")
    assert ask_stage._stage_write_baseline(tmp_path, rows, rec) == {"a.py": fingerprint(tmp_path, "a.py")}
    assert ask_stage._stage_write_baseline(tmp_path, rows) == {"a.py": fingerprint(tmp_path, "a.py")}


def test_stage_of_a_halted_run_keeps_the_prior_baseline(tmp_path, fake_git):
    _sizing(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("first fire output\n", encoding="utf-8", newline="\n")
    _halt(tmp_path, "r3", {"pkg/a.py": "0" * 40, "pkg/new.py": None}, BASE)
    m = stage(tmp_path, run_id="r3", sizing_rel=SIZING_REL, writes=["pkg/a.py", "pkg/new.py"])
    assert m.write_baseline["pkg/a.py"] == "0" * 40
    assert m.write_baseline["pkg/new.py"] is None


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_capture_persists_the_raw_crlf_blob_and_it_matches_head(tmp_path):
    from coordinator_core.git.git_dir import resolve_git_common_dir
    from coordinator_core.git.git_objects import read_object
    from coordinator_core.ops.dispatch_emit import kept_work
    from coordinator_core.ops.dispatch_emit.tests._kept_fixture import crlf_repo, git

    root, _, _, _ = crlf_repo(tmp_path)
    rel = "docs/fresh.txt"
    (root / rel).write_bytes(b"one\n")
    git(root, "add", rel)
    git(root, "commit", "-q", "-m", "fresh")
    crlf = b"one\r\n"
    (root / rel).write_bytes(crlf)
    head_sha = git(root, "rev-parse", f"HEAD:{rel}")
    assert not (resolve_git_common_dir(root) / "objects" / blob_sha(crlf)[:2] / blob_sha(crlf)[2:]).exists()

    got = ask_stage._write_baseline.capture(root, [rel, "docs/absent.txt"])

    assert got == {rel: blob_sha(crlf), "docs/absent.txt": None}
    assert read_object(resolve_git_common_dir(root), got[rel]) == ("blob", crlf)
    (root / rel).unlink()
    assert kept_work.matches_committed(root, rel, got[rel], head_sha)


def test_capture_survives_an_unwritable_object_store(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8", newline="\n")

    def deny(*a, **k):
        raise PermissionError("denied")

    monkeypatch.setattr("coordinator_core.git.git_objects.write_object", deny)
    assert ask_stage._write_baseline.capture(tmp_path, ["a.py"]) == {"a.py": fingerprint(tmp_path, "a.py")}


def test_run_base_blobs_persist_each_segment_body(tmp_path, fake_git):
    from coordinator_core.git.git_objects import read_object

    (tmp_path / ".git").mkdir()
    got = ask_stage._run_base_blobs(tmp_path, BASE, ["pkg/extra.py", "pkg/gone.py"])
    assert got == {"pkg/extra.py": BASE_EXTRA, "pkg/gone.py": None}
    assert read_object(tmp_path / ".git", BASE_EXTRA) == ("blob", EXTRA_BYTES)


def test_adopts_stranded_flag_is_deterministic():
    f = ask_stage._adopts_stranded
    assert f({"authorization": {"source": "em-inferred", "rationale": "the stranded 9 files"}})
    assert not f({"authorization": {"source": "em-inferred", "rationale": "other"}})
    assert not f({"authorization": {"source": "pm", "rationale": "stranded"}})
    assert not f({"title": "adopt stranded work"})
    assert not f(None)
