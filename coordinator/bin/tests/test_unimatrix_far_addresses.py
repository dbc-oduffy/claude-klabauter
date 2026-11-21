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


def _cfg(**extra):
    base = {"unimatrix.addresses.b": f"{A},{B},{C}", "unimatrix.hostkey.b": "SHA256:pin"}
    base.update(extra)
    return _reader(base)


def _ok_on(*good):
    return lambda target: (fa.OK, "") if target.split("@")[-1] in good else (fa.UNREACHABLE, "timeout")


def test_route_verified_lan_carries_the_verified_endpoint():
    r = fa.select_route("b", "u", "u@b", _cfg(), connect=lambda a: True, verify=_ok_on(B), fallback="u@b-tunnel", relay_key="k")
    assert (r.transport, r.endpoint, r.verification, r.fallback_reason) == (fa.LAN, B, fa.VERIFIED, None)
    assert (r.peer, r.alias, r.relay_key, r.hostkey) == ("b", "u@b", "k", "SHA256:pin")


def test_route_unpinned_peer_never_takes_lan_even_when_tcp_and_verify_answer():
    probes = []
    r = fa.select_route("b", "u", "u@b", _cfg(**{"unimatrix.hostkey.b": ""}), connect=lambda a: probes.append(a) or True,
                        verify=lambda t: probes.append(t) or (fa.OK, ""), fallback="u@b-tunnel")
    assert (r.transport, r.endpoint, r.fallback_reason) == (fa.FALLBACK, "u@b-tunnel", fa.NO_PINNED_HOSTKEY) and probes == []
    bare = fa.select_route("b", "u", "u@b", _cfg(**{"unimatrix.hostkey.b": ""}), connect=lambda a: True, verify=_ok_on(A))
    assert (bare.transport, bare.endpoint, bare.verification) == (None, None, fa.UNAVAILABLE)


def test_route_wrong_host_key_and_stale_address_fall_back():
    def verify(t):
        return (fa.NOT_THIS_BOX, "x") if t.endswith(A) else (fa.UNREACHABLE, "t")

    r = fa.select_route("b", "u", "u@b", _cfg(), connect=lambda a: True, verify=verify, fallback="u@b-tunnel")
    assert (r.transport, r.fallback_reason) == (fa.FALLBACK, fa.NOT_THIS_BOX)
    stale = fa.select_route("b", "u", "u@b", _cfg(), connect=lambda a: False, verify=_ok_on(A), fallback="u@b-tunnel")
    assert (stale.transport, stale.fallback_reason) == (fa.FALLBACK, fa.UNREACHABLE)


def test_route_unavailable_and_no_address_are_typed_not_raised():
    r = fa.select_route("b", "u", "u@b", _cfg(**{"unimatrix.addresses.b": ""}), connect=lambda a: True, verify=_ok_on(A))
    assert (r.transport, r.verification, r.fallback_reason) == (None, fa.UNAVAILABLE, fa.NO_LAN_ADDRESS)


def test_route_verifies_at_most_max_tried_addresses():
    seen = []
    fa.select_route("b", "u", "u@b", _cfg(), connect=lambda a: True, verify=lambda t: seen.append(t) or (fa.UNREACHABLE, "t"))
    assert seen == [f"u@{A}", f"u@{B}"]


def test_route_restored_lan_wins_over_fallback_on_next_selection():
    up = {"lan": False}
    sel = lambda: fa.select_route("b", "u", "u@b", _cfg(), connect=lambda a: up["lan"], verify=_ok_on(A), fallback="u@b-tunnel")
    assert sel().transport == fa.FALLBACK
    up["lan"] = True
    assert sel().transport == fa.LAN
