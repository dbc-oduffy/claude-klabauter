"""ask.stage records write_baseline for each dispatched row's declared file writes."""

from __future__ import annotations

import hashlib
import json
import subprocess

import pytest
import yaml

from coordinator_core.git.git_objects import write_object
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


EXTRA_BYTES = b"base content\n"
BASE_EXTRA = blob_sha(EXTRA_BYTES)


def _oid(kind: bytes, payload: bytes) -> str:
    return hashlib.sha1(kind + b" %d\0" % len(payload) + payload).hexdigest()


_PKG_TREE = b"100644 extra.py\0" + bytes.fromhex(BASE_EXTRA)
_ROOT_TREE = b"40000 pkg\0" + bytes.fromhex(_oid(b"tree", _PKG_TREE))
_COMMIT = b"tree " + _oid(b"tree", _ROOT_TREE).encode() + b"\n\nbase\n"
BASE = _oid(b"commit", _COMMIT)


@pytest.fixture
def base_repo(tmp_path):
    """A `.git` holding commit `BASE` whose tree holds only pkg/extra.py, written in process."""
    gitdir = tmp_path / ".git"
    (gitdir / "objects").mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8", newline="\n")
    for kind, payload in ((b"blob", EXTRA_BYTES), (b"tree", _PKG_TREE), (b"tree", _ROOT_TREE), (b"commit", _COMMIT)):
        write_object(gitdir, kind, payload)
    return tmp_path


def test_refire_baseline_is_the_halted_run_base_not_the_live_tree(tmp_path, base_repo):
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


def test_refire_without_a_usable_manifest_baselines_at_the_run_base(tmp_path, base_repo):
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


@pytest.fixture(autouse=True)
def _hermetic_git_config(tmp_path, monkeypatch):
    """No system, global or env-injected git config: the zero-spawn path is read against a known stack."""
    empty = tmp_path.parent / f"{tmp_path.name}-empty-gitconfig"
    empty.write_text("", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)


@pytest.fixture
def git_spawns(monkeypatch):
    """Counts `run_git` spawns made by `ask_stage`; any other process start fails the test."""
    from coordinator_core.git import run as git_run

    real = git_run.run_git
    calls: list[list[str]] = []

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", counting)
    return calls


def test_refire_baseline_converts_in_process_with_zero_spawns(base_repo, git_spawns, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("process spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert ask_stage._run_base_blobs(base_repo, BASE, ["pkg/extra.py", "pkg/nope.py"]) == {
        "pkg/extra.py": BASE_EXTRA,
        "pkg/nope.py": None,
    }
    (base_repo / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8", newline="\n")
    assert ask_stage._run_base_blobs(base_repo, BASE, ["pkg/extra.py"]) == {
        "pkg/extra.py": blob_sha(b"base content\r\n")
    }
    assert git_spawns == []


def _real_repo(tmp_path, attrs=None):
    """A real git repo whose base commit holds LF files f.txt and g.txt; returns (root, base sha)."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    root = tmp_path / "real"
    root.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", "-C", str(root), *a], check=True, capture_output=True, text=True, creationflags=flags
        ).stdout.strip()

    git("init", "-q")
    if attrs:
        (root / ".gitattributes").write_text(attrs, encoding="utf-8", newline="\n")
        git("add", "--", ".gitattributes")
    for name in ("f.txt", "g.txt"):
        (root / name).write_bytes(b"a\nb\n")
        git("add", "--", name)
    git("-c", "user.email=t@t", "-c", "user.name=t", "-c", "core.autocrlf=false", "commit", "-qm", "base")
    return root, git("rev-parse", "HEAD"), git


CRLF_SHA = blob_sha(b"a\r\nb\r\n")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_plain_config_refire_baseline_spawns_nothing_on_a_real_repo(tmp_path, git_spawns):
    root, base, _ = _real_repo(tmp_path)
    assert ask_stage._run_base_blobs(root, base, ["f.txt", "g.txt"]) == {
        "f.txt": blob_sha(b"a\nb\n"),
        "g.txt": blob_sha(b"a\nb\n"),
    }
    assert git_spawns == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_eol_crlf_attribute_converts_in_process_on_a_real_repo(tmp_path, git_spawns):
    root, base, _ = _real_repo(tmp_path, attrs="f.txt text eol=crlf\n")
    assert ask_stage._run_base_blobs(root, base, ["f.txt", "g.txt"]) == {
        "f.txt": CRLF_SHA,
        "g.txt": blob_sha(b"a\nb\n"),
    }
    assert git_spawns == []


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_repo_autocrlf_true_takes_one_spawn_and_succeeds(tmp_path, git_spawns):
    root, base, git = _real_repo(tmp_path)
    git("config", "core.autocrlf", "true")
    assert ask_stage._run_base_blobs(root, base, ["f.txt", "g.txt"]) == {"f.txt": CRLF_SHA, "g.txt": CRLF_SHA}
    assert len(git_spawns) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_global_only_autocrlf_true_takes_the_spawn_path(tmp_path, git_spawns, monkeypatch):
    root, base, _ = _real_repo(tmp_path)
    glob = tmp_path / "global-gitconfig"
    glob.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8", newline="\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert ask_stage._run_base_blobs(root, base, ["f.txt"]) == {"f.txt": CRLF_SHA}
    assert len(git_spawns) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_env_injected_autocrlf_takes_the_spawn_path(tmp_path, git_spawns, monkeypatch):
    root, base, _ = _real_repo(tmp_path)
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.autocrlf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    assert ask_stage._run_base_blobs(root, base, ["f.txt"]) == {"f.txt": CRLF_SHA}
    assert len(git_spawns) == 1


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_filter_and_text_auto_paths_share_one_spawn_and_never_refuse(tmp_path, git_spawns):
    root, base, _ = _real_repo(tmp_path, attrs="f.txt filter=nothing\ng.txt text=auto\n")
    assert ask_stage._run_base_blobs(root, base, ["f.txt", "g.txt"]) == {
        "f.txt": blob_sha(b"a\nb\n"),
        "g.txt": blob_sha(b"a\nb\n"),
    }
    assert len(git_spawns) == 1


def test_refire_whose_run_base_cannot_be_read_refuses_by_name(tmp_path):
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


def test_refire_with_a_whitespace_path_refuses_before_spawning_git(tmp_path, base_repo, git_spawns):
    rec = _halt(tmp_path, "ws", {}, BASE)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/has space.py",), 0)]
    with pytest.raises(ask_stage.RefireBaselineError):
        ask_stage._stage_write_baseline(tmp_path, rows, rec)
    assert git_spawns == []


def test_refire_with_every_path_recorded_spawns_no_git(tmp_path, base_repo, git_spawns):
    rec = _halt(tmp_path, "full", {"pkg/a.py": None}, BASE)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/a.py",), 0)]
    assert ask_stage._stage_write_baseline(tmp_path, rows, rec) == {"pkg/a.py": None}
    assert git_spawns == []


def test_non_refire_record_still_captures_the_live_tree(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("a.py",), 0)]
    rec = ask_stage.record_dir(tmp_path, "r2")
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": {"a.py": "0" * 40}}), encoding="utf-8")
    assert ask_stage._stage_write_baseline(tmp_path, rows, rec) == {"a.py": fingerprint(tmp_path, "a.py")}
    assert ask_stage._stage_write_baseline(tmp_path, rows) == {"a.py": fingerprint(tmp_path, "a.py")}


def test_stage_of_a_halted_run_keeps_the_prior_baseline(tmp_path, base_repo):
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
