"""Tests for the far-box ordered address resolver; addresses are TEST-NET-1 (192.0.2.0/24)."""
import importlib.util
import subprocess
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "lib" / "unimatrix_far_addresses.py"
_spec = importlib.util.spec_from_file_location("unimatrix_far_addresses_t", _PATH)
fa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fa)

A, B, C, D = "192.0.2.1", "192.0.2.2", "192.0.2.3", "192.0.2.4"


def _reader(values):
    return lambda key: values.get(key)


def test_addresses_ordered_deduped_invalid_dropped():
    r = _reader({"unimatrix.addresses.box1": f" {A}, nonsense,{B} ,{A}, 999.1.1.1, {C}"})
    assert fa.box_addresses("box1", r) == [A, B, C]


def test_addresses_cap_eight_and_missing_empty():
    many = ",".join(f"192.0.2.{i}" for i in range(1, 20))
    assert len(fa.box_addresses("b", _reader({"unimatrix.addresses.b": many}))) == 8
    assert fa.box_addresses("b", _reader({})) == []


def test_pinned_hostkey():
    assert fa.pinned_hostkey("b", _reader({"unimatrix.hostkey.b": " SHA256:abc "})) == "SHA256:abc"
    assert fa.pinned_hostkey("b", _reader({})) is None
    assert fa.pinned_hostkey("b", _reader({"unimatrix.hostkey.b": "  "})) is None


def test_third_address_is_chosen():
    seen = []

    def verify(target):
        seen.append(target)
        return (fa.OK, "") if target.endswith(C) else (fa.UNREACHABLE, "timeout")

    got, skipped = fa.first_answering("b", "u", [A, B, C, D], connect=lambda a: True, verify=verify)
    assert got == C
    assert [a for a, _ in skipped] == [A, B]
    assert seen == [f"u@{A}", f"u@{B}", f"u@{C}"]


def test_connect_prefilter_skips_without_verify():
    calls = []
    got, skipped = fa.first_answering(
        "b", "u", [A, B], connect=lambda a: a == B, verify=lambda t: calls.append(t) or (fa.OK, ""))
    assert got == B and calls == [f"u@{B}"] and skipped == [(A, fa.UNREACHABLE)]


def test_mismatch_is_not_this_box_and_never_chosen():
    def verify(target):
        return (fa.NOT_THIS_BOX, "x") if target.endswith(A) else (fa.OK, "")

    got, skipped = fa.first_answering("b", "u", [A, B], connect=lambda a: True, verify=verify)
    assert got == B and skipped == [(A, fa.NOT_THIS_BOX)]
    only, skipped = fa.first_answering("b", "u", [A], connect=lambda a: True, verify=verify)
    assert only is None and skipped == [(A, fa.NOT_THIS_BOX)]


def test_empty_list_returns_nothing():
    assert fa.first_answering("b", "u", [], connect=lambda a: True, verify=lambda t: (fa.OK, "")) == (None, [])


def test_classify_stderr():
    assert fa.classify(255, b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@")[0] == fa.NOT_THIS_BOX
    assert fa.classify(255, b"Host key verification failed.\n")[0] == fa.NOT_THIS_BOX
    assert fa.classify(0, b"")[0] == fa.OK
    assert fa.classify(255, b"ssh: connect to host x port 22: Connection timed out\n") == (
        fa.UNREACHABLE, "ssh: connect to host x port 22: Connection timed out")


def test_verify_argv_is_strict_and_keyed_to_box():
    argv = fa.verify_argv("boxx", f"u@{A}", key="k")
    assert "HostKeyAlias=boxx" in argv and "StrictHostKeyChecking=yes" in argv
    assert "ConnectTimeout=3" in argv and "StrictHostKeyChecking=accept-new" not in argv
    assert argv[-2:] == [f"u@{A}", "roster"]


def test_make_verify_runs_one_ssh_and_classifies():
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 255, b"", b"Host key verification failed.\n")

    status, _ = fa.make_verify("boxx", run=run)(f"u@{A}")
    assert status == fa.NOT_THIS_BOX and len(ran) == 1 and "HostKeyAlias=boxx" in ran[0]

    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 8)

    assert fa.make_verify("boxx", run=boom)(f"u@{A}")[0] == fa.UNREACHABLE
