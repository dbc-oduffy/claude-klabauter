"""Behavior pins for the unimatrix relay `put` / `get` file leg."""
import hashlib
import importlib.util
import io
import json
import sys
import tarfile
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

RELAY = Path(__file__).resolve().parents[1] / "unimatrix-relay.py"


def load():
    spec = importlib.util.spec_from_file_location("unimatrix_relay_file_leg", RELAY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


R = load()
_spec = importlib.util.spec_from_file_location("unimatrix_file_relay_leg", RELAY.with_name("unimatrix-file-relay.py"))
FR = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(FR)
FR._RELAY.append(R)


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def folder(tmp_path):
    src = tmp_path / "src" / "photos"
    (src / "sub").mkdir(parents=True)
    (src / "a.jpg").write_bytes(b"AAAA" * 100)
    (src / "sub" / "b.jpg").write_bytes(b"bb")
    (src / "._a.jpg").write_bytes(b"appledouble")
    (src / "sub" / ".DS_Store").write_bytes(b"finder")
    (src / "__MACOSX").mkdir()
    (src / "__MACOSX" / "x").write_bytes(b"x")
    return src


def tar_of(entries, manifest=None):
    """A wire tar: entries are (name, bytes | tarinfo-kwargs); the manifest defaults to the regular files."""
    files = {n: d for n, d in entries if isinstance(d, bytes)}
    if manifest is None:
        manifest = {n: sha(d) for n, d in files.items()}
    text = R.manifest_bytes(manifest)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as tf:
        info = tarfile.TarInfo(R.MANIFEST)
        info.size = len(text)
        tf.addfile(info, io.BytesIO(text))
        for name, data in entries:
            if isinstance(data, bytes):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                info = tarfile.TarInfo(name)
                info.type = data["type"]
                info.linkname = data.get("linkname", "")
                tf.addfile(info)
    return buf.getvalue()


def put(home, data, name="set1", peer="alpha"):
    out = io.BytesIO()
    code = FR.file_leg("put", name, peer, home, stdin=io.BytesIO(data), stdout=out)
    return code, json.loads(out.getvalue())


def drop(home, name="set1", peer="alpha"):
    return home / "unimatrix" / "drop" / peer / name


def tree(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) if root.exists() else []


def test_parse_accepts_put_and_get_with_a_valid_name_only():
    assert FR.parse_mode("put set1") == ("put", "set1")
    assert FR.parse_mode("get set1") == ("get", "set1")
    for bad in ("put", "put ..", "put a/b", "put /abs", "put a..b", "put a b", "get ../x", "put  set1", "puts a"):
        assert FR.parse_mode(bad) is None


def test_the_relay_parser_refuses_put_get_and_the_file_parser_refuses_every_message_mode():
    for cmd in ("put set1", "get set1"):
        assert R.parse_mode(cmd) is None
        assert FR.parse_mode(cmd) == tuple(cmd.split())
    for cmd in ("roster", "source", "send 12", "inbox x", "reply-inbox x", "membership get", "membership put",
                "grant g n 10 -"):
        assert R.parse_mode(cmd) is not None
        assert FR.parse_mode(cmd) is None


def test_put_main_lands_folder_with_verified_manifest_and_no_appledouble(tmp_path, monkeypatch, folder, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    monkeypatch.setenv("SSH_ORIGINAL_COMMAND", "put set1")
    monkeypatch.setattr(sys, "argv", ["unimatrix-file-relay.py", "--peer", "alpha"])
    wire = io.BytesIO()
    manifest = R.pack(folder, wire)
    assert sorted(manifest) == ["a.jpg", "sub/b.jpg"]
    out = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(wire.getvalue())))
    monkeypatch.setattr(sys, "stdout", types.SimpleNamespace(buffer=out))
    with pytest.raises(SystemExit) as ex:
        FR.main()
    assert ex.value.code == 0
    reply = json.loads(out.getvalue())
    assert reply["ok"] is True and reply["files"] == 2 and reply["bytes"] == 402
    root = drop(home)
    assert tree(root) == ["MANIFEST.sha256", "a.jpg", "sub", "sub/b.jpg"]
    lines = (root / "MANIFEST.sha256").read_text().splitlines()
    assert lines == [f"{sha(b'AAAA' * 100)}  a.jpg", f"{sha(b'bb')}  sub/b.jpg"]
    assert reply["manifest_sha256"] == sha(R.manifest_bytes(manifest))
    assert not [p for p in tree(root.parent) if p.startswith(".tmp-")]


