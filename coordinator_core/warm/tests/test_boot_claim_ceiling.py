"""A held boot claim stops vouching once it is older than any boot.

`try_claim_boot`'s kernel lock releases when the claimer DIES. Nothing released
it when the claimer LIVED. The claimed fd is leaked for the claimer's whole
process life, so a resident process that claims a boot and outlives it holds
the claim forever -- and since the only thing that can override a held claim is
a published discovery record, a boot that then failed or died left the decision
permanently stuck at "someone else is booting".

Measured 2026-09-02 on the dev box: `warm-http.json` had never been written,
`supervisor.should_spawn` had answered False for 20 hours, and the hook
forwarder's dial count read 7988 received / 0 forwarded -- every Bash tool call
on the machine taking the cold rung, silently, because nothing could ever
spawn the listener.

These tests drive the real primitive against a real file rather than a mocked
clock-and-lock pair: the failure was in how the lock's lifetime relates to the
boot's, which a mock of the lock cannot express. Only the CLOCK is injected --
`try_claim_boot`/`should_spawn_decision` both take a `time.time()`-shaped `now`
for exactly this, and the claim instant lives in the lock file itself
(`_CLAIM_STAMP_OFFSET`), so backdating the file's mtime would age something the
primitive no longer reads.
"""

from __future__ import annotations

from coordinator_core.warm import breadcrumb as bc

_CLAIMED_AT = 1_756_800_000.0

#: A `now` far enough past `_CLAIMED_AT` that the claim has expired, and near
_EXPIRED = _CLAIMED_AT + bc.BOOT_CLAIM_MAX_SECS + 0.01


def _lock(tmp_path):
    return bc.boot_lock_path(tmp_path / "warm-http.json")


class TestBootClaimCeiling:
    def test_a_fresh_claim_still_blocks_a_second_caller(self, tmp_path):
        assert bc.try_claim_boot(_lock(tmp_path), now=_CLAIMED_AT) is True
        assert bc.try_claim_boot(_lock(tmp_path), now=_CLAIMED_AT) is False

    def test_a_claim_older_than_the_ceiling_stops_vouching(self, tmp_path):
        lock = _lock(tmp_path)
        assert bc.try_claim_boot(lock, now=_CLAIMED_AT) is True

        assert bc.try_claim_boot(lock, now=_EXPIRED) is True

    def test_passing_the_ceiling_restamps_so_the_herd_is_bounded_too(self, tmp_path):
        lock = _lock(tmp_path)
        bc.try_claim_boot(lock, now=_CLAIMED_AT)

        assert bc.try_claim_boot(lock, now=_EXPIRED) is True
        assert bc.try_claim_boot(lock, now=_EXPIRED) is False

    def test_the_ceiling_is_measured_from_the_claim_not_the_files_creation(
        self, tmp_path
    ):
        lock = _lock(tmp_path)
        bc.try_claim_boot(lock, now=_CLAIMED_AT)
        assert bc.try_claim_boot(lock, now=_EXPIRED) is True

        assert bc.try_claim_boot(lock, now=_EXPIRED) is False


class TestDecisionBodyHonoursIt:
    def test_no_record_plus_an_expired_claim_means_spawn(self, tmp_path):
        lock = _lock(tmp_path)
        bc.try_claim_boot(lock, now=_CLAIMED_AT)

        assert bc.should_spawn_decision(None, lock_path=lock, now=_EXPIRED) is True

    def test_no_record_plus_a_live_claim_still_means_wait(self, tmp_path):
        lock = _lock(tmp_path)
        bc.try_claim_boot(lock, now=_CLAIMED_AT)

        assert bc.should_spawn_decision(None, lock_path=lock, now=_CLAIMED_AT) is False
