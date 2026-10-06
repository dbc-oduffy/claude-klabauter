"""`compute_offer` offers only written paths and NAMES the read-only holds it left out."""

from __future__ import annotations

from coordinator_core.ops.session import safe_commit_offer
from coordinator_core.session import claim_index


def _fake_commit_set(paths, read_only):
    def _commit_set(session_id, cwd=None, **_):
        return claim_index.CommitSet(
            paths=list(paths), contested={}, complete=True, read_only=list(read_only)
        )

    return _commit_set


def test_twelve_reads_and_one_write_offers_only_the_write(monkeypatch):
    reads = ["r%02d.py" % i for i in range(12)]
    monkeypatch.setattr(claim_index, "commit_set", _fake_commit_set(["w.py"], reads))

    offer = safe_commit_offer.compute_offer("me", None)

    assert offer["safe_paths"] == ["w.py"]
    assert offer["read_only"] == reads
    line = safe_commit_offer._render_dry_run(
        offer, {"reconciled": False, "claimed_absent": [], "unclaimed": []}
    )
    assert "Read-only, left out of the offer: 12 path(s)" in line
    assert "(+7 more)" in line
    assert line.count("r0") + line.count("r1") == 5


def test_no_read_only_renders_no_line(monkeypatch):
    monkeypatch.setattr(claim_index, "commit_set", _fake_commit_set(["w.py"], []))

    offer = safe_commit_offer.compute_offer("me", None)

    assert "read_only" not in offer
    assert "Read-only" not in safe_commit_offer._render_dry_run(
        offer, {"reconciled": False, "claimed_absent": [], "unclaimed": []}
    )