def test_put_temp_root_is_exclusive_and_unique_per_call(tmp_path, folder, monkeypatch):
    home = tmp_path / "home"
    seen = []
    real_mkdir = FR.os.mkdir
    monkeypatch.setattr(FR.os, "mkdir", lambda p, *a, **k: (seen.append(Path(p).name), real_mkdir(p, *a, **k))[1])
    for name in ("n1", "n2"):
        wire = io.BytesIO()
        R.pack(folder, wire)
        assert FR.file_leg("put", name, "alpha", home, stdin=io.BytesIO(wire.getvalue()), stdout=io.BytesIO()) == 0
    tmps = [s for s in seen if s.startswith(".tmp-")]
    assert len(set(tmps)) == 2 and tmps[0] == tmps[1]


def test_get_returns_the_same_bytes_and_reply_goes_to_stderr(tmp_path, folder):
    home = tmp_path / "home"
    wire = io.BytesIO()
    R.pack(folder, wire)
    assert put(home, wire.getvalue())[0] == 0
    out, err = io.BytesIO(), io.StringIO()
    assert FR.file_leg("get", "set1", "alpha", home, stdout=out, stderr=err) == 0
    assert json.loads(err.getvalue())["ok"] is True
    back = tmp_path / "back"
    manifest = R.unpack(io.BytesIO(out.getvalue()), back)
    assert manifest == {"a.jpg": sha(b"AAAA" * 100), "sub/b.jpg": sha(b"bb")}
    assert (back / "a.jpg").read_bytes() == b"AAAA" * 100


def test_get_is_confined_to_the_calling_peer_and_to_existing_drops(tmp_path, folder):
    home = tmp_path / "home"
    wire = io.BytesIO()
    R.pack(folder, wire)
    put(home, wire.getvalue(), peer="alpha")
    for peer, name in (("beta", "set1"), ("alpha", "nope")):
        out, err = io.BytesIO(), io.StringIO()
        assert FR.file_leg("get", name, peer, home, stdout=out, stderr=err) == 1
        assert json.loads(err.getvalue()) == {"ok": False, "reason": "not_found"} and out.getvalue() == b""


def test_put_and_get_without_a_peer_pin_are_refused(tmp_path):
    home = tmp_path / "home"
    code, reply = put(home, tar_of([("a", b"1")]), peer=None)
    assert (code, reply) == (1, {"ok": False, "reason": "no_peer"})
    err = io.StringIO()
    assert FR.file_leg("get", "set1", None, home, stdout=io.BytesIO(), stderr=err) == 1
    assert not (home / "unimatrix").exists()
    assert FR.peer_arg([]) is None and FR.peer_arg(["--peer", "alpha"]) == "alpha" and FR.peer_arg(["alpha"]) is None


def test_put_onto_an_existing_drop_is_refused_and_keeps_the_first(tmp_path):
    home = tmp_path / "home"
    assert put(home, tar_of([("a", b"1")]))[0] == 0
    code, reply = put(home, tar_of([("a", b"2")]))
    assert (code, reply["reason"]) == (1, "exists")
    assert (drop(home) / "a").read_bytes() == b"1"


@pytest.mark.parametrize("entries,manifest,reason", [
    ([("a", b"1"), ("../evil", b"x")], {"a": sha(b"1")}, "bad_member_name"),
    ([("/abs", b"x")], {}, "bad_member_name"),
    ([("a/../../b", b"x")], {}, "bad_member_name"),
    ([("._a", b"x")], {}, "bad_member_name"),
    ([("d/.DS_Store", b"x")], {}, "bad_member_name"),
    ([("l", {"type": tarfile.SYMTYPE, "linkname": "/etc/passwd"})], {}, "bad_member_type"),
    ([("a", b"1"), ("h", {"type": tarfile.LNKTYPE, "linkname": "a"})], None, "bad_member_type"),
    ([("a", b"1"), ("extra", b"2")], {"a": sha(b"1")}, "extra_file"),
    ([("a", b"1")], {"a": sha(b"1"), "gone": sha(b"2")}, "missing_file"),
    ([("a", b"1")], {"a": sha(b"other")}, "sha_mismatch"),
    ([("a", b"1")], {"a": "zz"}, "bad_manifest"),
    ([("a", b"1")], {"../a": sha(b"1")}, "bad_manifest"),
])
def test_put_refuses_unsafe_or_unverified_tar_and_leaves_no_partial_drop(tmp_path, entries, manifest, reason):
    home = tmp_path / "home"
    code, reply = put(home, tar_of(entries, manifest))
    assert (code, reply) == (1, {"ok": False, "reason": reason})
    assert tree(drop(home).parent) == []


