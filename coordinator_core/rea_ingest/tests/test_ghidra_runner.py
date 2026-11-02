"""ghidra_runner: argv shape, permissive defaults vs --confine, refusals and the end-to-end record."""
import copy
import json
import signal
import subprocess
from pathlib import Path

import pytest

from coordinator_core.rea_ingest import ghidra_runner as gr

SAMPLE = json.loads((Path(__file__).resolve().parent / "fixtures" / "sample-export.json").read_text(encoding="utf-8"))


def _install(tmp_path, version="11.2.1", windows=False):
    root = tmp_path / "ghidra"
    (root / "Ghidra").mkdir(parents=True)
    (root / "Ghidra" / "application.properties").write_text(f"application.version={version}\n", encoding="utf-8")
    (root / "support").mkdir()
    (root / "support" / ("analyzeHeadless.bat" if windows else "analyzeHeadless")).write_text("", encoding="utf-8")
    return root


class FakeProc:
    def __init__(self, timeout_first=False):
        self.pid = 4242
        self.returncode = 0
        self.killed = False
        self._timeout_first = timeout_first
        self._handle = 7

    def communicate(self, timeout=None):
        if self._timeout_first and not self.killed:
            raise subprocess.TimeoutExpired("x", timeout)
        return "tail", None

    def kill(self):
        self.killed = True


class FakeJob:
    def __init__(self):
        self.assigned = None
        self.closed = False

    def assign(self, proc):
        self.assigned = proc

    def close(self):
        self.closed = True


def _popen_recorder(proc):
    calls = []

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return proc

    return popen, calls


def test_argv_import_then_process(tmp_path):
    exe, proj, copy_, out = tmp_path / "ah", tmp_path / "proj", tmp_path / "prog-ab", tmp_path / "e.json"
    imp = gr.build_argv(exe, proj, copy_, out, holds=False)
    assert imp[:5] == [str(exe), str(proj), gr.PROJECT_NAME, "-import", str(copy_)]
    assert imp[-3:] == ["-postScript", gr.SCRIPT_NAME, str(out)]
    assert "-scriptPath" in imp and str(gr.SCRIPT_DIR) in imp
    proc = gr.build_argv(exe, proj, copy_, out, holds=True)
    assert proc[3:5] == ["-process", "prog-ab"] and "-import" not in proc
    for argv in (imp, proc):
        assert "-deleteProject" not in argv
        assert argv[0] == str(exe) != str(copy_)


def test_stage_names_copy_by_hash_and_is_stable(tmp_path):
    src = tmp_path / "target.bin"
    src.write_bytes(b"\x7fELF-bytes")
    c1, sha1 = gr.stage_binary(src, tmp_path / "s")
    c2, sha2 = gr.stage_binary(src, tmp_path / "s")
    assert c1 == c2 and sha1 == sha2 and c1.name == f"prog-{sha1[:16]}"
    assert c1.read_bytes() == src.read_bytes()
    with pytest.raises(gr.GhidraRefusal):
        gr.stage_binary(tmp_path / "missing", tmp_path / "s")


def test_project_holds_reads_prp_names(tmp_path):
    assert not gr.project_holds(tmp_path, "prog-1")
    idata = tmp_path / f"{gr.PROJECT_NAME}.rep" / "idata" / "00"
    idata.mkdir(parents=True)
    (idata / "~0.prp").write_bytes(b"NAME:String:prog-1\n")
    assert gr.project_holds(tmp_path, "prog-1")
    assert not gr.project_holds(tmp_path, "prog-2")


def test_resolution_refusals(tmp_path):
    with pytest.raises(gr.GhidraRefusal, match="GHIDRA_INSTALL_DIR"):
        gr.resolve_install(None, env={})
    with pytest.raises(gr.GhidraRefusal, match="not a directory"):
        gr.resolve_install(None, env={"GHIDRA_INSTALL_DIR": str(tmp_path / "nope")})
    assert gr.resolve_install(None, env={"GHIDRA_INSTALL_DIR": str(tmp_path)}) == tmp_path
    assert gr.read_version(_install(tmp_path)) == "11.2.1"
    assert gr.read_version(_install(tmp_path / "new", version="12.2")) == "12.2"
    with pytest.raises(gr.GhidraRefusal, match="need 11.x or later"):
        gr.read_version(_install(tmp_path / "old", version="10.4"))
    with pytest.raises(gr.GhidraRefusal, match="analyzeHeadless not found"):
        gr.analyze_headless_path(tmp_path / "ghidra", windows=True)
    win = _install(tmp_path / "w", windows=True)
    assert gr.analyze_headless_path(win, windows=True).name == "analyzeHeadless.bat"


