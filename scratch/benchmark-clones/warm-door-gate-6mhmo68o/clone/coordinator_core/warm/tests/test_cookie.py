"""Tests for `coordinator_core.warm.cookie` -- the loopback HTTP
listener's boot cookie.

Spec backlink: docs/plans/2026-08-26-the-loopback-listener-gets-a-credential.md § C1

Every test redirects `COORDINATOR_WARM_RUNTIME_BASE` into `tmp_path`
(`breadcrumb._runtime_base`'s documented test seam) so nothing here ever
mints a real cookie under the operator's `%LOCALAPPDATA%`.
"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from coordinator_core.warm import breadcrumb, cookie


@pytest.fixture(autouse=True)
def _redirect_runtime_base(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "runtime-base"
    monkeypatch.setenv(breadcrumb.RUNTIME_BASE_ENV, str(override))


# ---------------------------------------------------------------------------
# AC1 -- mint/read/clear, atomic write
# ---------------------------------------------------------------------------


def test_mint_writes_64_hex_chars(tmp_path: Path) -> None:
    token = cookie.mint(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{64}", token)
    path = cookie.cookie_path(tmp_path)
    assert path.exists()
    assert path.read_text(encoding="ascii") == token


def test_read_returns_the_minted_token(tmp_path: Path) -> None:
    token = cookie.mint(tmp_path)
    assert cookie.read(tmp_path) == token


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert cookie.read(tmp_path) is None


def test_clear_removes_the_cookie_and_is_idempotent(tmp_path: Path) -> None:
    cookie.mint(tmp_path)
    cookie.clear(tmp_path)
    assert not cookie.cookie_path(tmp_path).exists()
    # Idempotent -- calling again on an already-absent cookie must not raise.
    cookie.clear(tmp_path)


def test_clear_on_never_minted_cookie_does_not_raise(tmp_path: Path) -> None:
    cookie.clear(tmp_path)


def test_mint_tmp_sibling_does_not_survive(tmp_path: Path) -> None:
    cookie.mint(tmp_path)
    path = cookie.cookie_path(tmp_path)
    tmp_sibling = path.with_name(path.name + ".tmp")
    assert not tmp_sibling.exists()


def test_mint_is_atomic_via_os_replace(tmp_path: Path, monkeypatch) -> None:
    """The rename is the load-bearing step -- assert `os.replace` is
    actually the mechanism used, not merely that the end state looks
    right (which a non-atomic write-then-rename-free sequence could also
    produce)."""
    calls = []
    real_replace = cookie.os.replace

    def _spy_replace(src, dst):
        calls.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(cookie.os, "replace", _spy_replace)
    token = cookie.mint(tmp_path)

    # TWO atomic writes now, not one: the cookie and its curl config. BOTH
    # must go through the rename -- a curlrc written in place could be read
    # half-formed, and a curlrc that lagged the cookie would deliver a stale
    # credential the refusal path would then correctly reject.
    assert len(calls) == 2, f"expected an atomic replace per written file, got {calls!r}"
    destinations = {dst for _src, dst in calls}
    assert destinations == {
        str(cookie.cookie_path(tmp_path)),
        str(cookie.curl_config_path(tmp_path)),
    }
    assert all(src.endswith(".tmp") for src, _dst in calls)
    assert cookie.read(tmp_path) == token


# ---------------------------------------------------------------------------
# AC2 -- assert_directory_private passes on a normally-created dir, and the
# assertion is on the DACL/mode, never on mere file existence.
# ---------------------------------------------------------------------------


def test_assert_directory_private_passes_on_normal_dir(tmp_path: Path) -> None:
    cookie.mint(tmp_path)
    cookie.assert_directory_private(tmp_path)  # must not raise


@pytest.mark.skipif(sys.platform != "win32", reason="DACL assertion is Windows-only")
def test_assert_directory_private_checks_the_dacl_not_existence(tmp_path: Path) -> None:
    """A directory that exists but whose DACL is wrong must still fail --
    proves the check reads the DACL rather than merely checking the path
    is present."""
    from coordinator_core.warm import election

    directory = breadcrumb.svc_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    cookie.mint(tmp_path)

    owner_sid = election.current_user_sid()
    sddl = cookie._directory_dacl_sddl(directory)
    actual = cookie._parse_sddl_ace_sids(sddl)
    # The normally-created directory's own raw ACE set must be exactly
    # three trustees -- SY, BA, and the owner (spelled either as the
    # literal SID or as the "OW" alias, see cookie.assert_directory_private's
    # own comment on that alias) -- otherwise this test is not exercising
    # the DACL read at all.
    assert len(actual) == 3
    assert {"SY", "BA"} <= actual
    assert (owner_sid in actual) or ("OW" in actual)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode assertion")
def test_assert_directory_private_checks_mode_not_existence_posix(tmp_path: Path) -> None:
    cookie.mint(tmp_path)
    path = cookie.cookie_path(tmp_path)
    # File exists at 0600 already (via mint's os.open) -- passes.
    cookie.assert_directory_private(tmp_path)
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


# ---------------------------------------------------------------------------
# AC3 -- the negative case: a loosened DACL/mode must raise, fail-closed.
# ---------------------------------------------------------------------------


@pytest.mark.spawns_process
@pytest.mark.cadence
@pytest.mark.skipif(sys.platform != "win32", reason="DACL negative case is Windows-only")
def test_assert_directory_private_raises_on_loosened_dacl(tmp_path: Path) -> None:
    """Grants Everyone (WD) an ACE on the svc dir via `icacls`, then
    asserts `assert_directory_private` raises `DirectoryNotPrivateError`.

    `icacls` is used here, in the TEST ONLY, as the plan's brief permits
    ("or build the descriptor with ctypes"); it runs as the current
    (non-admin) user granting a right on a directory that user owns,
    which does not require elevation.
    """
    directory = breadcrumb.svc_dir(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    cookie.mint(tmp_path)
    # Sanity: passes before loosening.
    cookie.assert_directory_private(tmp_path)

    result = subprocess.run(
        ["icacls", str(directory), "/grant", "*S-1-1-0:(OI)(CI)F"],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        pytest.skip(
            "icacls could not grant an extra ACE on this box "
            f"(rc={result.returncode}, stderr={result.stderr!r}) -- "
            "cannot construct the AC3 negative case here"
        )

    with pytest.raises(cookie.DirectoryNotPrivateError):
        cookie.assert_directory_private(tmp_path)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode negative case")
def test_assert_directory_private_raises_on_loosened_mode_posix(tmp_path: Path) -> None:
    cookie.mint(tmp_path)
    path = cookie.cookie_path(tmp_path)
    path.chmod(0o644)
    with pytest.raises(cookie.DirectoryNotPrivateError):
        cookie.assert_directory_private(tmp_path)


# ---------------------------------------------------------------------------
# cookie_path -- reuses breadcrumb.svc_dir rather than re-deriving it.
# ---------------------------------------------------------------------------


def test_cookie_path_is_under_svc_dir(tmp_path: Path) -> None:
    assert cookie.cookie_path(tmp_path) == breadcrumb.svc_dir(tmp_path) / cookie.COOKIE_FILENAME


# --------------------------------------------------------------------------
# OWNER RIGHTS ("OW") handling. Added after review of the first C1 pass,
# which treated "OW" as a plain alias for the owning user. It is not: "OW"
# is OWNER RIGHTS (S-1-3-4), a DYNAMIC grant to whoever currently owns the
# object, so it is only equivalent to the owning user when the descriptor's
# own `O:` field IS that user.
#
# Measured on this box: the production `svc_dir()` under `%LOCALAPPDATA%`
# renders the literal owner SID, while a directory under `%TEMP%` -- which
# is what this file's own runtime-base redirect produces -- renders "OW".
# So the accommodation is real, but it is a property of the TEST's location
# and must not widen what the guard accepts in production.
#
# A directory genuinely owned by another principal cannot be created here
# without administrator rights, so the foreign-owner leg drives the parser
# and the branch directly with a synthetic descriptor rather than faking a
# filesystem state it cannot reach.
# --------------------------------------------------------------------------

_OTHER_SID = "S-1-5-21-9999999999-8888888888-7777777777-1234"


def test_parse_sddl_owner_sid_reads_the_owner_component():
    """The `O:` field ends at the next COMPONENT marker, not at its leading
    letter -- every SID starts with `S`, so a `[^GDS]+` exclusion matches
    nothing. Regression pin for exactly that bug."""
    sddl = (
        "O:S-1-5-21-1-2-3-1002"
        "G:S-1-5-21-1-2-3-513"
        "D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"
    )
    assert cookie._parse_sddl_owner_sid(sddl) == "S-1-5-21-1-2-3-1002"


def test_parse_sddl_owner_sid_returns_none_when_absent():
    assert cookie._parse_sddl_owner_sid("D:(A;;FA;;;SY)") is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL semantics")
def test_ow_ace_is_accepted_only_when_the_owner_is_this_user(monkeypatch, tmp_path):
    """OW + owner==us is the ordinary %TEMP% shape and must pass."""
    from coordinator_core.warm.election import current_user_sid

    me = current_user_sid()
    sddl = f"O:{me}G:{me}D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"
    monkeypatch.setattr(cookie, "_directory_dacl_sddl", lambda _d: sddl)
    cookie.assert_directory_private()  # must not raise


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL semantics")
def test_ow_ace_is_refused_when_another_principal_owns_the_directory(monkeypatch):
    """THE POINT OF THE BRANCH. Same ACE set, different owner -- OW then
    grants a principal that is not this user, and the guard must refuse."""
    sddl = (
        f"O:{_OTHER_SID}G:{_OTHER_SID}"
        "D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)"
    )
    monkeypatch.setattr(cookie, "_directory_dacl_sddl", lambda _d: sddl)
    with pytest.raises(cookie.DirectoryNotPrivateError, match="OWNER RIGHTS"):
        cookie.assert_directory_private()


# --------------------------------------------------------------------------
# The curl config -- the interpreter-free delivery path.
#
# WHY THIS FILE EXISTS AT ALL: the entire justification for the HTTP
# transport is that curl reaches the listener in ~10.4ms against a ~26.0ms
# bare-interpreter floor. A delivery path that starts Python to read the
# cookie has already spent more than the transport saves, so the credential
# has to be readable by curl itself. `curl --config <file>` is that path.
# --------------------------------------------------------------------------


def test_mint_writes_a_curl_config_carrying_the_token(tmp_path):
    token = cookie.mint(tmp_path)
    text = cookie.curl_config_path(tmp_path).read_text(encoding="ascii")
    assert text == f'header = "{cookie.COOKIE_HEADER}: {token}"\n'


def test_mint_writes_the_listener_url_when_port_given(tmp_path):
    """`port` lands in the SAME write as the header -- the whole point being
    that C5's forwarder resolves nothing: `curl --config <file>` alone
    carries both the credential and the destination."""
    token = cookie.mint(tmp_path, port=54321)
    text = cookie.curl_config_path(tmp_path).read_text(encoding="ascii")
    assert text == (
        f'header = "{cookie.COOKIE_HEADER}: {token}"\n'
        'url = "http://127.0.0.1:54321"\n'
    )


def test_mint_omits_the_url_line_when_port_is_none(tmp_path):
    """The default -- today's only caller (`ensure()`, before the bind)
    does not yet have a port to give. Omission, not a placeholder."""
    token = cookie.mint(tmp_path)
    text = cookie.curl_config_path(tmp_path).read_text(encoding="ascii")
    assert text == f'header = "{cookie.COOKIE_HEADER}: {token}"\n'
    assert "url" not in text


def test_clear_removes_the_curl_config_too(tmp_path):
    """Leaving the curlrc behind would leave a readable secret on disk after
    a clean exit that was supposed to remove it."""
    cookie.mint(tmp_path)
    assert cookie.curl_config_path(tmp_path).exists()
    cookie.clear(tmp_path)
    assert not cookie.cookie_path(tmp_path).exists()
    assert not cookie.curl_config_path(tmp_path).exists()


def test_curl_config_and_cookie_never_disagree(tmp_path):
    """Re-minting rewrites BOTH. A curlrc that lagged the cookie would send a
    stale credential the refusal path would correctly reject -- a
    self-inflicted outage that looks like an attack."""
    for _ in range(3):
        token = cookie.mint(tmp_path)
        text = cookie.curl_config_path(tmp_path).read_text(encoding="ascii")
        assert token in text
        assert cookie.read(tmp_path) == token


def test_curl_actually_consumes_the_config_and_sends_the_header(tmp_path):
    """END-TO-END, against a real socket and the real curl binary.

    This is the assertion the whole file is for: not that the config LOOKS
    right, but that curl parses it and puts the header on the wire. A config
    with the wrong directive name or quoting would satisfy every test above
    and still deliver nothing.
    """
    curl = shutil.which("curl")
    if not curl:
        pytest.skip("no curl on PATH")

    token = cookie.mint(tmp_path)
    seen = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["header"] = self.headers.get(cookie.COOKIE_HEADER)
            seen["host"] = self.headers.get("Host")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        proc = subprocess.run(
            [
                curl, "-s", "-S", "--max-time", "10",
                "--config", str(cookie.curl_config_path(tmp_path)),
                f"http://127.0.0.1:{port}/health",
            ],
            capture_output=True, text=True, timeout=20,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    assert proc.returncode == 0, proc.stderr
    assert seen.get("header") == token, f"curl did not send the cookie header: {seen!r}"
    # Same capture confirms the authority the Host pin matches against, so
    # the two controls are verified against ONE observation rather than two
    # assumptions.
    assert seen.get("host") == f"127.0.0.1:{port}"


# --------------------------------------------------------------------------
# AC1/AC2/AC3 -- THE WIRING. `cookie.py` shipped as an unreferenced module
# with a WIRING NOTE deferring this to "a later chunk"; nothing called it,
# so no cookie was ever generated and the boot guard never ran. These pin
# the callable INTO the boot path, which is what those ACs actually asked
# for and what a gate reading the cookie depends on.
# --------------------------------------------------------------------------


def test_ensure_generates_on_first_absence(tmp_path: Path) -> None:
    assert cookie.read(tmp_path) is None
    token = cookie.ensure(tmp_path)
    assert len(token) == 64
    assert cookie.read(tmp_path) == token


def test_ensure_does_not_rotate_an_existing_cookie(tmp_path: Path) -> None:
    """THE REFUTED LIFETIME, PINNED AS REFUSED. A boot that re-mints strands
    every session launched before it: the hook-fire caller holds the secret
    in a launch-fixed environment with no re-export channel."""
    first = cookie.ensure(tmp_path)
    for _ in range(3):
        assert cookie.ensure(tmp_path) == first


def test_ensure_refuses_a_present_but_unreadable_cookie(tmp_path: Path) -> None:
    """THE CASE THE HAPPY-PATH TEST DOES NOT REACH. `read()` returns None
    for a missing file AND for an unreadable one, so an `ensure` that
    branches on `read()` alone re-mints over a cookie that is merely
    unreadable -- rotating the secret out from under every live session on
    what may be a transient I/O error. Refuse, never replace."""
    cookie.cookie_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    cookie.cookie_path(tmp_path).write_bytes(bytes([0xFF, 0xFE]) + b" not ascii")
    with pytest.raises(cookie.CookieUnreadableError):
        cookie.ensure(tmp_path)


def test_ensure_refuses_when_presence_cannot_be_determined(
    tmp_path: Path, monkeypatch
) -> None:
    """THE BRANCH THE DECODE-FAILURE TESTS NEVER REACH. All the other
    unreadable-cookie tests fabricate the same failure -- non-ASCII bytes, a
    UnicodeDecodeError. The costlier real-world shapes are `OSError`:
    permission denied, a locked file, a filesystem error. `Path.exists()`
    swallows every one of those and reports False, which would send us down
    the mint path on no information at all."""
    def _boom(_path):
        raise PermissionError(13, "access denied")

    # The module's own seam, NOT `Path.lstat`: this suite runs live listener
    # threads, and patching a stdlib method process-wide breaks whichever
    # unrelated tests happen to be running beside it.
    monkeypatch.setattr(cookie, "_probe_presence", _boom)
    with pytest.raises(cookie.CookieUnreadableError):
        cookie.ensure(tmp_path)


def test_ensure_refuses_a_dangling_symlink_rather_than_minting_through_it(
    tmp_path: Path,
) -> None:
    """`exists()` follows links, so a dangling symlink at the cookie path
    reads as absent and `mint` would write THROUGH it onto whatever it
    targets. `lstat` sees the link itself."""
    path = cookie.cookie_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(tmp_path / "nowhere-at-all")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation unavailable (Windows without developer mode)")
    with pytest.raises(cookie.CookieUnreadableError):
        cookie.ensure(tmp_path)
    assert path.is_symlink(), "the link must survive the refusal, not be replaced"


def test_ensure_does_not_replace_the_bytes_of_an_unreadable_cookie(
    tmp_path: Path,
) -> None:
    """Refusing is only half of it -- the file must still be there,
    untouched, for an operator to inspect or recover."""
    path = cookie.cookie_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = bytes([0xFF, 0xFE]) + b" not ascii"
    path.write_bytes(corrupt)
    with pytest.raises(cookie.CookieUnreadableError):
        cookie.ensure(tmp_path)
    assert path.read_bytes() == corrupt


def test_a_refused_boot_on_an_unreadable_cookie_does_not_serve(
    tmp_path: Path,
) -> None:
    """The guard must propagate the refusal, not swallow it into a mint."""
    from coordinator_core.warm import supervisor

    path = cookie.cookie_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes([0xFF, 0xFE]) + b" not ascii")
    with pytest.raises(cookie.CookieUnreadableError):
        supervisor._assert_credential_ready(tmp_path)


def test_ensure_keeps_the_curlrc_in_step_on_first_generation(tmp_path: Path) -> None:
    token = cookie.ensure(tmp_path)
    assert token in cookie.curl_config_path(tmp_path).read_text(encoding="ascii")


def test_credential_guard_generates_the_cookie_when_the_directory_is_sound(
    tmp_path: Path,
) -> None:
    from coordinator_core.warm import supervisor

    supervisor._assert_credential_ready(tmp_path)
    assert cookie.read(tmp_path) is not None


def test_credential_guard_raises_on_a_directory_that_is_not_private(
    tmp_path: Path, monkeypatch
) -> None:
    """AC2. The guard's whole job is to be the thing that says no."""
    from coordinator_core.warm import supervisor

    def _boom(engine_root=None):
        raise cookie.DirectoryNotPrivateError("directory is not private")

    monkeypatch.setattr(cookie, "assert_directory_private", _boom)
    with pytest.raises(cookie.DirectoryNotPrivateError):
        supervisor._assert_credential_ready(tmp_path)