def test_put_refuses_an_empty_drop_and_one_over_the_byte_cap_but_lands_one_at_the_cap(tmp_path, monkeypatch):
    home = tmp_path / "home"
    code, reply = put(home, tar_of([]))
    assert (code, reply) == (1, {"ok": False, "reason": "empty_drop"})
    monkeypatch.setattr(R, "MAX_DROP_BYTES", 4)
    code, reply = put(home, tar_of([("a", b"123"), ("b", b"45")]), name="big")
    assert (code, reply) == (1, {"ok": False, "reason": "too_large"})
    assert tree(drop(home).parent) == []
    assert put(home, tar_of([("a", b"123"), ("b", b"4")]), name="fits")[0] == 0


def test_put_refuses_more_members_than_the_file_cap_but_lands_one_at_the_cap(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(R, "MAX_DROP_FILES", 2)
    code, reply = put(home, tar_of([("a", b"1"), ("b", b"2"), ("c", b"3")]), name="many")
    assert (code, reply) == (1, {"ok": False, "reason": "too_many_files"})
    code, reply = put(home, tar_of([("d1", {"type": tarfile.DIRTYPE}), ("d2", {"type": tarfile.DIRTYPE}),
                                    ("d3", {"type": tarfile.DIRTYPE}), ("a", b"1")]), name="dirs")
    assert (code, reply) == (1, {"ok": False, "reason": "too_many_files"})
    assert tree(drop(home).parent) == []
    assert put(home, tar_of([("a", b"1"), ("b", b"2")]), name="fits")[0] == 0


def test_put_refuses_a_stream_whose_first_member_is_not_the_manifest_or_is_not_a_tar(tmp_path):
    home = tmp_path / "home"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w|") as tf:
        info = tarfile.TarInfo("a")
        info.size = 1
        tf.addfile(info, io.BytesIO(b"1"))
    assert put(home, buf.getvalue())[1]["reason"] == "manifest_not_first"
    assert put(home, b"not a tar at all" * 100)[1]["reason"] in ("bad_tar", "manifest_not_first")
    assert tree(drop(home).parent) == []


def test_put_without_tarfile_data_filter_exits_one_and_never_extracts(tmp_path, monkeypatch):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    home = tmp_path / "home"
    code, reply = put(home, tar_of([("a", b"1")]))
    assert (code, reply["reason"]) == (1, "tarfile_filter_unavailable")
    assert tree(drop(home).parent) == []


def test_file_line_names_the_file_relay_script_with_the_peer_pin_and_the_relay_reconciler_manages_it():
    f = FR.file_line("AAAAkey", "alpha")
    assert f.startswith("restrict,command=") and "unimatrix-file-relay.py" in f
    assert f.endswith(' --peer alpha" ssh-ed25519 AAAAkey unimatrix')
    assert "unimatrix-file-relay.py" not in R.relay_line("AAAAkey", "alpha")
    assert R.managed_key_body(f) == "AAAAkey"


def test_pack_single_file_and_membership_line_carries_the_peer_pin(tmp_path):
    f = tmp_path / "one.bin"
    f.write_bytes(b"xyz")
    assert R.pack(f, io.BytesIO()) == {"one.bin": sha(b"xyz")}
    line = R.relay_line("AAAAkey", "alpha")
    assert line.startswith("restrict,command=") and line.endswith(' --peer alpha" ssh-ed25519 AAAAkey unimatrix')
    assert R.managed_key_body(line) == "AAAAkey"