def test_default_spawn_has_no_confinement():
    popen, calls = _popen_recorder(FakeProc())
    rc, tail = gr._spawn_headless(["x"], 5, popen=popen, windows=False)
    kwargs = calls[0][1]
    assert (rc, tail) == (0, "tail")
    assert "start_new_session" not in kwargs and "creationflags" not in kwargs
    popen, calls = _popen_recorder(FakeProc())
    made = []
    gr._spawn_headless(["x"], 5, popen=popen, windows=True, job_factory=lambda: made.append(1))
    assert not made and "creationflags" in calls[0][1]


def test_confine_posix_new_session_and_group_kill_on_timeout():
    popen, calls = _popen_recorder(FakeProc())
    gr._spawn_headless(["x"], 5, confine=True, popen=popen, windows=False)
    assert calls[0][1]["start_new_session"] is True
    killed = []
    popen, _ = _popen_recorder(FakeProc(timeout_first=True))
    proc = FakeProc(timeout_first=True)
    popen, _ = _popen_recorder(proc)
    with pytest.raises(gr.GhidraRefusal, match="timeout"):
        gr._spawn_headless(["x"], 1, confine=True, popen=popen, windows=False,
                           killpg=lambda pid, sig: (killed.append((pid, sig)), setattr(proc, "killed", True)))
    assert killed == [(4242, signal.SIGKILL)]


def test_confine_windows_job_object_assigned_and_closed():
    proc = FakeProc()
    popen, _ = _popen_recorder(proc)
    job = FakeJob()
    gr._spawn_headless(["x"], 5, confine=True, popen=popen, windows=True, job_factory=lambda: job)
    assert job.assigned is proc and job.closed


def test_unconfined_timeout_kills_child_and_refuses():
    proc = FakeProc(timeout_first=True)
    popen, _ = _popen_recorder(proc)
    with pytest.raises(gr.GhidraRefusal, match="timeout"):
        gr._spawn_headless(["x"], 1, popen=popen, windows=False)
    assert proc.killed


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _fake_spawn(sha, calls, rc=0, write=True):
    def spawn(argv, timeout, confine):
        calls.append((argv, timeout, confine))
        if write:
            doc = copy.deepcopy(SAMPLE)
            doc["program"]["sha256"] = sha
            Path(argv[-1]).write_text(json.dumps(doc), encoding="utf-8")
        return rc, "log"

    return spawn


def test_ingest_end_to_end_default_keeps_project_and_records(tmp_path):
    install = _install(tmp_path)
    target = tmp_path / "target.bin"
    target.write_bytes(b"\x00data")
    repo = _repo(tmp_path)
    sha = gr._hash_file(target)
    calls = []
    report = gr.ingest(target, ghidra=str(install), repo_root=repo, scratch=tmp_path / "scr",
                       spawn=_fake_spawn(sha, calls))
    argv, _, confine = calls[0]
    assert confine is False and argv[3] == "-import" and argv[0] != str(target)
    assert Path(report["project_dir"]).is_dir()
    assert report["mode"] == "import" and report["written"]["evidence_record"] > 0
    assert all(report["counts"][k] >= 1 for k in ("functions", "xrefs", "imports", "strings"))
    again = gr.ingest(target, ghidra=str(install), repo_root=repo, scratch=tmp_path / "scr",
                      spawn=_fake_spawn(sha, calls), project_dir=Path(report["project_dir"]), confine=True)
    assert calls[1][2] is True and again["written"] == {"claim": 0, "evidence_record": 0}


def test_ingest_refuses_failed_child_missing_export_and_hash_mismatch(tmp_path):
    install = _install(tmp_path)
    target = tmp_path / "t.bin"
    target.write_bytes(b"abc")
    sha = gr._hash_file(target)
    kw = dict(ghidra=str(install), repo_root=_repo(tmp_path), scratch=tmp_path / "scr")
    with pytest.raises(gr.GhidraRefusal, match="exited 3"):
        gr.ingest(target, spawn=_fake_spawn(sha, [], rc=3), **kw)
    with pytest.raises(gr.GhidraRefusal, match="wrote no export"):
        gr.ingest(target, spawn=_fake_spawn(sha, [], write=False), **kw)
    with pytest.raises(gr.GhidraRefusal, match="differs"):
        gr.ingest(target, spawn=_fake_spawn("0" * 64, []), **kw)


def test_ingest_refuses_without_ghidra_before_any_spawn(tmp_path):
    target = tmp_path / "t.bin"
    target.write_bytes(b"abc")
    calls = []
    with pytest.raises(gr.GhidraRefusal, match="GHIDRA_INSTALL_DIR"):
        gr.ingest(target, env={}, spawn=_fake_spawn("0", calls), scratch=tmp_path / "s")
    assert not calls


def test_runner_has_no_rmtree_and_no_deleteproject():
    src = Path(gr.__file__).read_text(encoding="utf-8")
    assert "rmtree" not in src and "-deleteProject" not in src
