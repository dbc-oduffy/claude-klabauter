"""Receipt extras may not shadow the keys the wrapper always writes."""

from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.receipt_extras_guard import (
    refuse_colliding_receipt_extras,
)


@pytest.mark.parametrize("key", ["sha256", "session_id", "emitted_at", "plan"])
def test_each_core_key_is_refused(key):
    with pytest.raises(ValueError) as exc:
        refuse_colliding_receipt_extras({key: "x", "queue": []})
    assert key in str(exc.value)


def test_new_keys_and_empty_extras_pass():
    refuse_colliding_receipt_extras({"queue": [], "profile": "p"})
    refuse_colliding_receipt_extras({})
    refuse_colliding_receipt_extras(None)
