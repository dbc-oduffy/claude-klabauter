"""Offline private binding/build tests. Native parser gate uses an explicitly built fixture."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import subprocess
import pytest
from coordinator_core.session_hub_path import hub_for_common
from coordinator_core.warm.door import build as builder
from coordinator_core.warm.door import private_required


def test_required_build_rejects_posix_before_compiler(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.sys,"platform","linux")
    monkeypatch.setattr(builder,"_find_compiler",lambda *_: pytest.fail("compiler must not be reached"))
    with pytest.raises(SystemExit,match="Windows"):
        builder.build(tmp_path,require_warm=True)


def test_required_build_bakes_mode_and_preserves_provenance(tmp_path,monkeypatch):
    monkeypatch.setattr(builder.sys,"platform","win32")
    root=tmp_path/"engine"; (root/"coordinator_core").mkdir(parents=True)
    (root/"coordinator_core"/"_engine_stamp").write_text("published-test")
    monkeypatch.setattr(builder,"_find_compiler",lambda *_:("clang","test-clang.exe"))
    monkeypatch.setattr(builder,"_compiler_version",lambda *_:"fixture compiler")
    seen={}
    def compile_fake(kind,compiler,source,output):
        seen["rendered"]=source.read_bytes(); output.write_bytes(b"fixture image")
    monkeypatch.setattr(builder,"_compile",compile_fake)
    output=tmp_path/"door.exe"
    builder.build(root,output=output,require_warm=True)
    receipt=json.loads(output.with_name("door.exe.provenance.json").read_text())
    assert seen["rendered"].startswith(b"#define DOOR_PRIVATE_REQUIRED 1\n")
    assert receipt["require_warm"] is True
    assert receipt["rendered_source_sha256"]==hashlib.sha256(seen["rendered"]).hexdigest()
    assert receipt["image_sha256"]==hashlib.sha256(output.read_bytes()).hexdigest()
    assert set(receipt["sources"])=={"door.c","door_core.c","door_core.h","door_env_set.h"}
    assert receipt["python_bin"] and "/Brepro" in receipt["compile_flags"]


def _row():
    return dict(schema_version=1,fire_id="a"*32,engine_root="C:\\engine",engine_token="b"*16,
        settings_home="C:\\settings",repo_root="C:\\repo",bindings_path="C:\\bindings.json",
        server_pid=123,server_creation_time_ft="134000000000000001",source_sha="c"*40,script_sha256="d"*64)


@pytest.fixture
def native_parser():
    path=os.environ.get("COORDINATOR_PRIVATE_PARSER_TEST_EXE")
    if not path: pytest.skip("requires root-built offline parser fixture")
    executable=Path(path)
    assert executable.is_file() and executable.suffix.lower()==".exe"
    def parse(text):
        # Exact native fixture, no shell, model, service, inherited pipe or fallback.
        result=subprocess.run([str(executable),text],stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=3)
        return result.returncode==0
    return parse


def test_native_private_parser_exact_fields_ranges_and_duplicates(native_parser):
    row=_row(); assert native_parser(json.dumps(row))
    for key,value in (("schema_version",True),("server_pid",0),("server_pid",4294967296),
                      ("server_creation_time_ft","18446744073709551616"),("fire_id","A"*32)):
        bad=dict(row);bad[key]=value;assert not native_parser(json.dumps(bad))
    assert not native_parser(json.dumps(row)[:-1]+',"server_pid":123}')
    assert not native_parser(json.dumps(dict(row,unknown=1)))


def test_native_private_parser_strict_unicode_and_decoded_nul(native_parser):
    row=_row()
    for escaped in (r"\u20gx",r"\ud800",r"\udc00",r"\ud800\u0041",r"\u0000junk",r"\u001f"):
        text=json.dumps(row).replace('"C:'+'\\'*2+'engine"','"C:'+'\\'*2+'engine'+escaped+'"')
        assert text!=json.dumps(row)
        assert not native_parser(text)
    text=json.dumps(row).replace('engine"',r'engine\ud83d\ude00"')
    assert native_parser(text)


@pytest.fixture
def private_door(tmp_path):
    if os.name != "nt": pytest.skip("Windows native REQUIRED door")
    binary=os.environ.get("COORDINATOR_PRIVATE_REQUIRED_TEST_EXE")
    if not binary: pytest.skip("requires root-built REQUIRED binary")
    import ctypes
    from ctypes import wintypes
    import shutil
    from coordinator_core.warm.tests.door_test_support import make_stub_engine_root
    root=make_stub_engine_root(tmp_path)
    repo=(tmp_path/"repo");repo.mkdir();(repo/".git").mkdir()
    settings=tmp_path/"settings";settings.mkdir()
    fire=hub_for_common(repo/".git")/"workflow-fires"/"headless"/("a"*32)
    aliases=fire/"private-bin";aliases.mkdir(parents=True)
    bindings=fire/"worker-bindings.json";bindings.write_text("{}")
    executable=aliases/"coordinator-invoke.exe";shutil.copyfile(binary,executable)
    (aliases/"door.engine-root.txt").write_text(str(root))
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.GetCurrentProcess.restype=wintypes.HANDLE
    kernel.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    times=[wintypes.FILETIME() for _ in range(4)]
    assert kernel.GetProcessTimes(kernel.GetCurrentProcess(),*[ctypes.byref(v) for v in times])
    birth=(times[0].dwHighDateTime<<32)|times[0].dwLowDateTime
    row=_row();row.update(engine_root=str(root),repo_root=str(repo.resolve()),settings_home=str(settings.resolve()),
        bindings_path=str(bindings.resolve()),server_pid=os.getpid(),server_creation_time_ft=str(birth),
        engine_token=hashlib.sha1(b"engine-stamp:"+(root/"coordinator_core"/"_engine_stamp").read_bytes()).hexdigest()[:16])
    def write(): (aliases/"door.private.json").write_text(json.dumps(row))
    write()
    def run(cwd=repo):
        from coordinator_core.diagnostics.contained_run import spawn_in_capped_job
        env=dict(os.environ,COORDINATOR_WARM="0",COORDINATOR_SETTINGS_HOME=str(tmp_path/"wrong-profile"))
        out=tmp_path/"stdout";err=tmp_path/"stderr"
        with out.open("wb") as stdout,err.open("wb") as stderr:
            proc,close=spawn_in_capped_job([str(executable),"ping"],popen_kwargs=dict(cwd=str(cwd),env=env,
                stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr),active_process_limit=2,job_memory_limit_bytes=256*1024*1024)
            try: code=proc.wait(timeout=8)
            finally: close()
        return code,out.read_text(),err.read_text()
    return root,repo,settings,row,write,run


def test_required_native_missing_server_and_wrong_repo_never_cold_replay(private_door,tmp_path):
    root,repo,settings,row,write,run=private_door
    for cwd in (repo,tmp_path):
        code,out,err=run(cwd)
        assert code!=0 and "not-delivered" in err
        assert "DOOR-TEST-FALLBACK-RAN" not in out


@pytest.mark.parametrize("wrong_witness", ["pid", "birth"])
def test_required_native_pid_birth_refusal_prewrite_and_postwrite_indeterminate(private_door, wrong_witness):
    from coordinator_core.warm.tests.door_test_support import ReplyingServer,pipe_name_for
    root,repo,settings,row,write,run=private_door
    # Wrong raw birth must refuse BEFORE any request bytes. Real kernel pipe peer is this process.
    server=ReplyingServer(pipe_name_for(root),b"malformed-reply\n")
    try:
        original = dict(row)
        if wrong_witness == "pid": row["server_pid"] += 1
        else: row["server_creation_time_ft"]=str(int(row["server_creation_time_ft"])+1)
        write()
        code,out,err=run()
        assert code!=0 and "not-delivered" in err and not server.request
    finally:
        server._winapi.CloseHandle(server._handle);server._thread.join(timeout=1)
    row.update(original);write()
    server=ReplyingServer(pipe_name_for(root),b"malformed-reply\n")
    try:
        code,out,err=run()
        assert code!=0 and server.request
        assert "not-delivered" not in err
        assert "DOOR-TEST-FALLBACK-RAN" not in out
        envelope=json.loads(server.request.decode().splitlines()[0])
        assert envelope["_env"]["COORDINATOR_SETTINGS_HOME"]==str(settings)
        assert "entrypoint" not in envelope["params"]
    finally:
        server._winapi.CloseHandle(server._handle);server._thread.join(timeout=1)


def test_private_preflight_rejects_unc_before_filesystem():
    with pytest.raises(ValueError,match="local drives"):
        private_required._path(r"\\server\share\root",directory=True)


def test_required_native_adjacent_root_embedded_nul_refuses(private_door):
    root,repo,settings,row,write,run=private_door
    aliases=hub_for_common(repo/".git")/"workflow-fires"/"headless"/row["fire_id"]/"private-bin"
    (aliases/"door.engine-root.txt").write_bytes(str(root).encode()+b"\x00unexpected")
    code,out,err=run()
    assert code!=0 and "not-delivered" in err
    assert "DOOR-TEST-FALLBACK-RAN" not in out


def test_legacy_native_opt_out_still_runs_only_fixture_cold_path(tmp_path):
    import shutil
    from coordinator_core.diagnostics.contained_run import spawn_in_capped_job
    from coordinator_core.warm.tests.door_test_support import make_stub_engine_root, FALLBACK_MARKER, FALLBACK_EXIT
    binary = os.environ.get("COORDINATOR_PRIVATE_LEGACY_TEST_EXE")
    if not binary: pytest.skip("requires root-built legacy control binary")
    root = make_stub_engine_root(tmp_path)
    aliases = tmp_path / "legacy-aliases"; aliases.mkdir()
    image = aliases / "coordinator-invoke.exe"; shutil.copyfile(binary, image)
    (aliases / "door.engine-root.txt").write_text(str(root))
    env = dict(os.environ, COORDINATOR_WARM="0", COORDINATOR_DOOR_ENGINE_ROOT=str(root))
    with (tmp_path / "out").open("wb") as out, (tmp_path / "err").open("wb") as err:
        proc, close = spawn_in_capped_job([str(image), "ping"], popen_kwargs={
            "cwd": str(tmp_path), "env": env, "stdin": subprocess.DEVNULL,
            "stdout": out, "stderr": err}, active_process_limit=4, job_memory_limit_bytes=256*1024**2)
        try: code = proc.wait(timeout=8)
        finally: close()
    assert code == FALLBACK_EXIT
    assert FALLBACK_MARKER in (tmp_path / "out").read_text()
