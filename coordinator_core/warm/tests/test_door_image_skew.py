"""The door image's own build identity -- axis 3 (F-022, C1 of
docs/plans/2026-09-01-the-dogfooded-install-stops-lying-about, ledger item
8). DR-328 and DR-331 establish the two existing skew axes and REJECTED
weakening either; this suite only exercises the third, ADDITIVE one.

Test surface, per this chunk's brief: a server recording door identity A
must reject a request from a door built with identity B, and accept one
with identity A. Neither the `build`/`build_posix` half nor the `skew`
half shells out to a compiler or a real door binary -- `write_image_identity`
only ever reads bytes off disk (a fake output file stands in for a real
build, same convention as test_door_build_provenance.py), and
`ServerVersionState` is exercised directly against its own constructor/
`is_skewed` surface, never through door.c/server.py wiring (out of this
chunk's scope; see the brief's negative spec).
"""

from __future__ import annotations

import hashlib

from coordinator_core import lifecycle
from coordinator_core.warm import skew
from coordinator_core.warm.door import build as door_build
from coordinator_core.warm.door import build_posix as door_build_posix


def _write_head_and_ref(git_dir, ref_rel: str, sha: str) -> None:
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(f"ref: {ref_rel}\n", encoding="utf-8")
    ref_path = git_dir / ref_rel
    ref_path.parent.mkdir(parents=True, exist_ok=True)
    ref_path.write_text(sha + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# build.write_image_identity / build_posix (same writer, imported)
# ---------------------------------------------------------------------------


def test_write_image_identity_records_the_output_exes_own_hash(tmp_path):
    output_exe = tmp_path / "door.exe"
    output_exe.write_bytes(b"stand-in compiled binary bytes")

    sidecar_path = door_build.write_image_identity(output_exe)

    assert sidecar_path == output_exe.parent / door_build.DOOR_IMAGE_STAMP_FILENAME
    assert sidecar_path.read_text(encoding="utf-8").strip() == hashlib.sha256(
        output_exe.read_bytes()
    ).hexdigest()


def test_write_image_identity_changes_when_the_binary_changes(tmp_path):
    output_exe = tmp_path / "door.exe"
    output_exe.write_bytes(b"binary A")
    identity_a = door_build.write_image_identity(output_exe).read_text(encoding="utf-8")

    output_exe.write_bytes(b"binary B -- a door-only rebuild")
    identity_b = door_build.write_image_identity(output_exe).read_text(encoding="utf-8")

    assert identity_a != identity_b


def test_build_posix_reuses_the_same_writer_not_a_second_implementation(tmp_path):
    output = tmp_path / "door"
    output.write_bytes(b"stand-in posix compiled binary bytes")

    sidecar_path = door_build_posix.write_image_identity(output)

    assert sidecar_path.read_text(encoding="utf-8").strip() == hashlib.sha256(
        output.read_bytes()
    ).hexdigest()
    # Genuinely the same function object, not a parallel reimplementation.
    assert door_build_posix.write_image_identity is door_build.write_image_identity


# ---------------------------------------------------------------------------
# skew.ServerVersionState -- axis 3
# ---------------------------------------------------------------------------


def _stamped_state(tmp_path, *, boot_door_image=None):
    root = tmp_path / "clone"
    _write_head_and_ref(root / ".git", "refs/heads/main", "a" * 40)
    skew.write_engine_stamp(root, "sha:" + "1" * 40)
    return root


def test_axis3_absent_when_neither_side_supplies_a_door_image(monkeypatch, tmp_path):
    """No boot identity recorded, no per-request identity supplied -- the
    axis stays inert. This is the ordinary case for every caller that
    predates this chunk (server.py/door.c wiring lands later)."""
    root = _stamped_state(tmp_path)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root)
    client_token = skew.compute_client_token(root)

    assert state.is_skewed(client_token) is False
    assert state.last_skew_axes == ()


def test_axis3_absent_when_boot_identity_known_but_request_omits_it(monkeypatch, tmp_path):
    """A boot identity is recorded, but this particular request carries no
    door-image token (e.g. a non-door caller) -- fabricating a mismatch out
    of a missing value would be a false positive, not a signal."""
    root = _stamped_state(tmp_path)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root, boot_door_image="identity-A")
    client_token = skew.compute_client_token(root)

    assert state.is_skewed(client_token) is False
    assert state.last_skew_axes == ()


def test_axis3_accepts_a_request_carrying_the_identity_it_booted_against(monkeypatch, tmp_path):
    root = _stamped_state(tmp_path)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root, boot_door_image="identity-A")
    client_token = skew.compute_client_token(root)

    assert state.is_skewed(client_token, door_image_token="identity-A") is False
    assert state.last_skew_axes == ()


def test_axis3_rejects_a_request_from_a_door_built_with_a_different_identity(
    monkeypatch, tmp_path
):
    root = _stamped_state(tmp_path)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root, boot_door_image="identity-A")
    client_token = skew.compute_client_token(root)

    assert state.is_skewed(client_token, door_image_token="identity-B") is True
    assert state.last_skew_axes == (skew.SKEW_AXIS_DOOR_IMAGE,)


def test_axis3_is_reported_alongside_axis1_when_both_hold(monkeypatch, tmp_path):
    """Attribution discipline (claude-klabauter-22, 2026-08-26) extends to the
    third axis: all axes that hold are reported, never short-circuited."""
    root = _stamped_state(tmp_path)
    monkeypatch.setattr(skew.engine_version, "resolve_engine_sha", lambda: "deadbeef")
    monkeypatch.setattr(lifecycle, "_compute_core_version", lambda: "hash-0")

    state = skew.ServerVersionState(root, boot_door_image="identity-A")

    assert (
        state.is_skewed("a-stale-client-token", door_image_token="identity-B") is True
    )
    assert state.last_skew_axes == (skew.SKEW_AXIS_TOKEN, skew.SKEW_AXIS_DOOR_IMAGE)


def test_axis3_reuses_the_existing_engine_skew_response_not_a_second_protocol():
    """NEGATIVE SPEC: axis 3 must take the existing -32002 (ENGINE_SKEW)
    path -- `build_skew_response` -- not a new error code or envelope."""
    response = skew.build_skew_response(1, "deadbeef", "identity-B")

    assert response["error"]["code"] == -32002
    assert response["error"]["data"]["client_token"] == "identity-B"