def test_the_guard_runs_before_the_bind() -> None:
    """AC3, AS AN ORDERING PROPERTY -- the half a mocked unit test cannot
    see. A listener that binds first and checks second has already been
    reachable on a port it could not protect, so the ORDER is the control,
    not the presence of a call. Read off the source of `main` because that
    is where the ordering actually lives.

    PRECEDING THE BIND SUBSUMES PRECEDING THE PUBLISH: the discovery record
    is written from the bound port, so it cannot precede the bind. Asserted
    against the bind alone and not also against the publish symbol, because
    naming that symbol here would trip the warm suite's litter guard, whose
    scan is a substring match -- and joining `_WRITER_MODULES` to quiet it
    would record this module as a writer it is not.
    """
    import inspect

    from coordinator_core.warm import supervisor

    src = inspect.getsource(supervisor.main)
    # Anchors checked for PRESENCE first: a rename or a refactor that wraps
    # the bind in a helper would otherwise raise an opaque ValueError from
    # `index` instead of failing as the ordering violation it is.
    assert "_assert_credential_ready" in src, "the credential guard left `main`"
    assert "ThreadingHTTPServer(" in src, (
        "the bind is no longer a literal in `main` -- this ordering test has "
        "gone blind and needs rewriting against the new shape"
    )
    guard = src.index("_assert_credential_ready")
    bind = src.index("ThreadingHTTPServer(")
    assert guard < bind, "the credential guard must precede the bind"


def test_a_refused_boot_returns_nonzero_before_binding() -> None:
    """The refusal's observable half: a non-zero return taken in the guard
    block itself, so a refused boot never reaches the bind and no client
    ever finds a port. The failure degrades to the named pipe rather than
    to an unprotected listener."""
    import inspect

    from coordinator_core.warm import supervisor

    src = inspect.getsource(supervisor.main)
    assert "_assert_credential_ready" in src and "ThreadingHTTPServer(" in src, (
        "an anchor moved -- this test has gone blind and needs rewriting"
    )
    guard_block = src[
        src.index("_assert_credential_ready") : src.index("ThreadingHTTPServer(")
    ]
    assert "return 3" in guard_block, "the guard must refuse with a non-zero exit"
