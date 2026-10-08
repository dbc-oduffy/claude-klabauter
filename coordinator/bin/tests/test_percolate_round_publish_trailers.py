"""The round's commit repeats publish's trailers from the RoundManifest stamp,
and only when the manifest carries a `stamp_source_head`."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.git.git_state import parse_publish_trailers

from . import test_percolate_round as base

_mod = base._mod

SHA_A = "a" * 40
SHA_B = "b" * 40


def _drive(tmp_path, monkeypatch, *, stamp_source_head="", stamp_signature=""):
    def _stub(mp, spy):
        def _read(repo_root, not_before):
            return _mod._RoundManifest(
                round_id="round-xyz",
                added_or_updated=frozenset({"added-file.md"}),
                declared_payload=frozenset({"added-file.md"}),
                stamp_source_head=stamp_source_head,
                stamp_signature=stamp_signature,
            )

        mp.setattr(_mod, "_read_fresh_round_manifest", _read)

    monkeypatch.setattr(base, "_install_manifest_stub", _stub)
    calls = base._install_commit_pipeline_stub(monkeypatch)
    monkeypatch.setattr(Path, "exists", lambda self: True)
    rc, out, spy, dest = base._run_round(tmp_path, monkeypatch, no_publish=True)
    assert rc == _mod._EXIT_OK
    assert len(calls) == 1
    args, _kwargs = calls[0]
    return args[2], spy


def test_stamped_manifest_appends_all_three_trailers(tmp_path, monkeypatch):
    message, _ = _drive(
        tmp_path, monkeypatch, stamp_source_head=SHA_A, stamp_signature=SHA_B
    )
    parsed = parse_publish_trailers(message)
    assert (parsed.round_id, parsed.source_head, parsed.signature) == (
        "round-xyz", SHA_A, SHA_B,
    )


def test_stamp_source_head_without_signature_omits_signature(tmp_path, monkeypatch):
    message, _ = _drive(tmp_path, monkeypatch, stamp_source_head=SHA_A)
    parsed = parse_publish_trailers(message)
    assert parsed.source_head == SHA_A
    assert parsed.signature is None


def test_unstamped_manifest_adds_no_trailers(tmp_path, monkeypatch):
    message, _ = _drive(tmp_path, monkeypatch)
    assert "Percolate-" not in message


def test_round_spawns_no_run_all_checks(tmp_path, monkeypatch):
    _, spy = _drive(tmp_path, monkeypatch, stamp_source_head=SHA_A)
    assert not [c for c in spy.calls if "run-all-checks.py" in " ".join(map(str, c))]
