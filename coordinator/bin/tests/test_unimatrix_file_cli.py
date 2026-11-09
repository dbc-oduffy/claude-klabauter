"""unimatrix-file.py put/get, driven against the relay's file leg through an injected ssh runner."""
import importlib.util
import io
import os
import tarfile
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.complete

BIN = Path(__file__).resolve().parents[1]


def load(filename, modname):
    spec = importlib.util.spec_from_file_location(modname, BIN / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


F = load("unimatrix-file.py", "unimatrix_file_cli_under_test")
R = load("unimatrix-relay.py", "unimatrix_relay_for_file_cli")
FR = load("unimatrix-file-relay.py", "unimatrix_file_relay_for_file_cli")
FR._RELAY.append(R)

MEMBERS = [
    {"box": "machine-b", "host": "machine-b.local", "user": "dp", "relay_pub": "ssh-ed25519 AAAA", "from_cidrs": []},
    {"box": "winbox", "host": "winbox.local", "user": "me", "relay_pub": "ssh-ed25519 BBBB", "from_cidrs": []},
]


class Proc:
    def __init__(self, stdin, stdout, stderr, thread):
        self.stdin, self.stdout, self.stderr, self._t = stdin, stdout, stderr, thread

    def wait(self):
        self._t.join(10)
        return 0


def relay_popen(home, caller="winbox", seen=None):
    """A Popen stand-in whose far end is the relay's own file leg, run in a thread on os pipes."""
    def popen(argv):
        if seen is not None:
            seen.append(argv)
        mode, name = argv[-1].split()
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        rin, wout, werr = os.fdopen(in_r, "rb"), os.fdopen(out_w, "wb"), os.fdopen(err_w, "w")

        def run():
            try:
                FR.file_leg(mode, name, caller, home, stdin=rin, stdout=wout, stderr=werr)
            finally:
                for fp in (rin, wout, werr):
                    fp.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return Proc(os.fdopen(in_w, "wb"), os.fdopen(out_r, "rb"), os.fdopen(err_r, "rb"), t)
    return popen


def run(argv, popen, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    (tmp_path / "k").touch()
    rc = F.main(argv, popen=popen, members=MEMBERS, key=tmp_path / "k", out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def tree(tmp_path):
    src = tmp_path / "photos"
    (src / "sub").mkdir(parents=True)
    (src / "a.jpg").write_bytes(b"alpha" * 1000)
    (src / "sub" / "b.jpg").write_bytes(b"beta")
    (src / "._a.jpg").write_bytes(b"appledouble")
    (src / ".DS_Store").write_bytes(b"finder")
    return src


def listing(root):
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def test_put_lands_verified_drop_without_appledouble(tmp_path):
    home = tmp_path / "far"
    seen = []
    rc, out, err = run(["put", "machine-b", str(tree(tmp_path))], relay_popen(home, seen=seen), tmp_path)
    assert rc == 0, err
    drop = home / "unimatrix" / "drop" / "winbox" / "photos"
    assert listing(drop) == ["MANIFEST.sha256", "a.jpg", "sub/b.jpg"]
    assert (drop / "a.jpg").read_bytes() == b"alpha" * 1000
    assert seen[0][-2:] == ["dp@machine-b.local", "put photos"]


def test_get_returns_same_bytes_and_manifest_is_verified(tmp_path):
    home = tmp_path / "far"
    assert run(["put", "machine-b", str(tree(tmp_path))], relay_popen(home), tmp_path)[0] == 0
    dest = tmp_path / "back"
    rc, out, err = run(["get", "machine-b", "photos", "--dest", str(dest)], relay_popen(home), tmp_path)
    assert rc == 0, err
    assert listing(dest) == ["a.jpg", "sub/b.jpg"]
    assert (dest / "sub" / "b.jpg").read_bytes() == b"beta"


def test_put_single_file_round_trips(tmp_path):
    home = tmp_path / "far"
    f = tmp_path / "one.bin"
    f.write_bytes(b"\x00\x01" * 5000)
    assert run(["put", "machine-b", str(f), "--name", "blob"], relay_popen(home), tmp_path)[0] == 0
    dest = tmp_path / "out"
    assert run(["get", "machine-b", "blob", "--dest", str(dest)], relay_popen(home), tmp_path)[0] == 0
    assert (dest / "one.bin").read_bytes() == f.read_bytes()


def test_unknown_peer_exits_2_and_names_known_boxes(tmp_path):
    rc, out, err = run(["put", "nobody", str(tree(tmp_path))], relay_popen(tmp_path / "far"), tmp_path)
    assert rc == 2
    assert "machine-b" in err and "winbox" in err


def test_second_put_to_same_name_is_refused(tmp_path):
    home = tmp_path / "far"
    src = tree(tmp_path)
    assert run(["put", "machine-b", str(src)], relay_popen(home), tmp_path)[0] == 0
    rc, out, err = run(["put", "machine-b", str(src)], relay_popen(home), tmp_path)
    assert rc == 1 and "exists" in err


def test_get_missing_name_fails_and_leaves_no_dest(tmp_path):
    dest = tmp_path / "back"
    rc, out, err = run(["get", "machine-b", "nope", "--dest", str(dest)], relay_popen(tmp_path / "far"), tmp_path)
    assert rc == 1
    assert not dest.exists()
    assert not list(tmp_path.glob(".back.part-*"))


def test_get_refuses_existing_destination(tmp_path):
    dest = tmp_path / "back"
    dest.mkdir()
    rc, out, err = run(["get", "machine-b", "photos", "--dest", str(dest)], relay_popen(tmp_path / "far"), tmp_path)
    assert rc == 1 and "exists" in err


def test_get_rejects_bytes_corrupted_in_transit_and_leaves_no_dest(tmp_path):
    home = tmp_path / "far"
    assert run(["put", "machine-b", str(tree(tmp_path))], relay_popen(home), tmp_path)[0] == 0
    real = relay_popen(home)

    def corrupting(argv):
        proc = real(argv)
        proc.stdout = io.BytesIO(proc.stdout.read().replace(b"beta", b"BETA"))
        return proc

    dest = tmp_path / "back"
    rc, out, err = run(["get", "machine-b", "photos", "--dest", str(dest)], corrupting, tmp_path)
    assert rc == 1 and "sha_mismatch" in err
    assert not dest.exists()
    assert not list(tmp_path.glob(".back.part-*"))


def test_client_flags_a_reply_whose_manifest_digest_differs(tmp_path):
    src = tree(tmp_path)

    class Liar:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdin.close = lambda: None
            self.stdout = io.BytesIO(b'{"ok": true, "files": 2, "bytes": 1, "manifest_sha256": "' + b"0" * 64 + b'"}\n')
            self.stderr = io.BytesIO()

        def wait(self):
            return 0

    rc, out, err = run(["put", "machine-b", str(src)], lambda argv: Liar(), tmp_path)
    assert rc == 1 and "manifest_mismatch" in err


def _no_ssh(argv):
    raise AssertionError("ssh must not start when the client refuses locally")


@pytest.mark.parametrize("const,limit,reason", [("MAX_DROP_BYTES", 10, "too_large"), ("MAX_DROP_FILES", 2, "too_many_files")])
def test_put_over_cap_is_refused_before_ssh(tmp_path, monkeypatch, const, limit, reason):
    monkeypatch.setattr(R, const, limit)
    load_sibling = F._sibling
    monkeypatch.setattr(F, "_sibling", lambda fn, mn: R if fn == "unimatrix-relay.py" else load_sibling(fn, mn))
    rc, out, err = run(["put", "machine-b", str(tree(tmp_path))], _no_ssh, tmp_path)
    assert rc == 1 and reason in err


def test_put_within_cap_still_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_DROP_FILES", 3)
    load_sibling = F._sibling
    monkeypatch.setattr(F, "_sibling", lambda fn, mn: R if fn == "unimatrix-relay.py" else load_sibling(fn, mn))
    rc, out, err = run(["put", "machine-b", str(tree(tmp_path))], relay_popen(tmp_path / "far"), tmp_path)
    assert rc == 0, err


def test_appledouble_does_not_count_toward_caps(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "MAX_DROP_BYTES", 5000 + 4)
    assert F.over_cap(R, tree(tmp_path)) is None


def test_over_cap_counts_exactly_what_pack_sends(tmp_path):
    src = tree(tmp_path)
    (src / "MANIFEST.sha256").write_bytes(b"x")
    sent = [rel for _k, rel, _p in R.iter_tree(src)]
    out = io.BytesIO()
    manifest = R.pack(src, out)
    members = set(tarfile.open(fileobj=io.BytesIO(out.getvalue())).getnames()) - {R.MANIFEST}
    assert set(sent) == members and set(manifest) <= members


def test_invalid_name_exits_2(tmp_path):
    rc, out, err = run(["put", "machine-b", str(tree(tmp_path)), "--name", "../x"], relay_popen(tmp_path / "far"), tmp_path)
    assert rc == 2


def _call_with_default_key(tmp_path, monkeypatch, popen, argv):
    home = tmp_path / "settings"
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    out, err = io.StringIO(), io.StringIO()
    rc = F.main(argv, popen=popen, members=MEMBERS, out=out, err=err)
    return home, rc, err.getvalue()


def test_missing_file_key_is_a_named_refusal_and_the_relay_key_is_never_reused(tmp_path, monkeypatch):
    relay_key = tmp_path / "settings" / ".secrets" / "ssh" / "unimatrix-zero"
    relay_key.parent.mkdir(parents=True)
    relay_key.write_text("relay private key")
    home, rc, err = _call_with_default_key(tmp_path, monkeypatch, _no_ssh, ["put", "machine-b", str(tree(tmp_path))])
    assert rc == 2
    assert "file_key_missing" in err and str(home / ".secrets" / "ssh" / "unimatrix-file") in err


def test_get_with_a_missing_file_key_is_the_same_named_refusal(tmp_path, monkeypatch):
    _, rc, err = _call_with_default_key(tmp_path, monkeypatch, _no_ssh, ["get", "machine-b", "set1"])
    assert rc == 2 and "file_key_missing" in err


def test_present_file_key_is_the_identity_ssh_is_given(tmp_path, monkeypatch):
    home = tmp_path / "settings"
    key = home / ".secrets" / "ssh" / "unimatrix-file"
    key.parent.mkdir(parents=True)
    key.write_text("file private key")
    (home / ".secrets" / "ssh" / "unimatrix-zero").write_text("relay private key")
    seen = []
    _, rc, err = _call_with_default_key(tmp_path, monkeypatch, relay_popen(tmp_path / "far", seen=seen),
                                        ["put", "machine-b", str(tree(tmp_path))])
    assert rc == 0, err
    assert seen[0][seen[0].index("-i") + 1] == str(key)
